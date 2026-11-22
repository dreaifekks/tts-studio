"""训练时用 ref_id 指定了别的角色的参考：那个角色算进任务占用（训练中不能删），服务重启接管时也一样。
要 gateway 能导入：除 conftest 列的依赖外还要 httpx、scipy。"""
import asyncio
import json
import threading
import time

import pytest
from fastapi import HTTPException

from conftest import write_wav

from ttsapi import gateway, jobs, settings
from ttsapi.models import Job, TrainRequest, TrainResult


@pytest.fixture
def ref_of_b(monkeypatch, tmp_path, lib):
    """用户角色 usr-b 的一条参考；数据集 X 存在（真实的 .list：gateway 启动时后台线程会读它的时长）。"""
    gateway.refs.index["usr-b-1"] = dict(id="usr-b-1", character="usr-b", text="こんにちは", lang="ja", duration=5.0,
                                         path="/nonexistent.wav")
    lst = tmp_path / "x.list"
    write_wav(tmp_path / "1.wav", 1.0)
    lst.write_text("1.wav|x|ja|こんにちは\n", encoding="utf-8")
    monkeypatch.setattr(gateway.datasets, "scan", lambda: {"X": (lst, None)})
    yield
    gateway.refs.index.pop("usr-b-1")


def test_train_explicit_ref_holds_owner(ref_of_b, monkeypatch):
    seen = {}

    async def on_local(task, compute=None, approved=False):  # 这里测占用，不测选设备：固定派到本机
        return dict(device="local", name="mini", type="local", seconds=60, units=None,
                    plan_reason_code="picked", plan_reason="手动选择")
    monkeypatch.setattr(gateway.dispatch, "choose", on_local)

    def fake_start(kind, target, fn, involves=()):
        seen["involves"] = set(involves)
        job = Job(id="train-test-1", kind=kind, target=target, status="queued", progress=0.0, stage="", created="")
        jobs.runs[job.id] = jobs.JobRun(job, time.time(), involves)
        return job

    monkeypatch.setattr(jobs, "start", fake_start)
    try:
        asyncio.run(gateway.train(TrainRequest(slug="usr-a", dataset_id="X", ref_id="usr-b-1")))
        assert seen["involves"] == {"usr-a", "usr-b"}
        with pytest.raises(HTTPException) as e:
            asyncio.run(gateway.delete_character("usr-b"))
        assert e.value.status_code == 409
    finally:
        jobs.runs.pop("train-test-1", None)
    # 自己训练列表里的句子（还没进索引）、不给 ref_id：只有自己
    asyncio.run(gateway.train(TrainRequest(slug="usr-a", dataset_id="X", ref_id="usr-a-123")))
    assert seen["involves"] == {"usr-a"}
    jobs.runs.pop("train-test-1", None)


def test_resume_restores_ref_owner(ref_of_b, monkeypatch):
    run_name = "usr-a-20261119-000000"
    (settings.TRAIN_DIR / run_name).mkdir(parents=True, exist_ok=True)
    req = TrainRequest(slug="usr-a", dataset_id="X", ref_id="usr-b-1")
    (settings.TRAIN_DIR / run_name / "runner_spec.json").write_text(json.dumps(dict(req=req.model_dump())))
    job = Job(id="train-test-2", kind="train", target="usr-a", status="running", progress=0.5, stage="", created="",
              train_result=TrainResult(slug="usr-a", engine="v2pro", run_name=run_name, lines_used=1, minutes_used=0.1,
                                       gpt_weights=[], sovits_weights=[]))
    seen = {}
    monkeypatch.setattr(jobs, "recover", lambda ok: [(job, 1.0)])
    monkeypatch.setattr(jobs, "resume", lambda job, created, fn, involves=(): seen.update(involves=set(involves)))

    async def boot():
        async with gateway.lifespan(gateway.app):
            pass

    asyncio.run(boot())
    for t in threading.enumerate():  # gateway 启动时读数据集时长的后台线程：等它在测试桩还生效时读完
        if t is not threading.current_thread() and t.daemon:
            t.join(10)
    assert seen["involves"] == {"usr-a", "usr-b"}
