"""阶段 2 给 Studio 补的接口：GET /cloud、云端设置（开哪张卡、最长用多久、做完先别关）真的进调度和开卡、
添加账号的后台登录（假 CLI 子进程，不连 Google、不开 VM）、GET /system/versions 的缓存、局域网设备的唤醒（假的发包函数）。"""
import asyncio
import subprocess
import time

import pytest
from test_compute import AUTO, cand, l4, lan, mini

from ttsapi import gateway, settings, version
from ttsapi.compute import dispatch, registry, remote, scheduler
from ttsapi.compute.colab import ColabPool
from ttsapi.compute.colab.test_colab import LoginCLI, digest
from ttsapi.compute.scheduler import Task
from ttsapi.models import ComputeChoice


@pytest.fixture
def api(monkeypatch, tmp_path):
    """gateway（不进 lifespan），本机当成 mini，没装 colab CLI（同 test_compute 的 api）。"""
    from fastapi.testclient import TestClient
    monkeypatch.setattr(settings, "COMPUTE_FILE", tmp_path / "compute.json")
    monkeypatch.setattr(settings, "SPEEDS_FILE", tmp_path / "speeds.json")
    monkeypatch.setattr(settings, "installed_engines", lambda: ["v2pro", "v2proplus", "v4"])
    monkeypatch.setattr(registry, "_hw", dict(cpu="Apple M6", cores=12, ram_gb=32, accel="mps", gpu=None, os="darwin",
                                              torch="2.11.0"))

    async def local(scope, receive, send):
        await gateway.app(dict(scope, client=("127.0.0.1", 50000)) if scope["type"] == "http" else scope, receive, send)
    return TestClient(local)


@pytest.fixture
def cloud(api, monkeypatch, tmp_path):
    """装了云端组件：账号池用假 CLI（默认账号余额 10，新账号 new@example.invalid 余额 5）。"""
    cli = LoginCLI(tmp_path)
    cli.balances[digest(cli.email)] = 5
    pool = ColabPool(tmp_path / "pool", cli=cli, clock=time.time)
    monkeypatch.setattr(registry, "pool", pool)
    return api, pool, cli


# ---------------------------------------------------------------- GET /cloud、设置

def test_cloud_not_installed(api):
    r = api.get("/cloud").json()
    assert (r["installed"], r["accounts"], r["total_balance"], r["usage_monthly"]) == (False, [], 0, [])
    assert r["settings"] == dict(gpu_pref="auto", max_minutes=None, keep_minutes=0)
    l4_ = next(g for g in r["gpus"] if g["gpu"] == "L4")
    assert (l4_["available"], l4_["reason"], l4_["units_per_hour"]) == (False, "没有装云端 GPU 组件", 1.94)
    assert l4_["speeds"]["v4"] == dict(sec_per_item=7.5, source="reference")
    assert api.post("/cloud/accounts/x/refresh").status_code == 404
    assert api.get("/cloud/accounts/login/nope").status_code == 404


def test_cloud_accounts_gpus_and_disable(cloud):
    api, pool, cli = cloud
    r = api.get("/cloud", params=dict(refresh=True)).json()
    assert r["installed"] and [a["id"] for a in r["accounts"]] == ["default"]
    a = r["accounts"][0]
    assert (a["balance"], a["disabled"], a["email"], a["month_used_estimate"]) == (10, False, None, 0)
    gpus = {g["gpu"]: g for g in r["gpus"]}
    assert gpus["L4"]["available"] and gpus["L4"]["account_count"] == 1
    assert gpus["L4"]["hours_left"] == round(10 / 1.94, 2) and gpus["G4"]["label"] == "RTX PRO 6000"
    assert gpus["G4"]["premium"] and not gpus["H100"]["available"] and gpus["H100"]["reason"] == "费率未测"
    assert r["total_balance"] == 10
    cli.balances["default"] = 7
    assert api.post("/cloud/accounts/default/refresh").json()["balance"] == 7
    assert api.patch("/compute/devices/colab-T4", json=dict(paused=True)).status_code == 200
    assert next(g for g in api.get("/cloud").json()["gpus"] if g["gpu"] == "T4")["reason"] == "已暂停"
    assert api.post("/cloud/accounts/default/disable").json()["disabled"]
    r = api.get("/cloud").json()
    assert r["accounts"][0]["disabled"] and r["total_balance"] == 0
    assert not next(g for g in r["gpus"] if g["gpu"] == "L4")["available"]
    assert not api.post("/cloud/accounts/default/enable").json()["disabled"]
    assert api.get("/cloud").json()["total_balance"] == 7
    assert api.delete("/cloud/accounts/default").status_code == 204          # default 只停用
    assert api.get("/cloud").json()["accounts"][0]["disabled"]
    for path in ("/cloud/accounts/nope/disable", "/cloud/accounts/nope/enable"):
        assert api.post(path).status_code == 404
    assert api.delete("/cloud/accounts/nope").status_code == 404


def test_cloud_remove_account(cloud):
    api, pool, cli = cloud
    other = pool.register(cli.email, "备用号")
    cli.balances[other] = 50
    with pool.start("T4", task_id="busy", estimated_minutes=10) as run:
        assert run.account_id == other
        assert api.delete(f"/cloud/accounts/{other}").status_code == 409     # VM 还在跑任务
    with pool.db() as db:
        db.execute("UPDATE usage SET actual_units=0.4")
    cli.forget_fails = True                                                    # 删授权失败：502，账号停用、没移除
    r = api.delete(f"/cloud/accounts/{other}")
    assert r.status_code == 502 and "再试" in r.json()["detail"]
    assert next(a for a in api.get("/cloud").json()["accounts"] if a["id"] == other)["disabled"]
    cli.forget_fails = False
    assert api.delete(f"/cloud/accounts/{other}").status_code == 204
    assert [c[0] for c in cli.calls if c[1] == "forget"] == [other, other]
    r = api.get("/cloud").json()
    assert [a["id"] for a in r["accounts"]] == ["default"]
    assert r["usage_monthly"][0]["by_account"] == {other: 0.4} and r["account_labels"][other] == "备用号"
    assert r["month_used_estimate"] == 0.4
    assert api.delete(f"/cloud/accounts/{other}").status_code == 404


def test_cloud_settings_partial_and_validation(api):
    assert api.put("/cloud/settings", json=dict(gpu_pref="T4")).json() == \
        dict(gpu_pref="T4", max_minutes=None, keep_minutes=0)
    assert api.put("/cloud/settings", json=dict(max_minutes=90, keep_minutes=10)).json() == \
        dict(gpu_pref="T4", max_minutes=90, keep_minutes=10)
    assert api.put("/cloud/settings", json=dict(max_minutes=None)).json()["max_minutes"] is None
    for bad in (dict(gpu_pref="H100"), dict(max_minutes=10), dict(keep_minutes=-1)):
        assert api.put("/cloud/settings", json=bad).status_code == 422
    assert api.get("/cloud").json()["settings"] == dict(gpu_pref="T4", max_minutes=None, keep_minutes=10)


def test_usage_monthly_and_month_total_include_disabled(cloud):
    api, pool, cli = cloud
    with pool.start("T4", task_id="t", estimated_minutes=10):
        pass
    with pool.db() as db:
        db.execute("UPDATE usage SET actual_units=0.4")
    pool.disable("default")
    r = api.get("/cloud").json()
    assert r["month_used_estimate"] == 0.4 and r["usage_monthly"][0]["by_account"] == {"default": 0.4}
    assert registry.month_used() == pytest.approx(0.4)                # 提醒线也算停用账号这个月花的


# ---------------------------------------------------------------- 添加账号（后台登录）

def wait_state(api, login_id):
    for _ in range(200):
        st = api.get(f"/cloud/accounts/login/{login_id}").json()
        if st["state"] != "waiting":
            return st
        time.sleep(0.05)
    raise AssertionError("login still waiting")


def test_cloud_login_flow(cloud):
    api, pool, cli = cloud
    start = api.post("/cloud/accounts/login", json=dict(label="副号")).json()
    assert start["needs_code"] is True and start["auth_url"].startswith("https://accounts.google.com/")
    assert api.get(f"/cloud/accounts/login/{start['login_id']}").json() == dict(state="waiting", account=None,
                                                                              added=False, error=None)
    r = api.post(f"/cloud/accounts/login/{start['login_id']}", json=dict(code="good"))
    assert r.status_code == 200
    assert api.post(f"/cloud/accounts/login/{start['login_id']}", json=dict(code="good")).status_code == 409
    st = wait_state(api, start["login_id"])
    assert st["state"] == "done" and st["added"] is True and st["error"] is None
    assert (st["account"]["id"], st["account"]["label"], st["account"]["email"]) == \
        (digest(cli.email), "副号", cli.email)
    pool.login_status(start["login_id"]).thread.join(10)           # 等试卡做完
    ids = [a["id"] for a in api.get("/cloud").json()["accounts"]]
    assert sorted(ids) == sorted(["default", digest(cli.email)])
    # Re-logging into an existing account returns added=False.
    relogin = api.post("/cloud/accounts/login").json()
    api.post(f"/cloud/accounts/login/{relogin['login_id']}", json=dict(code="good"))
    st = wait_state(api, relogin["login_id"])
    assert st["state"] == "done" and st["added"] is False
    # 授权码不对：failed，账号不加
    start = api.post("/cloud/accounts/login").json()
    api.post(f"/cloud/accounts/login/{start['login_id']}", json=dict(code="nope"))
    st = wait_state(api, start["login_id"])
    assert st["state"] == "failed" and st["added"] is False and "invalid_grant" in st["error"]
    assert api.post("/cloud/accounts/login/unknown", json=dict(code="x")).status_code == 404


def test_cloud_login_cancel_endpoint(cloud, monkeypatch):
    from ttsapi.compute.colab import pool as pool_module
    from ttsapi.compute.colab.cli import CliError
    api, pool, cli = cloud
    account = digest(cli.email)
    assert api.delete("/cloud/accounts/login/unknown").status_code == 204
    # Cancel before commit: terminates process and never adds the account.
    pending = api.post("/cloud/accounts/login").json()
    assert api.delete(f"/cloud/accounts/login/{pending['login_id']}").status_code == 204
    assert api.get(f"/cloud/accounts/login/{pending['login_id']}").json()["state"] == "failed"
    assert account not in [a["id"] for a in api.get("/cloud").json()["accounts"]]
    # Cancel after done for a newly added account: removes the account; 502 is retryable.
    fresh = api.post("/cloud/accounts/login").json()
    api.post(f"/cloud/accounts/login/{fresh['login_id']}", json=dict(code="good"))
    st = wait_state(api, fresh["login_id"])
    assert st["state"] == "done" and st["added"] is True
    pool.login_status(fresh["login_id"]).thread.join(10)
    cli.forget_fails = True
    assert api.delete(f"/cloud/accounts/login/{fresh['login_id']}").status_code == 502
    assert next(a for a in api.get("/cloud").json()["accounts"] if a["id"] == account)["disabled"]
    cli.forget_fails = False
    assert api.delete(f"/cloud/accounts/login/{fresh['login_id']}").status_code == 204
    assert account not in [a["id"] for a in api.get("/cloud").json()["accounts"]]
    assert api.delete(f"/cloud/accounts/login/{fresh['login_id']}").status_code == 204
    # Cancel after done for an existing account (added=False): keeps the account.
    pool.register(cli.email, "已有号")
    existing = api.post("/cloud/accounts/login").json()
    api.post(f"/cloud/accounts/login/{existing['login_id']}", json=dict(code="good"))
    st = wait_state(api, existing["login_id"])
    assert st["state"] == "done" and st["added"] is False
    assert api.delete(f"/cloud/accounts/login/{existing['login_id']}").status_code == 204
    assert account in [a["id"] for a in api.get("/cloud").json()["accounts"]]
    # Timeout while waiting for probe release returns 409.
    pool.remove(account)
    monkeypatch.setattr(pool_module, "PROBE_WAIT", 0.2)
    orig_call = cli.call

    def fail_release(acct, action, *args, **kw):
        if action == "release":
            raise CliError("release failed")
        return orig_call(acct, action, *args, **kw)
    monkeypatch.setattr(cli, "call", fail_release)
    stuck = api.post("/cloud/accounts/login").json()
    api.post(f"/cloud/accounts/login/{stuck['login_id']}", json=dict(code="good"))
    st = wait_state(api, stuck["login_id"])
    assert st["state"] == "done" and st["added"] is True
    pool.login_status(stuck["login_id"]).thread.join(10)
    assert api.delete(f"/cloud/accounts/login/{stuck['login_id']}").status_code == 409
    monkeypatch.setattr(cli, "call", orig_call)
    pool.reap()
    assert api.delete(f"/cloud/accounts/login/{stuck['login_id']}").status_code == 204


def test_cloud_login_installs_cli_first(api, monkeypatch, tmp_path):
    """没装云端组件时，第一次添加账号先装（这里装和起账号池都是假的）。"""
    steps = []
    monkeypatch.setattr(gateway, "colab_installed", lambda: False)
    monkeypatch.setattr(gateway, "install_colab", lambda: steps.append("install"))

    async def start_pool():
        steps.append("pool")
        registry.pool = ColabPool(tmp_path / "pool", cli=LoginCLI(tmp_path))
    monkeypatch.setattr(gateway, "start_pool", start_pool)
    monkeypatch.setattr(registry, "pool", None)
    start = api.post("/cloud/accounts/login").json()
    assert steps == ["install", "pool"] and start["needs_code"]
    api.post("/cloud/accounts/login")
    assert steps == ["install", "pool"]                              # 装过了不再装
    for login in registry.pool.logins.values():
        login.proc.kill()


# ---------------------------------------------------------------- 设置进调度

def test_gpu_pref_changes_recommended_card():
    t4 = l4(id="colab-T4", name="云端 T4", gpu="T4", rate=1.15)
    task = Task("train", [("v2pro", 100)], lines=800)
    assert scheduler.plan([mini(), l4(), t4], task, AUTO)["recommended"] == "colab-L4"
    p = scheduler.plan([mini(), l4(), t4], task, dict(AUTO, gpu_pref="T4"))
    assert (p["recommended"], p["reason_code"], p["reason"]) == ("colab-T4", "train_needs_gpu", "训练要 GPU，用云端 T4")
    # 偏好的卡不能用：按原来的规则（本机）
    p = scheduler.plan([mini(), l4(), dict(t4, status="paused")], task, dict(AUTO, gpu_pref="T4"))
    assert p["recommended"] == "local"


def test_max_minutes_blocks_long_cloud_jobs():
    task = Task("train", [("v2pro", 100)], lines=800)
    sec = cand(scheduler.plan([l4()], task, AUTO), "colab-L4")["seconds"]
    p = scheduler.plan([mini(), l4(max_minutes=sec // 60)], task, AUTO)
    c = cand(p, "colab-L4")
    assert (c["available"], c["reason_code"]) == (False, "over_max_minutes") and "最长用多久" in c["reason"]
    assert cand(scheduler.plan([l4(max_minutes=sec // 60 + 1)], task, AUTO), "colab-L4")["available"]


def test_warm_vm_shortens_cloud_prep():
    task = Task("eval", [("v4", 36)], upload_mb=24)
    cold = cand(scheduler.plan([l4()], task, AUTO), "colab-L4")
    warm = cand(scheduler.plan([l4(warm=True)], task, AUTO), "colab-L4")
    assert cold["seconds"] - warm["seconds"] == scheduler.CLOUD_BOOT - scheduler.WARM_PREP
    # 开始前要准备多久：新开卡是开卡、装环境 + 上传，接手留机只是清目录、核对环境 + 上传
    upload = 24 / scheduler.UPLOAD_MBPS
    assert (cold["prep_sec"], warm["prep_sec"]) == (round(scheduler.CLOUD_BOOT + upload), round(scheduler.WARM_PREP + upload))


def test_compute_snapshot_carries_cloud_settings(cloud):
    api, pool, cli = cloud
    api.put("/cloud/settings", json=dict(gpu_pref="T4", max_minutes=60))
    devices, auto = asyncio.run(registry.snapshot())
    assert auto["gpu_pref"] == "T4"
    assert {d["id"]: d.get("max_minutes") for d in devices if d["type"] == "cloud"}["colab-L4"] == 60
    with pool.start("L4", task_id="x", estimated_minutes=5) as run:
        run.keep_idle(10)
    devices, _ = asyncio.run(registry.snapshot())
    assert {d["id"]: d["warm"] for d in devices if d["type"] == "cloud"} == \
        {"colab-T4": False, "colab-L4": True, "colab-A100": False, "colab-G4": False, "colab-H100": False}
    with pool.start("L4", task_id="y", estimated_minutes=5) as again:
        assert again.reused


def test_reused_vm_is_reset_before_upload(tmp_path):
    from ttsapi.compute.colab import job as job_module
    from ttsapi.compute.colab.run import RunState

    class VM:
        reused = True

        def __init__(self):
            self.events = []

        def execute(self, code, timeout=90):
            self.events.append(("exec", code))

        def upload(self, local, remote):
            self.events.append(("upload", remote))

        def run_background(self, *a, **kw):
            pass

        def wait(self):
            return RunState.GONE
    vm = VM()
    job_module.run_job(vm, dict(run_id="r"), {}, tmp_path / "out.tar")
    assert vm.events[0] == ("exec", job_module.RESET) and vm.events[1][0] == "upload"
    vm = VM()
    vm.reused = False
    job_module.run_job(vm, dict(run_id="r"), {}, tmp_path / "out.tar")
    assert vm.events[0][0] == "upload"


# ---------------------------------------------------------------- 唤醒

@pytest.fixture
def sleepy(monkeypatch, tmp_path):
    """一台配了唤醒的局域网设备（gpu-worker），一开始连不上；发了 wake_after 个魔术包后上线。"""
    monkeypatch.setattr(settings, "COMPUTE_FILE", tmp_path / "compute.json")
    monkeypatch.setattr(registry, "_health", {})
    monkeypatch.setattr(registry, "_wake_failed", {})
    monkeypatch.setattr(registry, "WAKE_POLL", 0.01)
    monkeypatch.setattr(registry, "WAKE_RESEND", 0)
    dev = dict(id="lan-aaaaaa", name="gpu-worker", address="http://192.168.1.20:9880", token="t",
               hw=dict(cpu="x", cores=8, ram_gb=64, accel="cpu", gpu=None, os="linux", torch="2"), machine="gpu-worker",
               engines=["v2pro", "v2proplus"], wake=dict(mac="aa:bb:cc:dd:ee:ff", broadcast="192.168.1.255"))
    registry.save(dict(devices=[dev], paused=[]))
    state = dict(sent=[], wake_after=2)
    monkeypatch.setattr(registry, "send_magic", lambda mac, bc: state["sent"].append((mac, bc)))

    async def health(self, timeout=3):
        if len(state["sent"]) < state["wake_after"]:
            raise OSError("host is down")
        return dict(hw=dev["hw"], machine="gpu-worker", engines=["v2pro", "v2proplus"], loaded=[], busy=False,
                    training=False)
    monkeypatch.setattr(remote.Remote, "health", health)
    return state


def test_send_magic_packet(monkeypatch):
    sent = []

    class Sock:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def setsockopt(self, *a):
            pass

        def sendto(self, data, addr):
            sent.append((data, addr))
    monkeypatch.setattr(registry.socket, "socket", Sock)
    registry.send_magic("aa:bb:cc:dd:ee:ff")
    ((data, addr),) = sent
    assert addr == ("255.255.255.255", 9) and data == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16


def test_sleeping_device_is_candidate_and_wakes(sleepy):
    st = asyncio.run(registry.lan_state(registry.lan_device("lan-aaaaaa"), []))
    assert (st["status"], st["wakeable"], st["engines"]) == ("offline", True, ["v2pro", "v2proplus"])
    st["speeds"] = lan()["speeds"]
    c = scheduler.estimate(st, Task("gen", [("v2pro", 10)]))
    awake = scheduler.estimate(dict(st, status="idle", wakeable=False), Task("gen", [("v2pro", 10)]))
    assert c["available"] and c["wake"] and c["seconds"] - awake["seconds"] == scheduler.WAKE_SEC

    seen = []

    async def go():
        task = asyncio.ensure_future(dispatch.prepare(dict(c), seen.append))
        await asyncio.sleep(0)
        mid = await registry.lan_state(registry.lan_device("lan-aaaaaa"), [])
        await task
        return mid
    mid = asyncio.run(go())
    assert (mid["status"], mid["status_reason"]) == ("waking", "唤醒中")
    assert seen == ["唤醒gpu-worker中"] and sleepy["sent"][0] == ("aa:bb:cc:dd:ee:ff", "192.168.1.255")
    st = asyncio.run(registry.lan_state(registry.lan_device("lan-aaaaaa"), []))
    assert st["status"] == "idle" and "lan-aaaaaa" not in registry.waking


def test_wake_timeout_counts_as_offline(sleepy, monkeypatch):
    monkeypatch.setattr(registry, "WAKE_TIMEOUT", 0.05)
    sleepy["wake_after"] = 10 ** 9
    chosen = dict(device="lan-aaaaaa", name="gpu-worker", wake=True)
    with pytest.raises(dispatch.PlanError) as e:
        asyncio.run(dispatch.prepare(chosen))
    assert e.value.code == "compute_unavailable" and "按离线处理" in e.value.detail
    st = asyncio.run(registry.lan_state(registry.lan_device("lan-aaaaaa"), []))
    assert (st["status"], st["wakeable"], st["status_reason"]) == ("offline", False, "离线（唤醒超时）")
    st["speeds"] = lan()["speeds"]
    assert scheduler.estimate(st, Task("gen", [("v2pro", 1)]))["reason_code"] == "offline"


def test_auto_dispatch_wakes_inside_job(sleepy, monkeypatch):
    """自动推荐到睡着的设备：任务里先唤醒再跑（on_device）。"""
    st = asyncio.run(registry.lan_state(registry.lan_device("lan-aaaaaa"), []))
    st["speeds"] = lan()["speeds"]
    ran = []

    async def snapshot():
        return [mini(engines=["v4"]), st], AUTO
    monkeypatch.setattr(registry, "snapshot", snapshot)
    chosen = asyncio.run(dispatch.choose(Task("gen", [("v2pro", 30)]), ComputeChoice()))
    assert chosen["device"] == "lan-aaaaaa" and chosen["wake"]

    class Run:
        def stage(self, text):
            ran.append(text)

    async def fn(run):
        ran.append(type(gateway.synth.EXEC.get()).__name__)
    asyncio.run(gateway.on_device(chosen, fn)(Run()))
    assert ran == ["唤醒gpu-worker中", "Remote"] and len(sleepy["sent"]) >= 2


def test_patch_device_wake(api, sleepy):
    r = api.patch("/compute/devices/lan-aaaaaa", json=dict(wake=dict(mac="aa:bb:cc:dd:ee:ff")))
    assert r.status_code == 200 and r.json()["wake"] == dict(mac="aa:bb:cc:dd:ee:ff", broadcast=None)
    assert api.patch("/compute/devices/lan-aaaaaa", json=dict(paused=True)).json()["wake"]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert api.patch("/compute/devices/lan-aaaaaa", json=dict(wake=dict(mac="nope"))).status_code == 422
    assert api.patch("/compute/devices/local", json=dict(wake=dict(mac="aa:bb:cc:dd:ee:ff"))).status_code == 400
    r = api.patch("/compute/devices/lan-aaaaaa", json=dict(wake=None, paused=False))
    assert r.json()["wake"] is None and "wake" not in registry.lan_device("lan-aaaaaa")


# ---------------------------------------------------------------- GET /system/versions

@pytest.fixture
def git_remote(monkeypatch):
    calls, out = [], dict(tags="", rc=0, url="https://github.com/dreaifekks/tts-studio.git", hang=False)

    def run(*args, timeout=None):
        calls.append(args)
        if args[0] == "ls-remote":
            if out["hang"]:
                raise subprocess.TimeoutExpired("git", timeout)
            return subprocess.CompletedProcess(args, out["rc"], out["tags"], "fatal: could not read Username")
        return subprocess.CompletedProcess(args, 0, out["url"] + "\n", "")
    monkeypatch.setattr(version, "run", run)
    monkeypatch.setattr(version, "_latest", {})
    clock = [1000.0]
    monkeypatch.setattr(version.time, "time", lambda: clock[0])
    return calls, out, clock


def test_versions_latest_tag_and_cache(api, git_remote):
    calls, out, clock = git_remote
    out["tags"] = "\n".join(f"{'a' * 40}\trefs/tags/{t}" for t in
                            ("v0.1.9", "v0.1.10", "v0.1.10^{}", "v0.2.0-rc1", "v0.1.3", "nightly"))
    r = api.get("/system/versions").json()
    assert r["studio"] == dict(current=version.VERSION, latest="0.1.10",
                               notes_url="https://github.com/dreaifekks/tts-studio/releases/tag/v0.1.10", error=None)
    assert r["core"] == dict(current=version.VERSION)
    n = len(calls)
    clock[0] += 599
    assert api.get("/system/versions").json()["studio"]["latest"] == "0.1.10" and len(calls) == n   # 10 分钟内用缓存
    out["tags"] += f"\n{'b' * 40}\trefs/tags/v0.2.0"
    clock[0] += 2
    assert api.get("/system/versions").json()["studio"]["latest"] == "0.2.0"
    n = len(calls)
    api.get("/system/versions", params=dict(refresh=True))
    assert len(calls) > n


def test_versions_failure_is_reported_and_retried_sooner(api, git_remote):
    calls, out, clock = git_remote
    out["rc"] = 128
    r = api.get("/system/versions").json()["studio"]
    assert r["latest"] is None and "could not read Username" in r["error"]
    clock[0] += version.LATEST_FAIL_TTL + 1
    out.update(rc=0, tags=f"{'a' * 40}\trefs/tags/v1.0.0", url="git@github.com:dreaifekks/tts-studio.git")
    r = api.get("/system/versions").json()["studio"]
    assert (r["latest"], r["error"], r["notes_url"]) == \
        ("1.0.0", None, "https://github.com/dreaifekks/tts-studio/releases/tag/v1.0.0")
    out["hang"] = True
    r = api.get("/system/versions", params=dict(refresh=True)).json()["studio"]
    assert r["latest"] is None and "超过" in r["error"]
    out.update(hang=False, url="/srv/git/tts-studio")
    assert api.get("/system/versions", params=dict(refresh=True)).json()["studio"]["notes_url"] is None


def test_half_installed_cli_is_installed_again(api, monkeypatch, tmp_path):
    """装 CLI 时建了 venv、同步依赖失败：python 在但导不进依赖，不算装好，下次添加账号照样先装。"""
    from ttsapi.compute.colab import install
    root = tmp_path / "colab-root"
    python = root / "colab-venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 1\n")
    python.chmod(0o755)
    monkeypatch.setattr(settings, "COLAB_ROOT", root)
    assert not install.ready(root) and not gateway.colab_installed()
    steps = []
    monkeypatch.setattr(gateway, "install_colab", lambda: steps.append("install"))

    async def start_pool():
        registry.pool = ColabPool(tmp_path / "pool", cli=LoginCLI(tmp_path))
    monkeypatch.setattr(gateway, "start_pool", start_pool)
    monkeypatch.setattr(registry, "pool", None)
    api.post("/cloud/accounts/login")
    assert steps == ["install"]
    for login in registry.pool.logins.values():
        login.proc.kill()
    python.write_text("#!/bin/sh\nexit 0\n")
    assert install.ready(root)
