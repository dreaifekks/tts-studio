"""命令行测速：python -m ttsapi.bench [--engine v4 ...] [--quick]

使用资料库中首个有可用默认参考的角色；结果写进资料库 compute/bench/<machine>/<YYYY-MM-DD-HHMMSS>.json 并打印。
最佳设备和线程数写回本机 DATA/tuning.json。"""
import argparse
import asyncio
import json
import sys
from datetime import datetime

from ttsapi import library, settings
from ttsapi.bench import bench_ref, run_bench

def bench_rel(at: datetime) -> str:
    return f"compute/bench/{settings.MACHINE_NAME}/{at:%Y-%m-%d-%H%M%S}.json"


def progress(stage: str, fraction: float):
    print(f"[{fraction:4.0%}] {stage}", file=sys.stderr, flush=True)


async def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ttsapi.bench", description="测本机每个引擎和打分器的速度，写回最快的设置")
    ap.add_argument("--engine", action="append", choices=list(settings.ENGINES),
                    help="只测这个引擎（可重复）；不给时测本机装好的全部引擎和打分器")
    ap.add_argument("--quick", action="store_true", help="快速测速：只测常用模型（默认 v2proplus），每个引擎只试现在的最佳和一个备选，不测打分器")
    args = ap.parse_args(argv)
    lib = library.open_configured()
    if lib is None:
        sys.exit(f"没有可用的资料库：{library.error() or '未配置（DATA/core.json 或 TTSAPI_LIBRARY）'}")
    started = datetime.now()
    result = await run_bench(bench_ref(), args.engine, args.quick, progress)
    rel = bench_rel(started)
    lib.put(rel, result)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"写进了资料库 {rel}", file=sys.stderr)
    return rel


if __name__ == "__main__":
    asyncio.run(main())
