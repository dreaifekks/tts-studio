"""测速选出来的每个 worker（引擎名或 scorer）的最佳设备和线程数，存在本机 settings.TUNING_FILE。
worker 进程启动时由 settings.device_setting / torch_threads 读（优先于 TTSAPI_DEVICE_* / TTSAPI_TORCH_THREADS），
所以写回后要重启这个 worker（save 之后调用方关掉正在跑的 worker，下次按需启动就用新设置）。"""
import json

from ttsapi import settings


def load() -> dict:
    return json.loads(settings.TUNING_FILE.read_text(encoding="utf-8")) if settings.TUNING_FILE.exists() else {}


def save(worker: str, device: str, threads: int = None):
    """写回一个 worker 的最佳设置；threads 为 None 表示不设（GPU 上不看线程数）。"""
    cur = load()
    cur[worker] = {k: v for k, v in dict(device=device, threads=threads).items() if v is not None}
    settings.TUNING_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.TUNING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(settings.TUNING_FILE)
