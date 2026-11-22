"""假 gateway（tests/colab_sim）：colab/vm_run.py 用到的接口，不训练、不推理，但资料库、任务记录用 tts-core 自己的代码写
（ttsapi.library / catalog / db），和 VM 上真的 gateway 一样按环境变量 TTSAPI_LIBRARY、TTSAPI_DATA_DIR、TTSAPI_GSV_ROOT 找东西：
- POST /library/claim：认领资料库（输入资料库归控制端，不认领写不进去）
- POST /train：数据集要在资料库里、已经解压在数据根；训出 2 个 GPT、1 个 SoVITS（假文件），登记 run，按参考（不填用默认参考，
  新角色沿用内置角色的）评测每个组合；slug 以 bad 开头的直接失败
- POST /weights/eval：权重文件、参考音频都要在 VM 上；记一条评测
- POST /tts：权重、参考都要在 VM 上；写一条静音 wav 到 DATA/audio/<id>.wav（扫参的样本）
任务提交时就做完（GET /jobs/{id} 直接是结束状态），记进 tts.db 和任务日志。"""
import json
import os
import secrets
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ttsapi import datasets, db, library, settings, synth
from ttsapi.catalog import Catalog
from ttsapi.models import (AudioItem, CandidatesParams, CandidatesResult, ComboEval, CompareParams, CompareResult,
                           EvalRequest, JobItemDone, TrainEval, TrainRequest, TrainResult)
from ttsapi.refs import RefLibrary

library.open_configured()
catalog = Catalog()
refs = RefLibrary(catalog)
synth.catalog = catalog
synth.refs = refs


def combo(gpt, sovits, score):
    return ComboEval(gpt=gpt, sovits=sovits, n=12, cer_median=0.0, spk_sim_median=0.9, score_median=score,
                     score_mean=score)


def train(job, req: TrainRequest):
    list_path, _ = datasets.scan()[req.dataset_id]
    if req.slug.startswith("bad"):
        raise RuntimeError("boom")
    run_name = f"{req.slug}-{datetime.now():%Y%m%d-%H%M%S}"
    train_dir, wdir = settings.DATA / "training" / run_name, settings.DATA / "weights" / run_name
    train_dir.mkdir(parents=True)
    wdir.mkdir(parents=True)
    lines = [line.split("|", 1) for line in list_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    (train_dir / "train.list").write_text("".join(f"{datasets.resolve_audio(p, list_path)}|{rest}\n"
                                                  for p, rest in lines), encoding="utf-8")
    files = dict(gpt=[wdir / f"{run_name}-e5.ckpt", wdir / f"{run_name}-e10.ckpt"],
                 sovits=[wdir / f"{run_name}_e4_s8.pth"])
    for p in (*files["gpt"], *files["sovits"]):
        p.write_bytes(p.name.encode() * 100)
    catalog.register_run(req.slug, req.dataset_id, dict(
        run_name=run_name, engine=req.engine, job_id=job["id"], best=None, filter=None, lines_used=len(lines),
        minutes_used=0.1, params=req.model_dump()), files, train_dir=train_dir, move=True)
    refs.reload()
    ref_id = req.ref_id or refs.default_for(req.slug)
    combos = sorted((combo(g.name, s.name, 90 + i) for i, (g, s) in enumerate(
        (g, s) for g in files["gpt"] for s in files["sovits"])), key=lambda c: -c.score_median)
    for c in combos:
        catalog.record_eval(req.engine, req.slug, ref_id, job["id"], "hard", c)
    catalog.set_best(run_name, combos[0].gpt, combos[0].sovits)
    job["train_result"] = TrainResult(
        slug=req.slug, engine=req.engine, run_name=run_name, lines_used=len(lines), minutes_used=0.1,
        gpt_weights=[p.name for p in files["gpt"]], sovits_weights=[p.name for p in files["sovits"]],
        eval=TrainEval(job_id=job["id"], engine=req.engine, character=req.slug, ref_id=ref_id, eval_set="hard",
                       sentences=["s"], candidates_per_sentence=1, combos=combos, best=combos[0])).model_dump()


def weights_eval(job, req: EvalRequest):
    _, gpt_path, _, sovits_path = catalog.resolve(req.engine, req.character, req.gpt, req.sovits)
    for p in (gpt_path, sovits_path, refs.index[req.ref_id]["path"]):
        if not Path(p).exists():
            raise FileNotFoundError(p)
    c = combo(req.gpt, req.sovits, 93.5)
    catalog.record_eval(req.engine, req.character, req.ref_id, job["id"], req.eval_set, c)
    job["eval_result"] = TrainEval(job_id=job["id"], engine=req.engine, character=req.character, ref_id=req.ref_id,
                                   eval_set=req.eval_set, sentences=["s"], candidates_per_sentence=3, combos=[c],
                                   best=c).model_dump()


def tts(body):
    _, gpt_path, _, sovits_path = catalog.resolve(body["engine"], body["character"], body["gpt"], body["sovits"])
    for p in (gpt_path, sovits_path, refs.index[body["ref_id"]]["path"]):
        if not Path(p).exists():
            raise FileNotFoundError(p)
    audio_id = secrets.token_hex(6)
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    (settings.AUDIO_DIR / f"{audio_id}.wav").write_bytes(b"RIFF" + body["text"].encode())
    return dict(id=audio_id, seed=body["seed"], ref_id=body["ref_id"], duration=1.0, infer_sec=0.1)


def _make_candidates(p, engine: str, n: int, base_seed: int) -> CandidatesResult:
    gpt, gpt_path, sovits, sovits_path = catalog.resolve(engine, p.character, p.gpt, p.sovits, remote=True,
                                                         version=p.version)
    ref = refs.index[p.ref_id]
    if not Path(ref["path"]).exists():
        raise FileNotFoundError(ref["path"])
    for rid in p.aux_ref_ids or ():
        if not Path(refs.index[rid]["path"]).exists():
            raise FileNotFoundError(refs.index[rid]["path"])
    for k, wpath in (("gpt", gpt_path), ("sovits", sovits_path)):
        if Path(wpath) != settings.ENGINES[engine][f"pretrained_{k}"] and not Path(wpath).exists():
            raise FileNotFoundError(wpath)
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for i in range(n):
        now = time.time()
        aid = f"vm-{engine}-{secrets.token_hex(4)}"
        (settings.AUDIO_DIR / f"{aid}.wav").write_bytes(b"RIFF" + f"{engine}:{p.text}:{base_seed + i}".encode())
        item = AudioItem(
            id=aid, created=synth.iso(now), audio_url=f"/audio/{aid}", text=p.text, text_lang=p.text_lang,
            character=p.character, engine=engine, gpt=gpt, sovits=sovits, ref_id=ref["id"], seed=base_seed + i,
            top_k=p.top_k, top_p=p.top_p, temperature=p.temperature, repetition_penalty=p.repetition_penalty,
            speed=p.speed, fragment_interval=p.fragment_interval, text_split_method=p.text_split_method,
            sample_steps=p.sample_steps, aux_ref_ids=list(p.aux_ref_ids or ()),
            duration=2.5, infer_sec=1.25 + 0.1 * i, synth_wall_sec=1.35 + 0.1 * i,
            semantic_tokens=50, vram_peak_mb=2048, ratio_vs_ref=[1.0],
            asr_text=p.text, cer=0.0, cer_kana=0.0, spk_sim=0.92, score=97.6 - i, compute="local",
        )
        synth.save_audio(item.model_dump(exclude={"review"}), now)
        items.append(item)
    return CandidatesResult(engine=engine, ref_id=ref["id"], ref_text=ref["text"], elapsed_sec=2.5, candidates=items)


def candidates_job(job, req: CandidatesParams):
    seed = req.seed if req.seed >= 0 else 100
    if req.text == "FAIL_AFTER_ONE":
        partial = _make_candidates(req, req.engine, 1, seed)
        item = partial.candidates[0]
        done = JobItemDone(audio_id=item.id, audio_url=item.audio_url, score=item.score, seed=item.seed,
                           done=1, total=req.n).model_dump()
        job["completed"] = [done]
        job["latest"] = done
        raise RuntimeError("midway boom")
    res = _make_candidates(req, req.engine, req.n, seed)
    job["completed"] = [
        JobItemDone(audio_id=c.id, audio_url=c.audio_url, score=c.score, seed=c.seed, done=i + 1, total=req.n).model_dump()
        for i, c in enumerate(res.candidates)
    ]
    if job["completed"]:
        job["latest"] = job["completed"][-1]
    job["candidates_result"] = res.model_dump()


def compare_job(job, req: CompareParams):
    seed = req.seed if req.seed >= 0 else 200
    ref = refs.index[req.ref_id]
    results = [
        _make_candidates(
            req if e == req.engine else req.model_copy(update=dict(gpt=None, sovits=None, version=None)),
            e, req.n, seed,
        )
        for e in req.engines
    ]
    job["compare_result"] = CompareResult(
        text=req.text, ref_id=ref["id"], ref_text=ref["text"], base_seed=seed, elapsed_sec=4.0, results=results,
    ).model_dump()


def run_job(kind, body):
    now = time.time()
    job = dict(id=f"{kind}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}", kind=kind, status="running",
               stage="完成", progress=1.0, target=body.get("slug") or body.get("character"), error=None)
    settings.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    v5_env = dict(v5_root=os.environ.get("TTSAPI_GSV_V5_ROOT"), v5_python=os.environ.get("TTSAPI_V5_PYTHON"))
    (settings.JOBS_DIR / f"{job['id']}.log").write_text(
        f"{kind} {json.dumps(body)} env={json.dumps(v5_env)}\n", encoding="utf-8")
    try:
        if kind == "train":
            train(job, TrainRequest(**body))
        elif kind == "eval":
            weights_eval(job, EvalRequest(**body))
        elif kind == "candidates":
            candidates_job(job, CandidatesParams(**body))
        elif kind == "compare":
            compare_job(job, CompareParams(**body))
        job["status"] = "succeeded"
    except Exception as e:
        job.update(status="failed", stage="失败", error=f"{type(e).__name__}: {e}")
    db.put_doc("jobs", job["id"], now, job)
    return job


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            return self.send(dict(version="sim", commit="sim"))
        if self.path.startswith("/jobs/"):
            return self.send(db.get_doc("jobs", self.path.split("/")[2]))
        return self.send({"detail": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/library/claim":
            lib = library.current()
            lib.claim()
            return self.send(dict(id=lib.info["id"], counts=lib.counts()))
        if self.path == "/tts":
            return self.send(tts(body))
        if self.path in ("/train", "/weights/eval", "/tts/candidates/jobs", "/compare/jobs"):
            kind = {
                "/train": "train",
                "/weights/eval": "eval",
                "/tts/candidates/jobs": "candidates",
                "/compare/jobs": "compare",
            }[self.path]
            return self.send(run_job(kind, body))
        return self.send({"detail": "not found"}, 404)


HTTPServer(("127.0.0.1", 9880), Handler).serve_forever()
