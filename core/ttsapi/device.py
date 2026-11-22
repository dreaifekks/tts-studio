"""worker 进程里的推理设备：选设备、半精度、清缓存、显存峰值。只在 worker 里 import（要 import torch，gateway 不碰 torch）。

设备由 settings.device_setting(worker) 决定：cuda / mps / cpu，auto = 有 CUDA 用 cuda，Apple Silicon 用 mps，否则 cpu。
MPS 上不支持的算子由 PyTorch 自动退回 CPU（PYTORCH_ENABLE_MPS_FALLBACK=1，workers.py 启 worker 时设）；
整个引擎在 MPS 上跑不了或更慢时，用 TTSAPI_DEVICE_<引擎>=cpu 整体退回 CPU。"""
import torch

from ttsapi import settings


def pick(worker: str) -> str:
    want = settings.device_setting(worker)
    if want != "auto":
        return want
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def setup(worker: str) -> str:
    """worker 启动时调用：选好设备、设 CPU 线程数，返回设备名。"""
    if threads := settings.torch_threads(worker):
        torch.set_num_threads(threads)
    return pick(worker)


def half(device: str) -> bool:
    """CUDA 上一直用半精度；MPS 上要 TTSAPI_MPS_HALF=1；CPU 上不用。"""
    return device.startswith("cuda") or (device == "mps" and settings.MPS_HALF)


def empty_cache(device: str):
    if device.startswith("cuda"):
        torch.cuda.empty_cache()
    elif device == "mps":
        torch.mps.empty_cache()


def release(device: str):
    """一次推理做完后调用。MPS 用的是和其他程序共用的统一内存，PyTorch 缓存的显存不还给系统时，
    v4 worker 合成一句后能占到 14GB；清掉以后空闲时只剩模型本身。CUDA 上不清（显存独占，留着缓存下次更快）。"""
    if device == "mps":
        torch.mps.empty_cache()


def reset_peak(device: str):
    if device.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats()


def peak_mb(device: str) -> int:
    """这次合成的显存峰值（MB）。MPS 没有峰值统计，给的是合成后 Metal 驱动占用的内存；CPU 上是 0。"""
    if device.startswith("cuda"):
        return round(torch.cuda.max_memory_allocated() / 2**20)
    if device == "mps":
        return round(torch.mps.driver_allocated_memory() / 2**20)
    return 0


def describe(device: str) -> dict:
    return dict(device=device, half=half(device), torch_threads=torch.get_num_threads(), torch=torch.__version__)
