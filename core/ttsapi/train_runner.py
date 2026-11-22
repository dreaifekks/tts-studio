"""训练进程：按 GPT-SoVITS 原流程（1-get-text → 2-get-hubert-wav32k → [2-get-sv] → 3-get-semantic → s2 → s1）
用子进程跑上游脚本，所有中间产物写到 training/<run>/，权重写到 weights/<engine>/<run>/。

gateway 用 CREATE_BREAKAWAY_FROM_JOB 启动本进程，它不在 gateway 的「关闭即杀」Job Object 里，gateway 重启或崩溃时训练继续跑。
和 gateway 之间只通过文件通信（都是只追加写，两边同时读写没有冲突）：
- training/<run>/runner_spec.json：gateway 写的训练参数，本进程只读
- training/<run>/runner_events.jsonl：本进程写的阶段、进度和结束状态，gateway 从头重放得到当前状态
- training/<run>/runner_pid.json：本进程启动时写的 pid 和启动时间
- data/jobs/<job_id>.log：任务日志，本进程直接追加
本进程自己的 stdout/stderr（崩溃时的 traceback）在 training/<run>/runner.out。

上游脚本的 my_save 会先把临时文件写到当前目录再移走，脚本里也有很多相对 GPT_SoVITS/ 的路径，
所以脚本的工作目录是 training/_ws/<repo>，里面用目录联接（junction）指向对应仓库的 GPT_SoVITS 和 tools。

python -m ttsapi.train_runner <run_dir>
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import psutil
import yaml

from ttsapi import settings
from ttsapi.osutil import IS_WINDOWS, NO_WINDOW, child_env, keep_awake, link_dir

# 没有 NVIDIA 显卡（macOS、纯 CPU）时上游脚本在 CPU 上训练（训练脚本走 CPU 后端）。照上游 WebUI 在 is_half=False 时的
# 做法改用 fp32：不改的话 Lightning 把 GPT 的 16-mixed 换成 bf16-mixed，bf16 的矩阵乘在 CPU 上走 PyTorch 单线程的
# 参考实现，M6 上 GPT 1 轮（100 句）十几分钟都跑不完。CUDA 环境继续使用原有配置
CPU_ONLY = not shutil.which("nvidia-smi")

# Windows：上游脚本要能加载老目录自带的 ffmpeg 共享库。工作目录在 PYTHONPATH 里，这个 sitecustomize 启动时自动执行
SITECUSTOMIZE = f'''import os
os.add_dll_directory(r"{settings.FFMPEG_BIN}")
os.environ["PATH"] = r"{settings.FFMPEG_BIN};" + os.environ["PATH"]
'''


def workspace(repo: Path):
    ws = settings.TRAIN_DIR / "_ws" / repo.name
    ws.mkdir(parents=True, exist_ok=True)
    for name in ("GPT_SoVITS", "tools"):
        link_dir(ws / name, repo / name)
    if IS_WINDOWS and settings.FFMPEG_BIN:
        (ws / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    return ws


class Runner:
    def __init__(self, run_dir: Path):
        self.spec = json.loads((run_dir / "runner_spec.json").read_text(encoding="utf-8"))
        self.events = run_dir / "runner_events.jsonl"
        self.log_path = Path(self.spec["log_path"])
        self._last_progress = 0.0

    def event(self, **ev):
        with open(self.events, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(ev, ts=time.time()), ensure_ascii=False) + "\n")

    def log(self, line: str):
        line = line.rstrip()
        if line:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def stage(self, stage: str, progress: float):
        self.log(f"[{datetime.now():%H:%M:%S}] {stage}")
        self.event(t="stage", stage=stage, progress=round(progress, 4))

    def progress(self, progress: float):
        # 进度条刷新很密，每秒最多记一次
        if time.time() - self._last_progress > 1:
            self._last_progress = time.time()
            self.event(t="progress", progress=round(progress, 4))

    def run_script(self, eng: dict, ws: Path, args: list, env_extra: dict, on_line=None):
        env = child_env(
            [ws, eng["repo"]], **env_extra, PYTHONPYCACHEPREFIX=settings.RUNTIME / "pycache",
            NUMBA_CACHE_DIR=settings.RUNTIME / "numba", PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
            PYTHONUNBUFFERED="1", _CUDA_VISIBLE_DEVICES="0", is_half="True", hz="25hz",
            GPT_SOVITS_ENABLE_PROGRESS_BAR="1",
        )
        sys.stderr.write(f"$ {' '.join(map(str, args))}\n")
        sys.stderr.flush()
        proc = subprocess.Popen([str(eng["python"]), "-s", *map(str, args)], cwd=ws, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=NO_WINDOW)
        # Progress bars use \r in-place; raw output stays in runner.out (stderr),
        # while on_line extracts clean progress/epoch updates for the job log.
        def handle(line: bytes):
            text = line.decode("utf-8", "replace")
            stripped = text.rstrip()
            if on_line:
                on_line(text)
            if stripped:
                sys.stderr.write(stripped + "\n")
                sys.stderr.flush()

        buf = b""
        while chunk := proc.stdout.read1(65536):
            *lines, buf = re.split(rb"[\r\n]", buf + chunk)
            for line in lines:
                handle(line)
        handle(buf)
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"{Path(str(args[0])).name} 退出码 {rc}，详见任务日志")


def merge(parts: list, out: Path, header: str = None):
    lines = [header] if header else []
    for p in parts:
        lines += [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        p.unlink()
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def train(r: Runner):
    spec = r.spec
    req = spec["req"]
    eng = settings.ENGINES[req["engine"]]
    repo = eng["repo"]
    ws = workspace(repo)
    run_name = spec["run_name"]
    run_dir = settings.TRAIN_DIR / run_name
    exp = run_dir / "exp"
    weights_dir = settings.WEIGHTS_DIR / req["engine"] / run_name
    train_list = spec["train_list"]
    pre = repo / "GPT_SoVITS" / "pretrained_models"
    base = dict(inp_text=train_list, inp_wav_dir="", exp_name=run_name, opt_dir=str(exp), i_part="0",
                all_parts="1", version=eng["version"])
    r.stage("1/6 提取文本特征（BERT）", 0.03)
    r.run_script(eng, ws, ["GPT_SoVITS/prepare_datasets/1-get-text.py"],
                 base | dict(bert_pretrained_dir=str(pre / "chinese-roberta-wwm-ext-large")))
    merge([exp / "2-name2text-0.txt"], exp / "2-name2text.txt")

    r.stage("2/6 提取 HuBERT 特征、重采样到 32k", 0.08)
    hubert_env = base | dict(cnhubert_base_dir=str(pre / "chinese-hubert-base"),
                             sv_path=str(pre / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt"))
    r.run_script(eng, ws, ["GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py"], hubert_env)
    if eng["sv"]:
        r.stage(f"3/6 提取说话人向量（{eng['version']}）", 0.14)
        r.run_script(eng, ws, ["GPT_SoVITS/prepare_datasets/2-get-sv.py"], hubert_env)

    r.stage("4/6 提取语义 token", 0.18)
    r.run_script(eng, ws, ["GPT_SoVITS/prepare_datasets/3-get-semantic.py"],
                 base | dict(pretrained_s2G=str(eng["pretrained_sovits"]), s2config_path=str(repo / eng["s2_config"])))
    merge([exp / "6-name2semantic-0.tsv"], exp / "6-name2semantic.tsv", "item_name\tsemantic_audio")

    r.stage(f"5/6 训练 SoVITS（{req['sovits_epochs']} 轮）", 0.22)
    cfg = json.loads((repo / eng["s2_config"]).read_text(encoding="utf-8"))
    lora_rank = str(req.get("lora_rank") or 32)
    cfg["train"].update(batch_size=req["sovits_batch"], epochs=req["sovits_epochs"], text_low_lr_rate=0.4,
                        pretrained_s2G=str(eng["pretrained_sovits"]), pretrained_s2D=str(eng["pretrained_s2D"] or ""),
                        if_save_latest=True, if_save_every_weights=True, save_every_epoch=req["sovits_save_every"],
                        gpu_numbers="0", grad_ckpt=False, lora_rank=lora_rank)
    if CPU_ONLY:
        cfg["train"]["fp16_run"] = False
    cfg["model"]["version"] = eng["version"]
    cfg["data"]["exp_dir"] = cfg["s2_ckpt_dir"] = str(exp)
    cfg.update(save_weight_dir=str(weights_dir), name=run_name, version=eng["version"])
    (exp / f"logs_s2_{eng['version']}").mkdir(exist_ok=True)
    s2_cfg = run_dir / "s2_config.json"
    s2_cfg.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    s2_seen = set()

    def s2_progress(line):
        if m := re.search(r"====> Epoch: (\d+)", line):
            e = int(m.group(1))
            r.progress(0.22 + 0.38 * e / req["sovits_epochs"])
            if e not in s2_seen:
                s2_seen.add(e)
                r.log(f"SoVITS epoch {e}/{req['sovits_epochs']}")

    # 上游脚本从环境变量 version 选音素表（v1 和其他版本不同）；预处理的 base 里已经带了，s1 调用处也传了
    r.run_script(eng, ws, [eng["s2_script"], "--config", s2_cfg], dict(version=eng["version"]), s2_progress)

    r.stage(f"6/6 训练 GPT（{req['gpt_epochs']} 轮）", 0.6)
    # GPT 训练一律用老目录（他的 fork）的 s1_train：各版本的 AR 代码相同，只是底模和配置（v1 用 s1longer.yaml）不同；
    # 上游版在 Windows 单卡上也走 DDP/gloo 会崩（0xC0000005），DataLoader 也不支持 0 个 worker，fork 都修过
    s1_eng = settings.ENGINES["v2pro"]
    s1 = yaml.safe_load((s1_eng["repo"] / eng["s1_config"]).read_text(encoding="utf-8"))
    s1["data"]["num_workers"] = 0
    s1["train"].update(batch_size=req["gpt_batch"], epochs=req["gpt_epochs"], save_every_n_epoch=req["gpt_save_every"],
                       if_save_every_weights=True, if_save_latest=True, if_dpo=False,
                       half_weights_save_dir=str(weights_dir), exp_name=run_name)
    if CPU_ONLY:
        s1["train"]["precision"] = "32"
    s1.update(pretrained_s1=str(eng["pretrained_gpt"]), train_semantic_path=str(exp / "6-name2semantic.tsv"),
              train_phoneme_path=str(exp / "2-name2text.txt"), output_dir=str(exp / "logs_s1"))
    s1_cfg = run_dir / "s1_config.yaml"
    s1_cfg.write_text(yaml.safe_dump(s1, allow_unicode=True, sort_keys=False), encoding="utf-8")

    seen = set()

    def s1_progress(line):
        # Lightning 的 Rich 进度条：「Epoch 0/1  ---- 3/49 ...」，后面跟几行缩进很深的指标
        if m := re.search(r"Epoch (\d+)/\d+\s.*?(\d+)/(\d+)\s", line):
            e, b, nb = map(int, m.groups())
            r.progress(0.6 + 0.25 * (e + b / nb) / req["gpt_epochs"])
            if e not in seen:
                seen.add(e)
                r.log(f"GPT epoch {e + 1}/{req['gpt_epochs']}")
            return True
        return re.match(r"\s{20,}\S", line) is not None

    r.run_script(s1_eng, workspace(s1_eng["repo"]), ["GPT_SoVITS/s1_train.py", "--config_file", s1_cfg],
                 dict(version=eng["version"]), s1_progress)


def main(run_dir: Path):
    r = Runner(run_dir)
    # 自己发布身份（pid + 启动时间），gateway 据此判断死活、取消时杀进程树。先写临时文件再改名，gateway 不会读到半个文件
    tmp = run_dir / "runner_pid.json.tmp"
    tmp.write_text(json.dumps(dict(pid=os.getpid(), create_time=psutil.Process().create_time())), encoding="utf-8")
    os.replace(tmp, run_dir / "runner_pid.json")
    # gateway 不在时也不让系统睡眠；进程退出后自动失效
    keep_awake(True)
    r.event(t="start", pid=os.getpid())
    try:
        train(r)
    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        sys.stderr.flush()
        r.event(t="end", state="failed", error=f"{type(e).__name__}: {e}")
        return
    r.event(t="end", state="succeeded")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
