"""测速：本机对每个引擎和打分器试几种设备 × 线程数，量出每句秒数（给估时用），把最快的设置写回 tuning.json。

每个配置起一个临时 worker（端口在正常 worker 上再加 PORT_OFFSET + 序号，用 TTSAPI_BENCH_DEVICE / TTSAPI_BENCH_THREADS
指定设备和线程，见 settings.device_setting / torch_threads），热身 1 句后计时：
- 粗筛：每个配置计前 SCREEN_ITEMS 句（第一个配置的第 1 句就超过 ROUGH_SEC 秒的慢引擎只计 1 句，同一引擎的配置计一样的句子）。
  比目前最快的配置慢 CUT_FACTOR 倍以上的（热身或某句超时）当场停掉，记 cut_after_sec；
  CPU 的某一档线程被停掉后，这个引擎剩下的 CPU 档不再试（skipped：线程数之间差不出这么多）。
- 最快的配置接着跑满 items 句；它在 CPU 上每句超过 ROUGH_SEC 秒时（v4 这类）只跑 ROUGH_ITEMS 句，标 rough（粗测）。
- 打分器用前面合成出来的音频测 /score 的每条秒数（只在完整测速、不指定引擎时测）。
快速测速（quick）：只测常用模型（QUICK_ENGINES，给了 engines 就测给的），每个引擎只试 2 档——tuning 里现在的最佳
（没测过是 CPU 6 线程）和一个备选（见 _quick_configs），每档热身 1 句后计 QUICK_ITEMS 句，取快的；不测打分器。
结果格式见设计第 4 节；命令行 python -m ttsapi.bench 把它写进资料库 compute/bench/<machine>/<日期>.json。
"""
import asyncio
import statistics
import tempfile
import time
from pathlib import Path

import httpx
import psutil
from fastapi import HTTPException

from ttsapi import settings, workers
from ttsapi.catalog import Catalog
from ttsapi.compute import hw, tuning
from ttsapi.library import now_iso

BENCH_VERSION = 1
# 第 k 个配置的临时 worker 的端口 = 正常 worker 的端口 + PORT_OFFSET + k（worker 进程里用 TTSAPI_WORKER_PORT_SHIFT
# 算出同一个端口）。粗筛时目前最快的那个配置还开着，所以每个配置一个端口
PORT_OFFSET = 50
SEED = 1234
# 热身句用短句：热身也有时限，短句让慢配置更早被停掉
WARMUP_TEXT = settings.EVAL_SETS["mini"]["sentences"][0]["text"]
# 固定题目：hard 评测集里长度中等的 6 句（难读汉字、多音、数字单位、英文字母、反问、口语缩约）
SENTENCES = [settings.EVAL_SETS["hard"]["sentences"][i]["text"] for i in (2, 3, 4, 5, 9, 10)]
LANG = "ja"
SYNTH_PARAMS = dict(top_k=15, top_p=1.0, temperature=1.0, repetition_penalty=1.35, speed=1.0, text_split_method="cut5",
                    sample_steps=None, debug=False)
FULL = dict(threads=(6, 8, 4), items=6)
# 快速测速：只测这些常用模型，每档配置计这么多句
QUICK_ENGINES = ["v2proplus"]
QUICK_ITEMS = 3
QUICK_DEFAULT_THREADS = 6
SCREEN_ITEMS = 2
ROUGH_SEC = 20
ROUGH_ITEMS = 2
CUT_FACTOR = 2.5
CUT_MARGIN_SEC = 5
# 打分器的 GPU 配置：whisper（ctranslate2）在 Apple 上仍跑 CPU，线程数用前面 CPU 档里最快的那档
CPU_BEST = "cpu-best"


class Trial:
    """一个配置（设备 × 线程数）的临时 worker 和它的计时。"""

    def __init__(self, job, device: str, threads, k: int):
        shift = PORT_OFFSET + k
        env = dict(TTSAPI_BENCH_DEVICE=device, TTSAPI_WORKER_PORT_SHIFT=settings.WORKER_PORT_SHIFT + shift)
        if threads:
            # 打分器的 whisper 线程数是 settings.ASR_CPU_THREADS（没有测速写回时读 TTSAPI_ASR_THREADS）
            env.update(TTSAPI_BENCH_THREADS=threads, TTSAPI_ASR_THREADS=threads)
        self.device, self.threads = device, threads
        self.worker = workers.Worker(f"bench-{job.name}", job.module, dict(job.cfg, port=job.cfg["port"] + shift),
                                     job.args, env)
        self.load_sec = self.warmup_sec = self.screen_sec = self.cut_after = self.error = None
        self.screen_n = 0
        self.health = {}
        self.secs, self.audio = [], []  # 计时的每句秒数、音频时长（打分器没有时长）
        self.peak_mb = 0
        self._watch = None

    async def start(self):
        t = time.perf_counter()
        await self.worker.ensure()
        self.load_sec = round(time.perf_counter() - t, 1)
        self.health = await _health(self.worker)
        self._watch = asyncio.create_task(self._watch_rss())

    async def _watch_rss(self):
        proc = psutil.Process(self.worker.proc.pid)
        while True:
            self.peak_mb = max(self.peak_mb, proc.memory_info().rss // 2**20)
            await asyncio.sleep(0.5)

    async def run(self, path: str, payload: dict, limit=None):
        """调一次 worker，返回 (结果, 墙钟秒数)；超过 limit 秒返回 None（调用方停掉这个 worker）。"""
        t = time.perf_counter()
        try:
            res = await asyncio.wait_for(self.worker.call(path, payload), limit)
        except asyncio.TimeoutError:
            self.cut_after = round(limit, 2)
            return None
        return res, time.perf_counter() - t

    def stop(self):
        if self._watch:
            self._watch.cancel()
        self.worker.stop()

    def summary(self) -> dict:
        """tried 里的一项：sec_per_item 是粗筛那几句的平均（各配置可比）。"""
        sec = None if self.screen_sec is None else round(self.screen_sec, 2)
        out = dict(device=self.device, threads=self.threads, sec_per_item=sec, items=self.screen_n,
                   load_sec=self.load_sec, warmup_sec=self.warmup_sec, peak_gb=round(self.peak_mb / 1024, 1),
                   torch_threads=self.health.get("torch_threads"))
        if self.device == "cpu" and self.screen_sec is not None and self.screen_sec >= ROUGH_SEC:
            out["rough"] = True
        if "asr" in self.health:
            out["asr"] = self.health["asr"]
        if self.cut_after is not None:
            out["cut_after_sec"] = self.cut_after
        if self.error:
            out["error"] = self.error
        return out


async def _health(worker) -> dict:
    """worker 的 /health：实际用的设备、torch 线程数（打分器还有 whisper 的设置）。"""
    async with httpx.AsyncClient() as client:
        return (await client.get(worker.url + "/health", timeout=10)).json()


class EngineJob:
    """引擎的题目：/synth，每句秒数用 worker 返回的 infer_sec；合成出的音频留给打分器测速（wavs：{句子序号: {audio, text}}，
    -1 是热身句）。"""
    module, path = "ttsapi.engine_worker", "/synth"

    def __init__(self, engine: str, ref: dict, out_dir: Path, wavs: dict):
        self.name, self.ref, self.out_dir, self.wavs = engine, ref, out_dir, wavs
        self.cfg, self.args = settings.ENGINES[engine], [engine]

    def payload(self, i: int, tag: str) -> dict:
        return dict(SYNTH_PARAMS, text=WARMUP_TEXT if i < 0 else SENTENCES[i], text_lang=LANG,
                    ref_audio=self.ref["path"], prompt_text=self.ref["text"], prompt_lang=self.ref["lang"],
                    gpt=str(self.cfg["pretrained_gpt"]), sovits=str(self.cfg["pretrained_sovits"]), seed=SEED,
                    out_path=str(self.out_dir / f"{self.name}-{tag}-{i}.wav"))

    def record(self, i: int, trial: Trial, payload: dict, res: dict, wall: float) -> float:
        self.wavs.setdefault(i, dict(audio=payload["out_path"], text=payload["text"]))
        if i >= 0:
            trial.secs.append(res["infer_sec"])
            trial.audio.append(res["duration"])
            trial.peak_mb = max(trial.peak_mb, res["vram_peak_mb"])
        return res["infer_sec"]


class ScorerJob:
    """打分器的题目：对前面合成出的音频调 /score，每条秒数用墙钟时间。"""
    module, path, name, args = "ttsapi.scorer_worker", "/score", "scorer", []

    def __init__(self, ref: dict, wavs: dict):
        self.ref, self.wavs, self.cfg = ref, wavs, settings.SCORER

    def payload(self, i: int, tag: str) -> dict:
        return dict(audio=self.wavs[i]["audio"], text=self.wavs[i]["text"], text_lang=LANG, ref_audio=self.ref["path"])

    def record(self, i: int, trial: Trial, payload: dict, res: dict, wall: float) -> float:
        if i >= 0:
            trial.secs.append(round(wall, 3))
        return wall


async def _screen(job, device: str, threads, k: int, best, n: int) -> Trial:
    """起第 k 个配置的 worker，热身 1 句、计前 n 句。比 best 慢 CUT_FACTOR 倍的（热身或某一句超时）当场停掉。
    第一个配置（还没有 best）第 1 句就超过 ROUGH_SEC 秒时只计这 1 句。"""
    trial = Trial(job, device, threads, k)
    tag = f"{device}{threads or ''}"
    try:
        await trial.start()
        for i in range(-1, n):
            limit = best and CUT_FACTOR * (best.warmup_sec if i < 0 else best.secs[i]) + CUT_MARGIN_SEC
            payload = job.payload(i, tag)
            out = await trial.run(job.path, payload, limit)
            if out is None:
                return trial
            sec = job.record(i, trial, payload, *out)
            if i < 0:
                trial.warmup_sec = round(sec, 2)
            elif best is None and sec >= ROUGH_SEC:
                break
        trial.screen_sec, trial.screen_n = statistics.mean(trial.secs), len(trial.secs)
    except (RuntimeError, HTTPException, httpx.TransportError) as e:  # 这个设备跑不了（MPS 上缺算子、内存不够崩了）
        trial.error = f"{type(e).__name__}: {getattr(e, 'detail', e)}"
    except BaseException:
        # 启动、热身、计时中被取消（测速任务取消）或出了别的错：这个试验还没交给调用方，自己的临时进程自己关
        trial.stop()
        raise
    return trial


async def _bench_worker(job, configs: list, items: int, step, screen: int = SCREEN_ITEMS) -> dict:
    """依次试 configs（[(device, threads)]，threads 是 CPU_BEST 时用前面 CPU 档里最快的线程数），每档粗筛 screen 句，
    最快的跑满 items 句。返回 {device, threads, sec_per_item, rough, tried, best: Trial}。"""
    n = min(screen, items)
    best, tried, cpu_cut = None, [], False
    try:
        for k, (device, threads) in enumerate(configs):
            if device == "cpu" and cpu_cut:
                tried.append(dict(device=device, threads=threads, skipped=True))
                continue
            if threads == CPU_BEST:
                threads = best.threads if best else None
            step(f"{job.name} {device}{f' ×{threads}' if threads else ''}")
            trial = await _screen(job, device, threads, k, best, n)
            tried.append(trial)
            if trial.screen_sec is not None and (best is None or trial.screen_sec < best.screen_sec):
                if best:
                    best.stop()
                best, n = trial, trial.screen_n
            else:
                trial.stop()
                cpu_cut = cpu_cut or (device == "cpu" and trial.cut_after is not None)
        if best is None:
            raise RuntimeError(f"{job.name}: 所有配置都没跑通：{[t.summary() for t in tried]}")
        rough = best.device == "cpu" and best.screen_sec >= ROUGH_SEC
        total = min(items, ROUGH_ITEMS) if rough else items
        if total > n:
            step(f"{job.name} {best.device}{f' ×{best.threads}' if best.threads else ''} 计满 {total} 句")
        for i in range(n, total):
            payload = job.payload(i, "best")
            job.record(i, best, payload, *await best.run(job.path, payload))
    finally:
        if best:
            best.stop()
    return dict(device=best.device, threads=best.threads, sec_per_item=round(statistics.mean(best.secs), 2),
                rough=rough, tried=[t.summary() if isinstance(t, Trial) else t for t in tried], best=best)


def _engine_result(r: dict) -> dict:
    best = r["best"]
    out = dict(device=r["device"], threads=r["threads"], sec_per_item=r["sec_per_item"],
               rtf=round(sum(best.secs) / sum(best.audio), 3), load_sec=best.load_sec,
               peak_gb=round(best.peak_mb / 1024, 1), items=len(best.secs))
    if r["rough"]:
        out["rough"] = True
    return dict(out, tried=r["tried"])


def _quick_configs(engine: str, accel: list, threads: list) -> list:
    """快速测速的 2 档：tuning 里现在的最佳（没测过是 CPU 6 线程）+ 备选。最佳是 CPU 时备选是本机的加速（mps / cuda），
    没有加速时是 CPU 另一档线程；最佳是加速时备选是 CPU 6 线程。"""
    cur = settings.tuning(engine)
    device = cur.get("device", "cpu")
    if device != "cpu":
        return [(device, None), ("cpu", QUICK_DEFAULT_THREADS)]
    best = cur.get("threads") or QUICK_DEFAULT_THREADS
    alt = (accel[0], None) if accel else ("cpu", next(t for t in [QUICK_DEFAULT_THREADS, *threads] if t != best))
    return [("cpu", best), alt]


# 同一台设备上的测速串行（主控的 POST /bench 和 worker 的 /worker/bench 都走 run_bench）：同时测会互相抢 CPU，
# 结果不准，tuning.json 也会被交错写回
_lock = asyncio.Lock()


async def run_bench(ref: dict, engines: list = None, quick: bool = False, progress=None) -> dict:
    """测速；本进程里已经有测速在跑时先等它做完。ref = dict(path, text, lang) 参考音频；
    engines 不给是本机装好的全部引擎加打分器（quick 时只有 QUICK_ENGINES，不测打分器），给了只测这些引擎、不测打分器。
    progress(stage, fraction) 可选。
    跑完把每个引擎（和打分器）的最佳设置写回 tuning.json，并停掉测过的正常 worker（下次按需启动就用新设置）。"""
    async with _lock:
        return await _run_bench(ref, engines, quick, progress)


async def _run_bench(ref: dict, engines: list, quick: bool, progress) -> dict:
    if workers.training_job:
        raise RuntimeError(f"正在训练（任务 {workers.training_job}），训练结束后再测速")
    machine = hw.hardware()
    accel = [machine["accel"]] if machine["accel"] != "cpu" else []
    threads = [t for t in FULL["threads"] if t <= machine["cores"]] or [machine["cores"]]
    with_scorer = engines is None and not quick
    if engines is None:
        engines = QUICK_ENGINES if quick else settings.installed_engines()
    full_configs = [(d, None) for d in accel] + [("cpu", t) for t in threads]
    engine_configs = {e: _quick_configs(e, accel, threads) if quick else full_configs for e in engines}
    scorer_configs = [("cpu", t) for t in threads] + [(d, CPU_BEST) for d in accel]
    total = sum(len(c) + 1 for c in engine_configs.values()) + (len(scorer_configs) + 1 if with_scorer else 0)
    done = 0

    def step(stage: str):
        nonlocal done
        if progress:
            progress(stage, round(min(done / total, 1), 3))
        done += 1

    t0 = time.perf_counter()
    result = dict(machine=settings.MACHINE_NAME, alias=settings.MACHINE_NAME, at=now_iso(), bench_version=BENCH_VERSION,
                  quick=quick, hw=machine, engines={})
    wavs = {}
    with tempfile.TemporaryDirectory(prefix="ttsapi-bench-") as tmp:
        for e in engines:
            job = EngineJob(e, ref, Path(tmp), wavs)
            if quick:
                r = await _bench_worker(job, engine_configs[e], QUICK_ITEMS, step, screen=QUICK_ITEMS)
            else:
                r = await _bench_worker(job, engine_configs[e], FULL["items"], step)
            result["engines"][e] = _engine_result(r)
        if with_scorer:
            items = min(FULL["items"], len(wavs) - 1)
            r = await _bench_worker(ScorerJob(ref, wavs), scorer_configs, items, step)
            result["scorer"] = dict(device=r["device"], threads=r["threads"], sec_per_item=r["sec_per_item"],
                                    items=len(r["best"].secs), tried=r["tried"])
    result["elapsed_sec"] = round(time.perf_counter() - t0)
    for e, r in result["engines"].items():
        tuning.save(e, r["device"], r["threads"])
        workers.ENGINES[e].stop()
    if with_scorer:
        tuning.save("scorer", result["scorer"]["device"], result["scorer"]["threads"])
        workers.SCORER.stop()
    if progress:
        progress("done", 1.0)
    return result


def bench_ref(refs=None) -> dict:
    """Use the first registered character with a usable primary reference."""
    from ttsapi.refs import RefLibrary

    catalog = Catalog()
    refs = RefLibrary(catalog) if refs is None else refs
    for slug in catalog.slugs():
        if ref_id := refs.default_or_none(slug):
            r = refs.index[ref_id]
            return dict(id=ref_id, path=r["path"], text=r["text"], lang=r["lang"])
    raise KeyError("资料库没有可用于测速的参考音频")
