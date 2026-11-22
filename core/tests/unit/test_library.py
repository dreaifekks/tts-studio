"""资料库：原子写、扫描和本地索引缓存、Drive 冲突副本计数、管理它的控制端（归属切换）、权重的 sha256、选库和 API。"""
import hashlib
import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from conftest import seed_carol
from ttsapi import library, settings
from ttsapi.catalog import Catalog
from ttsapi.library import Library, NotALibrary, NotOwner
from ttsapi.library import store


def reopen(lb: Library, machine=None) -> Library:
    """同一个文件夹另开一个实例（像服务重启，或者另一台机器打开）。"""
    return Library(lb.root, settings.LIBRARY_INDEX_DIR, machine or lb.machine)


# ---------------------------------------------------------------- 原子写

def test_put_writes_whole_file_and_no_temp(lib):
    lib.put("characters/a.json", dict(slug="a", n=1))
    lib.put("characters/a.json", dict(slug="a", n=2))
    assert json.loads(lib.path("characters/a.json").read_text(encoding="utf-8")) == dict(slug="a", n=2)
    assert [p.name for p in lib.path("characters").iterdir()] == ["a.json"]
    umask = os.umask(0)
    os.umask(umask)
    assert lib.path("characters/a.json").stat().st_mode & 0o777 == 0o666 & ~umask  # 不是临时文件的 0600


def test_failed_write_keeps_old_file(lib, monkeypatch):
    lib.put("characters/a.json", dict(slug="a", n=1))
    real = os.replace

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(store.os, "replace", boom)
    with pytest.raises(OSError):
        lib.put("characters/a.json", dict(slug="a", n=2))
    monkeypatch.setattr(store.os, "replace", real)
    assert json.loads(lib.path("characters/a.json").read_text(encoding="utf-8"))["n"] == 1
    assert [p.name for p in lib.path("characters").iterdir()] == ["a.json"]  # 临时文件删掉了
    assert lib.get("characters/a.json")["n"] == 1


def test_put_rejects_non_record_path(lib):
    with pytest.raises(ValueError):
        lib.put("characters/sub/a.json", {})
    with pytest.raises(ValueError):
        lib.put("characters/a (1).json", {})


def test_kind_of():
    assert store.kind_of("weights/carol/v2pro/carol-e15/meta.json") == "weights"
    assert store.kind_of("weights/carol/v2pro/carol-e15/gpt.ckpt") is None
    assert store.kind_of("runs/r1/run.json") == "runs"
    assert store.kind_of("runs/r1/runner_spec.json") is None
    assert store.kind_of("works/w-1/audio/x.json") is None
    assert store.kind_of("compute/bench/studio-host/2026-11-20.json") == "bench"


# ---------------------------------------------------------------- 扫描和索引

def test_scan_uses_index_then_picks_up_changes(lib):
    for i in range(3):
        lib.put(f"blind/b-{i}.json", dict(id=f"b-{i}", n=i))
    again = reopen(lib)
    assert again.last_scan == dict(files=3, parsed=0, unchanged=3, removed=0, seconds=again.last_scan["seconds"])
    assert again.get("blind/b-1.json") == dict(id="b-1", n=1)

    # 外部改了一个文件（Drive 同步过来、手改）：mtime / size 变了就重读
    p = lib.path("blind/b-1.json")
    p.write_text(json.dumps(dict(id="b-1", n=100)), encoding="utf-8")
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 10**9))
    stats = again.scan()
    assert (stats["parsed"], stats["unchanged"]) == (1, 2) and again.get("blind/b-1.json")["n"] == 100

    # 删掉一个记录文件：从记录和索引里去掉
    lib.path("blind/b-2.json").unlink()
    assert again.scan()["removed"] == 1 and not again.has("blind/b-2.json")
    conn = sqlite3.connect(again.index_path)
    assert {r[0] for r in conn.execute("SELECT path FROM records")} == {"blind/b-0.json", "blind/b-1.json"}
    conn.close()


def test_index_deleted_is_rebuilt(lib):
    lib.put("characters/a.json", dict(slug="a"))
    lib.put("evals/2026-11/e-1.json", dict(id="e-1"))
    lib.index_path.unlink()
    again = reopen(lib)
    assert again.last_scan["parsed"] == 2 and again.counts()["characters"] == 1 and again.counts()["evals"] == 1
    assert reopen(lib).last_scan["unchanged"] == 2


def test_scan_skips_temp_and_non_records(lib):
    lib.path("characters").mkdir(parents=True, exist_ok=True)
    lib.path("characters/.a.json.123.tmp").write_text("{", encoding="utf-8")  # 写了一半的临时文件
    lib.path("runs/r1").mkdir(parents=True)
    lib.path("runs/r1/runner_spec.json").write_text("{}", encoding="utf-8")
    lib.path("blind/b-x.json").parent.mkdir(parents=True)
    lib.path("blind/b-x.json").write_text("{not json", encoding="utf-8")
    lib.scan()
    assert lib.counts()["characters"] == 0 and lib.counts()["runs"] == 0
    assert lib.broken == ["blind/b-x.json"]


# ---------------------------------------------------------------- Drive 冲突副本

def test_conflict_copies_counted_not_read(lib):
    lib.put("characters/carol.json", dict(slug="carol"))
    for rel in ("characters/carol (1).json", "weights/carol/v2pro/carol-e15 (1)/meta.json", "datasets/SAMPLE_DS (2).tar"):
        lib.path(rel).parent.mkdir(parents=True, exist_ok=True)
        lib.path(rel).write_text("{}", encoding="utf-8")
    lib.scan()
    assert sorted(lib.conflicts) == ["characters/carol (1).json", "datasets/SAMPLE_DS (2).tar",
                                     "weights/carol/v2pro/carol-e15 (1)/meta.json"]
    assert lib.counts()["characters"] == 1 and lib.counts()["weights"] == 0
    lib.path("characters/carol (1).json").unlink()
    lib.scan()
    assert len(lib.conflicts) == 2


# ---------------------------------------------------------------- 归属

def test_other_machine_reads_but_cannot_write_until_claim(lib):
    lib.put("characters/a.json", dict(slug="a"))
    other = reopen(lib, dict(id="other-machine", name="mba"))
    assert not other.owned and other.get("characters/a.json") == dict(slug="a")
    with pytest.raises(NotOwner) as e:
        other.put("characters/b.json", dict(slug="b"))
    assert lib.machine["name"] in e.value.detail and e.value.code == "library_not_owned"

    other.claim()
    assert other.owned and other.info["owner"] == dict(machine_id="other-machine", name="mba")
    other.put("characters/b.json", dict(slug="b"))
    # 原来的控制端扫描后（或重启后）知道自己不再管理它
    lib.scan()
    assert not lib.owned
    with pytest.raises(NotOwner):
        lib.put("characters/c.json", {})
    lib.claim()
    assert lib.owned


# ---------------------------------------------------------------- 权重 sha256

def test_add_weight_records_sha256(lib, tmp_path):
    seed_carol(lib)
    src = tmp_path / "carol-x-e5.ckpt"
    src.write_bytes(b"gpt weights" * 1000)
    m = Catalog.add_weight("carol", "v2pro", "gpt", src, "user")  # 不带 run：带了要有 run.json 才出现在目录里
    assert m["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest() and m["size"] == src.stat().st_size
    stored = lib.path("weights/carol/v2pro/carol-x-e5/gpt.ckpt")
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == m["sha256"]
    assert lib.get("weights/carol/v2pro/carol-x-e5/meta.json")["name"] == "carol-x-e5.ckpt"
    x = Catalog().find("v2pro:carol-x-e5.ckpt")
    assert x["path"] == str(stored) and x["character"] == "carol"
    entry = next(w for w in Catalog().weight_entries() if w.id == "v2pro:carol-x-e5.ckpt")
    assert entry.sha256 == m["sha256"]


def test_add_weight_move_and_link(lib, tmp_path):
    seed_carol(lib)
    a, b = tmp_path / "carol-m_e2_s4.pth", tmp_path / "carol-l_e2_s4.pth"
    a.write_bytes(b"a" * 100)
    b.write_bytes(b"b" * 100)
    Catalog.add_weight("carol", "v2pro", "sovits", a, "user", move=True)
    assert not a.exists() and lib.path("weights/carol/v2pro/carol-m_e2_s4/sovits.pth").read_bytes() == b"a" * 100
    m = Catalog.add_weight("carol", "v2pro", "sovits", b, "july", link=True)
    assert os.path.samefile(b, lib.path("weights/carol/v2pro/carol-l_e2_s4/sovits.pth"))
    assert m["sha256"] == hashlib.sha256(b"b" * 100).hexdigest()


# ---------------------------------------------------------------- 选库

def test_select_creates_opens_and_rejects(tmp_path, clean_data):
    try:
        new = library.select(tmp_path / "new")
        assert new.owned and (tmp_path / "new" / "library.json").exists()
        assert json.loads(settings.CORE_CONFIG.read_text(encoding="utf-8"))["library"] == str(tmp_path / "new")
        new.put("characters/a.json", dict(slug="a"))
        assert library.open_configured().get("characters/a.json") == dict(slug="a")  # 重启后按配置打开
        (tmp_path / "junk").mkdir()
        (tmp_path / "junk" / "notes.txt").write_text("x")
        with pytest.raises(NotALibrary):
            library.select(tmp_path / "junk")
        (tmp_path / "empty").mkdir()
        (tmp_path / "empty" / ".DS_Store").write_text("")
        assert library.select(tmp_path / "empty").counts()["characters"] == 0
    finally:
        settings.CORE_CONFIG.unlink(missing_ok=True)
        library.use(None)


def test_configured_path_missing(tmp_path, clean_data, monkeypatch):
    monkeypatch.setattr(settings, "LIBRARY", str(tmp_path / "gone"))
    assert library.open_configured() is None and "gone" in library.error()
    with pytest.raises(library.NotSelected):
        library.current()
    library.use(None)


# ---------------------------------------------------------------- API

@pytest.fixture
def client():
    """不进 lifespan：资料库由测试自己选。请求来源改成 127.0.0.1（gateway 只接受本机、tailscale、局域网的请求）。"""
    from ttsapi import gateway

    async def local(scope, receive, send):
        await gateway.app(dict(scope, client=("127.0.0.1", 50000)) if scope["type"] == "http" else scope, receive, send)
    return TestClient(local)


def test_api_not_selected(client):
    library.use(None)
    info = client.get("/library").json()
    assert info["selected"] is False and info["this_machine"]["name"] == settings.MACHINE_NAME
    for method, path in (("get", "/models"), ("get", "/blind"), ("get", "/works"), ("get", "/weights"),
                         ("get", "/refs"), ("post", "/library/rescan"), ("post", "/library/claim")):
        r = getattr(client, method)(path)
        assert r.status_code == 409 and r.json()["code"] == "library_not_selected", path
    assert client.get("/history").status_code == 200  # 生成历史在本机，不用资料库


def test_api_select_rescan_claim(client, tmp_path, clean_data):
    try:
        r = client.post("/library", json=dict(path=str(tmp_path / "lib")))
        info = r.json()
        assert r.status_code == 200 and info["selected"] and info["owned"] and info["kind"] == "local"
        assert info["counts"]["characters"] == 0 and info["conflicts"] == 0
        lb = library.current()
        seed_carol(lb)
        (tmp_path / "lib" / "characters" / "carol (1).json").write_text("{}", encoding="utf-8")
        info = client.post("/library/rescan").json()
        assert info["counts"]["characters"] == 1 and info["counts"]["datasets"] == 1
        assert info["conflicts"] == 1 and info["conflict_files"] == ["characters/carol (1).json"]
        assert client.get("/models").json()["characters"][0]["slug"] == "carol"

        lb.claim()  # 先让另一台机器管理它
        lb.info["owner"] = dict(machine_id="x", name="mba")
        (tmp_path / "lib" / "library.json").write_text(json.dumps(dict(lb.info)), encoding="utf-8")
        info = client.post("/library/rescan").json()
        assert not info["owned"] and info["owner"]["name"] == "mba"
        r = client.post("/works", json=dict(title="t", lines=[]))
        assert r.status_code == 409 and r.json()["code"] == "library_not_owned"
        assert client.post("/library/claim").json()["owned"]
        assert client.post("/works", json=dict(title="t", lines=[])).status_code == 200

        r = client.post("/library", json=dict(path=str(tmp_path)))  # 有别的文件、不是资料库
        assert r.status_code == 400 and r.json()["code"] == "not_a_library"
    finally:
        settings.CORE_CONFIG.unlink(missing_ok=True)
        library.use(None)


WRITES = [("patch", "/characters/carol/prefs"), ("delete", "/characters/carol"), ("put", "/refs/carol-1"),
          ("post", "/refs/upload"), ("post", "/works"),
          ("patch", "/works/w-1"), ("delete", "/works/w-1"), ("post", "/works/w-1/generate"),
          ("post", "/works/w-1/lines"), ("post", "/works/w-1/lines/l1/candidates"), ("put", "/works/w-1/lines/l1/pick"),
          ("post", "/works/w-1/lines/l1/regenerate"), ("post", "/works/w-1/render"), ("post", "/train"),
          ("post", "/train/sweep"),
          ("post", "/runs/import"), ("patch", "/weights/v2pro:x.ckpt"), ("delete", "/weights/v2pro:x.ckpt"),
          ("post", "/weights/eval"), ("post", "/blind"), ("put", "/blind/b-1/questions/q01/vote"),
          ("post", "/blind/b-1/reveal"), ("post", "/blind/b-1/generate"), ("delete", "/blind/b-1"), ("post", "/bench")]


def test_api_writes_checked_at_entry(client, lib):
    """别的控制端管理的资料库：会写资料库的接口在入口就 409（不改内存、不入队），读照常。"""
    from ttsapi import jobs
    seed_carol(lib)
    lib.info["owner"] = dict(machine_id="other", name="mba")
    before = dict(jobs.runs)
    for method, path in WRITES:
        kw = {} if method == "delete" else dict(json={})
        r = getattr(client, method)(path, **kw)
        assert r.status_code == 409 and r.json()["code"] == "library_not_owned", (method, path, r.text)
    assert jobs.runs == before
    assert client.get("/models").status_code == 200
    from ttsapi import gateway
    routes = {(m.lower(), r.path) for r in gateway.app.routes if hasattr(r, "dependant")
              for m in r.methods if any(d.call is gateway.owned for d in r.dependant.dependencies)}
    assert len(routes) == len(WRITES)


def test_failed_claim_keeps_old_owner(lib, monkeypatch):
    """接管时写 library.json 失败：内存里的归属不变，本机仍不能写。"""
    lib.info["owner"] = dict(machine_id="other", name="mba")
    (lib.root / "library.json").write_text(json.dumps(lib.info), encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(store.os, "replace", boom)
    with pytest.raises(OSError):
        lib.claim()
    monkeypatch.undo()
    assert not lib.owned
    with pytest.raises(NotOwner):
        lib.put("works/w-1.json", dict(id="w-1"))
    lib.claim()
    assert lib.owned and json.loads((lib.root / "library.json").read_text())["owner"]["name"] == lib.machine["name"]
