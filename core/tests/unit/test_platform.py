"""没有 NVIDIA 显卡的机器（macOS 的 MPS、纯 CPU）上的设备选择、内存检查、防睡眠，以及 macOS 部署用的环境变量。"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from ttsapi import osutil, settings, workers

ROOT = Path(__file__).resolve().parents[2]


def test_device_setting(monkeypatch):
    monkeypatch.delenv("TTSAPI_DEVICE", raising=False)
    monkeypatch.delenv("TTSAPI_DEVICE_V4", raising=False)
    assert settings.device_setting("v4") == "auto"
    monkeypatch.setenv("TTSAPI_DEVICE", "MPS")
    assert settings.device_setting("v4") == "mps" and settings.device_setting("scorer") == "mps"
    monkeypatch.setenv("TTSAPI_DEVICE_V4", "cpu")
    assert settings.device_setting("v4") == "cpu" and settings.device_setting("v2pro") == "mps"


def test_free_mem_without_nvidia_smi(monkeypatch):
    """没有 nvidia-smi 时比的是系统可用内存（原来会因为找不到 nvidia-smi 而起不了第二个 worker）。"""
    monkeypatch.setattr(workers.shutil, "which", lambda name: None)
    assert 0 < workers.free_vram_mb() <= psutil.virtual_memory().total // 2**20


def test_worker_env_mps_fallback(monkeypatch):
    monkeypatch.delenv("PYTORCH_ENABLE_MPS_FALLBACK", raising=False)
    assert workers.ENGINES["v2pro"].env()["PYTORCH_ENABLE_MPS_FALLBACK"] == "1"
    monkeypatch.setenv("PYTORCH_ENABLE_MPS_FALLBACK", "0")
    assert workers.SCORER.env()["PYTORCH_ENABLE_MPS_FALLBACK"] == "0"


def test_env_settings(tmp_path):
    """Settings use the home directory by default and honor explicit engine and service settings."""
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), TTSAPI_HOST="100.64.0.10",
               TTSAPI_PORT="9999", TTSAPI_WORKER_MEM_MB="8000", TTSAPI_IDLE_UNLOAD_MIN="5")
    env.pop("TTSAPI_GSV_ROOT", None)
    env.pop("TTSAPI_GSV_V5_ROOT", None)
    code = ("import json; from ttsapi import settings as s; "
            "print(json.dumps([str(s.OLD_REPO), str(s.V5_REPO), s.HOST, s.PORT, s.WORKER_VRAM_MB, "
            "s.IDLE_UNLOAD_MIN, s.CODE_DESC['fork'], s.ALLOWED_NETS]))")

    def config():
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    home_gsv = tmp_path / "GPT-SoVITS"
    nets = ["127.0.0.0/8", "::1/128", "100.64.0.0/10", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"]
    assert config() == [str(home_gsv), str(tmp_path / "GPT-SoVITS-v5"), "100.64.0.10", 9999, 8000, 5,
                        f"{home_gsv}（GPT-SoVITS 固定上游提交加补丁）", nets]
    env.update(TTSAPI_GSV_ROOT=str(tmp_path / "engine"), TTSAPI_GSV_V5_ROOT=str(tmp_path / "engine-v5"))
    assert config()[:2] == [env["TTSAPI_GSV_ROOT"], env["TTSAPI_GSV_V5_ROOT"]]


@pytest.mark.skipif(not osutil.IS_MACOS, reason="macOS 的 IOKit 电源断言")
def test_keep_awake_macos():
    osutil.keep_awake(True)
    try:
        out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True).stdout
        mine = [line for line in out.splitlines() if f"pid {os.getpid()}(" in line]
        assert any("PreventUserIdleSystemSleep" in line and "tts-api" in line for line in mine)
    finally:
        osutil.keep_awake(False)
    assert osutil._assertion is None


@pytest.mark.parametrize("fail_on", [1, 2])
def test_job_releases_power_when_saving_fails(monkeypatch, fail_on):
    """任务开头（第 1 次）或收尾（第 2 次）存盘失败：防睡眠断言照样放开，任务也移出 runs。"""
    import asyncio
    from ttsapi import jobs, power
    calls, awake = [], []
    monkeypatch.setattr(power, "keep_awake", awake.append)
    real_save = jobs.JobRun.save

    def save(self):
        calls.append(1)
        if len(calls) == fail_on + 1:  # jobs.start 里先存一次（排队）
            raise sqlite3.OperationalError("disk I/O error")
        real_save(self)
    monkeypatch.setattr(jobs.JobRun, "save", save)

    async def go():
        async def fn(run):
            pass
        job = jobs.start("candidates", "x", fn)
        task = jobs.runs[job.id].task
        with pytest.raises(sqlite3.OperationalError):
            await task
        return job.id
    job_id = asyncio.run(go())
    assert awake == [True, False] and power._holders == 0 and job_id not in jobs.runs
