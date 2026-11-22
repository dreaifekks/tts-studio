"""派发：规则 4 跟本机和局域网里最快的比；训练估时含轮数和训后评测；引擎能力表和无报价卡；先选设备再解析底模；
盲听补生成不会并发开两次、能带设备和批准；作品没有要生成的行时不选设备；worker 不开文档路由。"""
import asyncio
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from test_compute import AUTO, cand, l4, mini

from ttsapi import blind, gateway, jobs, settings, synth, training, works
from ttsapi.catalog import Catalog
from ttsapi.compute import dispatch, registry, remote, scheduler, worker_api
from ttsapi.compute.scheduler import Task
from ttsapi.models import ComputeChoice, GenerateRequest, Job, PlanRequest, TrainRequest, TTSParams, WorkCreate, WorkLineIn


def snapshot_of(devices, auto=AUTO):
    async def snapshot():
        return devices, auto
    return snapshot


# ---------------------------------------------------------------- 规则 4

def c(device, type_, seconds, gpu=None, units=None):
    return dict(device=device, name=device, type=type_, gpu=gpu, seconds=seconds, units=units, available=True,
                idle=True)


def test_rule4_compares_with_fastest_of_local_and_lan():
    # 本机 20 分钟、LAN 14 分 15 秒（规则 3 不成立：没快一半）、L4 9 分钟：LAN 不到 15 分钟，不上云端，按规则 5 用本机
    cands = [c("local", "local", 1200), c("lan-a", "lan", 855), c("colab-L4", "cloud", 540, "L4", 0.3)]
    assert scheduler.recommend(cands, Task("eval", [("v4", 36)]))[:2] == ("local", "fastest")
    # LAN 也要 20 分钟、L4 9 分钟（砍掉一半以上）：上云端，理由里比的是本机和 LAN 里最快的那台
    cands[1]["seconds"] = 1100
    dev, code, reason = scheduler.recommend(cands, Task("eval", [("v4", 36)]))
    assert (dev, code) == ("colab-L4", "cloud_halves") and reason.startswith("lan-a")
    # L4 没砍掉一半：不上云端
    cands[2]["seconds"] = 600
    assert scheduler.recommend(cands, Task("eval", [("v4", 36)]))[:2] == ("local", "fastest")


# ---------------------------------------------------------------- 训练估时

def train_req(**kw):
    return TrainRequest(slug="usr-a", dataset_id="X", **kw)


def test_train_task_counts_epochs_and_eval():
    hard = len(settings.EVAL_SETS[settings.EVAL_DEFAULT_SET]["sentences"])
    t = training.train_task(train_req(), 800)
    combos = (15 // 5) * (8 // 4)
    assert (t.kind, t.lines, t.epochs) == ("train", 800, 1.0)
    assert t.parts == [("v2pro", combos * hard * training.train_eval_candidates("v2pro", combos))]
    t10 = training.train_task(train_req(gpt_epochs=150, sovits_epochs=80), 800)
    assert t10.epochs == 10.0
    assert t10.parts == [("v2pro", 30 * 20 * hard * training.train_eval_candidates("v2pro", 30 * 20))]
    with pytest.raises(ValidationError):
        train_req(gpt_epochs=4)                                        # 小于保存间隔 5：训完没有权重


def test_train_estimate_and_confirm_follow_epochs():
    default = scheduler.plan([mini(), l4()], training.train_task(train_req(), 800), AUTO)
    more = scheduler.plan([mini(), l4()], training.train_task(train_req(gpt_epochs=150, sovits_epochs=80), 800), AUTO)
    d, m = cand(default, "colab-L4"), cand(more, "colab-L4")
    parts = training.train_task(train_req(), 800).parts
    ev = sum(n * (l4()["speeds"][e][0] + l4()["speeds"]["scorer"][0]) for e, n in parts)
    assert ev > 0 and d["seconds"] == round(360 + 800 * 1.1 + ev)
    assert m["units"] > 5 * d["units"]
    assert more["recommended"] == "colab-L4" and more["confirm"] == ["over_task"]


def test_plan_preview_matches_submit(monkeypatch):
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()]))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)
    p = asyncio.run(gateway.plan(PlanRequest(kind="train", engine="v2pro", lines=800, gpt_epochs=150)))
    expect = scheduler.estimate(l4(), training.train_task(train_req(gpt_epochs=150), 800))
    assert cand(p, "colab-L4")["seconds"] == expect["seconds"] and cand(p, "colab-L4")["units"] == expect["units"]


# ---------------------------------------------------------------- 引擎能力表、无报价卡

def test_cloud_engines_from_one_table():
    assert settings.CLOUD_ENGINES == ["v2pro", "v2proplus", "v4", "v5dev", "v5turbo"]
    registry.pool = None
    devs = registry.cloud_states([])
    assert all(d["engines"] == settings.CLOUD_ENGINES for d in devs)
    # v3 training: unsupported on cloud, does not recommend L4
    p = scheduler.plan([mini(), l4()], Task("train", [("v3", 10)], lines=800), AUTO)
    assert cand(p, "colab-L4")["reason_code"] == "engine_missing" and p["recommended"] != "colab-L4"
    # v5turbo / v5dev training: supported on cloud, recommends L4
    for eng in ("v5turbo", "v5dev"):
        pv5 = scheduler.plan([mini(), l4()], Task("train", [(eng, 10)], lines=800), AUTO)
        assert pv5["recommended"] == "colab-L4" and cand(pv5, "colab-L4")["available"] is True


def test_pick_unpriced_card_is_409(monkeypatch):
    h100 = l4(id="colab-H100", name="云端 H100", gpu="H100", rate=None)
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4(), h100]))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)
    task = Task("eval", [("v4", 36)])
    p = scheduler.plan([mini(), l4(), h100], task, AUTO, pick="colab-H100")
    assert p["confirm"] == [] and cand(p, "colab-H100")["reason_code"] == "no_rate"
    assert cand(p, "colab-H100")["units"] is None
    with pytest.raises(dispatch.PlanError) as e:
        asyncio.run(dispatch.choose(task, ComputeChoice(compute="colab-H100", approved=True)))
    assert e.value.code == "compute_unavailable"
    # 云端卡没测过也没有参考值：指定了也挡（估不出花费）
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4(speeds={})]))
    with pytest.raises(dispatch.PlanError) as e:
        asyncio.run(dispatch.choose(task, ComputeChoice(compute="colab-L4", approved=True)))
    assert e.value.code == "compute_unavailable"


# ---------------------------------------------------------------- 先选设备再解析底模

def test_remote_resolves_pretrained_missing_here(lib):
    """单测环境里本机没装任何底模：本机跑时找不到，派到局域网设备时用那台的底模（按路径认出，不传文件）。"""
    cat = Catalog()
    with pytest.raises(KeyError):
        cat.resolve("v4", "carol")
    gpt, gpt_path, sovits, sovits_path = cat.resolve("v4", "carol", remote=True)
    cfg = settings.ENGINES["v4"]
    assert (gpt, sovits) == (cfg["pretrained_gpt"].name, cfg["pretrained_sovits"].name)
    assert remote.pretrained_key("v4", gpt_path) == "pretrained_gpt"
    assert remote.pretrained_key("v4", sovits_path) == "pretrained_sovits"


def test_tts_resolves_after_choosing_device(lib, monkeypatch):
    seen = {}
    dev = dict(id="lan-a", name="gpu-worker", address="http://127.0.0.1:9", token="t")
    monkeypatch.setattr(gateway.catalog, "resolve", Catalog().resolve)

    async def on_lan(task, compute=None, approved=False):
        return dict(device="lan-a", type="lan")
    monkeypatch.setattr(gateway.dispatch, "choose", on_lan)
    monkeypatch.setattr(gateway.dispatch, "executor", lambda chosen: remote.Remote(dev))

    class Stop(Exception):
        pass

    async def fake_synth(p, engine, ref, weights, seed, score):
        seen["weights"], seen["exec"] = weights, synth.EXEC.get()
        raise Stop
    monkeypatch.setattr(synth, "synth", fake_synth)
    gateway.refs.index["r-x"] = dict(id="r-x", character="carol", text="あ", lang="ja", path="/nonexistent.wav")
    try:
        with pytest.raises(Stop):
            asyncio.run(gateway.tts(TTSParams(text="おはよう", character="carol", engine="v4", ref_id="r-x")))
    finally:
        gateway.refs.index.pop("r-x")
    assert seen["weights"][0] == settings.ENGINES["v4"]["pretrained_gpt"].name and seen["exec"].id == "lan-a"


# ---------------------------------------------------------------- 盲听补生成

def failed_blind(lib):
    q = dict(id="q01", sentence=0, repeat=0, seed=1, left="A", audio=dict(A=None, B=None), score=dict(A=None, B=None),
             vote=None, note="", voted_at=None)
    combo = dict(engine="v2pro", gpt="g.ckpt", sovits="s.pth")
    lib.put("blind/b-00000002.json", dict(
        id="b-00000002", created=synth.iso(time.time()), created_ts=time.time(), title="t", character="carol",
        eval_set=settings.EVAL_DEFAULT_SET, repeats=1, ref_id="r1", combos=dict(A=combo, B=combo), questions=[q],
        revealed_at=None, job_id="blind-gone"))


def test_blind_generate_twice_concurrently_starts_once(lib, monkeypatch):
    failed_blind(lib)
    started = []

    async def slow_choose(task, compute=None, approved=False):
        for _ in range(3):
            await asyncio.sleep(0)                                     # 查局域网设备状态时让出事件循环
        return dict(device="local", type="local")
    monkeypatch.setattr(gateway.dispatch, "choose", slow_choose)

    def fake_start(doc, chosen):
        job = Job(id=f"blind-test-{len(started)}", kind="blind", target=doc["id"], status="queued", progress=0.0,
                  stage="", created="")
        jobs.runs[job.id] = jobs.JobRun(job, time.time(), ())
        started.append(job.id)
        doc["job_id"] = job.id
        blind.save(doc)
    monkeypatch.setattr(gateway, "blind_start", fake_start)

    async def both():
        return await asyncio.gather(gateway.blind_generate("b-00000002"), gateway.blind_generate("b-00000002"),
                                    return_exceptions=True)
    try:
        results = asyncio.run(both())
    finally:
        for j in started:
            jobs.runs.pop(j, None)
    assert len(started) == 1
    errors = [r for r in results if isinstance(r, HTTPException)]
    assert len(errors) == 1 and errors[0].status_code == 409


def test_blind_generate_rejects_cloud_and_accepts_local_compute(lib, monkeypatch):
    """Blind regeneration rejects cloud GPUs (cloud_capable=False) while accepting local/LAN compute options."""
    failed_blind(lib)
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()], dict(AUTO, ask_over=0)))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)
    started = []
    monkeypatch.setattr(gateway, "blind_start", lambda doc, chosen: started.append(chosen["device"]))

    async def local(scope, receive, send):
        await gateway.app(dict(scope, client=("127.0.0.1", 50000)) if scope["type"] == "http" else scope, receive, send)
    api = TestClient(local)
    url = "/blind/b-00000002/generate"
    r = api.post(url, json=dict(compute="colab-L4"))
    assert r.status_code == 409 and r.json()["code"] == "compute_unavailable" and started == []
    assert cand(r.json()["plan"], "colab-L4")["reason_code"] == "kind_unsupported"
    assert api.post(url).status_code == 200 and started[-1] == "local"
    r = api.post(url, json=dict(compute="lan-nope"))
    assert r.status_code == 409 and r.json()["code"] == "compute_unavailable"


# ---------------------------------------------------------------- 作品：没有要生成的行

def test_generate_work_with_nothing_to_do_skips_scheduling(lib, monkeypatch):
    wid = works.create(WorkCreate(title="t", lines=[WorkLineIn(character="carol", text="a")])).id
    doc = works.load(wid)
    doc["lines"][0]["candidates"] = ["20260101-000000-aaaaaa"]
    works.save(doc)

    async def no_choose(*a, **kw):
        raise AssertionError("不该选设备")
    monkeypatch.setattr(gateway.dispatch, "choose", no_choose)

    async def main():
        job = await gateway.generate_work(wid, GenerateRequest())
        await jobs.runs[job.id].task
        return job
    job = asyncio.run(main())
    assert job.status == "succeeded" and job.compute is None
    assert job.generate_result.lines == []


# ---------------------------------------------------------------- worker 不开文档路由

def test_worker_has_no_docs_routes():
    client = TestClient(worker_api.app)
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404
