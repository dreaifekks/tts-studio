"""作品和盲听在资料库里的数据：渲染任务固定提交时的资料库、取消后不发布；作品里候选音频的快照（换控制端后本机 tts.db 没有
这些音频也能打开、渲染）；盲听缺本机任务记录时能列出；清理生成音频时保留资料库里作品、盲听引用的。"""
import asyncio
import json
import logging
import threading
import time

import numpy as np
import pytest
import soundfile as sf

from ttsapi import blind, db, library, render, settings, synth, works
from ttsapi.library import Library
from ttsapi.models import WorkCreate, WorkLineIn


def make_audio(created: float = None) -> str:
    """本机生成历史里的一条音频（文件 + tts.db 的行）。"""
    aid = synth.new_audio_id()
    t = np.arange(16000) / 16000
    sf.write(settings.AUDIO_DIR / f"{aid}.wav", (0.3 * np.sin(2 * np.pi * 440 * t)).astype("float32"), 16000)
    created = created or time.time()
    synth.save_audio(dict(id=aid, created=synth.iso(created), audio_url=f"/audio/{aid}", text="こんにちは",
                          text_lang="ja", character="carol", engine="v2pro", gpt="g.ckpt", sovits="s.pth", ref_id="r1",
                          seed=1, duration=1.0, infer_sec=0.1, synth_wall_sec=0.2, semantic_tokens=10, vram_peak_mb=1,
                          ratio_vs_ref=[1.0], score=88.0), created)
    return aid


def new_work(n_lines: int = 2):
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    w = works.create(WorkCreate(title="t", lines=[WorkLineIn(character="carol", text=f"行{i}") for i in range(n_lines)]))
    aids = []
    for line in w.lines:
        aids.append(make_audio())
        works.attach(w.id, line.id, aids[-1], True)
    return w.id, aids


class FakeRun:
    def __init__(self):
        self.job = type("Job", (), {"render_result": None})()

    def stage(self, *_):
        pass


@pytest.fixture
def gated_render(monkeypatch):
    """渲染线程算完后停住（started），等测试放行（go）再返回（done）。"""
    started, go, done = threading.Event(), threading.Event(), threading.Event()
    real = render.render

    def slow(*a):
        out = real(*a)
        started.set()
        go.wait(10)
        done.set()
        return out
    monkeypatch.setattr(render, "render", slow)
    return started, go, done


def other_library(tmp_path):
    return library.open_or_create(tmp_path / "other")


def test_render_cancelled_then_library_switched_writes_nothing(lib, tmp_path, gated_render):
    started, go, done = gated_render
    wid, _ = new_work()
    b = other_library(tmp_path)
    fn = works.render_task(wid)

    async def main():
        task = asyncio.create_task(fn(FakeRun()))
        await asyncio.to_thread(started.wait, 10)
        library.use(b)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        go.set()  # 线程继续算完；asyncio.run 退出前等默认线程池里的线程结束
    asyncio.run(main())
    assert done.is_set()
    assert not b.path("works").exists()
    assert [p.name for p in lib.path(f"works/{wid}").iterdir()] == ["audio"]
    assert lib.get(f"works/{wid}.json")["render"] is None


def test_render_publishes_into_library_of_submission(lib, tmp_path, gated_render):
    started, go, _ = gated_render
    wid, _ = new_work()
    b = other_library(tmp_path)
    run = FakeRun()
    fn = works.render_task(wid)

    async def main():
        task = asyncio.create_task(fn(run))
        await asyncio.to_thread(started.wait, 10)
        library.use(b)
        go.set()
        await task
    asyncio.run(main())
    assert not b.path("works").exists()
    out = sorted(p.suffix for p in lib.path(f"works/{wid}").iterdir() if p.is_file())
    assert out == [".lrc", ".mp3", ".srt", ".wav"]
    assert lib.get(f"works/{wid}.json")["render"]["wav_url"] == run.job.render_result.wav_url


def test_render_of_work_deleted_meanwhile_is_dropped(lib, gated_render):
    started, go, _ = gated_render
    wid, _ = new_work()
    fn = works.render_task(wid)

    async def main():
        task = asyncio.create_task(fn(FakeRun()))
        await asyncio.to_thread(started.wait, 10)
        works.delete(wid)
        go.set()
        with pytest.raises(RuntimeError, match="被删除"):
            await task
    asyncio.run(main())
    assert not lib.path(f"works/{wid}").exists()


def test_work_opens_and_renders_from_snapshot_on_new_controller(lib, gated_render):
    _, go, _ = gated_render
    go.set()
    wid, aids = new_work()
    doc = json.loads(lib.path(f"works/{wid}.json").read_text(encoding="utf-8"))
    assert set(doc["audio"]) == set(aids) and doc["audio"][aids[0]]["score"] == 88.0
    # 新控制端：同一个资料库（同步过来的），本机 tts.db 和生成目录里都没有这些音频
    for a in aids:
        db.run("DELETE FROM audio WHERE id=?", a)
        (settings.AUDIO_DIR / f"{a}.wav").unlink()
    library.use(Library(lib.root, settings.LIBRARY_INDEX_DIR, dict(id="new-controller", name="new")))
    w = works.to_model(works.load(wid))
    assert [c.id for line in w.lines for c in line.candidates] == aids
    assert synth.get_audio(aids[0]).text == "こんにちは"
    assert synth.audio_file(aids[0]) == lib.path(f"works/{wid}/audio/{aids[0]}.wav")
    library.use(lib)  # 改为本机管理后照样能渲染（选中的音频从快照读）
    run = FakeRun()
    asyncio.run(works.render_task(wid)(run))
    assert run.job.render_result.duration > 1.5
    with pytest.raises(KeyError):
        synth.get_audio("20000101-000000-000000")


def blind_doc(job_id: str):
    q = dict(id="q01", sentence=0, repeat=0, seed=1, left="A", audio=dict(A=None, B=None), score=dict(A=None, B=None),
             vote=None, note="", voted_at=None)
    combo = dict(engine="v2pro", gpt="g.ckpt", sovits="s.pth")
    return dict(id="b-00000001", created=synth.iso(time.time()), created_ts=time.time(), title="t", character="carol",
                eval_set=settings.EVAL_DEFAULT_SET, repeats=1, ref_id="r1", combos=dict(A=combo, B=combo),
                questions=[q], revealed_at=None, job_id=job_id)


def test_unfinished_blind_without_local_job_row(lib):
    lib.put("blind/b-00000001.json", blind_doc("blind-20260101-000000-dead"))
    total, items = blind.list_tests()
    assert total == 1 and items[0].status == "failed"
    t = blind.to_model(blind.load("b-00000001"))
    assert t.status == "failed" and not t.questions[0].ready


def test_cleanup_keeps_audio_referenced_by_library(lib):
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    old = time.time() - (settings.AUDIO_KEEP_DAYS + 1) * 86400
    in_work, in_blind, loose = make_audio(old), make_audio(old), make_audio(old)
    # 挂候选时作品记录已经写进资料库、还没来得及标记保留就退出了：keep 还是 0
    wid = works.create(WorkCreate(title="t", lines=[WorkLineIn(character="carol", text="a")])).id
    doc = works.load(wid)
    doc["lines"][0]["candidates"].append(in_work)
    works.save(doc)
    b = blind_doc(None)
    b["questions"][0]["audio"]["A"] = in_blind
    lib.put("blind/b-00000001.json", b)
    synth.cleanup_audio(logging.getLogger("test"))
    for a in (in_work, in_blind):
        assert db.one("SELECT 1 FROM audio WHERE id=?", a) and (settings.AUDIO_DIR / f"{a}.wav").exists()
    assert db.one("SELECT 1 FROM audio WHERE id=?", loose) is None
    assert not (settings.AUDIO_DIR / f"{loose}.wav").exists()


class BlindRun:
    def stage(self, *_):
        pass

    def log(self, *_):
        pass


def test_blind_audio_lives_in_library(lib, monkeypatch, tmp_path):
    """盲听的音频跟着进资料库 blind/<id>/<音频 id>.wav：换主控（本机生成目录、tts.db 都没有）照样能听、GET /audio 能取；
    资料库里也没有（还没同步过来）时 404 audio_elsewhere，machine 是出题的主控在设备列表里的名字；删盲听连音频一起删。"""
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from ttsapi import gateway
    from ttsapi.compute import registry
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "COMPUTE_FILE", tmp_path / "compute.json")
    registry.patch_device("local", name="studio-host")
    monkeypatch.setattr(synth, "refs", SimpleNamespace(index=dict(r1=dict(character="carol"))))
    monkeypatch.setattr(synth, "resolve", lambda *a, **k: None)

    async def fake_synth(p, engine, ref, weights, seed, score):
        aid = make_audio()

        async def finish():
            return synth.get_audio(aid)
        return finish
    monkeypatch.setattr(synth, "synth", fake_synth)
    lib.put("blind/b-00000001.json", blind_doc(None))
    asyncio.run(blind._generate(BlindRun(), "b-00000001"))
    doc = blind.load("b-00000001")
    q = doc["questions"][0]
    a, b, left = q["audio"]["A"], q["audio"]["B"], q["audio"][q["left"]]
    assert doc["machine"] == "studio-host"
    for aid in (a, b):
        assert lib.path(f"blind/b-00000001/{aid}.wav").read_bytes() == (settings.AUDIO_DIR / f"{aid}.wav").read_bytes()
    assert blind.audio_path("b-00000001", "q01", "left") == lib.path(f"blind/b-00000001/{left}.wav")
    # 新主控：本机没有这些音频，从资料库里读
    for aid in (a, b):
        db.run("DELETE FROM audio WHERE id=?", aid)
        (settings.AUDIO_DIR / f"{aid}.wav").unlink()
    assert blind.audio_path("b-00000001", "q01", "left") == lib.path(f"blind/b-00000001/{left}.wav")

    async def local(scope, receive, send):
        await gateway.app(dict(scope, client=("127.0.0.1", 50000)) if scope["type"] == "http" else scope, receive, send)
    api = TestClient(local)
    assert api.get(f"/audio/{a}").content == lib.path(f"blind/b-00000001/{a}.wav").read_bytes()
    assert api.get("/blind/b-00000001/questions/q01/right").status_code == 200
    # 资料库里的还没同步过来
    lib.path(f"blind/b-00000001/{left}.wav").unlink()
    for r in (api.get(f"/audio/{left}"), api.get("/blind/b-00000001/questions/q01/left")):
        assert r.status_code == 404 and r.json()["code"] == "audio_elsewhere" and r.json()["machine"] == "studio-host"
    asyncio.run(blind.delete("b-00000001"))
    assert not lib.path("blind/b-00000001").exists() and not lib.path("blind/b-00000001.json").exists()


def test_blind_audio_reads_this_test_and_every_copy(lib, monkeypatch, tmp_path):
    """sol 的两个复现：换了主控、本机恰好有同名的音频时，题目接口播的是这场盲听目录里的那条；盲听目录里没有、挂着它的作品
    有快照时，GET /audio 给快照，不提前 404 audio_elsewhere。哪份都没有才 audio_elsewhere；谁都不记着的音频是普通 404。"""
    from fastapi.testclient import TestClient
    from ttsapi import gateway
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "COMPUTE_FILE", tmp_path / "compute.json")
    doc = blind_doc(None)
    q = doc["questions"][0]
    q["audio"] = dict(A="20260101-000000-aaaa", B="20260101-000000-bbbb")
    doc["machine"] = "studio-host"
    lib.put("blind/b-00000001.json", doc)
    lib.put_bytes(f"blind/b-00000001/{q['audio']['A']}.wav", b"blind-A")
    (settings.AUDIO_DIR / f"{q['audio']['A']}.wav").write_bytes(b"local-other")      # 本机恰好有同名的
    lib.put("works/w-1.json", dict(id="w-1", title="t", lines=[], audio={q["audio"]["B"]: {}}))
    lib.put_bytes(f"works/w-1/audio/{q['audio']['B']}.wav", b"work-B")

    async def local(scope, receive, send):
        await gateway.app(dict(scope, client=("127.0.0.1", 50000)) if scope["type"] == "http" else scope, receive, send)
    api = TestClient(local)
    side = {q["left"]: "left", "B" if q["left"] == "A" else "A": "right"}
    assert api.get(f"/blind/b-00000001/questions/q01/{side['A']}").content == b"blind-A"
    assert api.get(f"/audio/{q['audio']['B']}").content == b"work-B"
    lib.path(f"works/w-1/audio/{q['audio']['B']}.wav").unlink()
    r = api.get(f"/audio/{q['audio']['B']}")
    assert r.status_code == 404 and r.json()["code"] == "audio_elsewhere" and r.json()["machine"] == "studio-host"
    r = api.get("/audio/20260101-000000-cccc")
    assert r.status_code == 404 and r.json().get("code") is None


def test_blind_created_on_this_controller_records_its_name(lib, monkeypatch, tmp_path):
    from ttsapi.compute import registry
    monkeypatch.setattr(settings, "COMPUTE_FILE", tmp_path / "compute.json")
    assert registry.local_name() == settings.MACHINE_NAME
    registry.patch_device("local", name="studio-host")
    assert registry.local_name() == "studio-host"
