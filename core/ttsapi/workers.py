"""GPU worker 进程的启停和调用。gateway 自己不碰 GPU。"""
import asyncio
import logging
import os
import shutil
import subprocess
import time
from typing import Optional

import httpx
import psutil
from fastapi import HTTPException

from ttsapi import settings
from ttsapi.osutil import NO_WINDOW, child_env, cuda_lib_dirs

log = logging.getLogger("tts-api")

# 训练占用 GPU 时为正在训练的任务 id；这期间推理请求一律 503
training_job: Optional[str] = None


def free_vram_mb():
    """新 worker 能用的空闲显存（MB）。没有 NVIDIA 显卡时（macOS 的统一内存、纯 CPU）是系统可用内存。"""
    if not shutil.which("nvidia-smi"):
        return psutil.virtual_memory().available // 2**20
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return int(out.split()[0])


class Worker:
    def __init__(self, name: str, module: str, cfg: dict, args: list, extra_env: Optional[dict] = None):
        """extra_env：额外的环境变量（测速的临时 worker 用来指定设备、线程数和端口平移）。端口在 cfg["port"]。"""
        self.name, self.module, self.cfg, self.args = name, module, cfg, args
        self.extra_env = extra_env or {}
        self.url = f"http://127.0.0.1:{cfg['port']}"
        self.proc: Optional[subprocess.Popen] = None
        self.busy = 0
        self.last_used = time.time()
        self.start_lock = asyncio.Lock()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def make_room(self):
        # 显存是和游戏等共用的，挤满后 WDDM 会换页到内存，推理慢十几倍；先关掉最久没用的空闲 worker
        idle = sorted((w for w in ALL if w is not self and w.running and not w.busy), key=lambda w: w.last_used)
        for w in idle:
            if free_vram_mb() >= settings.WORKER_VRAM_MB:
                break
            w.stop()

    def env(self) -> dict:
        return child_env(
            [self.cfg["repo"], settings.ROOT], [settings.FFMPEG_BIN, self.cfg["python"].parent],
            cuda_lib_dirs(self.cfg["python"]),
            PYTHONPYCACHEPREFIX=settings.RUNTIME / "pycache",
            NUMBA_CACHE_DIR=settings.RUNTIME / "numba",
            PYTHONIOENCODING="utf-8",
            PYTHONUNBUFFERED="1",
            # MPS 上没实现的算子自动退回 CPU（只对 mps 设备起作用）；外面设了就用外面的
            PYTORCH_ENABLE_MPS_FALLBACK=os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK", "1"),
            **self.extra_env,
        )

    async def ensure(self):
        async with self.start_lock:
            if self.running:
                return
            self.make_room()
            env = self.env()
            log_path = settings.LOG_DIR / f"worker-{self.name}.log"
            if log_path.exists() and log_path.stat().st_size > 20 * 2**20:
                log_path.replace(log_path.with_suffix(".log.old"))
            t = time.time()
            log.info("starting worker %s", self.name)
            with open(log_path, "ab") as logf:
                proc = self.proc = subprocess.Popen(
                    [str(self.cfg["python"]), "-m", self.module, *self.args, str(os.getpid())],
                    cwd=settings.ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT,
                    creationflags=NO_WINDOW,
                )
            async with httpx.AsyncClient() as client:
                while True:
                    if proc.poll() is not None:
                        raise RuntimeError(f"worker {self.name} 启动失败，见 logs/worker-{self.name}.log")
                    try:
                        await client.get(self.url + "/health", timeout=2)
                        break
                    except httpx.TransportError:
                        await asyncio.sleep(1)
            log.info("worker %s ready in %.1fs", self.name, time.time() - t)

    def stop(self, proc: Optional[subprocess.Popen] = None):
        """停掉 worker。传了 proc 时只在它仍是当前进程时才停，免得并发请求把别人刚重启的进程又杀掉。"""
        proc = proc or self.proc
        if proc is None or proc is not self.proc:
            return
        if proc.poll() is None:
            log.info("stopping worker %s", self.name)
            proc.kill()
            proc.wait()
        self.proc = None

    async def call(self, path: str, payload: dict):
        if training_job:
            raise HTTPException(503, f"GPU 正在训练（任务 {training_job}），推理暂停，训练结束后自动恢复",
                                headers={"Retry-After": "120"})
        # busy 覆盖启动阶段，空闲卸载和腾显存都不会动正在启动或使用中的 worker
        self.busy += 1
        try:
            # 睡眠唤醒后 CUDA 上下文可能失效，worker 崩了或报 CUDA 错误时重启一次再试
            for attempt in (0, 1):
                await self.ensure()
                proc = self.proc
                try:
                    async with httpx.AsyncClient(timeout=900) as client:
                        r = await client.post(self.url + path, json=payload)
                except httpx.TransportError:
                    if attempt:
                        raise
                    self.stop(proc)
                    continue
                if r.status_code == 500 and "CUDA" in r.text and not attempt:
                    log.warning("worker %s CUDA error, restarting: %s", self.name, r.text)
                    self.stop(proc)
                    continue
                if r.status_code != 200:
                    raise HTTPException(500, f"{self.name}: {r.json()['error']}")
                return r.json()
        finally:
            self.busy -= 1
            self.last_used = time.time()

    def status(self):
        return dict(running=self.running, pid=self.proc.pid if self.running else None, busy=self.busy,
                    idle_sec=round(time.time() - self.last_used))


ENGINES = {name: Worker(name, "ttsapi.engine_worker", cfg, [name]) for name, cfg in settings.ENGINES.items()}
SCORER = Worker("scorer", "ttsapi.scorer_worker", settings.SCORER, [])
ALL = [*ENGINES.values(), SCORER]


def unload_idle():
    stopped = []
    for w in ALL:
        if w.running and not w.busy:
            w.stop()
            stopped.append(w.name)
    return stopped


async def unload_all_for_training(job_id: str):
    """训练开始：先挡住新的推理请求，等正在跑的推理做完，再关掉所有 worker 把显存让给训练。"""
    global training_job
    training_job = job_id
    while any(w.busy for w in ALL):
        await asyncio.sleep(1)
    for w in ALL:
        w.stop()


def end_training():
    global training_job
    training_job = None
