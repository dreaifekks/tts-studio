"""Unit tests for cloud GPU active status/scheduling and storage/disk usage APIs."""
import asyncio
import errno
import os
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ttsapi import gateway, jobs, library, settings, storage
from ttsapi.compute import colab_jobs, colab_sweep, dispatch, registry, scheduler, worker_api
from ttsapi.models import ComputeChoice, Job, SweepGroup, SweepResult, TrainRequest


class _FakeAccount:
    def __init__(self, gpus, balance):
        self.id = "acc-1"
        self.label = "acc@example.com"
        self.email = "acc@example.com"
        self.gpus = list(gpus)
        self.balance = balance
        self.recorded_month_used = 0.0
        self.disabled = False
        self.error = None


class _FakeGpuType:
    def __init__(self, gpu, account_count=1, hours=10.0, reason=None):
        self.gpu = gpu
        self.account_count = account_count
        self.hours = hours
        self.reason = reason


class _FakePool:
    def __init__(self, idle=None, accounts=None, gpu_types=None):
        self._idle = dict(idle or {})
        self._accounts = accounts if accounts is not None else [_FakeAccount(["L4", "T4"], 100.0)]
        self._gpu_types = gpu_types if gpu_types is not None else [
            _FakeGpuType("T4", 1, 80.0, None),
            _FakeGpuType("L4", 1, 18.0, None),
        ]

    def idle_vms(self):
        return dict(self._idle)

    def accounts(self, refresh=False, include_disabled=False):
        return list(self._accounts)

    def gpu_types(self):
        return list(self._gpu_types)

    def rates(self):
        return {"T4": 1.17, "L4": 5.4}

    def month_used(self):
        return 0.0

    def usage_monthly(self):
        return []

    def labels(self):
        return {"acc-1": "acc@example.com"}


def _client(app=gateway.app) -> TestClient:
    async def local(scope, receive, send):
        await app(dict(scope, client=("127.0.0.1", 50000)) if scope["type"] == "http" else scope, receive, send)
    return TestClient(local)


def _fake_run(job_id: str, kind: str) -> jobs.JobRun:
    settings.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job = Job(id=job_id, kind=kind, target="carol", status="running", progress=0.2, stage="运行中",
              created="2026-11-22T12:00:00+09:00")
    return jobs.JobRun(job, 0.0)


@pytest.fixture
def clean_cloud_state(monkeypatch):
    orig_pool = registry.pool
    orig_assigned = dict(registry.assigned)
    orig_runs = dict(jobs.runs)
    orig_health = dict(registry._health)
    registry.assigned.clear()
    jobs.runs.clear()
    registry._health.clear()
    yield
    registry.pool = orig_pool
    registry.assigned.clear()
    registry.assigned.update(orig_assigned)
    jobs.runs.clear()
    jobs.runs.update(orig_runs)
    registry._health.clear()
    registry._health.update(orig_health)


# ---------------------------------------------------------------- Task 1: Cloud GPU status & scheduling

@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_activity"),
    [
        ("train", "training", "正在训练"),
        ("sweep", "training", "正在训练"),
        ("eval", "busy", "正在评测"),
        ("candidates", "busy", "正在生成"),
        ("compare", "busy", "正在生成"),
    ],
)
def test_cloud_status_active_jobs_and_plan_allowed(clean_cloud_state, kind, expected_status, expected_activity):
    """Cloud GPU reflects running job status/activity while still accepting new plans; reverts to idle after exit."""
    registry.pool = _FakePool(idle={"L4": 2})
    run = _fake_run(f"{kind}-test-1", kind)
    jobs.runs[run.job.id] = run
    dispatch.assign(run.job, dict(
        device="colab-L4", name="云端 L4", type="cloud", seconds=300, units=0.45,
        plan_reason_code="train_needs_gpu", plan_reason="训练要 GPU",
    ))

    devs = {d["id"]: d for d in registry.cloud_states([])}
    l4 = devs["colab-L4"]
    assert l4["status"] == expected_status
    assert l4["activity"] == expected_activity
    assert l4["warm"] == 2
    assert devs["colab-T4"]["status"] == "idle"
    assert devs["colab-T4"]["activity"] is None
    assert devs["colab-T4"]["warm"] == 0

    # Cloud card with status="training"/"busy" must still accept new plans
    plan = scheduler.plan(
        list(devs.values()),
        scheduler.Task("train", [("v2pro", 12)], lines=100, epochs=1.0),
        dict(registry.DEFAULT_AUTO, gpu_pref="auto"),
    )
    cand_l4 = next(c for c in plan["candidates"] if c["device"] == "colab-L4")
    assert cand_l4["available"] is True
    assert cand_l4["reason_code"] is None
    assert plan["recommended"] == "colab-L4"

    # Also allowed for cloud candidates/eval
    gen_plan = scheduler.plan(
        list(devs.values()),
        scheduler.Task("gen", [("v2pro", 4)], cloud_capable=True),
        dict(registry.DEFAULT_AUTO, gpu_pref="auto"),
        pick="colab-L4",
    )
    gen_l4 = next(c for c in gen_plan["candidates"] if c["device"] == "colab-L4")
    assert gen_l4["available"] is True

    # Immediately recovers to idle when job exits jobs.runs
    jobs.runs.pop(run.job.id)
    assert registry.busy_kind("colab-L4") is None
    after = {d["id"]: d for d in registry.cloud_states([])}
    assert after["colab-L4"]["status"] == "idle"
    assert after["colab-L4"]["activity"] is None


def test_cloud_paused_while_running_and_compute_warm_exposed(clean_cloud_state, monkeypatch):
    """Paused cloud GPU still shows active status while a job runs, blocks new plans, and exposes warm via /compute."""
    registry.pool = _FakePool(idle={"L4": 3})
    run = _fake_run("train-paused-1", "train")
    jobs.runs[run.job.id] = run
    registry.assigned[run.job.id] = ("colab-L4", 9999999999.0)

    # Paused while running: status reflects active training, but scheduler blocks new tasks
    devs = {d["id"]: d for d in registry.cloud_states(["colab-L4"])}
    assert devs["colab-L4"]["status"] == "training"
    assert devs["colab-L4"]["activity"] == "正在训练"
    assert devs["colab-L4"]["warm"] == 3

    plan = scheduler.plan(
        list(devs.values()),
        scheduler.Task("train", [("v2pro", 12)], lines=100, epochs=1.0),
        dict(registry.DEFAULT_AUTO, gpu_pref="auto"),
    )
    cand_l4 = next(c for c in plan["candidates"] if c["device"] == "colab-L4")
    assert cand_l4["available"] is False
    assert cand_l4["reason_code"] == "paused"

    # GET /compute exposes warm and active cloud status; GET /cloud keeps available=True when not paused
    client = _client()
    comp = client.get("/compute").json()
    l4_dev = next(d for d in comp["devices"] if d["id"] == "colab-L4")
    assert l4_dev["status"] == "training"
    assert l4_dev["activity"] == "正在训练"
    assert l4_dev["warm"] == 3

    cloud_resp = client.get("/cloud").json()
    l4_gpu = next(g for g in cloud_resp["gpus"] if g["gpu"] == "L4")
    assert l4_gpu["available"] is True
    assert l4_gpu["reason"] is None

    # Local device in training status must still be blocked by scheduler
    local_dev = registry.local_state([])
    local_dev["status"] = "training"
    local_plan = scheduler.plan(
        [local_dev],
        scheduler.Task("gen", [("v2pro", 1)], cloud_capable=False),
        registry.DEFAULT_AUTO,
    )
    assert local_plan["candidates"][0]["available"] is False
    assert local_plan["candidates"][0]["reason_code"] == "training"


def test_colab_run_and_sweep_assigned_lifecycle(clean_cloud_state, lib, monkeypatch):
    """run_colab and run_sweep ensure assigned has colab-<gpu> while running and clear it in finally."""
    # 1. colab_jobs.run_colab
    run = _fake_run("eval-direct-1", "eval")
    jobs.runs[run.job.id] = run
    seen_during = {}

    def fake_export(*args, **kwargs):
        seen_during["eval"] = registry.busy_kind("colab-L4")
        raise RuntimeError("stop early")

    monkeypatch.setattr(colab_jobs, "export_inputs", fake_export)
    req = types.SimpleNamespace(engine="v2pro")
    with pytest.raises(RuntimeError, match="stop early"):
        asyncio.run(colab_jobs.run_colab(
            run, "eval", req, gpu="L4", estimated_minutes=5.0, approved=True,
            pool=_FakePool(), catalog=None, refs=None,
        ))
    assert seen_during["eval"] == "正在评测"
    assert run.job.id not in registry.assigned
    assert registry.busy_kind("colab-L4") is None
    jobs.runs.pop(run.job.id, None)

    # 2. colab_sweep.run_sweep
    sweep_run = _fake_run("sweep-direct-1", "sweep")
    g = SweepGroup(slug="carol-v2pro-s8g15-000000", character="carol", engine="v2pro",
                   sovits_epochs=8, sovits_save_every=4, gpt_epochs=15, gpt_save_every=5,
                   sovits_batch=8, gpt_batch=8, status="pending")
    tr = TrainRequest(slug=g.slug, dataset_id="SAMPLE_DS.sp0001", engine="v2pro")
    sweep_run.job.sweep_result = SweepResult(groups=[g])
    jobs.runs[sweep_run.job.id] = sweep_run

    def fake_segments(*args, **kwargs):
        seen_during["sweep"] = registry.busy_kind("colab-L4")
        return None

    monkeypatch.setattr(colab_sweep, "_segments", fake_segments)
    chosen = dict(gpu="L4", device="colab-L4", group_sec=[600.0], cold_prep_sec=210.0, budget=5.0, seconds=810)
    with pytest.raises(RuntimeError):
        asyncio.run(colab_sweep.run_sweep(
            sweep_run, [(g, tr)], chosen, approved=True, pool=_FakePool(),
            catalog=None, refs=None, keep=3, samples=0, into_builtin=True,
        ))
    assert seen_during["sweep"] == "正在训练"
    assert sweep_run.job.id not in registry.assigned
    assert registry.busy_kind("colab-L4") is None
    jobs.runs.pop(sweep_run.job.id, None)


# ---------------------------------------------------------------- Task 2: Storage & device disk stats

def _write_bytes(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"a" * size)
    return path


def test_storage_unselected_library(clean_data):
    """GET /system/storage works when no library is selected and returns disk + local overview."""
    library.use(None)
    storage.invalidate_cache()
    client = _client()
    r = client.get("/system/storage")
    assert r.status_code == 200
    data = r.json()
    assert data["measured_at"]
    assert data["disk"]["total_bytes"] > 0
    assert data["disk"]["free_bytes"] >= 0
    assert data["disk"]["used_bytes"] >= 0
    assert data["library"] == {
        "selected": False,
        "kind": None,
        "path": None,
        "total_bytes": 0,
        "local_bytes": 0,
        "categories": [],
    }
    assert data["local"]["machine"] == settings.MACHINE_NAME
    assert data["local"]["total_bytes"] == sum(c["bytes"] for c in data["local"]["categories"])


def test_storage_library_categories_and_sparse_drive_placeholder(tmp_path, clean_data):
    """Library storage groups top-level dirs, sorts desc by bytes, skips broken symlinks, and separates sparse local_bytes."""
    drive_root = tmp_path / "GoogleDrive" / "MyDrive" / "tts-lib"
    lb = library.open_or_create(drive_root)
    library.use(lb)
    try:
        _write_bytes(drive_root / "weights" / "carol" / "v2pro" / "w1" / "gpt.ckpt", 5000)
        _write_bytes(drive_root / "runs" / "r1" / "sample.wav", 3000)
        _write_bytes(drive_root / "works" / "w1" / "out.wav", 2000)
        _write_bytes(drive_root / "refs" / "carol" / "r1.wav", 1000)
        _write_bytes(drive_root / "evals" / "2026-11" / "e1.json", 400)
        _write_bytes(drive_root / "characters" / "carol.json", 250)

        # Create a sparse file mimicking a Google Drive FileProvider cloud-only placeholder (logical 10 MiB, 0 blocks)
        placeholder = drive_root / "datasets" / "cloud_only.tar"
        placeholder.parent.mkdir(parents=True, exist_ok=True)
        sparse_size = 10 * 1024 * 1024
        with open(placeholder, "wb") as f:
            f.truncate(sparse_size)

        # Broken symlink inside library must be skipped without error
        os.symlink(tmp_path / "does-not-exist", drive_root / "weights" / "broken-link")

        storage.invalidate_cache()
        client = _client()
        data = client.get("/system/storage").json()
        lib_data = data["library"]
        assert lib_data["selected"] is True
        assert lib_data["kind"] == "drive"
        assert lib_data["path"] == str(drive_root)

        cats = {c["key"]: c for c in lib_data["categories"]}
        assert list(cats.keys())[0] == "datasets"
        assert [c["bytes"] for c in lib_data["categories"]] == sorted(
            (c["bytes"] for c in lib_data["categories"]), reverse=True
        )
        assert cats["weights"]["label"] == "模型权重"
        assert cats["weights"]["bytes"] == 5000
        assert cats["weights"]["files"] == 1
        assert cats["datasets"]["label"] == "训练数据集"
        assert cats["datasets"]["bytes"] == sparse_size
        assert cats["datasets"]["files"] == 1
        assert cats["runs"]["label"] == "训练产出与样本"
        assert cats["runs"]["bytes"] == 3000
        assert cats["works"]["label"] == "作品音频"
        assert cats["works"]["bytes"] == 2000
        assert cats["refs"]["label"] == "参考音频"
        assert cats["refs"]["bytes"] == 1000
        assert cats["other"]["label"] == "评测、盲听与配置"
        lib_json_size = (drive_root / "library.json").stat().st_size
        assert cats["other"]["bytes"] == 400 + 250 + lib_json_size
        assert cats["other"]["files"] == 3

        assert lib_data["total_bytes"] == sum(c["bytes"] for c in lib_data["categories"])
        assert lib_data["local_bytes"] == sum(c["local_bytes"] for c in lib_data["categories"])

        if os.name != "nt" and placeholder.stat().st_blocks == 0:
            assert cats["datasets"]["local_bytes"] == 0
            assert lib_data["local_bytes"] < lib_data["total_bytes"]
    finally:
        library.use(None)


def test_storage_local_dedup_and_refresh_cache(tmp_path, monkeypatch, clean_data):
    """Local categories deduplicate nested/symlinked directories via realpath and respect 60s cache + refresh=true."""
    library.use(None)
    gsv = tmp_path / "gsv"
    ds_root = gsv / "datasets"  # Nested inside OLD_REPO (default layout)
    v5_link = tmp_path / "gsv-v5-link"  # Symlink pointing to OLD_REPO -> must not double-count
    data_dir = tmp_path / "data"
    blob_dir = data_dir / "blobs"  # Nested inside DATA (default layout)
    train_dir = tmp_path / "training"
    weights_dir = tmp_path / "weights"

    _write_bytes(gsv / "pretrained.ckpt", 4000)
    _write_bytes(ds_root / ".unpacked" / "ds1" / "clip.wav", 3000)
    _write_bytes(data_dir / "audio" / "a1.wav", 2000)
    _write_bytes(blob_dir / "deadbeef", 1500)
    _write_bytes(train_dir / "run1" / "ckpt.pth", 800)
    _write_bytes(weights_dir / "run1" / "out.pth", 700)
    os.symlink(gsv, v5_link)
    os.symlink(tmp_path / "missing-target", data_dir / "broken")

    monkeypatch.setattr(settings, "OLD_REPO", gsv)
    monkeypatch.setattr(settings, "V5_REPO", v5_link)
    monkeypatch.setattr(settings, "DATASET_ROOT", ds_root)
    monkeypatch.setattr(settings, "DATA", data_dir)
    monkeypatch.setattr(settings, "BLOB_DIR", blob_dir)
    monkeypatch.setattr(settings, "TRAIN_DIR", train_dir)
    monkeypatch.setattr(settings, "WEIGHTS_DIR", weights_dir)
    storage.invalidate_cache()

    client = _client()
    first = client.get("/system/storage").json()
    cats = {c["key"]: c for c in first["local"]["categories"]}
    assert cats["engine"]["label"] == "语音引擎与基础模型"
    assert cats["engine"]["bytes"] == 4000
    assert cats["engine"]["files"] == 1
    assert cats["dataset_cache"]["label"] == "数据集解包缓存"
    assert cats["dataset_cache"]["bytes"] == 3000
    assert cats["dataset_cache"]["files"] == 1
    assert cats["audio_history"]["label"] == "历史音频与运行数据"
    assert cats["audio_history"]["bytes"] == 2000
    assert cats["audio_history"]["files"] == 1
    assert cats["training_tmp"]["label"] == "训练暂存"
    assert cats["training_tmp"]["bytes"] == 1500
    assert cats["training_tmp"]["files"] == 2
    assert cats["blobs"]["label"] == "缓存模型"
    assert cats["blobs"]["bytes"] == 1500
    assert cats["blobs"]["files"] == 1
    assert first["local"]["total_bytes"] == 4000 + 3000 + 2000 + 1500 + 1500

    # Add a new blob; cached call returns old total, refresh=true rescans
    _write_bytes(blob_dir / "cafebabe", 2500)
    cached = client.get("/system/storage").json()
    assert cached["measured_at"] == first["measured_at"]
    assert {c["key"]: c["bytes"] for c in cached["local"]["categories"]}["blobs"] == 1500

    refreshed = client.get("/system/storage", params={"refresh": "true"}).json()
    assert {c["key"]: c["bytes"] for c in refreshed["local"]["categories"]}["blobs"] == 4000
    assert refreshed["local"]["total_bytes"] == first["local"]["total_bytes"] + 2500


def test_compute_and_worker_health_disk_fields(tmp_path, monkeypatch, clean_cloud_state):
    """GET /compute, /worker/health, and registry.lan_state report/pass through disk & cache bytes with backward compat."""
    blob_dir = tmp_path / "blobs"
    _write_bytes(blob_dir / "blob1", 4096)
    monkeypatch.setattr(settings, "BLOB_DIR", blob_dir)

    # Local device via GET /compute
    client = _client()
    comp = client.get("/compute").json()
    local_dev = next(d for d in comp["devices"] if d["id"] == "local")
    assert isinstance(local_dev["disk_free_bytes"], int) and local_dev["disk_free_bytes"] > 0
    assert isinstance(local_dev["disk_total_bytes"], int) and local_dev["disk_total_bytes"] >= local_dev["disk_free_bytes"]
    assert local_dev["cache_bytes"] == 4096

    # Worker /worker/health includes disk & cache fields
    orig_tok = getattr(worker_api.app.state, "token", None)
    worker_api.app.state.token = "tok-disk"
    try:
        wclient = _client(worker_api.app)
        h = wclient.get("/worker/health", headers={"Authorization": "Bearer tok-disk"}).json()
        assert isinstance(h["disk_free_bytes"], int) and h["disk_free_bytes"] > 0
        assert isinstance(h["disk_total_bytes"], int) and h["disk_total_bytes"] >= h["disk_free_bytes"]
        assert h["cache_bytes"] == 4096
    finally:
        worker_api.app.state.token = orig_tok

    # registry.lan_state passes through disk fields when present and keeps None for legacy workers
    d_new = dict(id="lan-new001", name="new-worker", address="http://127.0.0.1:9991", token="t1", engines=["v2pro"])
    d_old = dict(id="lan-old001", name="old-worker", address="http://127.0.0.1:9992", token="t2", engines=["v2pro"])
    base_health = dict(
        hw=dict(cpu="x", cores=8, ram_gb=32.0, accel="cuda", gpu="RTX", os="Linux", torch="2.0"),
        machine="worker-box", engines=["v2pro"], loaded=[], busy=False, training=None, caps=[],
    )
    registry._health[d_new["id"]] = (
        9999999999.0,
        dict(base_health, disk_free_bytes=123456, disk_total_bytes=999999, cache_bytes=777),
    )
    registry._health[d_old["id"]] = (9999999999.0, dict(base_health))

    st_new = asyncio.run(registry.lan_state(d_new, []))
    assert st_new["disk_free_bytes"] == 123456
    assert st_new["disk_total_bytes"] == 999999
    assert st_new["cache_bytes"] == 777

    st_old = asyncio.run(registry.lan_state(d_old, []))
    assert st_old["disk_free_bytes"] is None
    assert st_old["disk_total_bytes"] is None
    assert st_old["cache_bytes"] is None


# ---------------------------------------------------------------- Review fixes: concurrency, queued, pause, inode, EIO

def test_cloud_queued_waiting_for_gpu_not_reported_active(clean_cloud_state):
    """Queued cloud jobs (e.g. WaitingForGPU) must not report training/busy until status becomes running."""
    registry.pool = _FakePool()
    run = _fake_run("train-queued-1", "train")
    run.job.status = "queued"
    jobs.runs[run.job.id] = run
    registry.assigned[run.job.id] = ("colab-L4", 9999999999.0)

    assert registry.busy_kind("colab-L4") is None
    assert registry.cloud_activities() == {}
    devs = {d["id"]: d for d in registry.cloud_states([])}
    assert devs["colab-L4"]["status"] == "idle"
    assert devs["colab-L4"]["activity"] is None

    # Once VM starts and status flips to running, activity and status become active
    run.job.status = "running"
    assert registry.busy_kind("colab-L4") == "正在训练"
    assert registry.cloud_activities() == {"colab-L4": "正在训练"}
    devs_running = {d["id"]: d for d in registry.cloud_states([])}
    assert devs_running["colab-L4"]["status"] == "training"
    assert devs_running["colab-L4"]["activity"] == "正在训练"


def test_snapshot_passes_event_loop_activities_to_cloud_states(clean_cloud_state, monkeypatch):
    """snapshot() captures activities on the event loop so cloud_states() never reads assigned/jobs.runs in a worker thread."""
    registry.pool = _FakePool()
    run = _fake_run("eval-snap-1", "eval")
    jobs.runs[run.job.id] = run
    registry.assigned[run.job.id] = ("colab-L4", 9999999999.0)

    seen_args = []
    real_cloud_states = registry.cloud_states

    def spy_cloud_states(paused, cloud=None, activities=None):
        seen_args.append(activities)
        # Mutate assigned/jobs.runs before calling real_cloud_states: must still reflect the passed snapshot
        jobs.runs.clear()
        registry.assigned.clear()
        return real_cloud_states(paused, cloud, activities)

    monkeypatch.setattr(registry, "cloud_states", spy_cloud_states)
    devices, _ = asyncio.run(registry.snapshot())
    assert seen_args == [{"colab-L4": "正在评测"}]
    l4 = next(d for d in devices if d["id"] == "colab-L4")
    assert l4["status"] == "busy"
    assert l4["activity"] == "正在评测"


def test_dispatch_assign_and_choose_recheck_paused(clean_cloud_state, monkeypatch):
    """dispatch.assign() and dispatch.choose() synchronously reject devices paused in registry.load()."""
    registry.pool = _FakePool()
    chosen = dict(
        device="colab-L4", name="云端 L4", type="cloud", gpu="L4", seconds=300, units=0.45,
        plan_reason_code="train_needs_gpu", plan_reason="训练要 GPU",
    )
    registry.patch_device("colab-L4", paused=True)
    try:
        run = _fake_run("train-pause-race", "train")
        jobs.runs[run.job.id] = run
        with pytest.raises(dispatch.PlanError) as exc:
            dispatch.assign(run.job, chosen)
        assert exc.value.code == "compute_unavailable"
        assert run.job.id not in registry.assigned

        # Interleaved pause during await dispatch.plan(...) inside dispatch.choose()
        registry.patch_device("colab-L4", paused=False)
        real_plan = dispatch.plan

        async def plan_then_pause(task, pick=None):
            p = await real_plan(task, pick)
            registry.patch_device("colab-L4", paused=True)
            return p

        monkeypatch.setattr(dispatch, "plan", plan_then_pause)
        with pytest.raises(dispatch.PlanError) as exc2:
            asyncio.run(dispatch.choose(
                scheduler.Task("train", [("v2pro", 12)], lines=100, epochs=1.0),
                ComputeChoice(compute="colab-L4", approved=True, approved_device="colab-L4", approved_units=10.0),
            ))
        assert exc2.value.code == "compute_unavailable"
    finally:
        registry.patch_device("colab-L4", paused=False)


def test_storage_invalidate_cache_and_concurrent_switch_no_stale_write(tmp_path, monkeypatch, clean_data):
    """Library switch via POST /library and /library/claim bumps _cache_gen and drops stale in-flight cache writes."""
    monkeypatch.setattr(settings, "CORE_CONFIG", tmp_path / "core.json")
    lib_a_dir = tmp_path / "lib-a"
    lib_b_dir = tmp_path / "lib-b"
    lib_a = library.open_or_create(lib_a_dir)
    _write_bytes(lib_a_dir / "weights" / "a.ckpt", 1111)
    lib_b = library.open_or_create(lib_b_dir)
    _write_bytes(lib_b_dir / "weights" / "b.ckpt", 2222)
    del lib_b

    library.use(lib_a)
    storage.invalidate_cache()
    client = _client()

    # Simulate POST /library switching to lib_b mid-scan during storage.overview()
    real_scan_library = storage._scan_library
    switched = []

    def scan_with_mid_switch(lib):
        res = real_scan_library(lib)
        if not switched:
            switched.append(True)
            r = client.post("/library", json={"path": str(lib_b_dir)})
            assert r.status_code == 200
        return res

    monkeypatch.setattr(storage, "_scan_library", scan_with_mid_switch)
    try:
        stale_res = storage.overview(refresh=True)
        assert stale_res.library.path == str(lib_a_dir)
        # Generation changed mid-scan: stale result must NOT have been cached
        assert storage._cache is None

        # Next request scans lib_b cleanly and caches it
        fresh = client.get("/system/storage").json()
        assert fresh["library"]["path"] == str(lib_b_dir)
        assert {c["key"]: c["bytes"] for c in fresh["library"]["categories"]}["weights"] == 2222
        assert storage._cache is not None

        # POST /library/claim invalidates cache and bumps generation
        gen_before = storage._cache_gen
        claim_resp = client.post("/library/claim")
        assert claim_resp.status_code == 200
        assert storage._cache_gen == gen_before + 1
        assert storage._cache is None
    finally:
        library.use(None)


def test_storage_excludes_library_inside_data_and_dedups_by_inode(tmp_path, monkeypatch, clean_data):
    """Active library placed inside settings.DATA is excluded from local categories; case-variant dirs dedup by inode."""
    data_dir = tmp_path / "Data"
    blob_dir = data_dir / "blobs"
    lib_dir = data_dir / "embedded-library"
    gsv = tmp_path / "Gsv"

    _write_bytes(data_dir / "audio" / "hist.wav", 1200)
    _write_bytes(blob_dir / "b1", 800)
    _write_bytes(gsv / "base.ckpt", 3000)

    lb = library.open_or_create(lib_dir)
    _write_bytes(lib_dir / "weights" / "carol" / "v2pro" / "w1" / "gpt.ckpt", 5000)
    library.use(lb)

    # Point V5_REPO to a case-variant or symlinked path resolving to the same (st_dev, st_ino) as OLD_REPO
    v5_alias = tmp_path / "gsv" if (tmp_path / "gsv").exists() else tmp_path / "gsv-alias"
    if not v5_alias.exists():
        os.symlink(gsv, v5_alias)

    monkeypatch.setattr(settings, "DATA", data_dir)
    monkeypatch.setattr(settings, "BLOB_DIR", blob_dir)
    monkeypatch.setattr(settings, "OLD_REPO", gsv)
    monkeypatch.setattr(settings, "V5_REPO", v5_alias)
    monkeypatch.setattr(settings, "DATASET_ROOT", tmp_path / "no-datasets")
    monkeypatch.setattr(settings, "TRAIN_DIR", tmp_path / "no-train")
    monkeypatch.setattr(settings, "WEIGHTS_DIR", tmp_path / "no-weights")
    storage.invalidate_cache()
    try:
        client = _client()
        data = client.get("/system/storage").json()
        lib_cats = {c["key"]: c["bytes"] for c in data["library"]["categories"]}
        local_cats = {c["key"]: c["bytes"] for c in data["local"]["categories"]}

        # Library counts the 5000-byte weight; local audio_history excludes embedded-library completely
        assert lib_cats["weights"] == 5000
        assert local_cats["audio_history"] == 1200
        assert local_cats["blobs"] == 800
        assert local_cats["engine"] == 3000
        assert data["local"]["total_bytes"] == 1200 + 800 + 3000
    finally:
        library.use(None)


def test_storage_scandir_iteration_eio_returns_200(tmp_path, monkeypatch, clean_data):
    """OSError(EIO) raised mid-iteration of os.scandir is skipped gracefully and GET /system/storage still returns 200."""
    data_dir = tmp_path / "data"
    bad_dir = data_dir / "flaky"
    good_dir = data_dir / "healthy"
    _write_bytes(bad_dir / "ok_before_eio.bin", 500)
    _write_bytes(bad_dir / "after_eio.bin", 900)
    _write_bytes(good_dir / "good.bin", 1234)

    lib_dir = tmp_path / "lib"
    lb = library.open_or_create(lib_dir)
    _write_bytes(lib_dir / "weights" / "w.ckpt", 777)
    library.use(lb)

    bad_real = os.path.realpath(bad_dir)
    lib_real = os.path.realpath(lib_dir)
    real_scandir = os.scandir

    class _FlakyScandir:
        def __init__(self, inner):
            self._inner = inner
            self._yielded = 0

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def __iter__(self):
            return self

        def __next__(self):
            if self._yielded >= 1:
                raise OSError(errno.EIO, "Input/output error")
            item = next(self._inner)
            self._yielded += 1
            return item

    def flaky_scandir(path="."):
        it = real_scandir(path)
        if os.path.realpath(path) in (bad_real, lib_real):
            return _FlakyScandir(it)
        return it

    monkeypatch.setattr(settings, "DATA", data_dir)
    monkeypatch.setattr(settings, "BLOB_DIR", data_dir / "blobs")
    monkeypatch.setattr(settings, "OLD_REPO", tmp_path / "none-old")
    monkeypatch.setattr(settings, "V5_REPO", tmp_path / "none-v5")
    monkeypatch.setattr(settings, "DATASET_ROOT", tmp_path / "none-ds")
    monkeypatch.setattr(settings, "TRAIN_DIR", tmp_path / "none-train")
    monkeypatch.setattr(settings, "WEIGHTS_DIR", tmp_path / "none-weights")
    monkeypatch.setattr(storage.os, "scandir", flaky_scandir)
    storage.invalidate_cache()
    try:
        client = _client()
        resp = client.get("/system/storage")
        assert resp.status_code == 200
        body = resp.json()
        local_cats = {c["key"]: c["bytes"] for c in body["local"]["categories"]}
        # Healthy sibling directory and the first entry yielded before EIO are counted; EIO does not crash
        assert local_cats["audio_history"] >= 1234
        assert body["library"]["selected"] is True
    finally:
        library.use(None)
