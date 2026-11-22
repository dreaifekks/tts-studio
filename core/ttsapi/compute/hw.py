"""本机硬件：CPU 型号和核数、内存、可用加速（cuda / mps / cpu）、torch 版本。gateway 进程里调用，不 import torch。"""
import importlib.metadata
import platform
import shutil
import subprocess

import psutil


def _cpu_model() -> str:
    if platform.system() == "Darwin":
        return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    if platform.system() == "Linux":
        for line in open("/proc/cpuinfo", encoding="utf-8"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor()


def _gpu() -> str:
    """NVIDIA 显卡名（nvidia-smi 的第一块），没有是空字符串。"""
    if not shutil.which("nvidia-smi"):
        return ""
    out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True)
    return out.stdout.splitlines()[0].strip() if out.returncode == 0 and out.stdout.strip() else ""


def hardware() -> dict:
    gpu = _gpu()
    apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    try:
        torch = importlib.metadata.version("torch")
    except importlib.metadata.PackageNotFoundError:
        torch = None
    return dict(cpu=_cpu_model(), cores=psutil.cpu_count(logical=True),
                ram_gb=round(psutil.virtual_memory().total / 2**30), accel="cuda" if gpu else "mps" if apple else "cpu",
                gpu=gpu or None, os=platform.system().lower(), torch=torch)
