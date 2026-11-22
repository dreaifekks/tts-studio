"""算力：推荐规则（设计第 3 节「用实测数字套一遍」的表）、提醒线、移动平均、worker 令牌、按 sha256 传文件和缓存。"""
import asyncio
import base64
import hashlib
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from ttsapi import settings
from ttsapi.compute import dispatch, registry, remote, scheduler, speeds, worker_api
from ttsapi.compute.scheduler import Task
from ttsapi.models import ComputeChoice

AUTO = dict(mode="auto", device=None, ask_over=2.0, ask_month=500.0)


def mini(**kw):
    """mini 本机：CPU 跑 v2pro / v2proplus，v4 用 MPS（参考表 d08ff602 的数），没加载任何模型。"""
    dev = dict(id="local", name="studio-host", type="local", status="idle", engines=["v2pro", "v2proplus", "v4"], loaded=[],
               ram_gb=32, queue_sec=0,
               speeds={"v2pro": (2.4, "measured"), "v2proplus": (2.9, "measured"), "v4": (31, "measured"),
                       "scorer": (5, "measured"), "train": (5.5, "reference")})
    return {**dev, **kw}


def l4(**kw):
    dev = dict(id="colab-L4", name="云端 L4", type="cloud", gpu="L4", status="idle", engines=list(settings.CLOUD_ENGINES),
               loaded=[], ram_gb=None, queue_sec=0, rate=1.94, premium=False,
               speeds={"v2pro": (4.7, "reference"), "v2proplus": (4.7, "reference"), "v4": (7.5, "reference"),
                       "v5turbo": (10.0, "reference"), "v5dev": (38.0, "reference"),
                       "scorer": (0, "reference"), "train": (1.1, "reference")})
    return {**dev, **kw}


def lan(**kw):
    dev = dict(id="lan-aaaaaa", name="gpu-worker", type="lan", status="idle", engines=["v2pro", "v2proplus", "v4"],
               loaded=[], ram_gb=64, queue_sec=0,
               speeds={"v2pro": (4.0, "measured"), "v2proplus": (4.0, "measured"), "v4": (10, "measured"),
                       "scorer": (2, "measured")})
    return {**dev, **kw}


def cand(p, device):
    return next(c for c in p["candidates"] if c["device"] == device)


# ---------------------------------------------------------------- 设计第 3 节的表

def test_design_table_v2proplus_gen_2():
    p = scheduler.plan([mini(), l4()], Task("gen", [("v2proplus", 2)]), AUTO)
    assert (p["recommended"], p["reason_code"]) == ("local", "local_fast_enough")
    assert cand(p, "local")["seconds"] == 21                      # 约 21 秒（15 秒加载 + 2 × 2.9）
    c = cand(p, "colab-L4")
    assert (c["units"], c["available"], c["reason_code"]) == (0.25, True, None)
    assert 360 <= c["seconds"] <= 380                              # 约 6 分钟
    assert p["confirm"] == []
    # Non-cloud-capable gen tasks (sync / works / blind) still reject cloud GPUs.
    p_sync = scheduler.plan([mini(), l4()], Task("gen", [("v2proplus", 2)], cloud_capable=False), AUTO)
    cs = cand(p_sync, "colab-L4")
    assert (cs["units"], cs["available"], cs["reason_code"]) == (0.25, False, "kind_unsupported")
    assert cs["reason"] == "云端 GPU 暂支持合成、评测和训练"


def test_design_table_v4_gen_2():
    p = scheduler.plan([mini(), l4()], Task("gen", [("v4", 2)]), AUTO)
    assert (p["recommended"], p["reason_code"]) == ("local", "local_fast_enough")
    assert cand(p, "local")["seconds"] == 77                      # 约 1 分 17 秒
    assert (cand(p, "colab-L4")["seconds"], cand(p, "colab-L4")["units"]) == (375, 0.25)  # 6 分 15 秒 · 0.25


def test_design_table_v4_hard_eval_36_goes_to_cloud():
    p = scheduler.plan([mini(), l4()], Task("eval", [("v4", 36)]), AUTO)
    assert (p["recommended"], p["reason_code"]) == ("colab-L4", "cloud_halves")
    assert round(cand(p, "local")["seconds"] / 60) == 22           # 每条 31 秒合成 + 5 秒打分
    c = cand(p, "colab-L4")
    assert (round(c["seconds"] / 60), c["units"]) == (10, 0.34)    # 约 11 分钟 · 0.34 单元
    assert p["confirm"] == []                                      # 0.34 没超提醒线，不弹窗


def test_design_table_v2proplus_eval_36_stays_local():
    p = scheduler.plan([mini(), l4()], Task("eval", [("v2proplus", 36)]), AUTO)
    assert (p["recommended"], p["reason_code"]) == ("local", "fastest")
    assert round(cand(p, "local")["seconds"] / 60) == 5
    assert round(cand(p, "colab-L4")["seconds"] / 60) == 9


def test_design_table_train_800_goes_to_l4():
    p = scheduler.plan([mini(), l4()], Task("train", [("v2pro", 0)], lines=800), AUTO)
    assert (p["recommended"], p["reason_code"]) == ("colab-L4", "train_needs_gpu")
    assert round(cand(p, "local")["seconds"] / 3600, 1) == 1.2
    c = cand(p, "colab-L4")
    assert c["seconds"] == 1240 and c["units"] == 0.67             # 约 21 分钟 · 0.67 单元
    # 知道要上传多少时，开机准备 = 开卡、装环境 + 上传量 ÷ 2.4MB/s（917MB 的整包数据集约 6.4 分钟，只带一个角色 70MB 约 0.5 分钟）
    for mb in (917, 70):
        c = cand(scheduler.plan([l4()], Task("train", [("v2pro", 0)], lines=800, upload_mb=mb), AUTO), "colab-L4")
        assert c["seconds"] == round(1240 - 360 + scheduler.CLOUD_BOOT + mb / 2.4)


def test_calibrated_train_2346_lines_on_l4_matches_measured(no_speeds):
    """Regression test against 2026-11-21 measured L4 run: alice v2proplus 2346 lines (~61 min / 1.89 units)."""
    from ttsapi import training
    from ttsapi.models import TrainRequest
    assert speeds.lookup(dict(id="colab-L4", type="cloud", gpu="L4"), "train") == (1.1, "reference")
    assert speeds.lookup(dict(id="colab-T4", type="cloud", gpu="T4"), "train") == (1.75, "reference")
    assert speeds.lookup(dict(id="colab-A100", type="cloud", gpu="A100"), "train") == (0.66, "reference")
    assert speeds.lookup(dict(id="colab-G4", type="cloud", gpu="G4"), "train") == (0.55, "reference")
    assert speeds.lookup(M6, "train") == (5.5, "reference")
    task = training.train_task(TrainRequest(slug="alice", dataset_id="SAMPLE_DS.sp0002", engine="v2proplus"), 2346)
    p = scheduler.plan([mini(), l4()], task, AUTO)
    assert (p["recommended"], p["reason_code"]) == ("colab-L4", "train_needs_gpu")
    c = cand(p, "colab-L4")
    assert 3300 <= c["seconds"] <= 3900
    assert 1.7 <= c["units"] <= 2.2


# ---------------------------------------------------------------- 其余规则

def test_train_without_cloud_falls_back_to_local():
    p = scheduler.plan([mini(), lan(), l4(status="unavailable", status_reason="没有可用账号")],
                       Task("train", [("v2pro", 0)], lines=100), AUTO)
    assert (p["recommended"], p["reason_code"]) == ("local", "fastest")
    assert cand(p, "lan-aaaaaa")["reason_code"] == "kind_unsupported"
    assert cand(p, "colab-L4")["reason"] == "没有可用账号"


def test_rule3_idle_lan_much_faster():
    task = Task("gen", [("v4", 10)])                                # 本机 325 秒，gpu-worker 115 秒
    p = scheduler.plan([mini(), lan(), l4()], task, AUTO)
    assert (p["recommended"], p["reason_code"]) == ("lan-aaaaaa", "lan_faster")
    assert "快 2.8 倍" in p["reason"]
    busy = scheduler.plan([mini(), lan(status="busy"), l4()], task, AUTO)
    assert (busy["recommended"], busy["reason_code"]) == ("local", "fastest")   # 局域网设备忙：不换
    away = scheduler.plan([mini(status="offline"), lan(status="busy", queue_sec=300)], task, AUTO)
    assert (away["recommended"], away["reason_code"]) == ("lan-aaaaaa", "fastest")  # 本机不能用：局域网里最快的


def test_rule3_needs_a_minute_saved():
    # 本机 2.4 × 60 = 159 秒，gpu-worker 1.5 × 60 = 105 秒：快 1.5 倍但只省 54 秒
    p = scheduler.plan([mini(), lan(speeds={"v2pro": (1.5, "measured")})], Task("gen", [("v2pro", 60)]), AUTO)
    assert p["recommended"] == "local"


def test_rule0_unavailable_reasons():
    devs = [mini(status="training"), lan(ram_gb=8), l4(status="paused")]
    p = scheduler.plan(devs, Task("gen", [("v4", 2)]), AUTO)
    assert [c["reason_code"] for c in p["candidates"]] == ["training", "not_enough_memory", "paused"]
    assert cand(p, "lan-aaaaaa")["reason"] == "内存不够（约要 14GB）"
    assert (p["recommended"], p["reason_code"]) == (None, "no_device")
    p = scheduler.plan([mini(engines=["v2pro"]), lan(status="offline")], Task("gen", [("v4", 1)]), AUTO)
    assert [c["reason_code"] for c in p["candidates"]] == ["engine_missing", "offline"]


def test_untested_engine_stays_on_local():
    """本机没测过、参考表也没有：可用、估不出时间，自动选本机（新装的机器测速前也能生成）；局域网设备没测过不进推荐。"""
    p = scheduler.plan([mini(engines=["v4", "v5turbo"]), lan(engines=["v5turbo"])], Task("gen", [("v5turbo", 1)]), AUTO)
    c = cand(p, "local")
    assert (c["available"], c["reason_code"], c["seconds"], c["speed_source"]) == (True, None, None, "untested")
    assert (cand(p, "lan-aaaaaa")["available"], cand(p, "lan-aaaaaa")["reason_code"]) == (False, "untested")
    assert (p["recommended"], p["reason_code"]) == ("local", "local_untested")
    # 局域网设备测过、本机没测过：比不了，还是本机
    p = scheduler.plan([mini(engines=["v5turbo"]), lan(engines=["v5turbo"], speeds={"v5turbo": (1, "measured")})],
                       Task("gen", [("v5turbo", 1)]), AUTO)
    assert p["recommended"] == "local"


def test_loaded_model_and_queue():
    p = scheduler.plan([mini(loaded=["v4"], queue_sec=100)], Task("gen", [("v4", 2)]), AUTO)
    assert cand(p, "local")["seconds"] == 162                      # 排队 100 + 准备 0 + 62


def test_pinned_device():
    auto = dict(AUTO, mode="fixed", device="lan-aaaaaa")
    p = scheduler.plan([mini(), lan(), l4()], Task("gen", [("v2pro", 1)]), auto)
    assert (p["recommended"], p["reason_code"], [c["device"] for c in p["candidates"]]) == \
        ("lan-aaaaaa", "pinned", ["lan-aaaaaa"])
    off = scheduler.plan([mini(), lan(status="offline")], Task("gen", [("v2pro", 1)]), auto)
    assert (off["recommended"], off["reason_code"]) == (None, "pinned")


# ---------------------------------------------------------------- 提醒线

def test_confirm_lines():
    task = Task("eval", [("v4", 36)])                               # 云端 0.34 单元
    assert scheduler.plan([mini(), l4()], task, AUTO, month_used=100)["confirm"] == []
    assert scheduler.plan([mini(), l4()], task, dict(AUTO, ask_over=0.3))["confirm"] == ["over_task"]
    assert scheduler.plan([mini(), l4()], task, AUTO, month_used=499.8)["confirm"] == ["over_month"]
    assert scheduler.plan([mini(), l4()], task, dict(AUTO, ask_over=0, ask_month=0))["confirm"] == \
        ["over_task", "over_month"]                                 # 0 = 每次都问
    g4 = l4(id="colab-G4", name="云端 RTX PRO 6000", gpu="G4", rate=10.05, premium=True)
    assert scheduler.plan([mini(), g4], task, AUTO, pick="colab-G4")["confirm"] == ["premium_gpu"]
    # 选本机不花单元，不问
    assert scheduler.plan([mini(), l4()], task, dict(AUTO, ask_over=0), pick="local")["confirm"] == []
    # 训练 800 句约 0.67 单元：默认不问，提醒线调到 0.5 就问
    train = Task("train", [("v2pro", 0)], lines=800)
    assert scheduler.plan([mini(), l4()], train, AUTO)["confirm"] == []
    assert scheduler.plan([mini(), l4()], train, dict(AUTO, ask_over=0.5))["confirm"] == ["over_task"]


def test_choose_raises_confirm_required(monkeypatch):
    async def snapshot():
        return [mini(), l4()], dict(AUTO, ask_over=0.3)
    monkeypatch.setattr(registry, "snapshot", snapshot)
    task = Task("eval", [("v4", 36)])
    with pytest.raises(dispatch.PlanError) as e:
        asyncio.run(dispatch.choose(task))
    assert e.value.code == "confirm_required" and e.value.plan["recommended"] == "colab-L4"
    units = cand(e.value.plan, "colab-L4")["units"]
    chosen = asyncio.run(dispatch.choose(task, ComputeChoice(approved=True, approved_device="colab-L4",
                                                             approved_units=units)))
    assert (chosen["device"], chosen["plan_reason_code"], chosen["budget"]) == ("colab-L4", "cloud_halves", units)
    picked = asyncio.run(dispatch.choose(task, ComputeChoice(compute="local")))
    assert (picked["device"], picked["plan_reason_code"]) == ("local", "picked")
    # Manual cloud pick works for cloud-capable gen tasks, and is rejected for cloud_capable=False tasks.
    cloud_gen = asyncio.run(dispatch.choose(Task("gen", [("v4", 1)]), ComputeChoice(compute="colab-L4")))
    assert (cloud_gen["device"], cloud_gen["plan_reason_code"]) == ("colab-L4", "picked")
    with pytest.raises(dispatch.PlanError) as e:
        asyncio.run(dispatch.choose(Task("gen", [("v4", 1)], cloud_capable=False), ComputeChoice(compute="colab-L4")))
    assert e.value.code == "compute_unavailable"


def test_confirm_holds_only_for_the_device_and_units_seen(monkeypatch):
    """确认带着弹窗里的设备和单元：提交时重算涨价了（比如等确认时余额变了）、或换了设备、或只带 approved=true，
    都再要确认（附新 plan）；没涨价时确认的单元就是执行时的预算。"""
    async def snapshot():
        return [mini(), l4()], dict(AUTO, ask_over=0)
    monkeypatch.setattr(registry, "snapshot", snapshot)
    task = Task("eval", [("v4", 36)])
    units = cand(asyncio.run(dispatch.plan(task)), "colab-L4")["units"]
    for choice in (ComputeChoice(approved=True),
                   ComputeChoice(approved=True, approved_device="colab-L4", approved_units=units - 0.01),
                   ComputeChoice(approved=True, approved_device="colab-T4", approved_units=units)):
        with pytest.raises(dispatch.PlanError) as e:
            asyncio.run(dispatch.choose(task, choice))
        assert e.value.code == "confirm_required" and cand(e.value.plan, "colab-L4")["units"] == units
    ok = ComputeChoice(approved=True, approved_device="colab-L4", approved_units=units + 0.5)
    assert asyncio.run(dispatch.choose(task, ok))["budget"] == units + 0.5


# ---------------------------------------------------------------- 速度：参考值、实测、移动平均

@pytest.fixture
def no_speeds():
    settings.SPEEDS_FILE.unlink(missing_ok=True)
    yield
    settings.SPEEDS_FILE.unlink(missing_ok=True)


M6 = dict(id="local", type="local", machine="studio-host", hw=dict(cpu="Apple M6", accel="mps", ram_gb=32))


def test_reference_lookup(no_speeds):
    assert speeds.lookup(M6, "v2proplus") == (2.9, "reference")
    assert speeds.lookup(M6, "scorer") == (5, "reference")
    assert speeds.lookup(M6, "v5dev") == (None, "untested")
    assert speeds.lookup(dict(id="colab-L4", type="cloud", gpu="L4"), "v4") == (7.5, "reference")
    other = dict(id="lan-x", type="lan", hw=dict(cpu="Intel N100", accel="cpu"))
    assert speeds.lookup(other, "v2pro") == (8, "reference")       # 通用 CPU
    # 具体硬件的行里没有这一项、或者硬件不认识：按设备类型退到「通用 CPU 8 核」；那一行也没有的还是未测
    ryzen = dict(id="lan-y", type="lan", hw=dict(cpu="AMD Ryzen 7 7840H", accel="cpu"))
    assert speeds.lookup(ryzen, "v2pro") == (8, "reference") and speeds.lookup(ryzen, "v2proplus") == (10, "reference")
    assert speeds.lookup(dict(id="local", type="local", hw=dict(cpu="Intel Xeon", accel="cuda")), "scorer") == (6, "reference")
    assert speeds.lookup(dict(id="lan-z", type="lan", hw=None), "v2proplus") == (10, "reference")
    assert speeds.lookup(ryzen, "v4") == (None, "untested")


def test_moving_average_marks_measured(no_speeds):
    speeds.observe("local", "v2proplus", 4.0)
    assert speeds.lookup(M6, "v2proplus") == (4.0, "measured")     # 第一次直接用
    speeds.observe("local", "v2proplus", 2.0)
    assert speeds.lookup(M6, "v2proplus") == (3.4, "measured")     # 0.7 × 4 + 0.3 × 2
    data = json.loads(settings.SPEEDS_FILE.read_text())
    assert data["local"]["v2proplus"]["n"] == 2 and data["local"]["v2proplus"]["via"] == "task"


def test_per_hard_scales_by_length():
    assert speeds.per_hard(1.0, "あ" * round(speeds.HARD_CHARS)) == pytest.approx(1.0, rel=0.02)
    assert speeds.per_hard(1.0, "あ" * 10) > 3


def bench_doc(at, v4):
    return dict(machine="studio-host", at=at, engines={"v4": dict(device="mps", sec_per_item=v4)},
                scorer=dict(device="cpu", sec_per_item=4.0))


def test_newer_bench_in_library_wins(no_speeds, lib):
    lib.put("compute/bench/studio-host/2020-01-01-000000.json", bench_doc("2020-01-01T00:00:00+09:00", 20.0))
    assert speeds.lookup(M6, "v4") == (20.0, "measured")            # 只有测速结果
    speeds.observe("local", "v4", 40.0)
    assert speeds.lookup(M6, "v4") == (40.0, "measured")            # 比测速新的移动平均
    lib.put("compute/bench/studio-host/2099-01-01-000000.json", bench_doc("2099-01-01T00:00:00+09:00", 25.0))
    assert speeds.lookup(M6, "v4") == (25.0, "measured")            # 更新的测速
    assert speeds.lookup(dict(M6, id="lan-z", machine="other"), "v4") == (31, "reference")  # 别的机器的测速不算


# ---------------------------------------------------------------- worker：令牌、按 sha256 传文件

def sha(b: bytes):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def worker(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "WORKER_TOKEN", "tok-123")
    monkeypatch.setattr(settings, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(settings, "installed_engines", lambda: ["v2pro"])
    calls = []

    async def fake_call(path, payload):
        calls.append((path, payload))
        if path == "/synth":
            with open(payload["out_path"], "wb") as f:
                f.write(b"RIFF-fake-wav-" + open(payload["ref_audio"], "rb").read())
            return dict(duration=1.0, infer_sec=0.5, semantic_tokens=10, vram_peak_mb=0, segments=[])
        return dict(cer=0.0, spk_sim=0.9)
    monkeypatch.setattr(remote.ENGINES["v2pro"], "call", fake_call)
    monkeypatch.setattr(remote.SCORER, "call", fake_call)
    with TestClient(worker_api.app) as client:
        yield client, calls


def test_worker_needs_token(worker):
    client, _ = worker
    assert client.get("/worker/health").status_code == 401
    assert client.get("/worker/health", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.get("/worker/health", headers={"Authorization": "Bearer tok-123"})
    assert r.status_code == 200 and r.json()["role"] == "worker" and r.json()["engines"] == ["v2pro"]


@pytest.fixture
def unpaired(monkeypatch, tmp_path):
    """还没配对的 worker：没有 TTSAPI_WORKER_TOKEN、没有 device.json。"""
    monkeypatch.setattr(settings, "WORKER_TOKEN", None)
    monkeypatch.setattr(settings, "DATA", tmp_path)
    monkeypatch.setattr(settings, "DEVICE_FILE", tmp_path / "device.json")
    monkeypatch.setattr(settings, "BLOB_DIR", tmp_path / "blobs")
    return tmp_path


def test_pair_code(unpaired, monkeypatch):
    from ttsapi import pair
    code = pair.new_code()
    assert len(code) == 7 and code[3] == "-" and set(code.replace("-", "")) <= set(pair.ALPHABET)
    assert not set("0O1IL") & set(pair.ALPHABET)
    raw = (unpaired / "pair.json").read_text()
    assert code.replace("-", "") not in raw                                   # 只存 sha256
    assert oct(os.stat(unpaired / "pair.json").st_mode & 0o777) == "0o600"
    assert pair.token() is None
    assert pair.redeem("ABC-DEF") is None                                      # 不对
    tok = pair.redeem(" " + code.lower().replace("-", "") + " ")               # 大小写、横线、空格都行
    assert tok and len(tok) >= 24 and pair.token() == tok
    assert oct(os.stat(settings.DEVICE_FILE).st_mode & 0o777) == "0o600"
    assert pair.redeem(code) is None and pair.token() == tok                   # 用一次作废
    # 过期
    code = pair.new_code()
    now = time.time()
    monkeypatch.setattr(pair.time, "time", lambda: now + pair.TTL + 1)
    assert pair.redeem(code) is None and not (unpaired / "pair.json").exists()
    monkeypatch.setattr(pair.time, "time", lambda: now)
    # 输错 MAX_TRIES 次作废，之后对的也不行
    code = pair.new_code()
    for _ in range(pair.MAX_TRIES):
        assert pair.redeem("ZZZ-ZZZ" if code != "ZZZ-ZZZ" else "YYY-YYY") is None
    assert pair.redeem(code) is None
    # 新码换掉旧码；设了 TTSAPI_WORKER_TOKEN 时换到的就是它
    old, new = pair.new_code(), pair.new_code()
    assert old == new or pair.redeem(old) is None
    monkeypatch.setattr(settings, "WORKER_TOKEN", "fixed-token")
    assert pair.redeem(new) == "fixed-token"


def test_worker_pairing(unpaired, capsys):
    """没配对的 worker 启动时打印连接地址和配对码；/pair 不用令牌，换到令牌后其余接口用它；重新配对换掉旧令牌。"""
    from ttsapi import pair
    with TestClient(worker_api.app) as client:
        out = capsys.readouterr().out
        assert f"连接地址：http://{pair.mdns_host()}:{settings.PORT}" in out and pair.mdns_host().endswith(".local")
        code = out.split("配对码：")[1][:7]
        assert client.get("/worker/health").status_code == 401
        assert client.get("/worker/health", headers={"Authorization": "Bearer None"}).status_code == 401
        assert client.post("/pair", json=dict(code="ABC-DEF")).status_code == 403
        r = client.post("/pair", json=dict(code=code))
        assert r.status_code == 200 and r.json()["machine"] == settings.MACHINE_NAME
        tok = r.json()["token"]
        assert client.get("/worker/health", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
        assert client.post("/pair", json=dict(code=code)).status_code == 403   # 用过了
        tok2 = client.post("/pair", json=dict(code=pair.new_code())).json()["token"]
        assert tok2 != tok
        assert client.get("/worker/health", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
    with TestClient(worker_api.app):                                           # 已配对：重启不再打印配对码
        assert "配对码" not in capsys.readouterr().out
        assert worker_api.app.state.token == tok2


def test_worker_blob_cache(worker):
    client, _ = worker
    h = {"Authorization": "Bearer tok-123"}
    data = b"weights" * 1000
    assert client.put(f"/worker/blobs/{sha(b'other')}", content=data, headers=h).status_code == 400  # sha 对不上
    assert client.put(f"/worker/blobs/{sha(data)}", content=data, headers=h).json() == dict(sha=sha(data), cached=False)
    assert client.put(f"/worker/blobs/{sha(data)}", content=data, headers=h).json()["cached"] is True
    assert not list(settings.BLOB_DIR.glob(".*.tmp"))


def test_worker_call_reports_missing_blobs(worker):
    client, calls = worker
    h = {"Authorization": "Bearer tok-123"}
    body = dict(worker="v2pro", path="/synth", payload=dict(text="x"), blobs=dict(ref_audio=sha(b"ref")),
                pretrained=dict(gpt="pretrained_gpt", sovits="pretrained_sovits"), outputs=dict(out_path=".wav"))
    r = client.post("/worker/call", json=body, headers=h)
    assert r.status_code == 409 and r.json() == dict(detail="缺文件", code="missing_blobs", missing=[sha(b"ref")])
    client.put(f"/worker/blobs/{sha(b'ref')}", content=b"ref", headers=h)
    r = client.post("/worker/call", json=body, headers=h)
    assert r.status_code == 200
    wav = base64.b64decode(r.json()["outputs"]["out_path"])
    assert wav == b"RIFF-fake-wav-ref"
    payload = calls[-1][1]
    assert payload["gpt"] == str(settings.ENGINES["v2pro"]["pretrained_gpt"])   # 底模用设备自己的
    assert (settings.BLOB_DIR / sha(wav)).exists()                              # 合成的音频也缓存，打分不用再传
    assert client.post("/worker/call", json=dict(body, worker="v4"), headers=h).status_code == 400  # 没装的引擎


def test_remote_pushes_missing_files_once(worker, tmp_path, monkeypatch):
    """主控一侧：缺的文件 PUT 过去后重发；第二次同样的文件不再传；产出的文件写到主控的本地路径。"""
    client, calls = worker
    transport = httpx.ASGITransport(app=worker_api.app)
    real = httpx.AsyncClient
    puts = []

    def client_factory(**kw):
        c = real(transport=transport, base_url="http://w", **kw)
        orig = c.put

        async def put(url, **k):
            puts.append(url)
            return await orig(url, **k)
        c.put = put
        return c
    monkeypatch.setattr(remote.httpx, "AsyncClient", client_factory)
    ref = tmp_path / "ref.ogg"
    ref.write_bytes(b"ref-audio")
    gpt = tmp_path / "carol-e15.ckpt"
    gpt.write_bytes(b"gpt" * 100)
    dev = remote.Remote(dict(id="lan-aaaaaa", name="gpu-worker", address="http://w", token="tok-123"))

    async def go(out):
        return await dev.call("v2pro", "/synth", dict(text="x"), files=dict(ref_audio=ref, gpt=gpt,
                                                                             sovits=settings.ENGINES["v2pro"]["pretrained_sovits"]),
                              outputs=dict(out_path=out), pretrained=dict(sovits="pretrained_sovits"))
    res = asyncio.run(go(tmp_path / "a.wav"))
    assert res["infer_sec"] == 0.5 and (tmp_path / "a.wav").read_bytes() == b"RIFF-fake-wav-ref-audio"
    assert sorted(u.rsplit("/", 1)[1] for u in puts) == sorted([sha(b"ref-audio"), sha(b"gpt" * 100)])
    asyncio.run(go(tmp_path / "b.wav"))
    assert len(puts) == 2                                                       # 已缓存：不再传


def test_sha256_cache(tmp_path, monkeypatch):
    f = tmp_path / "w.pth"
    f.write_bytes(b"a")
    a, bb = sha(b"a"), sha(b"bb")
    assert remote.sha256_cached(f) == a
    n = []
    real = hashlib.sha256
    monkeypatch.setattr(remote.hashlib, "sha256", lambda *x: n.append(1) or real(*x))
    assert remote.sha256_cached(f) == a and n == []                   # 命中缓存，不重算
    f.write_bytes(b"bb")
    os.utime(f, ns=(time.time_ns(), time.time_ns() + 10**9))
    assert remote.sha256_cached(f) == bb and n == [1]                  # 文件变了：重算


# ---------------------------------------------------------------- 接口

@pytest.fixture
def api(monkeypatch, tmp_path):
    """gateway（不进 lifespan），本机当成 mini（Apple M6，装了 v2pro、v2proplus、v4），没装 colab CLI。"""
    from ttsapi import gateway
    monkeypatch.setattr(settings, "COMPUTE_FILE", tmp_path / "compute.json")
    monkeypatch.setattr(settings, "SPEEDS_FILE", tmp_path / "speeds.json")
    monkeypatch.setattr(settings, "installed_engines", lambda: ["v2pro", "v2proplus", "v4"])
    monkeypatch.setattr(registry, "_hw", dict(cpu="Apple M6", cores=12, ram_gb=32, accel="mps", gpu=None, os="darwin",
                                              torch="2.11.0"))

    async def local(scope, receive, send):
        await gateway.app(dict(scope, client=("127.0.0.1", 50000)) if scope["type"] == "http" else scope, receive, send)
    return TestClient(local)


def test_api_compute_list(api):
    r = api.get("/compute").json()
    devs = {d["id"]: d for d in r["devices"]}
    assert list(devs) == ["local", "colab-T4", "colab-L4", "colab-A100", "colab-G4", "colab-H100"]
    assert devs["local"]["status"] == "idle" and devs["local"]["engines"] == ["v2pro", "v2proplus", "v4"]
    assert devs["local"]["speeds"]["v4"] == dict(sec_per_item=31, source="reference")
    assert devs["colab-L4"]["status"] == "unavailable" and devs["colab-L4"]["status_reason"] == "没有装云端 GPU 组件"
    assert devs["colab-L4"]["speeds"]["v4"] == dict(sec_per_item=7.5, source="reference")
    assert devs["colab-G4"]["premium"] and devs["colab-H100"]["rate"] is None
    assert r["auto"] == AUTO and r["month_used"] == 0 and r["engines"] == ["v2pro", "v2proplus", "v4"]


def test_api_plan_and_auto(api):
    p = api.post("/plan", json=dict(kind="gen", engine="v4", items=2)).json()
    assert (p["recommended"], p["reason_code"], p["chosen"]) == ("local", "local_fast_enough", "local")
    l4 = next(c for c in p["candidates"] if c["device"] == "colab-L4")
    assert (l4["available"], l4["reason_code"], l4["seconds"], l4["units"]) == (False, "unavailable", 375, 0.25)
    assert (l4["prep_sec"], cand(p, "local")["prep_sec"]) == (360, None)  # 云端开始前约 6 分钟准备，含在 seconds 里
    # 云端不能用：v4 难句评测 36 条留在本机
    p = api.post("/plan", json=dict(kind="eval", engine="v4", items=36)).json()
    assert (p["recommended"], p["reason_code"]) == ("local", "fastest")
    assert api.put("/compute/auto", json=dict(mode="fixed")).status_code == 400
    assert api.put("/compute/auto", json=dict(mode="fixed", device="local", ask_over=0)).json() == \
        dict(AUTO, mode="fixed", device="local", ask_over=0)
    p = api.post("/plan", json=dict(kind="gen", engine="v2pro", items=1)).json()
    assert (p["recommended"], p["reason_code"], len(p["candidates"])) == ("local", "pinned", 1)
    assert api.put("/compute/auto", json=dict(mode="auto")).json() == dict(AUTO, ask_over=0)
    assert oct(os.stat(settings.COMPUTE_FILE).st_mode & 0o777) == "0o600"


def test_api_devices(api):
    r = api.post("/compute/devices", json=dict(name="gpu-worker", address="http://127.0.0.1:9", pair_code="ABC-DEF"))
    assert r.status_code == 400 and "连不上" in r.json()["detail"]
    r = api.patch("/compute/devices/colab-T4", json=dict(paused=True))
    assert r.status_code == 200 and r.json()["status"] == "paused"
    assert api.patch("/compute/devices/colab-T4", json=dict(name="x")).status_code == 400
    assert api.patch("/compute/devices/local", json=dict(address="http://x")).status_code == 400
    assert api.patch("/compute/devices/local", json=dict(pair_code="ABC-DEF")).status_code == 400
    assert api.patch("/compute/devices/lan-nope", json=dict(paused=True)).status_code == 404
    assert api.delete("/compute/devices/lan-nope").status_code == 404


def test_api_rename_local(api):
    r = api.patch("/compute/devices/local", json=dict(name="studio-host"))
    assert r.status_code == 200 and r.json()["name"] == "studio-host"
    assert api.get("/compute").json()["devices"][0]["name"] == "studio-host"
    assert api.patch("/compute/devices/local", json=dict(paused=True)).json()["name"] == "studio-host"
    assert api.patch("/compute/devices/local", json=dict(name="")).json()["name"] == settings.MACHINE_NAME


def test_api_add_device_with_pair_code(api, unpaired, monkeypatch):
    """主控 POST /compute/devices 用配对码找 worker 换令牌：令牌存在主控的 compute.json，接口里看不到；
    配对码不对 400 不登记；请求里不再收 token；重新配对（PATCH pair_code）换成新令牌。"""
    from ttsapi import pair
    transport = httpx.ASGITransport(app=worker_api.app)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    monkeypatch.setattr(worker_api.app.state, "token", None, raising=False)
    monkeypatch.setattr(settings, "installed_engines", lambda: ["v2pro", "v2proplus", "v4"])
    body = dict(name="gpu-worker", address="http://gpu-worker.local:9880/")
    r = api.post("/compute/devices", json=dict(body, token="x"))
    assert r.status_code == 422                                                # 只收配对码
    r = api.post("/compute/devices", json=dict(body, pair_code="ABC-DEF"))
    assert r.status_code == 400 and "配对码" in r.json()["detail"]
    assert registry.load()["devices"] == []
    r = api.post("/compute/devices", json=dict(body, pair_code=pair.new_code(),
                                               wake=dict(mac="aa:bb:cc:dd:ee:ff")))
    assert r.status_code == 200, r.text
    dev = r.json()
    assert dev["name"] == "gpu-worker" and dev["status"] == "idle" and dev["wake"]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert "token" not in json.dumps(dev) and "token" not in api.get("/compute").text
    saved = registry.load()["devices"][0]
    assert saved["token"] == pair.token() == worker_api.app.state.token and saved["address"] == "http://gpu-worker.local:9880"
    # 重新配对
    r = api.patch(f"/compute/devices/{dev['id']}", json=dict(pair_code="ABC-DEF"))
    assert r.status_code == 400 and registry.load()["devices"][0]["token"] == saved["token"]
    r = api.patch(f"/compute/devices/{dev['id']}", json=dict(pair_code=pair.new_code()))
    assert r.status_code == 200 and r.json()["status"] == "idle"
    assert registry.load()["devices"][0]["token"] == worker_api.app.state.token != saved["token"]


def test_single_engine_bench_keeps_other_engines(no_speeds, lib):
    lib.put("compute/bench/studio-host/2026-01-01-000000.json", bench_doc("2026-01-01T00:00:00+09:00", 30.0))
    lib.put("compute/bench/studio-host/2026-02-01-000000.json", dict(
        machine="studio-host", at="2026-02-01T00:00:00+09:00", scorer=None,
        engines={"v2proplus": dict(device="cpu", threads=6, sec_per_item=2.5)}))
    assert speeds.lookup(M6, "v2proplus") == (2.5, "measured")
    assert speeds.lookup(M6, "v4") == (30.0, "measured")             # 只测 v2proplus 的那次不盖掉 v4
    assert speeds.lookup(M6, "scorer") == (4.0, "measured")
    speeds.set_bench("local", dict(at="2026-03-01T00:00:00+09:00", scorer=None,
                                   engines={"v4": dict(sec_per_item=28.0)}))
    data = json.loads(settings.SPEEDS_FILE.read_text())
    assert list(data["local"]) == ["v4"] and data["local"]["v4"]["via"] == "bench"


def test_explicit_pick_of_untested_device(monkeypatch):
    """Colab VM 上的 gateway 提交时带 compute=local：VM 的 CPU 没有参考值，也照样跑；CUDA 设备不按内存挡。"""
    vm = mini(engines=["v4"], ram_gb=None, speeds={})

    async def snapshot():
        return [vm], AUTO
    monkeypatch.setattr(registry, "snapshot", snapshot)
    task = Task("eval", [("v4", 36)])
    for pick in (None, "local"):                                     # 自动也选本机（本机没测过不挡）
        chosen = asyncio.run(dispatch.choose(task, ComputeChoice(compute=pick)))
        assert (chosen["device"], chosen["seconds"], chosen["available"]) == ("local", None, True)


def test_work_lines_do_not_store_compute():
    """在哪跑是每次提交的选择，不是作品行的生成参数（作品生成按 GEN_FIELDS 从行上取参数）。"""
    from ttsapi import works
    from ttsapi.models import WorkLineIn
    assert not {"compute", "approved"} & works.GEN_FIELDS
    assert works.GEN_FIELDS <= set(WorkLineIn.model_fields)


def test_blob_cache_cap(worker, monkeypatch):
    """总量超过上限时从最久没用的开始删；用到的文件更新修改时间，不会先被删；7 天没用的照样删。"""
    client, _ = worker
    h = {"Authorization": "Bearer tok-123"}
    monkeypatch.setattr(settings, "BLOB_MAX_GB", 3000 / 2**30)  # 3000 字节
    blobs = [bytes([i]) * 1000 for i in range(3)]
    now = time.time()
    for i, b in enumerate(blobs):
        client.put(f"/worker/blobs/{sha(b)}", content=b, headers=h)
        os.utime(settings.BLOB_DIR / sha(b), (now - 100 + i, now - 100 + i))
    os.utime(settings.BLOB_DIR / sha(blobs[0]))                       # 最旧的这个刚用过
    new = b"n" * 1000
    client.put(f"/worker/blobs/{sha(new)}", content=new, headers=h)
    left = {f.name for f in settings.BLOB_DIR.iterdir()}
    assert left == {sha(blobs[0]), sha(blobs[2]), sha(new)}           # 删的是最久没用的 blobs[1]
    os.utime(settings.BLOB_DIR / sha(blobs[2]), (now - 8 * 86400,) * 2)
    worker_api.cleanup_blobs()
    assert sha(blobs[2]) not in {f.name for f in settings.BLOB_DIR.iterdir()}


def test_reupload_keeps_blobs_of_same_request(worker, monkeypatch):
    """请求要两个文件（共 800 字节，上限 1000），第一个已缓存、第二个缺：补传第二个时淘汰的是别的旧文件，不是第一个；
    重发能执行。"""
    client, _ = worker
    h = {"Authorization": "Bearer tok-123"}
    monkeypatch.setattr(settings, "BLOB_MAX_GB", 1000 / 2**30)
    monkeypatch.setattr(worker_api, "HELD", {})
    for f in settings.BLOB_DIR.iterdir():
        f.unlink()
    a, b, other = b"a" * 400, b"b" * 400, b"o" * 300
    now = time.time()
    for i, data in enumerate((a, other)):
        client.put(f"/worker/blobs/{sha(data)}", content=data, headers=h)
        os.utime(settings.BLOB_DIR / sha(data), (now - 100 + i,) * 2)    # a 比 other 还旧
    body = dict(worker="v2pro", path="/synth", payload=dict(text="x"), blobs=dict(ref_audio=sha(a), other=sha(b)),
                outputs=dict(out_path=".wav"))
    r = client.post("/worker/call", json=body, headers=h)
    assert r.status_code == 409 and r.json()["missing"] == [sha(b)]
    client.put(f"/worker/blobs/{sha(b)}", content=b, headers=h)
    assert {f.name for f in settings.BLOB_DIR.iterdir()} == {sha(a), sha(b)}  # 删的是 other
    assert client.post("/worker/call", json=body, headers=h).status_code == 200
