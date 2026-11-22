"""Colab VM 上 vm_setup.py / vm_run.py / vm_pack.py 共用：路径、作业配置、时间线、跑命令。

VM 上的布局：
  /content/job/          本目录的脚本、job.json、timeline.jsonl、gateway.out、各脚本的日志、summary.json
  <inputs>/              控制端上传的输入（job.json 的 inputs，见 ttsapi/compute/colab/job.py）：
                           library/  输入资料库（控制端资料库里这次作业用到的子集，新的 library.json）
                           code/     tts-core 的 git bundle 和 GPT-SoVITS 补丁
  /content/library/      VM 本地的资料库（vm_setup 从 <inputs>/library 移过来，vm_run 让 gateway 认领成本机管理）
  /content/GPTSoVits/    GPT-SoVITS（固定上游提交加补丁）+ .venv + 底模（HF）+ datasets/.unpacked/（gateway 解压的数据集）
  /content/tts-api/      tts-core（控制端已提交的 HEAD 里 tts-studio 的 core/，放在根上）；data/ 是 VM gateway 的 tts.db 和任务日志
不用 Drive、rclone：输入由控制端上传，结果由控制端下载 vm_pack.py 打的 tar。
"""
import json
import os
import subprocess
import time
from pathlib import Path

JOB = Path("/content/job")
CFG = json.loads((JOB / "job.json").read_text(encoding="utf-8"))
# 按主控的时区记时间（VM 是 UTC）：本进程的时间线，和继承环境变量的 gateway（run 名、任务日志）、训练进程
os.environ["TZ"] = CFG["tz"]
time.tzset()
INPUTS = Path(CFG["inputs"])
LIB = Path("/content/library")
GSV = Path("/content/GPTSoVits")
GSV_UPSTREAM = "https://github.com/RVC-Boss/GPT-SoVITS.git"
GSV_V5 = Path("/content/GPTSoVits-v5")
GSV_V5_COMMIT = "6d51ffceafbf240145619af4c92408a6e54b7924"
V5_ENGINES = frozenset({"v5turbo", "v5dev"})
API = Path("/content/tts-api")
DATA = API / "data"
PY = GSV / ".venv" / "bin" / "python"
PY_V5 = GSV_V5 / ".venv" / "bin" / "python"
TL = JOB / "timeline.jsonl"
_t0 = {}


def wants_v5(engines=None) -> bool:
    """Return True when the requested engine list includes v5turbo or v5dev."""
    return bool(set(CFG.get("engines", ()) if engines is None else engines) & V5_ENGINES)


def mark(step, phase="end", **kw):
    """时间线：phase=start/end 成对，end 带耗时 sec；其他事件 phase=event。"""
    now = time.time()
    if phase == "start":
        _t0[step] = now
    rec = dict(t=round(now, 3), ts=time.strftime("%H:%M:%S"), step=step, phase=phase, **kw)
    if phase == "end" and step in _t0:
        rec["sec"] = round(now - _t0[step], 1)
    with open(TL, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[{rec['ts']}] {step} {phase} {kw} {rec.get('sec', '')}", flush=True)


def sh(cmd, env=None, cwd=None, quiet=False):
    print("$", cmd, flush=True)
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         env=env, cwd=cwd, executable="/bin/bash")
    tail = []
    for line in p.stdout:
        tail = (tail + [line])[-60:]
        if not quiet:
            print(line, end="", flush=True)
    if p.wait():
        print("".join(tail), flush=True)
        raise RuntimeError(f"rc={p.returncode}: {cmd}")


def dir_bytes(p):
    return sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file())
