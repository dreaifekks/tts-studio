"""User-facing job failure messages: allowlist-only errors, traceback-free log_tail/SSE, and safe cloud cancel."""
import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from conftest import seed_carol
from ttsapi import db, jobs, train_runner, training
from ttsapi.catalog import Catalog
from ttsapi.compute import colab_jobs
from ttsapi.compute.colab import ColabPool, RunState
from ttsapi.compute.colab.cli import AllocationUnavailable, CliError, LoginRequired
from ttsapi.compute.colab.test_colab import FakeCLI
from ttsapi.library import sha256_of
from ttsapi.models import ComboEval, Job, TrainRequest
from ttsapi.refs import RefLibrary


async def _run_failing_job(kind: str, exc: Exception, stage: str = "准备输入", note: str | None = None):
    async def fn(run):
        run.stage(stage, 0.1)
        if note is not None:
            run.failure_note = note
        raise exc

    job = jobs.start(kind, "alice", fn)
    await jobs.runs[job.id].task
    return jobs.get(job.id)


async def _collect_sse_payloads(job_id: str) -> list[dict]:
    payloads = []
    async for chunk in jobs.events(job_id):
        if chunk.startswith("event: job\ndata: "):
            raw = chunk.split("data: ", 1)[1].strip()
            payloads.append(json.loads(raw))
    return payloads


def test_job_failure_hides_traceback_and_logs_to_core_logger(clean_data, caplog):
    with caplog.at_level(logging.ERROR, logger="tts-api"):
        got = asyncio.run(_run_failing_job("train", ZeroDivisionError("internal_secret_42")))
    assert got.status == "failed"
    assert got.stage == "失败"
    assert got.error == "出错了，详细信息在主控日志里"
    assert got.log_tail[-1] == got.error
    joined = "\n".join(got.log_tail)
    for forbidden in ("Traceback", "ZeroDivisionError", "internal_secret_42", 'File "'):
        assert forbidden not in joined
        assert forbidden not in got.error
    assert "ZeroDivisionError: internal_secret_42" in caplog.text
    assert "Traceback (most recent call last):" in caplog.text


@pytest.mark.parametrize(
    ("kind", "stage", "exc", "expected"),
    [
        (
            "train",
            "上传输入（数据集、权重、代码）",
            CliError("exec timed out (websocket handshake timed out)"),
            "上传到云端时连接超时",
        ),
        (
            "train",
            "取回结果",
            CliError("download timed out"),
            "从云端取回结果时连接超时",
        ),
        (
            "eval",
            "上传输入（数据集、权重、代码）",
            CliError("colab exec exited 1 (kernel connection lost)"),
            "上传到云端时连接中断",
        ),
        (
            "train",
            "云端训练，训完自动评测",
            CliError("colab download exited 1 (remote file not found)"),
            "云端文件不存在",
        ),
        (
            "train",
            "云端训练，训完自动评测",
            CliError("colab exec exited 1 (kernel dead)"),
            "云端运行环境异常，请重试",
        ),
        (
            "train",
            "云端训练，训完自动评测",
            CliError("colab exec exited 1 (HTTP 503)"),
            "云端服务暂时不可用，请稍后重试",
        ),
        (
            "train",
            "开云端 GPU（L4）",
            AllocationUnavailable("GPU allocation unavailable"),
            "云端 GPU 开不出来",
        ),
        (
            "train",
            "开云端 GPU（L4）",
            LoginRequired("Colab login required for acc"),
            "云端账号需要重新登录",
        ),
        (
            "train",
            "云端训练，训完自动评测",
            TimeoutError("Colab VM time limit reached"),
            "云端使用时间已到上限",
        ),
        (
            "candidates",
            "生成候选",
            TimeoutError("timed out"),
            "操作超时，请重试",
        ),
        (
            "train",
            "云端训练，训完自动评测",
            RuntimeError("CUDA out of memory"),
            "显存不足，请调小参数或换显存更大的卡",
        ),
        (
            "train",
            "云端训练，训完自动评测",
            RuntimeError("Traceback (most recent call last):\n  File a.py\nValueError: bad"),
            "训练脚本出错，详细信息在主控日志里",
        ),
        (
            "candidates",
            "排队中",
            HTTPException(503, "worker-a: internal overload details"),
            "设备暂时不可用，请稍后重试",
        ),
        (
            "candidates",
            "生成候选",
            HTTPException(500, "lan-gpu: Traceback (most recent call last):\nRuntimeError: /secret/weights.ckpt"),
            "设备运行出错，详细信息在主控日志里",
        ),
        (
            "candidates",
            "排队中",
            HTTPException(409, "internal lock detail /tmp/x"),
            "请求未能完成，请重试",
        ),
    ],
)
def test_job_error_messages_for_common_categories(clean_data, kind, stage, exc, expected):
    got = asyncio.run(_run_failing_job(kind, exc, stage=stage))
    assert got.status == "failed"
    assert got.error == expected
    assert got.log_tail[-1] == expected
    assert "Traceback" not in "\n".join(got.log_tail)


def test_only_explicit_user_messages_reach_api_and_sse(clean_data, caplog):
    """Unmarked Chinese RuntimeErrors with paths/internals and remote HTTP 500 details never enter error/log_tail/SSE."""
    raw_import = RuntimeError("云端结果没能写进资料库（OSError: /Users/me/secret/results.tar）；POST /runs/import")
    tagged = RuntimeError("云端结果没能写进资料库（OSError: /Users/me/secret/results.tar）；POST /runs/import")
    tagged.user_message = "云端结果写入资料库失败，详细信息在主控日志里"
    remote_500 = HTTPException(500, "win-gpu: Traceback (most recent call last):\nKeyError: '/private/path'")

    with caplog.at_level(logging.ERROR, logger="tts-api"):
        got_raw = asyncio.run(_run_failing_job("train", raw_import, stage="写进资料库"))
        got_tagged = asyncio.run(_run_failing_job("train", tagged, stage="写进资料库"))
        got_500 = asyncio.run(_run_failing_job("candidates", remote_500, stage="生成候选"))
        got_user = asyncio.run(
            _run_failing_job("work_render", jobs.UserError("这些行还没有选中的候选：line-01"), stage="拼接、响度归一、导出")
        )

    assert got_raw.error == "训练脚本出错，详细信息在主控日志里"
    assert got_tagged.error == "云端结果写入资料库失败，详细信息在主控日志里"
    assert got_500.error == "设备运行出错，详细信息在主控日志里"
    assert got_user.error == "这些行还没有选中的候选：line-01"

    for got in (got_raw, got_tagged, got_500):
        sse = asyncio.run(_collect_sse_payloads(got.id))
        blob = json.dumps(sse, ensure_ascii=False)
        for leaked in ("/Users/me/secret", "results.tar", "POST /runs/import", "win-gpu", "/private/path", "Traceback"):
            assert leaked not in got.error
            assert leaked not in "\n".join(got.log_tail)
            assert leaked not in blob

    assert "/Users/me/secret/results.tar" in caplog.text
    assert "win-gpu: Traceback" in caplog.text


def test_follow_filters_tracebacks_from_log_tail_and_sse_while_keeping_raw_disk_log(clean_data):
    """JobRun.follow strips Python tracebacks from log_tail/SSE but keeps progress/epoch lines and raw file on disk."""
    raw_vm_log = (
        "[12:00:01] 准备数据\n"
        "SoVITS epoch 1/8\n"
        "Traceback (most recent call last):\n"
        '  File "/content/job/train_runner.py", line 215, in main\n'
        "    train(r)\n"
        "ValueError: bad tensor shape\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "/content/job/train_runner.py", line 220, in main\n'
        "    cleanup()\n"
        "RuntimeError: cleanup failed at /secret/dir\n"
        "GPT epoch 1/10\n"
    )

    async def fn(run):
        offset = run.log_path.stat().st_size if run.log_path.exists() else 0
        with open(run.log_path, "a", encoding="utf-8") as f:
            f.write(f"---- 云端任务日志 ----\n{raw_vm_log}")
        run.follow(offset)

    async def run_it():
        job = jobs.start("train", "alice", fn)
        await jobs.runs[job.id].task
        got = jobs.get(job.id)
        sse = await _collect_sse_payloads(job.id)
        return got, sse

    got, sse = asyncio.run(run_it())
    joined = "\n".join(got.log_tail)
    sse_blob = json.dumps(sse, ensure_ascii=False)
    for kept in ("---- 云端任务日志 ----", "[12:00:01] 准备数据", "SoVITS epoch 1/8", "GPT epoch 1/10"):
        assert kept in joined
        assert kept in sse_blob
    for stripped in (
        "Traceback",
        "train_runner.py",
        "ValueError: bad tensor shape",
        "During handling of the above exception",
        "RuntimeError: cleanup failed",
        "/secret/dir",
    ):
        assert stripped not in joined
        assert stripped not in sse_blob

    disk_log = (jobs.settings.JOBS_DIR / f"{got.id}.log").read_text(encoding="utf-8")
    assert "Traceback (most recent call last):" in disk_log
    assert "RuntimeError: cleanup failed at /secret/dir" in disk_log


def test_train_runner_sends_traceback_to_stderr_not_job_log(tmp_path, monkeypatch, capsys):
    """train_runner.main writes exception tracebacks to stderr (runner.out), keeping the job log file clean."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_log = tmp_path / "job.log"
    (run_dir / "runner_spec.json").write_text(
        json.dumps({"log_path": str(job_log), "req": {}}),
        encoding="utf-8",
    )

    def boom(r):
        r.stage("训练 SoVITS", 0.25)
        r.log("SoVITS epoch 1/8")
        raise RuntimeError("secret_runner_crash /private/ckpt")

    monkeypatch.setattr(train_runner, "train", boom)
    train_runner.main(run_dir)

    captured = capsys.readouterr()
    assert "Traceback (most recent call last):" in captured.err
    assert "RuntimeError: secret_runner_crash /private/ckpt" in captured.err

    log_text = job_log.read_text(encoding="utf-8")
    assert "SoVITS epoch 1/8" in log_text
    assert "Traceback" not in log_text
    assert "secret_runner_crash" not in log_text

    events = [json.loads(line) for line in (run_dir / "runner_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["t"] == "end"
    assert events[-1]["state"] == "failed"


def test_train_runner_run_script_routes_raw_output_and_traceback_to_stderr_only(tmp_path, capsys):
    """Runner.run_script keeps raw subprocess stdout/tracebacks in stderr (runner.out), only on_line logs go to job log."""
    import sys

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_log = tmp_path / "job.log"
    (run_dir / "runner_spec.json").write_text(
        json.dumps({"log_path": str(job_log), "req": {}}),
        encoding="utf-8",
    )
    script = tmp_path / "fake_s2.py"
    script.write_text(
        "print('====> Epoch: 1', flush=True)\n"
        "raise ValueError('raw_subprocess_secret')\n",
        encoding="utf-8",
    )
    runner = train_runner.Runner(run_dir)
    eng = {"python": Path(sys.executable), "repo": tmp_path}

    def on_line(line):
        if "====> Epoch: 1" in line:
            runner.log("SoVITS epoch 1/8")

    with pytest.raises(RuntimeError, match="fake_s2.py"):
        runner.run_script(eng, tmp_path, [script], {}, on_line=on_line)

    captured = capsys.readouterr()
    assert "Traceback (most recent call last):" in captured.err
    assert "ValueError: raw_subprocess_secret" in captured.err

    log_text = job_log.read_text(encoding="utf-8")
    assert "SoVITS epoch 1/8" in log_text
    assert "Traceback" not in log_text
    assert "raw_subprocess_secret" not in log_text
    assert str(script) not in log_text


def test_cloud_job_failure_reports_shutdown_and_estimated_units(lib, tmp_path, monkeypatch, caplog):
    folder = seed_carol(lib)
    tar = lib.path("datasets/SAMPLE_DS.tar")
    with tarfile.open(tar, "w") as tf:
        tf.add(folder, arcname=f"SAMPLE_DS/characters/{folder.name}")
    lib.put(
        "datasets/SAMPLE_DS.json",
        dict(lib.get("datasets/SAMPLE_DS.json"), sha256=sha256_of(tar), size=tar.stat().st_size),
    )
    cat = Catalog()
    refs = RefLibrary(cat)
    refs.register("carol-14000140")
    lib.put("characters/carol.json", dict(cat.character("carol"), default_ref="carol-14000140"))
    refs.reload()

    def fake_bundle(bundle_dir):
        p = bundle_dir / "code.bundle"
        p.write_bytes(b"bundle")
        return (
            {"code/a.bundle": p},
            dict(
                ttsapi_bundle="code/a.bundle",
                ttsapi_commit="c" * 40,
                gsv_patch="code/gsv-a.patch",
                gsv_patch_sha256="d" * 64,
                gsv_base="e" * 40,
                gsv_branch="tts-studio",
            ),
            None,
        )

    monkeypatch.setattr(colab_jobs, "bundle_code", fake_bundle)
    from ttsapi.compute.colab import run as colab_run_module

    monkeypatch.setattr(colab_run_module, "RETRY_SECONDS", 0)

    now = [1_800_000_000]
    cli = FakeCLI()

    def failing_exec(account, name, code, timeout):
        now[0] += 200  # 3 retries = 600s = 10 min on L4 (1.94/h) -> ~0.32 units
        cli.balances["default"] = 9.41
        raise CliError("exec timed out (websocket handshake timed out)")

    cli.execute = failing_exec
    pool = ColabPool(tmp_path / "pool", cli=cli, clock=lambda: now[0])
    req = TrainRequest(slug="carol-colab", dataset_id="SAMPLE_DS.sp0001", engine="v2proplus")

    async def run_it():
        job = jobs.start(
            "train",
            req.slug,
            lambda run: colab_jobs.run_colab(
                run,
                "train",
                req,
                gpu="L4",
                estimated_minutes=40,
                approved=False,
                pool=pool,
                catalog=cat,
                refs=refs,
            ),
        )
        await jobs.runs[job.id].task
        return jobs.get(job.id)

    with caplog.at_level(logging.ERROR, logger="tts-api"):
        got = asyncio.run(run_it())

    assert not cli.vms
    assert got.status == "failed"
    # Immediately after stop, late_checked=0 so cost is reported as estimated with "约".
    assert got.error == "上传到云端时连接超时（已关机，约用了 0.32 单元）"
    assert got.log_tail[-1] == got.error
    assert "Traceback" not in "\n".join(got.log_tail)
    assert "CliError: exec timed out (websocket handshake timed out)" in caplog.text


def test_shutdown_note_settled_estimated_adopted_and_db_error(tmp_path):
    now = [1_800_000_000]
    cli = FakeCLI()
    pool = ColabPool(tmp_path / "pool", cli=cli, clock=lambda: now[0])

    # 1. Unsettled (late_checked=0) uses duration * rate with "约".
    vm = pool.start("L4", 30, task_id="job-est")
    now[0] += 600  # 10 min on L4 (1.94/h) -> 0.32 units
    vm.stop()
    assert colab_jobs._shutdown_note(pool, vm, "job-est") == "已关机，约用了 0.32 单元"

    # 2. Settled (late_checked=1) uses exact actual_units including 0 without "约".
    with pool.db() as conn:
        conn.execute("UPDATE usage SET actual_units=?, late_checked=1 WHERE name=?", (0.59, vm.name))
    assert colab_jobs._shutdown_note(pool, vm, "job-est") == "已关机，用了 0.59 单元"

    with pool.db() as conn:
        conn.execute("UPDATE usage SET actual_units=?, late_checked=1 WHERE name=?", (0.0, vm.name))
    assert colab_jobs._shutdown_note(pool, vm, "job-est") == "已关机，用了 0 单元"

    # 3. Very short unsettled run (<0.005 units) omits unit count.
    vm2 = pool.start("T4", 30, task_id="job-tiny")
    now[0] += 5
    vm2.stop()
    assert colab_jobs._shutdown_note(pool, vm2, "job-tiny") == "已关机"

    # 4. Handoff / adopted by another task (task_id != job_id) or not stopped -> None.
    vm3 = pool.start("L4", 30, task_id="job-first")
    assert colab_jobs._shutdown_note(pool, vm3, "job-first") is None
    now[0] += 600
    vm3.stop()
    with pool.db() as conn:
        conn.execute("UPDATE usage SET task_id=?, adopted=? WHERE name=?", ("job-second", now[0] - 100, vm3.name))
    assert colab_jobs._shutdown_note(pool, vm3, "job-first") is None

    # 5. Ledger read failure or missing db -> None (never claims "已关机" from vm.closed).
    class BrokenPool:
        def db(self):
            raise sqlite3.OperationalError("database is locked")

    fake_vm = SimpleNamespace(name="vm-x", stopped=True, closed=True)
    assert colab_jobs._shutdown_note(BrokenPool(), fake_vm, "job-x") is None
    assert colab_jobs._shutdown_note(SimpleNamespace(), fake_vm, "job-x") is None

    # 6. JobRun without failure_note formats cleanly without trailing parentheses.
    dummy_run = jobs.JobRun(
        Job(id="j1", kind="train", target="x", status="running", progress=0, stage="上传输入", created=""),
        0.0,
    )
    assert jobs.format_error(CliError("exec timed out"), dummy_run) == "上传到云端时连接超时"


def test_user_cancel_stays_cancelled_when_vm_stop_fails(clean_data, caplog):
    """If vm.stop() fails during user cancellation, job stays cancelled and release error goes to core log."""
    started = threading.Event()
    release = threading.Event()

    class FailingStopVM:
        name = "vm-cancel-503"
        pending_result = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def stop(self):
            release.set()
            raise CliError("colab release exited 1 (HTTP 503)")

    def blocking_work():
        started.set()
        release.wait(timeout=5)
        time.sleep(0.05)

    vm = FailingStopVM()

    async def fn(run):
        run.stage("云端训练，训完自动评测", 0.3)
        await colab_jobs.on_vm_thread(vm, blocking_work)

    async def run_it():
        job = jobs.start("train", "alice", fn)
        run = jobs.runs[job.id]
        while not started.is_set() and not run.task.done():
            await asyncio.sleep(0.01)
        assert started.is_set()
        await jobs.cancel(job.id)
        return jobs.get(job.id)

    with caplog.at_level(logging.ERROR, logger="tts-api"):
        got = asyncio.run(run_it())

    assert got.status == "cancelled"
    assert got.stage == "已取消"
    assert got.error is None
    assert "failed to release Colab VM vm-cancel-503" in caplog.text
    assert "HTTP 503" in caplog.text


def test_follow_allowlist_blocks_multiline_exceptions_and_keeps_progress_after_truncated_traceback(clean_data):
    """JobRun.follow uses a strict allowlist: unindented exception continuations and paths never enter log_tail/SSE,
    while progress/stage lines after a truncated traceback are preserved."""
    raw_log = (
        "run carol-20261121-010000: 20 句，2.5 分钟，数据集 /Users/secret/datasets/carol.list\n"
        "[12:00:01] 5/6 训练 SoVITS（8 轮）\n"
        "SoVITS epoch 1/8\n"
        "Traceback (most recent call last):\n"
        '  File "/content/tts-api/ttsapi/train_runner.py", line 228, in main\n'
        "    train(r)\n"
        "RuntimeError: first line of exception\n"
        "unindented continuation line with /private/weights.ckpt\n"
        "another stray diagnostic line\n"
        "Traceback (most recent call last):\n"
        '  File "/content/tts-api/ttsapi/train_runner.py", line 230, in main\n'
        "    truncated_without_summary()\n"
        "GPT epoch 1/10\n"
        "[12:00:05] 6/6 训练 GPT（10 轮）\n"
        "---- 云端任务日志：carol-v2proplus-s8g15-abc123 ----\n"
        "已取消，训练进程已结束\n"
    )

    async def fn(run):
        offset = run.log_path.stat().st_size if run.log_path.exists() else 0
        with open(run.log_path, "a", encoding="utf-8") as f:
            f.write(raw_log)
        run.follow(offset)

    async def run_it():
        job = jobs.start("train", "carol", fn)
        await jobs.runs[job.id].task
        return jobs.get(job.id), await _collect_sse_payloads(job.id)

    got, sse = asyncio.run(run_it())
    joined = "\n".join(got.log_tail)
    sse_blob = json.dumps(sse, ensure_ascii=False)

    for kept in (
        "[12:00:01] 5/6 训练 SoVITS（8 轮）",
        "SoVITS epoch 1/8",
        "GPT epoch 1/10",
        "[12:00:05] 6/6 训练 GPT（10 轮）",
        "---- 云端任务日志：carol-v2proplus-s8g15-abc123 ----",
        "已取消，训练进程已结束",
    ):
        assert kept in joined
        assert kept in sse_blob

    for blocked in (
        "/Users/secret/datasets/carol.list",
        "Traceback",
        "RuntimeError: first line",
        "unindented continuation line",
        "/private/weights.ckpt",
        "another stray diagnostic line",
        "truncated_without_summary",
    ):
        assert blocked not in joined
        assert blocked not in sse_blob

    disk_log = (jobs.settings.JOBS_DIR / f"{got.id}.log").read_text(encoding="utf-8")
    assert "unindented continuation line with /private/weights.ckpt" in disk_log
    assert "truncated_without_summary()" in disk_log


def test_vm_pack_and_collect_preserve_runner_out_in_local_diagnostics_without_follow(lib, tmp_path):
    """vm_pack.py bundles training/<run>/runner.out for failed runs (both full and ONLY modes);
    colab_jobs.collect places it into work/training/<run>/runner.out without leaking it into log_tail/SSE."""
    core_root = Path(__file__).resolve().parents[2]
    vm_root = tmp_path / "vm"
    content = vm_root / "content"
    job_dir = content / "job"
    api_dir = content / "tts-api"
    data_dir = api_dir / "data"
    train_dir = api_dir / "training"
    lib_dir = content / "library"
    for d in (job_dir, data_dir / "jobs", train_dir / "carol-fail-run", train_dir / "other-run", lib_dir):
        d.mkdir(parents=True, exist_ok=True)

    for script in ("vm_lib.py", "vm_pack.py"):
        src_text = (core_root / "colab" / script).read_text(encoding="utf-8")
        (job_dir / script).write_text(src_text.replace("/content", str(content)), encoding="utf-8")

    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "run_id": "train-fail-01",
                "tz": "<+0900>-09:00",
                "inputs": str(content / "inputs"),
                "tasks": [
                    {"kind": "train", "job_id": "train-fail-01"},
                    {"kind": "train", "job_id": "train-other-02"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "summary.json").write_text(
        json.dumps([{"index": 0, "job": "train-fail-01", "status": "failed", "error": "runner failed"}]),
        encoding="utf-8",
    )
    (data_dir / "jobs" / "train-fail-01.log").write_text(
        "[12:00:01] 5/6 训练 SoVITS（8 轮）\nSoVITS epoch 1/8\n",
        encoding="utf-8",
    )
    runner_tb = (
        "Traceback (most recent call last):\n"
        '  File "/content/tts-api/ttsapi/train_runner.py", line 228, in main\n'
        "    train(r)\n"
        "RuntimeError: raw_vm_crash_secret /content/private/ckpt\n"
    )
    (train_dir / "carol-fail-run" / "runner.out").write_text(runner_tb, encoding="utf-8")
    (train_dir / "other-run" / "runner.out").write_text("other task runner.out\n", encoding="utf-8")

    conn = sqlite3.connect(data_dir / "tts.db")
    conn.executescript(db.SCHEMA)
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?)",
        (
            "train-fail-01",
            time.time(),
            json.dumps(
                {
                    "id": "train-fail-01",
                    "kind": "train",
                    "target": "carol",
                    "status": "failed",
                    "train_result": {"run_name": "carol-fail-run"},
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?)",
        (
            "train-other-02",
            time.time(),
            json.dumps(
                {
                    "id": "train-other-02",
                    "kind": "train",
                    "target": "other",
                    "status": "failed",
                    "train_result": {"run_name": "other-run"},
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    # 1. Full pack (single cloud job) includes runner.out.
    full_tar = tmp_path / "full.tar"
    subprocess.run([sys.executable, str(job_dir / "vm_pack.py"), str(full_tar)], check=True)
    with tarfile.open(full_tar) as tf:
        full_names = set(tf.getnames())
    assert "training/carol-fail-run/runner.out" in full_names
    assert "training/other-run/runner.out" in full_names

    # 2. ONLY pack (sweep single group) includes only the failed task's runner.out via tts.db even without run.json.
    only_tar = tmp_path / "only.tar"
    subprocess.run([sys.executable, str(job_dir / "vm_pack.py"), str(only_tar), "0"], check=True)
    with tarfile.open(only_tar) as tf:
        only_names = set(tf.getnames())
    assert "training/carol-fail-run/runner.out" in only_names
    assert "training/other-run/runner.out" not in only_names

    # 3. collect() moves runner.out to local diagnostic dir and keeps it out of vm_log / follow() / SSE.
    work = tmp_path / "work"
    cat = Catalog()
    refs = RefLibrary(cat)
    _, _, _, vm_log = colab_jobs.collect(only_tar, work, "train-fail-01", cat, refs)
    diag_out = work / "training" / "carol-fail-run" / "runner.out"
    assert diag_out.exists()
    assert "RuntimeError: raw_vm_crash_secret /content/private/ckpt" in diag_out.read_text(encoding="utf-8")
    assert "raw_vm_crash_secret" not in vm_log

    async def fn(run):
        offset = run.log_path.stat().st_size if run.log_path.exists() else 0
        with open(run.log_path, "a", encoding="utf-8") as f:
            f.write(f"---- 云端任务日志 ----\n{vm_log.rstrip()}\n")
        run.follow(offset)

    async def run_it():
        job = jobs.start("train", "carol", fn)
        await jobs.runs[job.id].task
        return jobs.get(job.id), await _collect_sse_payloads(job.id)

    got, sse = asyncio.run(run_it())
    joined = "\n".join(got.log_tail)
    sse_blob = json.dumps(sse, ensure_ascii=False)
    assert "SoVITS epoch 1/8" in joined
    assert "raw_vm_crash_secret" not in joined
    assert "raw_vm_crash_secret" not in sse_blob
    assert "Traceback" not in sse_blob


def test_shutdown_note_settled_zero_on_adopted_vm(tmp_path):
    """An adopted (warm-reused) VM settled with actual_units=0 shows 0 units, not a duration estimate."""
    now = [1_800_000_000]
    cli = FakeCLI()
    pool = ColabPool(tmp_path / "pool", cli=cli, clock=lambda: now[0])

    # Task 1 starts L4 and keeps it idle for reuse.
    vm1 = pool.start("L4", 120, task_id="job-first")
    now[0] += 600
    assert vm1.keep_idle(30)

    # Task 2 adopts the same L4, runs 30 minutes with no balance debit, stops, and settles via reap().
    vm2 = pool.start("L4", 120, task_id="job-adopted-zero")
    assert vm2.reused is True
    now[0] += 1800  # 30 min on L4 (1.94/h) would estimate 0.97 units
    vm2.stop()
    now[0] += 1200  # past late-debit settlement window
    pool.reap()

    with pool.db() as conn:
        row = conn.execute(
            "SELECT task_id, adopted, actual_units, late_checked FROM usage WHERE name=?",
            (vm2.name,),
        ).fetchone()
    assert row["task_id"] == "job-adopted-zero"
    assert row["adopted"] is not None
    assert row["late_checked"] == 1
    assert row["actual_units"] == 0.0
    assert colab_jobs._shutdown_note(pool, vm2, "job-adopted-zero") == "已关机，用了 0 单元"

    # When settled actual_units > 0 on an adopted VM, still estimate this task's share with "约".
    with pool.db() as conn:
        conn.execute("UPDATE usage SET actual_units=? WHERE name=?", (1.50, vm2.name))
    assert colab_jobs._shutdown_note(pool, vm2, "job-adopted-zero") == "已关机，约用了 0.97 单元"


def test_late_stage_callback_ignored_after_terminal_status(clean_data):
    """Once a job is cancelled, failed, or succeeded, late run.stage() calls cannot overwrite stage or DB."""
    async def run_cases():
        # Succeeded
        done_job = jobs.start("train", "carol", lambda run: asyncio.sleep(0))
        done_run = jobs.runs[done_job.id]
        await done_run.task
        done_run.stage("取回结果", 0.9)
        assert jobs.get(done_job.id).stage == "完成"
        assert jobs.get(done_job.id).progress == 1.0

        # Failed
        async def fail_fn(run):
            raise jobs.UserError("失败原因")

        fail_job = jobs.start("train", "carol", fail_fn)
        fail_run = jobs.runs[fail_job.id]
        await fail_run.task
        fail_run.stage("取回结果", 0.9)
        assert jobs.get(fail_job.id).stage == "失败"

        # Cancelled
        started = asyncio.Event()

        async def wait_fn(run):
            run.stage("云端训练，训完自动评测", 0.2)
            started.set()
            await asyncio.sleep(60)

        cancel_job = jobs.start("train", "carol", wait_fn)
        cancel_run = jobs.runs[cancel_job.id]
        await started.wait()
        await jobs.cancel(cancel_job.id)
        cancel_run.stage("取回结果", 0.9)
        got = jobs.get(cancel_job.id)
        sse = await _collect_sse_payloads(cancel_job.id)
        assert got.status == "cancelled"
        assert got.stage == "已取消"
        assert all(ev["stage"] != "取回结果" for ev in sse)

    asyncio.run(run_cases())


def test_cancel_with_stop_503_stops_worker_before_download_and_keeps_cancelled_stage(lib, tmp_path, monkeypatch):
    """After user cancel when vm.stop() fails with 503, the worker thread stops before pack/download/import
    and late stage callbacks never overwrite '已取消' in DB or SSE."""
    folder = seed_carol(lib)
    tar = lib.path("datasets/SAMPLE_DS.tar")
    with tarfile.open(tar, "w") as tf:
        tf.add(folder, arcname=f"SAMPLE_DS/characters/{folder.name}")
    lib.put(
        "datasets/SAMPLE_DS.json",
        dict(lib.get("datasets/SAMPLE_DS.json"), sha256=sha256_of(tar), size=tar.stat().st_size),
    )
    cat = Catalog()
    refs = RefLibrary(cat)
    refs.register("carol-14000140")
    lib.put("characters/carol.json", dict(cat.character("carol"), default_ref="carol-14000140"))
    refs.reload()

    def fake_bundle(bundle_dir):
        p = bundle_dir / "code.bundle"
        p.write_bytes(b"bundle")
        return (
            {"code/a.bundle": p},
            dict(
                ttsapi_bundle="code/a.bundle",
                ttsapi_commit="c" * 40,
                gsv_patch="code/gsv-a.patch",
                gsv_patch_sha256="d" * 64,
                gsv_base="e" * 40,
                gsv_branch="tts-studio",
            ),
            None,
        )

    monkeypatch.setattr(colab_jobs, "bundle_code", fake_bundle)

    in_vm_run = threading.Event()
    stop_called = threading.Event()
    worker_done = threading.Event()
    commands: list[str] = []
    download_called = False
    collect_called = False

    def spy_collect(*args, **kwargs):
        nonlocal collect_called
        collect_called = True
        raise AssertionError("collect should not run after cancel")

    monkeypatch.setattr(colab_jobs, "collect", spy_collect)

    class SlowStopFailingVM:
        name = "vm-cancel-late-stage"
        account_id = "default"
        pending_result = None
        reused = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            worker_done.set()
            return False

        def upload(self, local, remote):
            pass

        def run_background(self, cmd, marker, heartbeat=None):
            commands.append(cmd)

        def wait(self):
            if commands and "vm_run.py" in commands[-1]:
                in_vm_run.set()
                stop_called.wait(timeout=5)
                time.sleep(0.05)
            return RunState.DONE

        def download(self, remote, local, timeout=180):
            nonlocal download_called
            download_called = True

        def stop(self):
            stop_called.set()
            raise CliError("colab release exited 1 (HTTP 503)")

    class FakePool:
        def __init__(self, vm):
            self.vm = vm

        def start(self, *args, **kwargs):
            return self.vm

    vm = SlowStopFailingVM()
    req = TrainRequest(slug="carol-colab", dataset_id="SAMPLE_DS.sp0001", engine="v2proplus")

    async def run_it():
        job = jobs.start(
            "train",
            req.slug,
            lambda run: colab_jobs.run_colab(
                run,
                "train",
                req,
                gpu="L4",
                estimated_minutes=40,
                approved=False,
                pool=FakePool(vm),
                catalog=cat,
                refs=refs,
            ),
        )
        run = jobs.runs[job.id]
        sse_task = asyncio.create_task(_collect_sse_payloads(job.id))
        await asyncio.to_thread(in_vm_run.wait, 5)
        assert in_vm_run.is_set()
        await jobs.cancel(job.id)
        await asyncio.to_thread(worker_done.wait, 5)
        await asyncio.sleep(0.05)  # flush any pending call_soon_threadsafe callbacks
        sse = await sse_task
        return jobs.get(job.id), sse, run

    got, sse, _ = asyncio.run(run_it())
    assert worker_done.is_set()
    assert got.status == "cancelled"
    assert got.stage == "已取消"
    assert sse[-1]["status"] == "cancelled"
    assert sse[-1]["stage"] == "已取消"
    assert all(ev["stage"] != "取回结果" for ev in sse)
    assert not any("vm_pack.py" in c for c in commands)
    assert not download_called
    assert not collect_called


def test_follow_keeps_real_evaluate_score_lines_and_blocks_fake_eval_paths(clean_data, monkeypatch):
    """JobRun.follow preserves real training.evaluate() per-combo score lines in log_tail/SSE
    (used by cloud eval and sweep logs) while still blocking eval-prefixed lines with paths or raw errors."""
    async def fake_eval_combo(engine, slug, gpt, sovits, ref, eval_set, n, version=None):
        score = 92.5 if "e10" in gpt else 88.1
        return ComboEval(
            gpt=gpt,
            sovits=sovits,
            n=6,
            cer_median=0.04,
            cer_mean=0.05,
            cer_max=0.11,
            spk_sim_median=0.89,
            spk_sim_mean=0.88,
            spk_sim_min=0.82,
            score_median=score,
            score_mean=score - 0.4,
            score_min=score - 3.0,
            per_sentence=[],
        )

    monkeypatch.setattr(training, "eval_combo", fake_eval_combo)
    fake_catalog = SimpleNamespace(record_eval=lambda *args, **kwargs: None)
    fake_refs = SimpleNamespace(index={"carol-ref": {"id": "carol-ref"}})

    async def run_it():
        # 1. Produce real log lines via training.evaluate() on a VM-like job.
        async def vm_eval_fn(run):
            await training.evaluate(
                run,
                "carol",
                "v2proplus",
                [("carol-e10.ckpt", "carol_e8_s64.pth"), ("carol-e5.ckpt", "carol_e4_s32.pth")],
                "carol-ref",
                fake_catalog,
                fake_refs,
                training.settings.EVAL_DEFAULT_SET,
                2,
            )

        vm_job = jobs.start("train", "carol", vm_eval_fn)
        await jobs.runs[vm_job.id].task
        assert jobs.get(vm_job.id).status == "succeeded"
        vm_log = (jobs.settings.JOBS_DIR / f"{vm_job.id}.log").read_text(encoding="utf-8")

        # 2. Replay vm_log through JobRun.follow() as cloud/sweep import does, plus fake unsafe eval lines.
        async def control_fn(run):
            offset = run.log_path.stat().st_size if run.log_path.exists() else 0
            with open(run.log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"---- 云端任务日志：carol-v2proplus-s8g10-abc123 ----\n"
                    f"{vm_log}"
                    "eval /secret/a.ckpt + b.pth: {'gpt': '/secret/a.ckpt', 'score_median': 90.0}\n"
                    "eval a.ckpt + b.pth: RuntimeError: internal failure\n"
                )
            run.follow(offset)

        ctrl_job = jobs.start("train", "carol", control_fn)
        await jobs.runs[ctrl_job.id].task
        return jobs.get(ctrl_job.id), await _collect_sse_payloads(ctrl_job.id)

    got, sse = asyncio.run(run_it())
    joined = "\n".join(got.log_tail)
    sse_blob = json.dumps(sse, ensure_ascii=False)

    assert "eval carol-e10.ckpt + carol_e8_s64.pth:" in joined
    assert "'score_median': 92.5" in joined
    assert "eval carol-e5.ckpt + carol_e4_s32.pth:" in joined
    assert "'score_median': 88.1" in joined
    assert "'score_median': 92.5" in sse_blob
    assert "'score_median': 88.1" in sse_blob
    assert "/secret/a.ckpt" not in joined
    assert "/secret/a.ckpt" not in sse_blob
    assert "RuntimeError: internal failure" not in joined
    assert "RuntimeError: internal failure" not in sse_blob
