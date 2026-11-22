import shutil
import sys
from pathlib import Path

import pytest

from . import ColabPool, RunState, WaitingForGPU, limits
from .pool import RATES
from . import run as run_module
from .cli import AllocationRefused, AllocationUnavailable, CliError


class FakeCLI:
    def __init__(self):
        self.balances = {"default": 10}
        self.vms = {}
        self.refuse = set()
        self.calls = []
        self.no_download = False
        self.ps_fails = False
        self.exec_fails = False
        self.corrupt = False
        self.forget_fails = False
        self.on_forget = None

    def api(self, account, action, *args):
        if action == "identity":
            return "default-test-digest"
        if action == "usage":
            return {
                "balance": self.balances[account],
                "gpus": ["T4", "L4", "A100", "G4", "H100"],
            }
        if action == "ps":
            if self.ps_fails:
                raise CliError("ps")
            return list(self.vms.values())
        if action == "recover":
            return self.vms.get(args[0])
        raise AssertionError(action)

    def endpoint(self, account, name):
        return self.vms.get(name)

    def call(self, account, action, *args, **kwargs):
        self.calls.append((account, action, args, kwargs))
        if action in ("new", "run"):
            if account in self.refuse:
                raise AllocationRefused()
            self.vms[args[1]] = "endpoint-" + args[1]
        elif action == "release":
            assert self.vms.pop(args[0]) == args[1]
        elif action == "forget":
            assert account != "default"
            if self.forget_fails:
                raise CliError("forget")
            if self.on_forget:
                self.on_forget(account)
        elif action in ("upload", "download"):
            src, dst = args[-2:]
            if action == "download" and (self.no_download or not Path(src).exists()):
                raise CliError("download")
            shutil.copyfile(src, dst)
            if action == "upload" and self.corrupt:
                Path(dst).write_bytes(b"oops")
        else:
            raise AssertionError(action)

    def execute(self, account, name, code, timeout):
        self.calls.append((account, "exec", code, timeout))
        if self.exec_fails:
            raise CliError("exec")
        exec(code, {})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(run_module, "RETRY_SECONDS", 0)
    now, cli = [1_800_000_000], FakeCLI()
    return ColabPool(tmp_path, cli=cli, clock=lambda: now[0]), cli, now


def test_upload_download(setup, tmp_path, monkeypatch):
    pool, cli, _ = setup
    monkeypatch.setattr(run_module, "CHUNK_BYTES", 7)
    src, dst, got = (tmp_path / name for name in ("local", "remote", "copy"))
    src.write_bytes(bytes(range(26)))
    with pool.start("T4", 30, task_id="transfer") as run:
        run.upload(src, str(dst))
        run.download(str(dst), got)
        assert got.read_bytes() == src.read_bytes()
        assert len([c for c in cli.calls if c[1] == "upload"]) == 4
        expected = dst.read_bytes()
        src.write_bytes(b"changed")
        cli.corrupt = True
        with pytest.raises(IOError, match="sha256"):
            run.upload(src, str(dst))
        assert dst.read_bytes() == expected


@pytest.mark.parametrize("lost_owner", [False, True])
def test_reap(setup, lost_owner):
    pool, cli, now = setup
    run = pool.start("T4", 30, task_id="reap")
    pool.reap()
    assert run.name in cli.vms
    if lost_owner:
        with pool.db() as db:
            db.execute("UPDATE usage SET pid_start=0")
    else:
        now[0] += 1800
    pool.reap()
    pool.reap()
    assert not cli.vms


def test_deadline_and_context_interrupt(setup):
    pool, cli, now = setup
    assert limits(1, 1.15) == (30, 0.25)
    assert limits(1500, 1.15)[0] == 1440
    with pytest.raises(KeyboardInterrupt):
        with pool.start("T4", task_id="interrupt", estimated_minutes=40) as run:
            assert run.deadline - now[0] == 4800
            raise KeyboardInterrupt
    run = pool.start("T4", 30, task_id="limit")
    now[0] += 1800
    with pytest.raises(TimeoutError):
        run.poll()
    assert not cli.vms


def test_poll(setup, tmp_path):
    pool, cli, now = setup
    with pool.start("T4", 60, task_id="poll") as run:
        marker = tmp_path / "rc"
        cli.exec_fails = True
        run.run_background("exit 3", str(marker))
        assert run.poll() == RunState.RUNNING
        now[0] += 299
        assert run.poll() == RunState.RUNNING
        assert not any(c[2] == "pass" for c in cli.calls)
        now[0] += 1
        assert run.poll() == RunState.RUNNING
        assert [c[2] for c in cli.calls].count("pass") == 1
        now[0] += 600
        assert run.poll() == RunState.RUNNING  # missing files but listed
        ep = cli.vms.pop(run.name)
        cli.ps_fails = True
        assert run.poll() == RunState.RUNNING
        cli.ps_fails = False
        (tmp_path / "rc.started").touch()
        assert run.poll() == RunState.RUNNING  # successful download resets timer
        cli.no_download = True
        assert run.poll() == RunState.RUNNING
        now[0] += 899
        assert run.poll() == RunState.RUNNING
        now[0] += 1
        assert run.poll() == RunState.GONE
        cli.vms[run.name] = ep
        cli.no_download = False
        marker.write_text("3")
        assert run.poll() == RunState.FAILED
        marker.write_text("0")
        assert run.poll() == RunState.DONE


def test_selection_and_usage(setup):
    pool, cli, now = setup
    low = pool.register("low@example.invalid")
    rich = pool.register("rich@example.invalid")
    cli.balances.update({low: 0.01, rich: 25})
    cli.refuse.add(rich)
    with pool.start("T4", 30, task_id="pick") as run:
        assert run.account_id == "default"
        assert [c[0] for c in cli.calls if c[1] == "new"] == [rich, "default"]
        cli.balances["default"] -= 0.375
    with pool.db() as db:
        (row,) = db.execute("SELECT * FROM usage").fetchall()
        assert row["actual_units"] == 0.375
        assert row["endpoint"] == run.endpoint
    assert (
        next(a for a in pool.accounts() if a.id == "default").recorded_month_used
        == 0.375
    )
    cli.refuse.add("default")
    with pytest.raises(WaitingForGPU) as exc:
        pool.start("T4", 30, task_id="wait")
    assert exc.value.retry_after == 600
    now[0] += 600
    cli.refuse.clear()
    with pool.start("T4", 30, task_id="retry") as run:
        assert run.account_id == rich


@pytest.mark.parametrize("gpu", ["G4", "A100", "H100"])
def test_approval(setup, gpu):
    pool, cli, _ = setup
    with pytest.raises(PermissionError):
        pool.start(gpu, 30, task_id="approval")
    assert not cli.calls


def test_run_script_passthrough(setup, tmp_path):
    pool, cli, _ = setup
    script = tmp_path / "sample.py"
    script.touch()
    pool.run_script("T4", script, ["--gpu", "0"], task_id="script", estimated_minutes=4)
    (call,) = [c for c in cli.calls if c[1] == "run"]
    args = call[2]
    assert args[args.index("--timeout") :] == (
        "--timeout",
        "0",
        "--",
        script,
        "--gpu",
        "0",
    )
    assert not cli.vms
    with pytest.raises(ValueError, match="--env"):
        pool.run_script(
            "T4", script, ["--env=X"], task_id="script", estimated_minutes=4
        )


def test_retry_unsettled_stop(setup, monkeypatch):
    pool, cli, _ = setup
    run = pool.start("T4", 30, task_id="partial-stop")
    api = cli.api

    def offline(account, action, *args):
        if action == "usage":
            raise CliError("offline")
        return api(account, action, *args)

    monkeypatch.setattr(cli, "api", offline)
    with pytest.raises(CliError):
        run.stop()
    assert not cli.vms
    with pool.db() as db:
        (row,) = db.execute("SELECT stopped,actual_units FROM usage")
        assert row["stopped"] is not None and row["actual_units"] is None
    monkeypatch.setattr(cli, "api", api)
    cli.balances["default"] -= 0.25
    pool.reap()
    run.stop()
    with pool.db() as db:
        assert db.execute("SELECT actual_units FROM usage").fetchone()[0] == 0.25
    assert [c[1] for c in cli.calls].count("release") == 1


def test_wrapped_background_is_idempotent(setup, tmp_path):
    import time

    pool, cli, _ = setup
    with pool.start("T4", 30, task_id="launch") as run:
        marker, output = tmp_path / "done", tmp_path / "result"
        run.run_background(f"echo once >> {output}", str(marker))
        cli.execute(run.account_id, run.name, run.launch, 10)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.01)
        assert run.poll() == RunState.DONE
        assert output.read_text() == "once\n"


def test_control_lifespan_closes_owned_run(setup):
    import asyncio

    pool, cli, _ = setup

    async def serve():
        async with pool.lifespan(interval=0.01):
            pool.start("T4", 30, task_id="lifespan")
            await asyncio.sleep(0.03)
            assert cli.vms

    asyncio.run(serve())
    assert not cli.vms


def test_no_drive_pipeline(setup, tmp_path):
    from .job import run_job

    import json

    uploaded, commands, stages = {}, [], []

    class Pipeline:
        def upload(self, local, remote):
            uploaded[remote] = Path(local).read_text()

        def run_background(self, cmd, marker, **kwargs):
            commands.append(cmd)

        def wait(self):
            return RunState.DONE

        def download(self, remote, local, timeout):
            assert remote in commands[-1]
            Path(local).touch()

    data = tmp_path / "library.json"
    data.write_text("{}")
    out = tmp_path / "output.tar"
    result = run_job(
        Pipeline(), {"run_id": "unit-test"}, {"library/library.json": data}, out,
        stages.append,
    )
    assert result == RunState.DONE and out.exists()
    assert [c.split()[4] for c in commands] == ["vm_setup.py", "vm_run.py", "vm_pack.py"]
    assert stages == ["upload", "setup", "run", "download"]
    assert all(
        "/content/job/" + n in uploaded
        for n in ("vm_lib.py", "vm_setup.py", "vm_run.py", "vm_pack.py")
    )
    mirror = json.loads(uploaded["/content/job/job.json"])["inputs"]
    assert mirror.startswith("/content/tts-inputs-")
    assert uploaded[f"{mirror}/library/library.json"] == "{}"


def test_busy_account_does_not_block_next_account(setup):
    pool, cli, _ = setup
    other = pool.register("other@example.invalid")
    cli.balances[other] = 5
    with pool.lock("default"):
        with pool.start("T4", 30, task_id="other") as run:
            assert run.account_id == other


@pytest.mark.parametrize("interruption", ["post-timeout", "worker-timeout", "shutdown"])
def test_uncertain_allocation_recovered_after_backoff(setup, monkeypatch, interruption):
    pool, cli, now = setup
    original = cli.call

    def interrupt(account, action, *args, **kwargs):
        if action == "new":
            if interruption == "shutdown":
                raise CliError("worker exited 125")
            raise AllocationUnavailable(interruption)
        return original(account, action, *args, **kwargs)

    monkeypatch.setattr(cli, "call", interrupt)
    with pytest.raises((WaitingForGPU, CliError)):
        pool.start("T4", 30, task_id="uncertain")
    with pool.db() as db:
        name, closing = db.execute("SELECT name,closing FROM usage").fetchone()
    assert closing
    queries = []
    monkeypatch.setattr(cli, "endpoint", lambda account, name: None)
    api = cli.api

    def recover(account, action, *args):
        if action == "recover":
            queries.append(now[0])
        return api(account, action, *args)

    monkeypatch.setattr(cli, "api", recover)
    now[0] += 9
    pool.reap()
    assert not queries
    now[0] += 1
    pool.reap()
    assert len(queries) == 1
    cli.vms[name] = "late-endpoint"
    now[0] += 60
    pool.reap()
    assert not cli.vms and len(queries) == 2
    with pool.db() as db:
        assert tuple(
            db.execute("SELECT endpoint,actual_units FROM usage").fetchone()
        ) == ("late-endpoint", 0)


def test_recover_expiry_requires_last_successful_lookup(setup, monkeypatch):
    pool, cli, now = setup
    run = pool.start("T4", 30, task_id="unknown")
    cli.vms.clear()
    with pool.db() as db:
        db.execute("UPDATE usage SET endpoint=NULL")
    run.stop()
    now[0] += 599
    pool.reap()
    with pool.db() as db:
        assert db.execute("SELECT count(*) FROM usage").fetchone()[0] == 1
    now[0] += 11
    api = cli.api

    def offline(account, action, *args):
        if action == "recover":
            raise CliError("offline")
        return api(account, action, *args)

    monkeypatch.setattr(cli, "api", offline)
    with pytest.raises(CliError):
        pool.reap()
    with pool.db() as db:
        assert db.execute("SELECT count(*) FROM usage").fetchone()[0] == 1
    monkeypatch.setattr(cli, "api", api)
    pool.reap()
    with pool.db() as db:
        assert db.execute("SELECT count(*) FROM usage").fetchone()[0] == 0


def test_default_dedup_before_authorization(setup, monkeypatch):
    import hashlib

    pool, cli, _ = setup
    digest = hashlib.sha256(b"duplicate@example.invalid").hexdigest()
    api = cli.api
    monkeypatch.setattr(
        cli,
        "api",
        lambda account, action, *args: (
            digest if action == "identity" else api(account, action, *args)
        ),
    )
    pool.accounts()
    with pytest.raises(ValueError, match="这个账号已经在池里"):
        pool.login(" Duplicate@EXAMPLE.INVALID ")
    assert len(pool.accounts()) == 1


def test_concurrent_billing_with_late_debit(setup):
    pool, cli, now = setup
    a = pool.start("T4", 30, task_id="a")
    now[0] += 60
    b = pool.start("L4", 30, task_id="b")
    assert len(cli.vms) == 2
    now[0] += 60
    a.stop()
    b.stop()
    cli.balances["default"] -= 0.4
    now[0] += 299
    pool.reap()
    with pool.db() as db:
        assert db.execute("SELECT SUM(actual_units) FROM usage").fetchone()[0] == 0
    now[0] += 1
    pool.reap()
    pool.reap()
    with pool.db() as db:
        rows = dict(db.execute("SELECT task_id,actual_units FROM usage"))
        assert rows["a"] == pytest.approx(0.4 * 1.15 * 120 / (1.15 * 120 + 1.94 * 60))
        assert sum(rows.values()) == pytest.approx(0.4)
        assert db.execute("SELECT sum(late_checked) FROM usage").fetchone()[0] == 2


def test_lost_upload_ack_and_part_download_retry(setup, tmp_path, monkeypatch):
    pool, cli, now = setup
    monkeypatch.setattr(run_module, "CHUNK_BYTES", 5)
    src, dst, got = (tmp_path / n for n in ("src", "dst", "got"))
    src.write_bytes(b"five-six-seven")
    execute = cli.execute
    lost = []

    def lose_ack(account, name, code, timeout):
        execute(account, name, code, timeout)
        if "Path(prefix).replace(target)" in code:
            lost.append(1)
            if len(lost) == 1:
                raise CliError("ack lost")

    monkeypatch.setattr(cli, "execute", lose_ack)
    transfer = cli.call
    attempts = []

    def flaky(account, action, *args, **kwargs):
        if action in ("download", "upload"):
            now[0] += 105
        if action == "download" and str(args[-2]).endswith("/1"):
            attempts.append(1)
            if len(attempts) == 1:
                raise CliError("connection lost")
        return transfer(account, action, *args, **kwargs)

    monkeypatch.setattr(cli, "call", flaky)
    with pool.start("T4", 60, task_id="transfer") as run:
        run.upload(src, str(dst))
        before = len([c for c in cli.calls if c[2] == "pass"])
        assert before >= 1 and len(lost) == 2
        run.download(str(dst), got)
        assert got.read_bytes() == src.read_bytes() and len(attempts) == 2
        assert len([c for c in cli.calls if c[2] == "pass"]) > before


@pytest.mark.parametrize("exit_mode", ["caught", "uncaught", "explicit-stop"])
def test_failed_download_releases_vm(setup, tmp_path, exit_mode):
    from contextlib import nullcontext

    pool, cli, _ = setup
    src, got = tmp_path / "src", tmp_path / "got"
    src.write_bytes(b"result")
    got.write_bytes(b"previous")
    cli.no_download = True
    with pytest.raises(CliError) if exit_mode == "uncaught" else nullcontext():
        with pool.start("T4", 30, task_id="result") as run:
            if exit_mode == "uncaught":
                run.download(str(src), got)
            else:
                with pytest.raises(CliError):
                    run.download(str(src), got)
                if exit_mode == "explicit-stop":
                    run.stop()
                    assert not cli.vms
    assert not cli.vms and got.read_bytes() == b"previous"
    assert [c[1] for c in cli.calls].count("release") == 1
    assert not (pool.root / f"{run.name}.lock").exists()


@pytest.mark.parametrize("end", ["retry", "deadline", "owner", "stop"])
def test_explicit_retention(setup, tmp_path, end):
    pool, cli, now = setup
    src, got = tmp_path / "src", tmp_path / "got"
    src.write_bytes(b"result")
    cli.no_download = True
    with pytest.raises(CliError):
        with pool.start("T4", 30, task_id="result") as run:
            try:
                run.download(str(src), got)
            except CliError:
                run.keep_for_retry()
                raise
    assert run.name in cli.vms and not got.exists()
    now[0] += 299
    pool.reap()
    assert not any(c[2] == "pass" for c in cli.calls)
    for seconds in (1, 300):
        now[0] += seconds
        pool.reap()
        pool.reap()
    assert sum(c[2] == "pass" for c in cli.calls) == 2
    if end == "retry":
        cli.no_download = False
        with run:
            run.download(*run.pending_result)
        assert got.read_bytes() == b"result"
    elif end == "stop":
        run.stop()
    else:
        if end == "deadline":
            now[0] = run.deadline
        else:
            with pool.db() as db:
                db.execute("UPDATE usage SET pid_start=0")
        pool.reap()
    assert not cli.vms
    now[0] += 300
    pool.reap()
    assert sum(c[2] == "pass" for c in cli.calls) == 2


def test_lifespan_recovers_after_unexpected_exception(setup, monkeypatch, caplog):
    import asyncio
    import threading

    pool, cli, _ = setup
    next_tick = threading.Event()
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("temporarily missing CLI")
        next_tick.set()

    monkeypatch.setattr(pool, "reap", flaky)

    async def serve():
        async with pool.lifespan(interval=0.01):
            assert await asyncio.to_thread(next_tick.wait, 2)

    asyncio.run(serve())
    assert len(calls) > 1 and "Colab maintenance failed" in caplog.text


def test_migrate_old_active_ledger(setup):
    pool, cli, now = setup
    run = pool.start("T4", 30, task_id="legacy")
    with pool.db() as db:
        db.execute(
            "CREATE UNIQUE INDEX one_vm_per_account ON usage(account) WHERE stopped IS NULL"
        )
        db.execute("DROP TABLE meter")
        for col in ["allocated", "recover_after", "late_checked"]:
            db.execute(f"ALTER TABLE usage DROP COLUMN {col}")
    pool = ColabPool(pool.root, cli=cli, clock=lambda: now[0])
    now[0] += 60
    cli.balances["default"] -= 0.2
    pool.stop("default", run.name)
    with pool.db() as db:
        assert db.execute("SELECT actual_units FROM usage").fetchone()[
            0
        ] == pytest.approx(0.2)
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='one_vm_per_account'"
        ).fetchone()


def test_non_posix_import():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['fcntl']=None; import ttsapi.compute.colab",
        ],
        check=True,
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS 钥匙串")
def test_mac_cli_home_finds_login_keychain(tmp_path):
    """CLI 子进程的 HOME 是账号目录：Security 框架在这个 HOME 里也要找到本机用户的登录钥匙串（存凭据要默认钥匙串，
    读和删要它在搜索列表里），不然存凭据时弹「Keychain Not Found」。只看钥匙串设置，不读写任何条目。"""
    import subprocess

    from .cli import ColabCLI

    login = str(Path.home() / "Library/Keychains/login.keychain-db")
    cli = ColabCLI(tmp_path, "unused")
    for _ in range(2):  # 第二次已经设好了
        _, env = cli.command("probe", "ps", ())

    def security(verb):
        out = subprocess.run(["/usr/bin/security", verb], env=env, capture_output=True, text=True, check=True)
        return [line.strip().strip('"') for line in out.stdout.splitlines()]

    assert env["HOME"] == str(tmp_path / "colab/probe")
    assert security("default-keychain") == [login]
    assert login in security("list-keychains")


def test_linux_cli_home_has_no_keychain_prefs(tmp_path, monkeypatch):
    from .cli import ColabCLI

    monkeypatch.setattr("sys.platform", "linux")
    ColabCLI(tmp_path, "unused").command("probe", "ps", ())
    assert not (tmp_path / "colab/probe/Library").exists()


def test_long_script_allows_another_vm(setup, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    pool, cli, _ = setup
    entered, finish = Event(), Event()
    original = cli.call

    def wait_script(account, action, *args, **kw):
        result = original(account, action, *args, **kw)
        if action == "run":
            entered.set()
            assert finish.wait(3)
        return result

    monkeypatch.setattr(cli, "call", wait_script)
    script = tmp_path / "task.py"
    script.touch()
    with ThreadPoolExecutor(max_workers=1) as executor:
        fut = executor.submit(
            pool.run_script, "T4", script, task_id="long", estimated_minutes=5
        )
        try:
            assert entered.wait(3)
            with pool.start("L4", 30, task_id="parallel"):
                assert len(cli.vms) == 2
        finally:
            finish.set()
        fut.result()
    assert not cli.vms


def test_late_debit_adds_to_immediate_charge(setup):
    pool, cli, now = setup
    old = pool.start("T4", 30, task_id="first")
    now[0] += 120
    cli.balances["default"] -= 0.1
    old.stop()
    later = pool.start("L4", 30, task_id="second")
    now[0] += 301
    cli.balances["default"] -= 0.3
    pool.reap()
    later.stop()
    with pool.db() as db:
        costs = dict(db.execute("SELECT task_id, actual_units FROM usage"))
    assert costs["first"] > 0.1 and costs["second"] > 0
    assert sum(costs.values()) == pytest.approx(0.4)


@pytest.mark.parametrize("retry_gpu", [None, "L4"])
def test_unconfirmed_attempt_does_not_block_task(setup, monkeypatch, retry_gpu):
    pool, cli, now = setup
    other = pool.register("fallback@example.invalid")
    cli.balances[other] = 0 if retry_gpu else 5
    call = cli.call
    attempts = []

    def allocate(account, action, *args, **kw):
        if action == "new":
            attempts.append((account, args[1]))
            if len(attempts) == 1:
                raise AllocationUnavailable("POST result unknown")
        return call(account, action, *args, **kw)

    monkeypatch.setattr(cli, "call", allocate)
    if retry_gpu:
        with pytest.raises(WaitingForGPU):
            pool.start("T4", 30, task_id="same-task")
    with pool.start(retry_gpu or "T4", 30, task_id="same-task") as run:
        assert run.account_id == ("default" if retry_gpu else other)
        first_account, first_name = attempts[0]
        assert first_account == "default" and first_name != run.name
        with pool.db() as db:
            row = db.execute(
                "SELECT * FROM usage WHERE name=?", (first_name,)
            ).fetchone()
            assert row["closing"] and row["endpoint"] is None
        assert (pool.root / f"{first_name}.lock").exists()
        cli.vms[first_name] = "late-endpoint"
        now[0] += 11
        pool.reap()
        assert set(cli.vms) == {run.name}
        assert not (pool.root / f"{first_name}.lock").exists()
    assert not cli.vms


def test_refresh_existing_login(setup, monkeypatch):
    pool, cli, _ = setup
    email = "account@example.invalid"
    account = pool.register(email, "my label")
    authorized = []
    monkeypatch.setattr(cli, "authorize", authorized.append, raising=False)
    assert pool.login(email.upper()) == account
    assert authorized == [account] and not cli.calls
    with pool.db() as db:
        (row,) = db.execute(
            "SELECT label, enabled FROM accounts WHERE id=?", (account,)
        )
        assert tuple(row) == ("my label", 1)


@pytest.mark.parametrize(("rate", "expected"), [(100 * 1024 * 1024, 300), (1, 828)])
def test_upload_merge_timeout(setup, tmp_path, monkeypatch, rate, expected):
    pool, cli, _ = setup
    monkeypatch.setattr(run_module, "MERGE_BYTES_PER_SECOND", rate)
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(bytes(256))
    with pool.start("T4", 30, task_id="upload") as run:
        run.upload(src, str(dst))
    (merge,) = [
        call
        for call in cli.calls
        if call[1] == "exec" and "Path(prefix).replace" in call[2]
    ]
    assert merge[3] == expected and dst.read_bytes() == src.read_bytes()


def test_old_terminal_lock_cleanup(setup):
    pool, cli, _ = setup
    with pool.start("T4", 30, task_id="old") as run:
        job_lock = pool.root / f"{run.name}.lock"
        assert job_lock.exists()
    job_lock.touch()  # previous core version left this terminal UUID behind
    unrelated = pool.root / ("f" * 32 + ".lock")
    unrelated.touch()
    pool.reap()
    assert (
        not job_lock.exists()
        and unrelated.exists()
        and (pool.root / "default.lock").exists()
    )


def test_caught_error_retained_for_one_handoff(setup, tmp_path):
    pool, cli, _ = setup
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.write_bytes(b"result")
    cli.no_download = True
    with pool.start("T4", 30, task_id="retry") as run:
        with pytest.raises(CliError):
            run.download(str(src), dest)
        run.keep_for_retry()
    assert run.name in cli.vms
    with pytest.raises(CliError):
        with run:
            run.download(*run.pending_result)
    assert not cli.vms


def test_failed_relogin_preserves_account(setup, monkeypatch):
    pool, cli, _ = setup
    email = "existing@example.invalid"
    account = pool.register(email, "existing label")

    def wrong_identity(selected):
        assert selected == account
        raise CliError("verified identity differs")

    monkeypatch.setattr(cli, "authorize", wrong_identity, raising=False)
    with pytest.raises(CliError):
        pool.login(email, "replacement")
    with pool.db() as db:
        (row,) = db.execute("SELECT label,enabled FROM accounts WHERE id=?", (account,))
        assert tuple(row) == ("existing label", 1)


def test_retry_retention_expires_and_settles(setup, tmp_path):
    pool, cli, now = setup
    src = tmp_path / "source"
    src.write_bytes(b"result")
    cli.no_download = True
    with pool.start("T4", 120, task_id="retained") as run:
        now[0] += 600
        with pytest.raises(CliError):
            run.download(str(src), tmp_path / "result")
        run.keep_for_retry()
    now[0] += 1799
    pool.reap()
    assert run.name in cli.vms
    now[0] += 2
    assert now[0] < run.deadline
    cli.balances["default"] -= 0.2
    pool.reap()
    pool.reap()
    assert not cli.vms
    assert sum(c[1] == "release" for c in cli.calls) == 1
    with pool.db() as db:
        row = db.execute(
            "SELECT stopped,actual_units FROM usage WHERE name=?", (run.name,)
        ).fetchone()
        assert row["stopped"] == now[0] and row["actual_units"] == pytest.approx(0.2)


# ---------------------------------------------------------------- 后台登录（假 CLI 子进程，不连 Google）

FAKE_LOGIN = r'''
import hashlib, json, os, pathlib, sys
sys.stderr.write("\nTo authorize colab-cli, visit this URL in any browser:\n\n  https://accounts.google.com/o/oauth2/auth?fake=1\n\n")
sys.stderr.flush()
code = sys.stdin.readline().strip()
if code != "good":
    sys.stderr.write("oauthlib.oauth2.rfc6749.errors.InvalidGrantError: (invalid_grant) Bad Request\n")
    sys.exit(1)
email = os.environ["FAKE_EMAIL"]
print("Authentication successful.", flush=True)  # CLI 自己往 stdout 打的行：主控跳过
print(json.dumps({"email": email, "account": hashlib.sha256(email.casefold().encode()).hexdigest()}), flush=True)
if sys.stdin.readline().strip() == "commit":
    pathlib.Path(os.environ["FAKE_SAVED"]).write_text(email)
    sys.exit(0)
sys.exit(1)
'''


class LoginCLI(FakeCLI):
    """login_process 起一个假的 CLI 登录子进程：stderr 打授权链接，stdin 读授权码，stdout 回邮箱，commit 后「保存凭据」。"""

    def __init__(self, tmp_path, email="new@example.invalid"):
        super().__init__()
        self.script, self.saved, self.email, self.slots = tmp_path / "fake_login.py", tmp_path / "saved", email, []
        self.script.write_text(FAKE_LOGIN)

    def login_process(self, slot):
        import os
        import subprocess
        import sys
        self.slots.append(slot)
        return subprocess.Popen([sys.executable, str(self.script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1,
                                env=dict(os.environ, FAKE_EMAIL=self.email, FAKE_SAVED=str(self.saved)))


def digest(email):
    import hashlib
    return hashlib.sha256(email.casefold().encode()).hexdigest()


@pytest.fixture
def login_pool(tmp_path):
    cli = LoginCLI(tmp_path)
    cli.balances[digest(cli.email)] = 5
    return ColabPool(tmp_path / "pool", cli=cli, clock=lambda: 1_800_000_000), cli


def finish(pool, login):
    login.thread.join(10)
    assert not login.thread.is_alive()
    return pool.login_status(login.id)


def test_login_paste_code_registers_and_probes(login_pool):
    pool, cli = login_pool
    login = pool.begin_login()
    assert login.auth_url == "https://accounts.google.com/o/oauth2/auth?fake=1"
    assert pool.login_status(login.id).state == "waiting" and not cli.saved.exists()
    pool.submit_code(login.id, " good \n")
    with pytest.raises(ValueError):
        pool.submit_code(login.id, "good")                          # 只能交一次
    st = finish(pool, login)
    account = digest(cli.email)
    assert (st.state, st.account, st.added, st.error) == ("done", account, True, None)
    assert cli.saved.read_text() == cli.email                       # 核对邮箱后才 commit、写凭据
    a = pool.account(account, refresh=False)
    assert (a.label, a.email, a.disabled, a.balance) == (cli.email, cli.email, False, 5)
    # 新账号：有报价、非高费率的卡各试开一次就释放
    assert sorted(c[2][3] for c in cli.calls if c[1] == "new" and c[0] == account) == ["L4", "T4"]
    assert not cli.vms
    assert not (pool.root / "colab" / cli.slots[0]).exists()       # 临时 HOME 删掉


def test_login_bad_code_and_timeout(login_pool, monkeypatch):
    pool, cli = login_pool
    login = pool.begin_login("x")
    pool.submit_code(login.id, "wrong")
    st = finish(pool, login)
    assert st.state == "failed" and "invalid_grant" in st.error and not cli.saved.exists()
    assert pool.accounts() == [a for a in pool.accounts() if a.id == "default"]
    from . import pool as pool_module
    monkeypatch.setattr(pool_module, "LOGIN_TIMEOUT", 0.3)
    login = pool.begin_login()
    st = finish(pool, login)                                        # 一直没交授权码
    assert (st.state, st.error) == ("failed", "授权超时，重新添加")


def test_login_rejects_default_and_relogin_keeps_label(login_pool, monkeypatch):
    pool, cli = login_pool
    account = digest(cli.email)
    monkeypatch.setattr(pool, "_default_identity", lambda: account)
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    st = finish(pool, login)
    assert st.state == "failed" and "默认账号" in st.error and not cli.saved.exists()
    # 默认账号停用过：用同一个 Google 账号登录就重新启用（不另存凭据）
    pool.disable("default")
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    st = finish(pool, login)
    assert (st.state, st.account, st.added) == ("done", "default", False) and not cli.saved.exists()
    assert not pool.account("default", refresh=False).disabled
    monkeypatch.undo()
    pool.register(cli.email, "我的号")
    pool.disable(account)
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    st = finish(pool, login)
    assert (st.state, st.added) == ("done", False)
    a = pool.account(account, refresh=False)
    assert (a.label, a.disabled) == ("我的号", False)               # 停用的账号重新登录就恢复，沿用原来的名字
    assert not [c for c in cli.calls if c[1] == "new"]              # 已有账号不再试卡
    # Previously removed account logged in again counts as added.
    pool.remove(account)
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    st = finish(pool, login)
    assert (st.state, st.added) == ("done", True)


def test_accounts_disabled_and_default_not_logged_in(setup, monkeypatch):
    pool, cli, _ = setup
    other = pool.register("o@example.invalid")
    cli.balances[other] = 3
    pool.disable(other)
    assert [a.id for a in pool.accounts()] == ["default"]
    listed = {a.id: a for a in pool.accounts(include_disabled=True)}
    assert listed[other].disabled and listed[other].balance is None and not listed["default"].disabled
    with pytest.raises(KeyError):
        pool.account("nope")
    # 本机 colab CLI 没登录：默认账号不列出来
    from .cli import LoginRequired

    asked = []

    def not_logged_in(account, action, *args):
        if action == "identity":
            asked.append(account)
            raise LoginRequired("x")
        return FakeCLI.api(cli, account, action, *args)
    monkeypatch.setattr(cli, "api", not_logged_in)
    fresh = ColabPool(pool.root / "fresh", cli=cli, clock=pool.clock)
    assert fresh.accounts() == [] and all(t.account_count == 0 for t in fresh.gpu_types())
    assert asked == ["default"]                                      # 10 分钟内不再起子进程查


# ---------------------------------------------------------------- 做完先别关（keep_minutes）

def test_keep_idle_then_next_task_adopts(setup):
    pool, cli, now = setup
    with pool.start("T4", task_id="first", estimated_minutes=10) as run:
        assert run.keep_idle(10)
    assert run.name in cli.vms and pool.idle_vms() == {"T4": 1}
    assert pool.idle_vms().get("L4") is None
    now[0] += 120
    with pool.start("T4", task_id="second", estimated_minutes=20) as again:
        assert (again.name, again.reused) == (run.name, True)
        assert again.deadline == now[0] + 40 * 60
        assert pool.idle_vms() == {}
    assert sum(c[1] == "new" for c in cli.calls) == 1 and not cli.vms
    with pool.db() as db:
        assert db.execute("SELECT task_id FROM usage WHERE name=?", (run.name,)).fetchone()[0] == "second"


def test_keep_idle_expires_and_other_gpu_does_not_adopt(setup):
    pool, cli, now = setup
    with pool.start("T4", task_id="first", estimated_minutes=10) as run:
        run.keep_idle(10)
    with pool.start("L4", task_id="other", estimated_minutes=10) as l4:
        assert l4.name != run.name and not l4.reused
    now[0] += 301
    pool.reap()                                                      # 留着期间照常保活
    assert run.name in cli.vms and any(c[1] == "exec" and c[2] == "pass" for c in cli.calls)
    now[0] += 300
    cli.balances["default"] -= 0.2
    pool.reap()
    assert run.name not in cli.vms and pool.idle_vms() == {}


def test_keep_idle_skipped_near_deadline(setup):
    pool, cli, now = setup
    with pool.start("T4", 30, task_id="short") as run:
        now[0] = run.deadline - 30
        assert run.keep_idle(10) is False
    assert not cli.vms


def test_usage_monthly(setup):
    pool, cli, now = setup
    with pool.start("T4", task_id="a", estimated_minutes=10):
        cli.balances["default"] -= 0.3
    with pool.db() as db:
        db.execute("UPDATE usage SET actual_units=0.3")
    (m,) = pool.usage_monthly()
    assert m["by_account"] == {"default": 0.3} and len(m["month"]) == 7


def test_adopt_checks_fresh_balance_remaining_life_and_owner(setup):
    pool, cli, now = setup
    other = pool.register("other@example.invalid")
    cli.balances[other] = 5
    with pool.start("T4", task_id="first", estimated_minutes=10) as run:
        assert run.account_id == "default"
        run.keep_idle(10)
    cli.balances["default"] = 0.1                                   # 上一个任务花光了：不接手，开别的账号的新卡
    with pool.start("T4", task_id="second", estimated_minutes=10) as fresh:
        assert not fresh.reused and fresh.account_id == other
    cli.balances["default"] = 10
    with pool.db() as db:                                            # 开机快满 24 小时：剩下的不够这次
        db.execute("UPDATE usage SET started=? WHERE name=?", (now[0] - 1430 * 60, run.name))
    with pool.start("T4", task_id="third", estimated_minutes=30) as fresh:
        assert not fresh.reused
    with pool.db() as db:
        db.execute("UPDATE usage SET started=? WHERE name=?", (now[0] - 60, run.name))
    again = pool.start("T4", task_id="fourth", estimated_minutes=10)
    assert again.reused and again.name == run.name
    run.stop()                                                       # 旧任务的句柄：已被接手，不释放
    assert run.name in cli.vms
    again.stop()
    assert run.name not in cli.vms


# ---------------------------------------------------------------- 第 2 轮审查：回收、停用、剩余时长、关停时的试卡

def test_reap_rechecks_under_vm_lock_after_adoption(setup, monkeypatch):
    """回收器读到旧时限后、关之前，这台被新任务接手、时限延长：在 VM 锁里按现在的记录判，不关。"""
    pool, cli, now = setup
    with pool.start("T4", 30, task_id="first") as run:
        run.keep_idle(60)
    adopted, real = [], pool._owner_alive

    def adopt_meanwhile(row):
        if row["name"] == run.name and not adopted:
            adopted.append(pool.start("T4", task_id="second", estimated_minutes=20))
            now[0] = run.deadline + 1                                 # 旧时限在回收器忙别的行时过了
        return real(row)
    monkeypatch.setattr(pool, "_owner_alive", adopt_meanwhile)
    pool.reap()
    assert adopted[0].reused and run.name in cli.vms
    adopted[0].stop()
    assert not cli.vms


def test_disabled_account_idle_vm_released_and_not_adopted(setup):
    pool, cli, now = setup
    other = pool.register("o@example.invalid")
    cli.balances[other] = 50
    with pool.start("T4", task_id="a", estimated_minutes=10) as run:
        assert run.account_id == other
        run.keep_idle(10)
    with pool.db() as db:                                            # 接手的原子更新里查 enabled
        db.execute("UPDATE accounts SET enabled=0 WHERE id=?", (other,))
    with pool.start("T4", task_id="b", estimated_minutes=10) as fresh:
        assert not fresh.reused and fresh.account_id == "default"
    with pool.db() as db:
        db.execute("UPDATE accounts SET enabled=1 WHERE id=?", (other,))
    pool.disable(other)                                              # 停用时它留着的 VM 马上释放
    assert run.name not in cli.vms and pool.idle_vms() == {}


def test_remove_account(setup):
    """移除：留着的 VM 释放、结算；删 core 写的凭据（worker.py forget）和这个账号的配置目录；不再列出；账本行保留
    （本月合计照算，名字还查得到），之后不再查它的余额回补结算；重新登录（register）就回来。"""
    pool, cli, now = setup
    other = pool.register("o@example.invalid", "备用号")
    cli.balances[other] = 50
    with pool.start("T4", task_id="a", estimated_minutes=10) as run:
        assert run.account_id == other
        run.keep_idle(10)
    home = pool.root / "colab" / other
    (home / ".config/colab-cli").mkdir(parents=True)
    pool.remove(other)
    assert run.name not in cli.vms and pool.idle_vms() == {}
    assert [c[0] for c in cli.calls if c[1] == "forget"] == [other] and not home.exists()
    assert [a.id for a in pool.accounts(include_disabled=True)] == ["default"]
    with pytest.raises(KeyError):
        pool.account(other)
    with pytest.raises(KeyError):
        pool.enable(other)                                           # 移除了的要重新登录
    with pool.db() as db:
        db.execute("UPDATE usage SET actual_units=0.3 WHERE account=?", (other,))
    assert pool.month_used() == pytest.approx(0.3) and pool.labels()[other] == "备用号"
    del cli.balances[other]                                          # 凭据没了：查余额会失败
    now[0] += 600
    pool.reap()                                                      # 不再给它回补结算
    pool.register("o@example.invalid")
    assert not pool.account(other, refresh=False).disabled


def test_remove_records_estimate_for_unposted_cost(setup):
    """移除时余额还没反映扣款（留着的 VM 刚释放）：按运行时长 × 费率记估值，不是 0，之后也不再补结算。"""
    pool, cli, now = setup
    other = pool.register("o@example.invalid")
    cli.balances[other] = 50
    with pool.start("T4", task_id="a", estimated_minutes=10) as run:
        now[0] += 600
        assert run.keep_idle(10)
    now[0] += 60
    pool.remove(other)
    with pool.db() as db:
        row = db.execute("SELECT actual_units, late_checked, started, stopped FROM usage WHERE name=?",
                         (run.name,)).fetchone()
    assert row["late_checked"] == 1
    assert row["actual_units"] == pytest.approx(RATES["T4"] * (row["stopped"] - row["started"]) / 3600)
    assert row["actual_units"] == pytest.approx(RATES["T4"] * 660 / 3600)
    assert pool.month_used() == pytest.approx(row["actual_units"])


def test_remove_forget_failure_keeps_retryable_state(setup):
    """删凭据失败：账号停用了但没标移除，配置目录还在，可以再删一次。"""
    pool, cli, _ = setup
    other = pool.register("o@example.invalid")
    home = pool.root / "colab" / other
    home.mkdir(parents=True)
    cli.forget_fails = True
    with pytest.raises(CliError):
        pool.remove(other)
    assert pool.account(other, refresh=False).disabled and home.exists()
    cli.forget_fails = False
    pool.remove(other)
    assert other not in [a.id for a in pool.accounts(include_disabled=True)] and not home.exists()


def test_remove_and_allocation_share_account_lock(setup):
    """删凭据时拿着账号锁（开卡登记也要这把锁）；开卡那边等锁、查余额期间账号被停用（移除的第一步）就不登记、不开卡。"""
    pool, cli, _ = setup
    other = pool.register("o@example.invalid")
    cli.balances[other] = 50
    held = []
    cli.on_forget = lambda account: held.append(pool.lock(account, wait=False).__enter__() is None)
    pool.remove(other)
    assert held == [True]
    third = pool.register("t@example.invalid")
    cli.balances[third] = 50
    usage = pool._usage

    def usage_then_removed(account):
        info = usage(account)
        if account == third:
            with pool.db() as db:
                db.execute("UPDATE accounts SET enabled=0 WHERE id=?", (third,))
        return info
    pool._usage = usage_then_removed
    with pool.start("T4", task_id="b", estimated_minutes=10) as run:
        assert run.account_id == "default"
    assert not [c for c in cli.calls if c[1] == "new" and c[0] == third]


def test_remove_default_only_disables_and_busy_refused(setup):
    pool, cli, _ = setup
    with pool.start("T4", task_id="busy", estimated_minutes=10):
        with pytest.raises(ValueError):
            pool.remove("default")                                   # VM 还在跑任务
        assert not pool.account("default", refresh=False).disabled
    pool.remove("default")
    assert pool.account("default", refresh=False).disabled          # 只停用：不删本机 CLI 的登录
    assert not [c for c in cli.calls if c[1] == "forget"]
    pool.enable("default")
    assert not pool.account("default", refresh=False).disabled


def test_adopt_needs_estimated_time_before_hard_limit(setup, monkeypatch):
    """设上限 30 分钟、预计 28 分钟：只剩 25 分钟的 VM 不接手（按预计时长判，不按时限的一半）；正式接手时再核一次。"""
    pool, cli, now = setup
    with pool.start("T4", 30, task_id="first") as run:
        run.keep_idle(60)
    with pool.db() as db:
        db.execute("UPDATE usage SET started=? WHERE name=?", (now[0] - (86400 - 25 * 60), run.name))
    with pool.start("T4", 30, task_id="short", estimated_minutes=28) as fresh:
        assert not fresh.reused
    with pool.db() as db:
        db.execute("UPDATE usage SET started=? WHERE name=?", (now[0] - (86400 - 29 * 60), run.name))
    usage = pool._usage

    def slow_usage(account):                                         # 查余额那会儿过去了 2 分钟：剩 27 分钟，不够
        now[0] += 120
        return usage(account)
    monkeypatch.setattr(pool, "_usage", slow_usage)
    assert pool.adopt("T4", task_id="late", estimated_minutes=28, max_minutes=30) is None
    monkeypatch.undo()
    with pool.db() as db:
        db.execute("UPDATE usage SET started=? WHERE name=?", (now[0] - (86400 - 29 * 60), run.name))
    again = pool.adopt("T4", task_id="ok", estimated_minutes=28, max_minutes=30)
    assert again.reused and again.name == run.name
    again.stop()


def test_shutdown_stops_login_probes_and_releases_inflight(login_pool):
    """登录后的试卡还在开卡时账号池收尾：不再试下一张卡，正在开的那台进入释放后才收最后一轮。"""
    import asyncio
    import threading
    pool, cli = login_pool
    opening, release = threading.Event(), threading.Event()
    call = cli.call

    def slow_new(account, action, *args, **kw):
        if action == "new":
            opening.set()
            release.wait(5)
        return call(account, action, *args, **kw)
    cli.call = slow_new

    async def main():
        async with pool.lifespan(interval=3600):
            login = await asyncio.to_thread(pool.begin_login)
            pool.submit_code(login.id, "good")
            assert await asyncio.to_thread(opening.wait, 5)
            threading.Timer(0.3, release.set).start()
        return login
    login = asyncio.run(main())
    assert not cli.vms                                                # 正在开的那台已经释放
    login.thread.join(5)
    assert sum(c[1] == "new" for c in cli.calls) == 1 and not cli.vms  # 收尾后没再开下一张


def test_remove_new_account_while_probing(login_pool):
    """Studio 登录新加的账号刚 done、还在后台试卡时移除（登错了马上「换一个」「取消」）：试卡的 VM 不算在跑任务，
    不再试下一张，等正在开的那台释放了再删凭据。"""
    import threading
    pool, cli = login_pool
    opening, release = threading.Event(), threading.Event()
    call = cli.call

    def slow_new(account, action, *args, **kw):
        if action == "new":
            opening.set()
            release.wait(5)
        return call(account, action, *args, **kw)
    cli.call = slow_new
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    assert opening.wait(5)                                            # 第一张卡正在开
    account = digest(cli.email)
    assert pool.login_status(login.id).state == "done"
    threading.Timer(0.3, release.set).start()
    pool.remove(account)
    login.thread.join(5)
    assert sum(c[1] == "new" for c in cli.calls) == 1 and not cli.vms  # 没再试下一张；开出来的那台释放了才删凭据
    assert [c[0] for c in cli.calls if c[1] in ("release", "forget")] == [account, account]
    assert [c[1] for c in cli.calls if c[1] in ("release", "forget")] == ["release", "forget"]
    assert account not in [a.id for a in pool.accounts(include_disabled=True)] and not pool.probing


def test_cancel_login_before_commit_and_after_done(login_pool, tmp_path):
    """cancel_login terminates an in-flight login before commit, removes newly added accounts, and keeps existing ones."""
    import threading
    import time
    pool, cli = login_pool
    account = digest(cli.email)
    # 1. Cancel before code submission: process killed, slot removed, nothing added.
    login = pool.begin_login()
    pool.cancel_login(login.id)
    assert pool.login_status(login.id).state == "failed"
    assert not cli.saved.exists() and not (pool.root / "colab" / login.slot).exists()
    with pytest.raises(ValueError):
        pool.submit_code(login.id, "good")
    # 2. Cancel after code submission while the subprocess is still verifying (before commit).
    slow_script = tmp_path / "slow_login.py"
    marker = tmp_path / "code_received"
    slow_script.write_text(
        "import pathlib, sys, time\n"
        "sys.stderr.write('https://accounts.google.com/o/oauth2/auth?slow=1\\n')\n"
        "sys.stderr.flush()\n"
        "sys.stdin.readline()\n"
        f"pathlib.Path({str(marker)!r}).write_text('ok')\n"
        "time.sleep(30)\n"
    )
    orig_script = cli.script
    cli.script = slow_script
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.02)
    assert marker.exists()
    pool.cancel_login(login.id)
    assert pool.login_status(login.id).state == "failed"
    assert account not in [a.id for a in pool.accounts(include_disabled=True)]
    assert not [c for c in cli.calls if c[1] == "new"]
    cli.script = orig_script
    # 3. Cancel after done for a newly added account (while probe is opening): stops probe and removes account.
    opening, release = threading.Event(), threading.Event()
    orig_call = cli.call

    def slow_new(acct, action, *args, **kw):
        if action == "new":
            opening.set()
            release.wait(5)
        return orig_call(acct, action, *args, **kw)
    cli.call = slow_new
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    assert opening.wait(5)
    assert pool.login_status(login.id).added is True
    threading.Timer(0.2, release.set).start()
    pool.cancel_login(login.id)
    pool.cancel_login(login.id)  # idempotent
    login.thread.join(5)
    assert not cli.vms and account not in [a.id for a in pool.accounts(include_disabled=True)]
    cli.call = orig_call
    # 4. Cancel after done for an existing account (added=False): keeps the account untouched.
    pool.register(cli.email, "已有号")
    cli.calls.clear()
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    st = finish(pool, login)
    assert st.state == "done" and st.added is False
    pool.cancel_login(login.id)
    assert not pool.account(account, refresh=False).disabled
    assert not [c for c in cli.calls if c[1] == "forget"]
    # 5. Unknown login_id is a no-op.
    pool.cancel_login("unknown")


def test_remove_waits_for_reaper_retry_after_probe_release_failure(login_pool):
    """When the probe's first release fails, remove waits until reap() confirms release in the ledger before forget."""
    import threading
    pool, cli = login_pool
    account = digest(cli.email)
    release_failed = threading.Event()
    orig_call = cli.call
    release_attempts = []

    def fail_first_release(acct, action, *args, **kw):
        if action == "release":
            release_attempts.append(acct)
            if len(release_attempts) == 1:
                release_failed.set()
                raise CliError("transient release error")
        return orig_call(acct, action, *args, **kw)
    cli.call = fail_first_release
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    assert release_failed.wait(5)
    login.thread.join(5)
    # Probe thread already exited, but the VM is still unreleased in the ledger.
    assert not pool.probing and cli.vms
    removed = threading.Event()
    errors = []

    def do_remove():
        try:
            pool.remove(account)
        except Exception as exc:
            errors.append(exc)
        finally:
            removed.set()

    t = threading.Thread(target=do_remove)
    t.start()
    # remove must stay waiting rather than failing immediately with 409.
    assert not removed.wait(0.25)
    assert not [c for c in cli.calls if c[1] == "forget"]
    # Reaper retries release and succeeds; remove then deletes credentials and removes the account.
    pool.reap()
    t.join(5)
    assert not errors and not cli.vms
    assert [c[1] for c in cli.calls if c[1] in ("release", "forget")] == ["release", "forget"]
    assert account not in [a.id for a in pool.accounts(include_disabled=True)]


def test_remove_probe_wait_timeout_keeps_credentials(login_pool, monkeypatch):
    """If the probe VM is not released before PROBE_WAIT, remove raises ValueError and keeps credentials for reaping."""
    from . import pool as pool_module
    pool, cli = login_pool
    account = digest(cli.email)
    monkeypatch.setattr(pool_module, "PROBE_WAIT", 0.25)
    orig_call = cli.call
    block_release = [True]

    def failing_release(acct, action, *args, **kw):
        if action == "release" and block_release[0]:
            raise CliError("still failing")
        return orig_call(acct, action, *args, **kw)
    cli.call = failing_release
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    login.thread.join(5)
    assert cli.vms
    with pytest.raises(ValueError, match="试开的卡还没释放完"):
        pool.remove(account)
    # Credentials are still intact and the account remains disabled so reap() can still release the VM.
    assert not [c for c in cli.calls if c[1] == "forget"]
    assert pool.account(account, refresh=False).disabled
    block_release[0] = False
    pool.reap()
    assert not cli.vms
    pool.remove(account)
    assert account not in [a.id for a in pool.accounts(include_disabled=True)]


def test_cancel_login_during_default_identity_check_prevents_commit_and_revive(login_pool, monkeypatch):
    """Cancelling while _default_identity() is running must neither save credentials nor revive a disabled account."""
    import threading
    pool, cli = login_pool
    account = digest(cli.email)
    # 1. Existing disabled account: cancel during _default_identity() must not commit or re-enable it.
    pool.register(cli.email, "停用号")
    pool.disable(account)
    checking, proceed = threading.Event(), threading.Event()

    def slow_identity_none():
        checking.set()
        proceed.wait(5)
        return None
    monkeypatch.setattr(pool, "_default_identity", slow_identity_none)
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    assert checking.wait(5)
    cancel_thread = threading.Thread(target=pool.cancel_login, args=(login.id,))
    cancel_thread.start()
    while not login.cancelled:
        threading.Event().wait(0.01)
    proceed.set()
    cancel_thread.join(5)
    assert pool.login_status(login.id).state == "failed"
    assert not cli.saved.exists()
    assert pool.account(account, refresh=False).disabled
    # 2. Disabled default account: cancel during _default_identity() must not revive default.
    pool.disable("default")
    checking.clear()
    proceed.clear()

    def slow_identity_match():
        checking.set()
        proceed.wait(5)
        return account
    monkeypatch.setattr(pool, "_default_identity", slow_identity_match)
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    assert checking.wait(5)
    cancel_thread = threading.Thread(target=pool.cancel_login, args=(login.id,))
    cancel_thread.start()
    while not login.cancelled:
        threading.Event().wait(0.01)
    proceed.set()
    cancel_thread.join(5)
    assert pool.login_status(login.id).state == "failed"
    assert pool.account("default", refresh=False).disabled


def test_probe_exits_on_disable_and_does_not_query_other_accounts(login_pool, monkeypatch):
    """Single-account probing queries only that account and exits immediately once disabled, so remove() does not time out."""
    import threading
    from . import pool as pool_module
    pool, cli = login_pool
    account = digest(cli.email)
    other = pool.register("other@example.invalid", "别的号")
    cli.balances[other] = 50
    monkeypatch.setattr(pool_module, "PROBE_WAIT", 0.35)
    other_usage_calls = []
    orig_api = cli.api

    def slow_other_usage(acct, action, *args):
        if acct == other and action == "usage":
            other_usage_calls.append(acct)
            threading.Event().wait(1.0)
        return orig_api(acct, action, *args)
    cli.api = slow_other_usage
    opening, release = threading.Event(), threading.Event()
    orig_call = cli.call

    def slow_new(acct, action, *args, **kw):
        if action == "new":
            opening.set()
            release.wait(5)
        return orig_call(acct, action, *args, **kw)
    cli.call = slow_new
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    assert opening.wait(5)
    threading.Timer(0.1, release.set).start()
    pool.remove(account)
    login.thread.join(5)
    assert not cli.vms and not pool.probing
    assert other_usage_calls == []
    assert sum(c[1] == "new" for c in cli.calls) == 1
    assert account not in [a.id for a in pool.accounts(include_disabled=True)]


def test_probe_skips_locked_usage_after_disable_during_candidate_check(login_pool, monkeypatch):
    """If the account is disabled during candidate balance check, _allocate skips the locked usage call and remove() completes."""
    import threading
    from . import pool as pool_module
    pool, cli = login_pool
    account = digest(cli.email)
    monkeypatch.setattr(pool_module, "PROBE_WAIT", 0.35)
    in_candidate_usage, resume_usage = threading.Event(), threading.Event()
    usage_calls = []
    orig_api = cli.api

    def tracked_usage(acct, action, *args):
        if acct == account and action == "usage":
            usage_calls.append(len(usage_calls) + 1)
            if len(usage_calls) == 2:
                # 1st call is _probe's GPU list; 2nd is accounts(only_account=account) inside _allocate.
                in_candidate_usage.set()
                resume_usage.wait(5)
            elif len(usage_calls) >= 3:
                # 3rd call would be the locked _usage inside _allocate; slow it so skipping it is required.
                threading.Event().wait(1.0)
        return orig_api(acct, action, *args)
    cli.api = tracked_usage
    login = pool.begin_login()
    pool.submit_code(login.id, "good")
    assert in_candidate_usage.wait(5)
    errors = []

    def do_remove():
        try:
            pool.remove(account)
        except Exception as exc:
            errors.append(exc)

    remover = threading.Thread(target=do_remove)
    remover.start()
    while pool._enabled(account):
        threading.Event().wait(0.01)
    resume_usage.set()
    remover.join(5)
    login.thread.join(5)
    assert not errors
    assert len(usage_calls) == 2
    assert not cli.vms and not pool.probing
    assert account not in [a.id for a in pool.accounts(include_disabled=True)]


def test_cli_request_timeout_and_safe_error_classification(tmp_path, monkeypatch):
    import subprocess
    from .cli import ColabCLI

    cli = ColabCLI(tmp_path, "unused")
    _, env = cli.command("default", "exec", ())
    assert env["REQUEST_TIMEOUT"] == "30"

    # Rich-wrapped HTTP 503 with signed URL split across lines must never leak any URL/signature tail.
    rich_503 = (
        "HTTPError: 503 Server Error: Service Unavailable for url:\n"
        "https://proxy.example/api/kernels?colab-runtime-proxy-token=AAA_PART1\n"
        "BBB_WRAPPED_SIGNATURE_TAIL_999\n"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(["worker"], 1, "", rich_503)
    )
    with pytest.raises(CliError) as exc:
        cli.call("default", "exec", "-s", "vm1")
    assert str(exc.value) == "colab exec exited 1 (HTTP 503)"
    for leaked in ("AAA_PART1", "BBB_WRAPPED_SIGNATURE_TAIL_999", "https://"):
        assert leaked not in str(exc.value)

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["worker"],
            timeout=90,
            stderr=b"RuntimeError: Connection was lost at https://proxy.example/k?token=SECRET123\n",
        )

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    with pytest.raises(CliError) as exc:
        cli.call("default", "exec", "-s", "vm1")
    assert str(exc.value) == "exec timed out (kernel connection lost)"
    assert "SECRET123" not in str(exc.value)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            ["worker"],
            1,
            "",
            "TimeoutError: Timeout waiting for output: no message for 30s.\n--timeout to allow longer silences.\n",
        ),
    )
    with pytest.raises(CliError) as exc:
        cli.call("default", "exec", "-s", "vm1")
    assert str(exc.value) == "colab exec exited 1 (kernel execution timed out)"

    # FileNotFoundError from colab contents API must map to a fixed label without leaking remote paths.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            ["worker"],
            1,
            "",
            "[colab] Download failed: File or directory not found: /content/secret/user_file.bin\n",
        ),
    )
    with pytest.raises(CliError) as exc:
        cli.call("default", "download", "-s", "vm1", "/content/secret/user_file.bin", "/tmp/out")
    assert str(exc.value) == "colab download exited 1 (remote file not found)"
    assert "/content/secret/user_file.bin" not in str(exc.value)

    # Unknown stderr text must not be echoed into CliError at all.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(["worker"], 1, "", "RAW_UNCLASSIFIED_SECRET_XYZ\n"),
    )
    with pytest.raises(CliError) as exc:
        cli.call("default", "exec", "-s", "vm1")
    assert str(exc.value) == "colab exec exited 1"
    assert "RAW_UNCLASSIFIED_SECRET_XYZ" not in str(exc.value)


def test_execute_restarts_kernel_only_for_replayable_upload(setup, tmp_path, monkeypatch):
    pool, cli, _ = setup
    restarts = []
    monkeypatch.setattr(
        cli, "restart_kernel", lambda account, name, timeout=30: restarts.append((account, name)),
        raising=False,
    )

    # 1. Plain execute and run_background (setup/run/pack) must never auto-restart the kernel.
    plain_attempts = []
    orig_exec = cli.execute

    def always_fail(account, name, code, timeout):
        plain_attempts.append(code)
        raise CliError("exec timed out")

    monkeypatch.setattr(cli, "execute", always_fail)
    with pool.start("T4", 30, task_id="no-restart") as run:
        with pytest.raises(CliError):
            run.execute("print(1)")
        assert len(plain_attempts) == 3 and restarts == []
        run.run_background("echo hi", str(tmp_path / "bg.exit"))
        assert len(plain_attempts) == 6 and restarts == []

        # 2. Replayable upload merge step restarts kernel before final attempt and recovers.
        merge_attempts = []

        def flaky_merge(account, name, code, timeout):
            if "Path(prefix).replace(target)" in code:
                merge_attempts.append(code)
                if len(merge_attempts) < 3:
                    raise CliError("exec timed out")
            return orig_exec(account, name, code, timeout)

        monkeypatch.setattr(cli, "execute", flaky_merge)
        src, dst = tmp_path / "in.bin", tmp_path / "out.bin"
        src.write_bytes(b"payload")
        run.upload(src, str(dst))
        assert dst.read_bytes() == b"payload"
        assert len(merge_attempts) == 3
        assert restarts == [("default", run.name)]


def test_upload_retries_transient_chunk_failure(setup, tmp_path, monkeypatch):
    pool, cli, _ = setup
    monkeypatch.setattr(run_module, "CHUNK_BYTES", 5)
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"hello-world")
    original = cli.call
    failed = []

    def flaky_upload(account, action, *args, **kwargs):
        if action == "upload" and str(args[-1]).endswith(".1") and not failed:
            failed.append(1)
            raise CliError("transient put error")
        return original(account, action, *args, **kwargs)

    monkeypatch.setattr(cli, "call", flaky_upload)
    with pool.start("T4", 30, task_id="upload-retry") as run:
        run.upload(src, str(dst))
    assert len(failed) == 1
    assert dst.read_bytes() == b"hello-world"
