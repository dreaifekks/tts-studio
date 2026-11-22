"""资料库文件夹：记录的读写、启动扫描和本地索引缓存、Drive 冲突副本、管理它的控制端。

记录是一个个 json 文件，位置见 RECORDS。写入一律先写同目录的临时文件（以「.」开头，扫描时跳过）再 rename，
Drive 按文件同步，同步过去的不会是写了一半的文件。
扫描：遍历整个资料库，记录文件按 mtime + size 和本地索引（sqlite）比，没变的直接用索引里的内容，变了的重新读；
索引只是缓存，删了下次扫描全部重读。
"""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

FORMAT = 1
# 记录文件的位置（相对资料库根目录）。别的 json（runs/<run>/ 里训练的配置之类）不是记录
RECORDS = {
    "characters": "characters/*.json",
    "refs": "refs/*/*.json",
    "datasets": "datasets/*.json",
    "weights": "weights/*/*/*/meta.json",
    "evals": "evals/*/*.json",
    "blind": "blind/*.json",
    "works": "works/*.json",
    "runs": "runs/*/run.json",
    "bench": "compute/bench/*/*.json",
}
# Google Drive 电脑版的冲突副本：「名字 (1).json」，文件夹也可能是「名字 (1)」
CONFLICT = re.compile(r" \(\d+\)(\.[^.]*)?$")


class LibraryError(Exception):
    """API 返回 status 和 {"detail", "code"}（gateway 里的处理器）。"""
    status = 409
    code = "library_error"

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NotSelected(LibraryError):
    code = "library_not_selected"


class NotOwner(LibraryError):
    code = "library_not_owned"


class NotALibrary(LibraryError):
    status = 400
    code = "not_a_library"


def kind_of(rel: str):
    """相对路径 -> 记录类别；不是记录时 None。"""
    parts = rel.split("/")
    for kind, pattern in RECORDS.items():
        pat = pattern.split("/")
        if len(pat) == len(parts) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(parts, pat, strict=True)):
            return kind
    return None


def is_conflict(rel: str) -> bool:
    return any(CONFLICT.search(p) for p in rel.split("/"))


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic(path: Path, write):
    """write(f) 写进同目录的临时文件，fsync 后 rename 成 path。临时文件按 umask 建（不用 mkstemp：它固定 0600）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class Library:
    def __init__(self, root: Path, index_dir: Path, machine: dict):
        self.root = root
        self.machine = machine  # 本机：{"id", "name"}
        self.info = self._read_info()
        index_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = index_dir / f"{self.info['id']}.sqlite"
        self.records = {k: {} for k in RECORDS}  # 类别 -> 相对路径 -> 内容
        self.conflicts: list[str] = []
        self.broken: list[str] = []  # 读不出 json 的记录文件（只计数，不当记录）
        self.scanned_at = None
        self.last_scan = None
        self.scan()

    @classmethod
    def create(cls, root: Path, index_dir: Path, machine: dict):
        """在空文件夹（或不存在的路径）上建一个新资料库，本机管理。"""
        info = dict(format=FORMAT, id=uuid.uuid4().hex, created=now_iso(),
                    owner=dict(machine_id=machine["id"], name=machine["name"]), claimed_at=now_iso())
        _atomic(root / "library.json", lambda f: f.write(_dump(info)))
        return cls(root, index_dir, machine)

    def _read_info(self):
        info = json.loads((self.root / "library.json").read_text(encoding="utf-8"))
        if info.get("format") != FORMAT:
            raise NotALibrary(f"{self.root} 的资料库格式版本是 {info.get('format')}，本机只认 {FORMAT}")
        return info

    # ------------------------------------------------------------ 属性

    @property
    def kind(self):
        """drive：在 Google Drive 电脑版同步的文件夹里；local：本地文件夹。"""
        return "drive" if "GoogleDrive" in str(self.root) else "local"

    @property
    def owned(self) -> bool:
        return self.info["owner"]["machine_id"] == self.machine["id"]

    def counts(self) -> dict:
        return {k: len(v) for k, v in self.records.items()}

    # ------------------------------------------------------------ 扫描

    def _index(self):
        conn = sqlite3.connect(self.index_path, isolation_level=None)
        conn.execute("CREATE TABLE IF NOT EXISTS records(path TEXT PRIMARY KEY, kind TEXT, mtime_ns INTEGER, "
                     "size INTEGER, data TEXT)")
        return conn

    def scan(self) -> dict:
        """重新扫描整个资料库，返回统计（files 记录文件数、parsed 重新读的、unchanged 用索引的、removed 消失的）。"""
        t = time.perf_counter()
        self.info = self._read_info()
        conn = self._index()
        cached = {r[0]: r[1:] for r in conn.execute("SELECT path, mtime_ns, size, data FROM records")}
        records, conflicts, broken = {k: {} for k in RECORDS}, [], []
        stats = dict(files=0, parsed=0, unchanged=0, removed=0)
        with conn:
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in sorted(filenames):
                    if name.startswith("."):
                        continue
                    path = Path(dirpath, name)
                    rel = path.relative_to(self.root).as_posix()
                    if is_conflict(rel):
                        conflicts.append(rel)
                        continue
                    kind = kind_of(rel)
                    if kind is None:
                        continue
                    st = path.stat()
                    stats["files"] += 1
                    hit = cached.get(rel)
                    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
                        records[kind][rel] = json.loads(hit[2])
                        stats["unchanged"] += 1
                        continue
                    text = path.read_text(encoding="utf-8")
                    try:
                        records[kind][rel] = json.loads(text)
                    except json.JSONDecodeError:
                        broken.append(rel)
                        continue
                    conn.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?)",
                                 (rel, kind, st.st_mtime_ns, st.st_size, text))
                    stats["parsed"] += 1
            seen = {rel for v in records.values() for rel in v}
            gone = [rel for rel in cached if rel not in seen]
            conn.executemany("DELETE FROM records WHERE path=?", [(rel,) for rel in gone])
        conn.close()
        stats["removed"] = len(gone)
        stats["seconds"] = round(time.perf_counter() - t, 3)
        self.records, self.conflicts, self.broken = records, conflicts, broken
        self.scanned_at, self.last_scan = now_iso(), stats
        return stats

    # ------------------------------------------------------------ 读

    def path(self, rel: str) -> Path:
        return self.root / rel

    def docs(self, kind: str) -> list:
        return list(self.records[kind].values())

    def items(self, kind: str) -> dict:
        return self.records[kind]

    def get(self, rel: str) -> dict:
        """记录内容；不存在时 KeyError（API 里是 404）。"""
        kind = kind_of(rel)
        if kind is None or rel not in self.records[kind]:
            raise KeyError(rel)
        return self.records[kind][rel]

    def has(self, rel: str) -> bool:
        kind = kind_of(rel)
        return kind is not None and rel in self.records[kind]

    # ------------------------------------------------------------ 写（只有管理它的控制端）

    def check_owner(self):
        if not self.owned:
            raise NotOwner(f"资料库由 {self.info['owner']['name']} 管理，POST /library/claim 改为由本机管理")

    def put(self, rel: str, doc: dict):
        """写一条记录（整份替换）。"""
        self.check_owner()
        kind = kind_of(rel)
        if kind is None or is_conflict(rel):
            raise ValueError(f"{rel} 不是记录文件的位置")
        text = _dump(doc)
        path = self.path(rel)
        _atomic(path, lambda f: f.write(text))
        st = path.stat()
        conn = self._index()
        with conn:
            conn.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?)",
                         (rel, kind, st.st_mtime_ns, st.st_size, text.decode("utf-8")))
        conn.close()
        self.records[kind][rel] = json.loads(text)

    def put_file(self, rel: str, src: Path, move: bool = False, link: bool = False) -> str:
        """把文件放进资料库，返回 sha256。move：放好后删掉 src；link：硬链接（开发建库用，同一个磁盘上不占空间）。"""
        self.check_owner()
        dst = self.path(rel)
        if link:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.parent / f".{dst.name}.{uuid.uuid4().hex[:8]}.tmp"
            os.link(src, tmp)
            os.replace(tmp, dst)
            return sha256_of(dst)
        h = hashlib.sha256()

        def copy(f):
            with open(src, "rb") as s:
                while chunk := s.read(1 << 20):
                    h.update(chunk)
                    f.write(chunk)
        _atomic(dst, copy)
        if move:
            src.unlink()
        return h.hexdigest()

    def put_bytes(self, rel: str, data: bytes):
        self.check_owner()
        _atomic(self.path(rel), lambda f: f.write(data))

    def delete(self, rel: str):
        """删掉一个文件（记录或普通文件）。"""
        self.check_owner()
        self.path(rel).unlink()
        self._forget(lambda r: r == rel)

    def delete_tree(self, rel_dir: str):
        """删掉一个目录和里面的记录。"""
        self.check_owner()
        shutil.rmtree(self.path(rel_dir))
        self._forget(lambda r: r.startswith(rel_dir.rstrip("/") + "/"))

    def _forget(self, match):
        gone = [(kind, rel) for kind, v in self.records.items() for rel in v if match(rel)]
        conn = self._index()
        with conn:
            conn.executemany("DELETE FROM records WHERE path=?", [(rel,) for _, rel in gone])
        conn.close()
        for kind, rel in gone:
            del self.records[kind][rel]

    def claim(self):
        """改为由本机管理。先把 library.json 写到盘上，成功了才改内存里的归属（写失败时本机仍不是 owner）。"""
        info = dict(self.info, owner=dict(machine_id=self.machine["id"], name=self.machine["name"]),
                    claimed_at=now_iso())
        _atomic(self.root / "library.json", lambda f: f.write(_dump(info)))
        self.info = info


def _dump(doc: dict) -> bytes:
    return json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")
