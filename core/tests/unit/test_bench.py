"""测速（ttsapi.bench）：不起真 worker，用假 worker 按「设备 × 线程数」给出每句秒数。
覆盖选最快配置、慢配置停掉、粗测、tuning 写回、只测一个引擎、结果结构、写进资料库的位置，以及测速对设置的强制覆盖。"""
import asyncio
import json
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

REF = dict(path="/ref.wav", text="参考", lang="ja")


class FakeWorker:
    """和 workers.Worker 同样的构造参数；每次调用按 TIMES[(名字, 设备, 线程数)] 睡这么多秒，infer_sec 就是这个数。
    HANG[("ensure" 或 "call", 名字, 设备, 线程数)] = asyncio.Event()：走到那一步时 set 这个事件，然后一直挂着（等测试取消）。"""
    TIMES = {}
    HANG = {}
    created = []

    def __init__(self, name, module, cfg, args, extra_env=None):
        self.name, self.module, self.cfg, self.args, self.env = name, module, cfg, args, extra_env
        self.url = f"http://127.0.0.1:{cfg['port']}"
        self.proc = None
        self.calls = []
        self.stopped = False
        FakeWorker.created.append(self)

    @property
    def key(self):
        threads = self.env.get("TTSAPI_BENCH_THREADS")
        return self.name.removeprefix("bench-"), self.env["TTSAPI_BENCH_DEVICE"], str(threads) if threads else None

    @property
    def running(self):
        return self.proc is not None

    async def _maybe_hang(self, step):
        if (entered := self.HANG.get((step, *self.key))) is not None:
            entered.set()
            await asyncio.Event().wait()

    async def ensure(self):
        self.proc = SimpleNamespace(pid=os.getpid())  # 和真 Worker 一样：进程先起来，再等它的 /health
        await self._maybe_hang("ensure")

    async def call(self, path, payload):
        self.calls.append((path, payload))
        await self._maybe_hang("call")
        sec = self.TIMES[self.key]
        await asyncio.sleep(sec)
        if path == "/synth":
            return dict(infer_sec=sec, duration=10 * sec, vram_peak_mb=0)
        return dict(cer=0, spk_sim=0.8)

    def stop(self):
        self.proc = None
        self.stopped = True


class Normal:
    """正常 worker（workers.ENGINES / SCORER）：只记有没有被停掉。"""

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture
def bench(monkeypatch, clean_data):
    from ttsapi import bench, settings, workers
    from ttsapi.compute import hw
    settings.TUNING_FILE.unlink(missing_ok=True)
    FakeWorker.TIMES, FakeWorker.HANG, FakeWorker.created = {}, {}, []
    monkeypatch.setattr(bench, "_lock", asyncio.Lock())
    monkeypatch.setattr(workers, "Worker", FakeWorker)
    monkeypatch.setattr(workers, "ENGINES", {e: Normal() for e in settings.ENGINES})
    monkeypatch.setattr(workers, "SCORER", Normal())
    monkeypatch.setattr(workers, "training_job", None)

    async def health(worker):
        return dict(torch_threads=worker.env.get("TTSAPI_BENCH_THREADS"))

    monkeypatch.setattr(bench, "_health", health)
    monkeypatch.setattr(bench, "CUT_MARGIN_SEC", 0)
    monkeypatch.setattr(bench, "ROUGH_SEC", 0.05)
    monkeypatch.setattr(settings, "installed_engines", lambda: ["v2pro", "v2proplus", "v4"])
    machine = dict(cpu="Apple M6", cores=12, ram_gb=32, accel="mps", gpu=None, os="darwin", torch="2.11.0")
    monkeypatch.setattr(hw, "hardware", lambda: dict(machine))
    yield bench
    settings.TUNING_FILE.unlink(missing_ok=True)


def times(name, mps=None, **cpu):
    """times("v2pro", mps=0.02, t6=0.01, ...)：一个 worker 各配置的每句秒数。"""
    out = {(name, "mps", None): mps} if mps is not None else {}
    out.update({(name, "cpu", t[1:]): sec for t, sec in cpu.items()})
    return out


def run(bench, **kw):
    return asyncio.run(bench.run_bench(REF, **kw))


def test_picks_fastest_config_and_saves_tuning(bench):
    from ttsapi import workers
    from ttsapi.compute import tuning
    FakeWorker.TIMES = times("v2pro", mps=0.03, t6=0.02, t8=0.01, t4=0.015)
    r = run(bench, engines=["v2pro"])
    e = r["engines"]["v2pro"]
    assert (e["device"], e["threads"], e["items"]) == ("cpu", 8, 6)
    assert e["sec_per_item"] == pytest.approx(0.01, abs=1e-3)
    assert e["rtf"] == pytest.approx(0.1, abs=1e-3)
    assert "rough" not in e
    # 先试加速，再按 6、8、4 线程试；每个配置粗筛 2 句，最快的跑满 6 句
    assert [(t["device"], t["threads"], t["items"]) for t in e["tried"]] == \
        [("mps", None, 2), ("cpu", 6, 2), ("cpu", 8, 2), ("cpu", 4, 2)]
    assert tuning.load() == {"v2pro": {"device": "cpu", "threads": 8}}
    assert workers.ENGINES["v2pro"].stopped and not workers.ENGINES["v4"].stopped
    # 临时 worker：测速专用的设备 / 线程环境变量，端口不和正常 worker 冲突，全部关掉
    cfg = workers.settings.ENGINES["v2pro"]
    ports = [w.cfg["port"] - cfg["port"] for w in FakeWorker.created]
    assert ports == [50, 51, 52, 53]
    assert all(w.env["TTSAPI_WORKER_PORT_SHIFT"] == workers.settings.WORKER_PORT_SHIFT + p
               for w, p in zip(FakeWorker.created, ports, strict=True))
    assert [w.env["TTSAPI_BENCH_DEVICE"] for w in FakeWorker.created] == ["mps", "cpu", "cpu", "cpu"]
    assert not any(w.running for w in FakeWorker.created)
    # 固定题目、固定 seed、底模
    best = FakeWorker.created[2]
    texts = [p["text"] for _, p in best.calls]
    assert texts == [bench.WARMUP_TEXT, *bench.SENTENCES]
    assert {p["seed"] for _, p in best.calls} == {bench.SEED}
    assert best.calls[0][1]["sovits"] == str(cfg["pretrained_sovits"])


def test_slow_cpu_config_is_cut_and_rest_skipped(bench):
    FakeWorker.TIMES = times("v4", mps=0.02, t6=0.3, t8=0.3, t4=0.3)
    r = run(bench, engines=["v4"])
    e = r["engines"]["v4"]
    assert (e["device"], e["threads"], e["items"]) == ("mps", None, 6)
    cpu6, cpu8, cpu4 = e["tried"][1:]
    assert cpu6["sec_per_item"] is None and cpu6["cut_after_sec"] == pytest.approx(bench.CUT_FACTOR * 0.02, abs=1e-3)
    assert cpu8 == dict(device="cpu", threads=8, skipped=True) and cpu4["skipped"]
    assert len(FakeWorker.created) == 2


def test_rough_on_slow_cpu(bench, monkeypatch):
    """没有加速的机器上 v4 每句超过 ROUGH_SEC：每个配置只计 1 句，最快的计 2 句，标粗测。"""
    from ttsapi.compute import hw, tuning
    hw_cpu = dict(hw.hardware(), accel="cpu")
    monkeypatch.setattr(hw, "hardware", lambda: hw_cpu)
    FakeWorker.TIMES = times("v4", t6=0.08, t8=0.06, t4=0.1)
    r = run(bench, engines=["v4"])
    e = r["engines"]["v4"]
    assert (e["device"], e["threads"], e["items"], e["rough"]) == ("cpu", 8, 2, True)
    assert [t["items"] for t in e["tried"]] == [1, 1, 1]
    assert all(t["rough"] for t in e["tried"])
    assert tuning.load()["v4"] == {"device": "cpu", "threads": 8}


def test_full_bench_structure_and_scorer(bench):
    from ttsapi import settings, workers
    from ttsapi.compute import tuning
    FakeWorker.TIMES = {**times("v2pro", mps=0.02, t6=0.01, t8=0.012, t4=0.015),
                        **times("v2proplus", mps=0.02, t6=0.012, t8=0.01, t4=0.015),
                        **times("v4", mps=0.03, t6=0.06, t8=0.06, t4=0.06),
                        **times("scorer", t6=0.02, t8=0.01, t4=0.02), ("scorer", "mps", "8"): 0.015}
    stages = []
    r = run(bench, progress=lambda s, f: stages.append((s, f)))
    assert set(r) == {"machine", "alias", "at", "bench_version", "quick", "hw", "engines", "scorer", "elapsed_sec"}
    assert r["machine"] == r["alias"] == settings.MACHINE_NAME
    assert datetime.fromisoformat(r["at"]).tzinfo is not None
    assert (r["bench_version"], r["quick"], r["hw"]["accel"]) == (1, False, "mps")
    assert list(r["engines"]) == ["v2pro", "v2proplus", "v4"]
    for e in r["engines"].values():
        assert {"device", "threads", "sec_per_item", "rtf", "load_sec", "peak_gb", "items", "tried"} <= set(e)
    # 打分器：先试 CPU 各档，再试 MPS（whisper 仍在 CPU 上，线程用最快的那档）；用前面合成出的 6 条音频
    s = r["scorer"]
    assert (s["device"], s["threads"], s["items"]) == ("cpu", 8, 6)
    assert [(t["device"], t["threads"]) for t in s["tried"]] == [("cpu", 6), ("cpu", 8), ("cpu", 4), ("mps", 8)]
    scorer = next(w for w in FakeWorker.created if w.name == "bench-scorer" and w.env.get("TTSAPI_BENCH_THREADS") == 8
                  and w.env["TTSAPI_BENCH_DEVICE"] == "cpu")
    assert scorer.env["TTSAPI_ASR_THREADS"] == 8
    assert [p["text"] for _, p in scorer.calls] == [bench.WARMUP_TEXT, *bench.SENTENCES]
    assert tuning.load() == {"v2pro": {"device": "cpu", "threads": 6}, "v2proplus": {"device": "cpu", "threads": 8},
                             "v4": {"device": "mps"}, "scorer": {"device": "cpu", "threads": 8}}
    assert workers.SCORER.stopped and all(workers.ENGINES[e].stopped for e in r["engines"])
    assert stages[-1] == ("done", 1.0) and all(0 <= f <= 1 for _, f in stages)
    assert json.loads(json.dumps(r)) == r  # 能原样写进资料库


def test_only_one_engine(bench):
    from ttsapi import workers
    from ttsapi.compute import tuning
    tuning.save("v4", "mps")
    FakeWorker.TIMES = times("v2proplus", mps=0.02, t6=0.01, t8=0.012, t4=0.015)
    r = run(bench, engines=["v2proplus"])
    assert list(r["engines"]) == ["v2proplus"] and "scorer" not in r
    assert tuning.load() == {"v4": {"device": "mps"}, "v2proplus": {"device": "cpu", "threads": 6}}
    assert not workers.SCORER.stopped and not workers.ENGINES["v4"].stopped
    assert {w.name for w in FakeWorker.created} == {"bench-v2proplus"}


def test_quick_untested_tries_cpu6_and_accel(bench):
    """快速测速：默认只测 v2proplus；没测过时试 CPU 6 线程和本机加速，每档热身 1 句 + 计 3 句；不测打分器。"""
    from ttsapi import workers
    from ttsapi.compute import tuning
    tuning.save("scorer", "mps", 8)
    FakeWorker.TIMES = times("v2proplus", mps=0.02, t6=0.01)
    r = run(bench, quick=True)
    assert r["quick"] is True and "scorer" not in r
    e = r["engines"]["v2proplus"]
    assert list(r["engines"]) == ["v2proplus"]
    assert [(t["device"], t["threads"], t["items"]) for t in e["tried"]] == [("cpu", 6, 3), ("mps", None, 3)]
    assert (e["device"], e["threads"], e["items"]) == ("cpu", 6, 3)
    for w in FakeWorker.created:
        assert [p["text"] for _, p in w.calls] == [bench.WARMUP_TEXT, *bench.SENTENCES[:3]]
    assert tuning.load() == {"scorer": {"device": "mps", "threads": 8}, "v2proplus": {"device": "cpu", "threads": 6}}
    assert not workers.SCORER.stopped and workers.ENGINES["v2proplus"].stopped


def test_quick_best_is_accel(bench):
    """tuning 里最佳是加速：备选是 CPU 6 线程；备选更快就换成它。"""
    from ttsapi.compute import tuning
    tuning.save("v2proplus", "mps")
    FakeWorker.TIMES = times("v2proplus", mps=0.02, t6=0.01)
    e = run(bench, quick=True)["engines"]["v2proplus"]
    assert [(t["device"], t["threads"]) for t in e["tried"]] == [("mps", None), ("cpu", 6)]
    assert tuning.load()["v2proplus"] == {"device": "cpu", "threads": 6}


def test_quick_without_accel_tries_other_threads(bench, monkeypatch):
    """没有加速的机器：最佳 CPU 4 线程的备选是 6 线程，最佳 6 线程的备选是 8 线程；给了 engines 就测给的。"""
    from ttsapi.compute import hw, tuning
    monkeypatch.setattr(hw, "hardware", lambda: dict(cpu="x", cores=8, ram_gb=16, accel="cpu", gpu=None, os="linux",
                                                     torch="2.11.0"))
    tuning.save("v2pro", "cpu", 4)
    tuning.save("v2proplus", "cpu", 6)
    FakeWorker.TIMES = {**times("v2pro", t4=0.02, t6=0.01), **times("v2proplus", t6=0.01, t8=0.02)}
    r = run(bench, engines=["v2pro", "v2proplus"], quick=True)
    assert [(t["device"], t["threads"]) for t in r["engines"]["v2pro"]["tried"]] == [("cpu", 4), ("cpu", 6)]
    assert [(t["device"], t["threads"]) for t in r["engines"]["v2proplus"]["tried"]] == [("cpu", 6), ("cpu", 8)]
    assert tuning.load() == {"v2pro": {"device": "cpu", "threads": 6}, "v2proplus": {"device": "cpu", "threads": 6}}


async def _cancel_when(entered: asyncio.Event, coro):
    """跑 coro，等 entered 被 set 后取消它。"""
    task = asyncio.create_task(coro)
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cancel_while_first_trial_starting(bench):
    """取消发生在第一个试验启动时（ensure 还没返回）：这个临时 worker 也要停掉，不遗留进程。"""
    async def go():
        entered = FakeWorker.HANG[("ensure", "v2pro", "mps", None)] = asyncio.Event()
        await _cancel_when(entered, bench.run_bench(REF, engines=["v2pro"]))

    asyncio.run(go())
    assert len(FakeWorker.created) == 1
    w = FakeWorker.created[0]
    assert w.stopped and not w.running


def test_cancel_during_screening(bench):
    """粗筛中途取消（第一个配置已经是 best，第二个正在计时）：两个临时 worker 都停掉。"""
    FakeWorker.TIMES = times("v2pro", mps=0.01, t6=0.01)

    async def go():
        entered = FakeWorker.HANG[("call", "v2pro", "cpu", "6")] = asyncio.Event()
        await _cancel_when(entered, bench.run_bench(REF, engines=["v2pro"]))

    asyncio.run(go())
    assert [w.key[1:] for w in FakeWorker.created] == [("mps", None), ("cpu", "6")]
    assert all(w.stopped and not w.running for w in FakeWorker.created)


def test_benches_run_one_at_a_time(bench):
    """同一进程里两个测速并发：第二个等第一个做完才开始。"""
    FakeWorker.TIMES = {**times("v2pro", mps=0.01, t6=0.01, t8=0.01, t4=0.01),
                        **times("v2proplus", mps=0.01, t6=0.01, t8=0.01, t4=0.01)}
    events = []

    async def go():
        return await asyncio.gather(
            bench.run_bench(REF, engines=["v2pro"], progress=lambda s, f: events.append(("a", s))),
            bench.run_bench(REF, engines=["v2proplus"], progress=lambda s, f: events.append(("b", s))))

    a, b = asyncio.run(go())
    assert list(a["engines"]) == ["v2pro"] and list(b["engines"]) == ["v2proplus"]
    runs = [r for r, _ in events]
    assert runs == sorted(runs) and events.index(("a", "done")) < runs.index("b")


def test_refuses_while_training(bench, monkeypatch):
    from ttsapi import workers
    monkeypatch.setattr(workers, "training_job", "job-1")
    with pytest.raises(RuntimeError, match="训练"):
        run(bench, engines=["v2pro"])
    assert FakeWorker.created == []


def test_cli_writes_into_library(lib, monkeypatch):
    from conftest import write_wav
    from ttsapi import library, settings
    from ttsapi.bench import __main__ as cli
    from ttsapi.catalog import Catalog
    write_wav(lib.path("refs/carol/carol-1.wav"), 6.0)
    lib.put("refs/carol/carol-1.json", dict(id="carol-1", character="carol", text="こんにちは", lang="ja", duration=6.0, tags=[],
                                        audio="carol-1.wav", source=None))
    Catalog.add_character("carol", "builtin", "SAMPLE_DS.sp0001", default_ref="carol-1")
    monkeypatch.setattr(library, "open_configured", lambda: lib)
    seen = {}

    async def fake_run(ref, engines, quick, progress):
        seen.update(ref=ref, engines=engines, quick=quick)
        return dict(machine=settings.MACHINE_NAME, engines={"v4": dict(device="mps", sec_per_item=30.0)})

    monkeypatch.setattr(cli, "run_bench", fake_run)
    rel = asyncio.run(cli.main(["--engine", "v4"]))
    assert seen == dict(ref=dict(id="carol-1", path=str(lib.path("refs/carol/carol-1.wav")), text="こんにちは", lang="ja"),
                        engines=["v4"], quick=False)
    assert rel.startswith(f"compute/bench/{settings.MACHINE_NAME}/") and rel.endswith(".json")
    assert datetime.strptime(rel.rsplit("/", 1)[1], "%Y-%m-%d-%H%M%S.json")
    assert lib.get(rel)["engines"]["v4"]["sec_per_item"] == 30.0
    assert rel in lib.items("bench")


def test_cli_without_library(monkeypatch, clean_data):
    from ttsapi import library
    from ttsapi.bench import __main__ as cli
    monkeypatch.setattr(library, "open_configured", lambda: None)
    with pytest.raises(SystemExit, match="资料库"):
        asyncio.run(cli.main([]))


def test_bench_env_overrides_tuning(monkeypatch, clean_data):
    """测速的临时 worker：TTSAPI_BENCH_DEVICE / THREADS 优先于 tuning.json；没给线程时用 TTSAPI_TORCH_THREADS。"""
    from ttsapi import settings
    from ttsapi.compute import tuning
    tuning.save("v4", "cpu", 8)
    try:
        assert (settings.device_setting("v4"), settings.torch_threads("v4")) == ("cpu", 8)
        monkeypatch.setenv("TTSAPI_BENCH_DEVICE", "mps")
        monkeypatch.setattr(settings, "TORCH_THREADS", 6)
        assert (settings.device_setting("v4"), settings.torch_threads("v4")) == ("mps", 6)
        monkeypatch.setenv("TTSAPI_BENCH_DEVICE", "cpu")
        monkeypatch.setenv("TTSAPI_BENCH_THREADS", "4")
        assert (settings.device_setting("v4"), settings.torch_threads("v4")) == ("cpu", 4)
    finally:
        settings.TUNING_FILE.unlink()
