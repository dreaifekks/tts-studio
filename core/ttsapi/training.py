"""训练任务和评测。

训练本身在独立的训练进程里跑（ttsapi/train_runner.py），它不在 gateway 的「关闭即杀」Job Object 里：
gateway 重启或崩溃时训练继续，gateway 起来后按 training/<run>/ 下的文件重新接管这个任务（进度、日志、取消），
训练结束后照常登记权重、跑自动评测。只有用户取消任务时才杀训练进程。
训练进程把权重导出到本机的暂存目录 weights/<engine>/<run>/，训练结束后复制进资料库（Catalog.register_run），
run.json 写好（登记提交）后才删暂存目录，登记中途失败时再进来能完整重来；
训练目录的小文件（train.list、配置、日志）也复制进资料库 runs/<run>/；中间产物留在本机 training/<run>/。
"""
import asyncio
import contextlib
import json
import re
import shutil
import statistics
import subprocess
from datetime import datetime
from pathlib import Path

import psutil

from ttsapi import library, settings, synth, workers
from ttsapi.catalog import rank
from ttsapi.compute import remote
from ttsapi.compute.scheduler import Task
from ttsapi.datasets import apply_filter, get, rows
from ttsapi.osutil import detached_popen_kwargs, kill_tree
from ttsapi.models import ComboEval, SentenceEval, TrainEpochs, TrainEval, TrainRequest, TrainResult, TTSParams

train_lock = asyncio.Lock()


def gpu_lock():
    """评测、盲听出题在本机跑时排在训练后面（同一把锁）；派到局域网设备时不用等本机的训练。"""
    return train_lock if synth.EXEC.get() is remote.LOCAL else contextlib.nullcontext()


def sorted_by_epoch(paths, run_name: str):
    return sorted((p.name for p in paths), key=lambda n: int(re.match(r"[-_]e(\d+)", n[len(run_name):]).group(1)))


async def train_job(run, req: TrainRequest, catalog, refs):
    list_src, _ = get(req.dataset_id)
    rs = await asyncio.to_thread(rows, list_src)
    kept, *_ = apply_filter(rs, req.min_sec, req.max_sec, req.exclude_interjections)
    if req.max_lines and len(kept) > req.max_lines:
        kept = [kept[i * len(kept) // req.max_lines] for i in range(req.max_lines)]
    run_name = f"{req.slug}-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir = settings.TRAIN_DIR / run_name
    for d in (run_dir / "exp", settings.WEIGHTS_DIR / req.engine / run_name):
        d.mkdir(parents=True, exist_ok=True)
    train_list = run_dir / "train.list"
    train_list.write_text("".join(f"{r.path}|{req.slug}|{r.lang}|{r.text}\n" for r in kept), encoding="utf-8")
    minutes = round(sum(r.duration for r in kept) / 60, 2)
    run.log(f"run {run_name}: {len(kept)} 句，{minutes} 分钟，数据集 {list_src}")
    run.job.train_result = TrainResult(slug=req.slug, engine=req.engine, run_name=run_name, lines_used=len(kept),
                                       minutes_used=minutes, gpt_weights=[], sovits_weights=[])
    run.job.status = "queued"
    run.stage("等待 GPU（其他训练结束、正在进行的推理完成）", 0.01)
    # 评测也在锁里：排队的下一个训练一拿到锁就会挡住推理，评测要在那之前做完
    async with train_lock:
        run.job.status = "running"
        try:
            await workers.unload_all_for_training(run.job.id)
            (run_dir / "runner_spec.json").write_text(json.dumps(dict(
                job_id=run.job.id, run_name=run_name, req=req.model_dump(), train_list=str(train_list),
                log_path=str(run.log_path)), ensure_ascii=False, indent=1), encoding="utf-8")
            offset = run.log_path.stat().st_size
            # 从这里起服务退出也不把任务标成中断：spec 已经写好，下次启动按它接管
            run.detached = True
            with open(run_dir / "runner.out", "ab") as out:
                subprocess.Popen(
                    [str(settings.OLD_PYTHON), "-m", "ttsapi.train_runner", str(run_dir)], cwd=settings.ROOT,
                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                    **detached_popen_kwargs())
            await follow_runner(run, run_dir, offset)
        finally:
            workers.end_training()
        await after_training(run, catalog, refs)


def resumable(job) -> bool:
    """上次服务退出时还没结束的训练任务，已经走到启动训练进程这一步（runner_spec.json 在）就能接管。"""
    return job.kind == "train" and job.train_result is not None and \
        (settings.TRAIN_DIR / job.train_result.run_name / "runner_spec.json").exists()


async def resume_train_job(run, catalog, refs):
    """服务启动时接管训练任务：训练进程还在跑就接着跟，已经跑完就直接登记权重、评测。"""
    run.detached = True
    run_dir = settings.TRAIN_DIR / run.job.train_result.run_name
    run.log(f"[{datetime.now():%H:%M:%S}] 服务重启，重新接管训练任务")
    offset = run.follow(0)
    run.save()
    async with train_lock:
        try:
            await workers.unload_all_for_training(run.job.id)
            await follow_runner(run, run_dir, offset)
        finally:
            workers.end_training()
        await after_training(run, catalog, refs)


def train_seconds(run_name: str) -> float:
    """训练进程从启动到结束的秒数（预处理 + SoVITS + GPT，不含训后评测）：资料库 runs/<run>/runner_events.jsonl
    （训练目录的小文件，登记 run 时随 run 放进资料库、Colab 上训的随结果导进来）的 start、end 事件。"""
    path = library.current().path(f"runs/{run_name}/runner_events.jsonl")
    ts = {ev["t"]: ev["ts"] for ev in map(json.loads, path.read_text(encoding="utf-8").splitlines())
          if ev["t"] in ("start", "end")}
    return ts["end"] - ts["start"]


def runner_state(run_dir: Path):
    """重放训练进程的事件文件：(当前阶段, 进度, 结束事件或 None)。"""
    stage, progress, end = None, None, None
    events = run_dir / "runner_events.jsonl"
    if events.exists():
        for line in events.read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            stage = ev.get("stage", stage)
            progress = ev.get("progress", progress)
            if ev["t"] == "end":
                end = ev
    return stage, progress, end


async def runner_started(run_dir: Path):
    """等训练进程写出 runner_pid.json（启动后一两秒）。"""
    for _ in range(120):
        if (run_dir / "runner_pid.json").exists():
            return
        await asyncio.sleep(0.5)
    raise RuntimeError(f"训练进程 60 秒内没有启动起来，见 {run_dir / 'runner.out'}")


def runner_alive(run_dir: Path) -> bool:
    info = json.loads((run_dir / "runner_pid.json").read_text(encoding="utf-8"))
    # 按启动时间核对，pid 被别的进程复用时不会认错；两次查询之间进程正好退出也算不在了
    try:
        return psutil.Process(info["pid"]).create_time() == info["create_time"]
    except psutil.NoSuchProcess:
        return False


def kill_runner(run_dir: Path):
    """杀掉训练进程和它的整棵子进程树（上游脚本、DataLoader 子进程）。"""
    pid = json.loads((run_dir / "runner_pid.json").read_text(encoding="utf-8"))["pid"]
    if runner_alive(run_dir):
        kill_tree(pid)


async def follow_runner(run, run_dir: Path, offset: int):
    """每秒把训练进程的日志和阶段、进度同步到任务上，直到它结束。用户取消时杀掉训练进程；
    服务退出时（任务协程被取消但不是用户取消）什么都不做，训练继续。"""
    try:
        await runner_started(run_dir)
        while True:
            offset = run.follow(offset)
            # 先看进程在不在再读事件：进程退出前写的结束事件一定能读到
            alive = runner_alive(run_dir)
            stage, progress, end = runner_state(run_dir)
            if stage and (stage, progress) != (run.job.stage, run.job.progress):
                run.job.stage, run.job.progress = stage, progress
                run._bump(force_save=True)
            if end:
                break
            if not alive:
                raise RuntimeError(f"训练进程意外退出，见 {run_dir / 'runner.out'}")
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        if run.cancel_requested:
            await runner_started(run_dir)  # 刚启动、还没写 pid 时取消：等它写出来再杀
            kill_runner(run_dir)
            run.log("已取消，训练进程已结束")
        raise
    run.follow(offset)
    if end["state"] != "succeeded":
        raise RuntimeError(end["error"])


async def after_training(run, catalog, refs):
    """训练进程跑完之后：权重搬进资料库并登记 run（接管时可能已经登记过）、建参考库、自动评测、设最好的组合。"""
    result = run.job.train_result
    run_dir = settings.TRAIN_DIR / result.run_name
    spec = json.loads((run_dir / "runner_spec.json").read_text(encoding="utf-8"))
    req = TrainRequest(**spec["req"])
    # 暂存目录：run.json 写好之前一个文件都不删（权重是复制进资料库的），中途失败或服务退出后再进来，
    # 按暂存目录里完整的文件重新登记一遍（没提交的 run 的权重不算数，重新登记会覆盖）
    weights_dir = settings.WEIGHTS_DIR / req.engine / result.run_name
    if not catalog.has_run(result.run_name):
        gpts = sorted_by_epoch(weights_dir.glob(f"{result.run_name}-e*.ckpt"), result.run_name)
        sovits = sorted_by_epoch(weights_dir.glob(f"{result.run_name}_e*.pth"), result.run_name)
        if not gpts or not sovits:
            from ttsapi.jobs import UserError
            raise UserError("训练结束但没有导出权重：轮数需要是保存间隔的整数倍")
        catalog.register_run(req.slug, req.dataset_id, dict(
            job_id=run.job.id, run_name=result.run_name, engine=req.engine, best=None,
            filter=dict(min_sec=req.min_sec, max_sec=req.max_sec, exclude_interjections=req.exclude_interjections,
                        max_lines=req.max_lines),
            lines_used=result.lines_used, minutes_used=result.minutes_used, params=req.model_dump()),
            dict(gpt=[weights_dir / n for n in gpts], sovits=[weights_dir / n for n in sovits]), train_dir=run_dir)
    # 评测、best 用资料库里登记的名字（和底模同名的登记时改过名）
    registered = catalog.run(result.run_name)
    result.gpt_weights, result.sovits_weights = registered["gpt"], registered["sovits"]
    if weights_dir.exists():  # run 已提交（包括上次提交后、删暂存前退出的），暂存可以清了
        shutil.rmtree(weights_dir)
    refs.reload()
    combos = [(g, s) for g in result.gpt_weights for s in result.sovits_weights]
    es = settings.EVAL_DEFAULT_SET
    n = train_eval_candidates(req.engine, len(combos))
    ref_id = req.ref_id or refs.default_for(req.slug)
    result.eval = await evaluate(run, req.slug, req.engine, combos, ref_id, catalog, refs, es, n)
    catalog.set_best(result.run_name, result.eval.best.gpt, result.eval.best.sovits)


# ---------------------------------------------------------------- 评测

def train_eval_candidates(engine: str, n_combos: int) -> int:
    """训练后自动评测每句出几个候选：按时间预算（settings.TRAIN_EVAL_BUDGET_SEC）估，1–3 个。"""
    per_candidate = n_combos * len(settings.EVAL_SETS[settings.EVAL_DEFAULT_SET]["sentences"]) * \
        settings.EVAL_SEC_PER_ITEM[engine]
    return max(1, min(3, int(settings.TRAIN_EVAL_BUDGET_SEC // per_candidate)))


def train_task(req, lines: int) -> Task:
    """训练的估时任务（req：TrainRequest 或 kind=train 的 PlanRequest）：POST /plan 预览、提交时选设备和提醒线、开云端卡的预计时长都用它。
    训练量 = 句数 × 轮数折算（训练每句秒数是默认轮数下的，按 SoVITS、GPT 各占一半折算）；
    parts 是训后自动评测的条数：每个导出的 GPT × SoVITS 组合在默认评测集上每句 train_eval_candidates 个候选。"""
    combos = (req.gpt_epochs // req.gpt_save_every) * (req.sovits_epochs // req.sovits_save_every)
    items = combos * len(settings.EVAL_SETS[settings.EVAL_DEFAULT_SET]["sentences"]) * \
        train_eval_candidates(req.engine, combos)
    default = TrainEpochs.model_fields
    epochs = (req.sovits_epochs / default["sovits_epochs"].default + req.gpt_epochs / default["gpt_epochs"].default) / 2
    return Task("train", [(req.engine, items)], lines=lines, epochs=epochs)


def _stats(cer, sim, score):
    return dict(cer_median=statistics.median(cer), cer_mean=round(statistics.mean(cer), 4), cer_max=max(cer),
                spk_sim_median=statistics.median(sim), spk_sim_mean=round(statistics.mean(sim), 4),
                spk_sim_min=min(sim), score_median=statistics.median(score),
                score_mean=round(statistics.mean(score), 2), score_min=min(score))


def _cols(items):
    return ([c.cer_kana if c.cer_kana is not None else c.cer for c in items], [c.spk_sim for c in items],
            [c.score for c in items])


async def eval_combo(engine: str, slug: str, gpt: str, sovits: str, ref: dict, eval_set: str, n: int,
                     version: str = None) -> ComboEval:
    """评测集里每句各出 n 个候选（seed 固定），统计 CER（读音）、说话人相似度和 score 的中位数、均值、最差值，以及逐句统计。
    version：按版本评测（权重按版本绑定的记录解析）。"""
    items, per = [], []
    for k, s in enumerate(settings.EVAL_SETS[eval_set]["sentences"]):
        p = TTSParams(text=s["text"], character=slug, engine=engine, gpt=gpt, sovits=sovits, version=version)
        res = await synth.run_candidates(p, engine, n, ref, 1000 + 10 * k)
        items += res.candidates
        st = _stats(*_cols(res.candidates))
        per.append(SentenceEval(index=k, text=s["text"], n=n, **{f: st[f] for f in SentenceEval.model_fields
                                                                   if f in st}))
    return ComboEval(gpt=gpt, sovits=sovits, n=len(items), per_sentence=per, **_stats(*_cols(items)))


async def evaluate(run, slug: str, engine: str, combos: list, ref_id: str, catalog, refs, eval_set: str,
                   n: int, version: str = None) -> TrainEval:
    """训练后评测和手动评测共用：每个组合跑 eval_combo，结果记进资料库的评测文件，按 score 中位数（相同再按均值）排序。
    version：手动评测按版本提交时（只有一个组合），解析权重和记权重归属都按版本绑定的记录。"""
    ref = refs.index[ref_id]
    out = []
    for i, (gpt, sovits) in enumerate(combos):
        if run.job.kind == "train":
            run.stage(f"评测（{eval_set}）{i + 1}/{len(combos)}：{gpt} + {sovits}", 0.85 + 0.15 * i / len(combos))
        else:
            run.stage(f"评测（{eval_set}）{gpt} + {sovits}", 0.05)
        out.append(await eval_combo(engine, slug, gpt, sovits, ref, eval_set, n, version))
        catalog.record_eval(engine, slug, ref_id, run.job.id, eval_set, out[-1], version)
        run.log(f"eval {gpt} + {sovits}: {out[-1].model_dump(exclude={'per_sentence'})}")
    out.sort(key=rank, reverse=True)
    return TrainEval(job_id=run.job.id, engine=engine, character=slug, ref_id=ref_id, eval_set=eval_set,
                     sentences=[s["text"] for s in settings.EVAL_SETS[eval_set]["sentences"]],
                     candidates_per_sentence=n, combos=out, best=out[0])


async def eval_job(run, req, slug: str, ref_id: str, catalog, refs):
    """手动评测一个组合（POST /weights/eval）。和训练用同一把锁：排在训练后面，评测期间新的训练也会等它做完。"""
    run.job.status = "queued"
    run.stage("等待 GPU（训练结束）", 0.01)
    async with gpu_lock():
        run.job.status = "running"
        run.job.eval_result = await evaluate(run, slug, req.engine, [(req.gpt, req.sovits)], ref_id, catalog, refs,
                                             req.eval_set, req.candidates or settings.EVAL_CANDIDATES[req.eval_set],
                                             req.version)
