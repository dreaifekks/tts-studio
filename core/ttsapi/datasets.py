"""训练数据：登记在资料库 datasets/<name>.json，数据本身是同目录的 <name>.tar（<name>/characters/<文件夹>/{*.list, audio/}）。
本机用的是解压出来的缓存 DATASET_ROOT/.unpacked/<库 id>-<tar sha256 前 16 位>/<name>/，第一次用到时从资料库的 tar 解压
（见 local_root）。tar 还没同步过来的数据集本机用不了：扫描时跳过，训练时报原因。每句时长缓存在 data/dataset_durations.json。

数据集记录：{"id": name, "name", "tar", "sha256", "size", "created",
            "characters": {"<文件夹>": {"id": "<name>.<文件夹前缀>", "list": "<.list 文件名>"}}}

.list 每行第一列是音频路径，例如 C:/datasets/SAMPLE_DS/characters/sp0001_alice/audio/001.wav。换到另一台机器时，
resolve_audio() 把它换算到本机的数据根（数据集的 .list 是它的解压目录，见 rows()），也支持相对 .list 所在目录的相对路径。"""
import json
import re
import shutil
import tarfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf

from ttsapi import library, settings
from ttsapi.library import sha256_of
from ttsapi.models import DatasetInfo, DatasetPreview

# 只由这些假名组成、去掉标点后不超过 4 个字的句子算「纯语气词」，比如「ん？」「……ふう」「はぁ」「えへへ」
INTERJECTION_CHARS = set("あぁいぃうぅえぇおぉんっーはひふへほやゃゆゅよょわ"
                         "アァイィウゥエェオォンッハヒフヘホヤャユュヨョワ")
_lock = threading.Lock()
_unpack_lock = threading.Lock()
UNPACKED = ".unpacked"  # DATASET_ROOT 下放解压缓存的目录


class DatasetUnavailable(library.LibraryError):
    """数据集登记了，但本机用不了（tar 还没同步过来、没同步完）。"""
    code = "dataset_unavailable"


@dataclass
class Row:
    path: str
    speaker: str
    lang: str
    text: str
    duration: float


def resolve_audio(path: str, list_path: Path = None, root: Path = None) -> str:
    """.list 里的音频路径 -> 本机能打开的路径。root：相当于别的机器上 datasets 目录的本机目录（数据集的 .list：它的解压目录）；
    不给时（训练列表）按路径里的数据集名找当前资料库里这个数据集在本机的目录（解压缓存，见 local_root）。
    - 文件存在：原样返回
    - 相对路径：相对 .list 所在目录
    - 绝对路径但不存在（别的机器上的绝对路径，Windows 或 POSIX）：取路径里最后一个 datasets 目录之后、数据集内的部分
      （<数据集名>/characters/...；别的资料库的解压缓存 .unpacked/<库 id>-<哈希>/ 这一层去掉），接到 root 下
    都不成立时原样返回，由读文件的地方报错。"""
    is_abs = bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith(("/", "\\"))
    if not is_abs:  # Windows 上写的 audio\1.wav 在 Linux 上也要能拆开，两种分隔符都认
        return str((list_path.parent if list_path else root or settings.DATASET_ROOT).joinpath(*re.split(r"[\\/]+", path)))
    # 前一个判断免得每行都查一次文件（数据集 .list 解在 root 下）
    if root is not None and path.startswith(str(root)) or Path(path).exists():
        return path
    parts = re.split(r"[\\/]+", path)
    if "datasets" not in parts:
        return path
    rest = parts[len(parts) - parts[::-1].index("datasets"):]
    if rest[:1] == [UNPACKED]:
        rest = rest[2:]
    return str((root or _dataset_dir(rest[0]) or settings.DATASET_ROOT).joinpath(*rest))


def _dataset_dir(name: str):
    """当前资料库里叫 name 的数据集在本机的目录的上一层（里面是 <name>/characters/...）；没选资料库、没有或本机用不了时 None。"""
    try:
        lib = library.current()
    except library.NotSelected:
        return None
    for doc in lib.docs("datasets"):
        if doc["name"] == name:
            try:
                return local_root(doc, lib).parent
            except DatasetUnavailable:
                return None
    return None


def clean(text: str):
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if unicodedata.category(c)[0] not in "PSZC")


def is_interjection(text: str):
    c = clean(text)
    return len(c) == 0 or (len(c) <= 4 and set(c) <= INTERJECTION_CHARS)


def dataset_id(name: str, folder: str):
    return f"{name}.{folder.split('_')[0]}"


def local_root(doc: dict, lib) -> Path:
    """数据集 doc（资料库 lib 里的记录）在本机的目录，里面是 characters/<文件夹>/。本机用不了时抛 DatasetUnavailable。
    - 登记时打了 tar 的（sha256 不为空）：解压缓存 DATASET_ROOT/.unpacked/<库 id>-<tar sha256 前 16 位>/<name>/，
      按库 id + tar 内容区分，两个资料库里同名的数据集互不影响，tar 换了内容就是新的缓存。
      还没有就从资料库的 tar 解压（几百 MB 要几十秒）：先核对 tar 的大小和 sha256（Drive 上 json 常常比 tar 先到），
      整个解压到旁边的临时目录，再 rename 成缓存目录，中途退出不会留下半个缓存。
    - 没有 tar 校验值的历史登记：本机已经解压在 DATASET_ROOT/<name>/ 的数据，只有这台机器能用。"""
    if doc["sha256"] is None:
        root = settings.DATASET_ROOT / doc["name"]
        if not root.is_dir():
            raise DatasetUnavailable(f"数据集 {doc['name']} 登记时没打 tar，本机也没有 {root}")
        return root
    cache = settings.DATASET_ROOT / UNPACKED / f"{lib.info['id']}-{doc['sha256'][:16]}"
    with _unpack_lock:
        if not cache.is_dir():
            tar = lib.path(f"datasets/{doc['tar']}")
            if not tar.is_file() or tar.stat().st_size != doc["size"] or sha256_of(tar) != doc["sha256"]:
                raise DatasetUnavailable(f"数据集 {doc['name']} 的 {doc['tar']} 还没同步到本机，或者和登记的大小、"
                                         "sha256 对不上（还在同步），同步完再用")
            tmp = cache.with_name(f".{cache.name}.tmp")
            if tmp.exists():  # 上次解压到一半退出
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True)
            with tarfile.open(tar) as tf:
                # filter="data"（拒绝绝对路径、..、设备文件）在 3.10.12+ 才有
                tf.extractall(tmp, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
            tmp.rename(cache)
    return cache / doc["name"]


def scan():
    """id -> (本机 .list 路径, 用它的内置角色 slug 或 None)，来自资料库登记、本机能用的数据集。
    本机用不了的（tar 还没同步过来）跳过：不挡启动和重扫，GET /datasets、参考候选里没有它，训练时 get() 报原因。"""
    lib = library.current()
    builtin = {c["dataset_id"]: c["slug"] for c in lib.docs("characters") if c["source"] == "builtin"}
    out = {}
    for doc in sorted(lib.docs("datasets"), key=lambda d: d["name"]):
        try:
            root = local_root(doc, lib)
        except DatasetUnavailable:
            continue
        for folder, c in sorted(doc["characters"].items()):
            out[c["id"]] = (root / "characters" / folder / c["list"], builtin.get(c["id"]))
    return out


def get(dataset_id: str):
    """(本机 .list 路径, 内置角色 slug 或 None)。没登记时 KeyError（404）；登记了但本机用不了时 DatasetUnavailable（409，带原因）。"""
    found = scan()
    if dataset_id not in found:
        lib = library.current()
        for doc in lib.docs("datasets"):
            if dataset_id in {c["id"] for c in doc["characters"].values()}:
                local_root(doc, lib)  # 本机用不了：抛 DatasetUnavailable，带原因
        raise KeyError(f"数据集 {dataset_id}")
    return found[dataset_id]


def rows(list_path: Path):
    """读数据集的 .list（<解压目录>/<name>/characters/<文件夹>/<.list>）并带上每句时长。别的机器上的绝对路径换算到
    这个数据集的解压目录。时长按 list 文件的修改时间缓存。"""
    with _lock:
        cache = json.loads(settings.DATASET_CACHE.read_text(encoding="utf-8")) if settings.DATASET_CACHE.exists() else {}
        lines = [l.split("|", 3) for l in list_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        lines = [[resolve_audio(p, list_path, list_path.parents[3]), *rest] for p, *rest in lines]
        key, mtime = str(list_path), list_path.stat().st_mtime
        if cache.get(key, {}).get("mtime") != mtime:
            cache[key] = dict(mtime=mtime, durations=[round(sf.info(p).duration, 3) for p, *_ in lines])
            settings.DATASET_CACHE.write_text(json.dumps(cache), encoding="utf-8")
        return [Row(p, spk, lang, text, d) for (p, spk, lang, text), d in zip(lines, cache[key]["durations"], strict=True)]


def info(dataset_id: str, list_path: Path, character):
    rs = rows(list_path)
    return DatasetInfo(id=dataset_id, name=list_path.parent.name, path=str(list_path), character=character,
                       lines=len(rs), total_minutes=round(sum(r.duration for r in rs) / 60, 2))


def apply_filter(rs: list, min_sec: float, max_sec: float, exclude_interjections: bool):
    kept, short, long, inter = [], 0, 0, []
    for r in rs:
        if r.duration < min_sec:
            short += 1
        elif r.duration > max_sec:
            long += 1
        elif exclude_interjections and is_interjection(r.text):
            inter.append(r.text)
        else:
            kept.append(r)
    return kept, short, long, inter


def preview(dataset_id: str, min_sec: float, max_sec: float, exclude_interjections: bool):
    list_path, _ = get(dataset_id)
    rs = rows(list_path)
    kept, short, long, inter = apply_filter(rs, min_sec, max_sec, exclude_interjections)
    return DatasetPreview(
        id=dataset_id, lines_total=len(rs), lines_kept=len(kept),
        minutes_kept=round(sum(r.duration for r in kept) / 60, 2), excluded_short=short, excluded_long=long,
        excluded_interjection=len(inter), interjection_examples=list(dict.fromkeys(inter))[:20],
    )
