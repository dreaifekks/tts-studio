# 在 Colab VM 上跑（vm_setup.py 之后）：起 tts-core gateway（127.0.0.1:9880），资料库是 /content/library（认领成本机管理），
# 按 job.json 的 tasks 依次执行，全部走 gateway 自己的 HTTP 接口：
#   {"kind": "train", "job_id", ...TrainRequest 的字段}                       → POST /train（训后自动评测、选默认组合）
#   {"kind": "eval",  "job_id", ...EvalRequest 的字段（character、ref_id 已由控制端定好）} → POST /weights/eval
# job_id 是控制端的任务 id：结束时 VM gateway 的任务 id 全部换成它（tts.db 的任务记录、任务日志、资料库里 run 的 job_id、
# 评测文件），控制端导入时 run 和评测记在它自己的任务名下。
# 一个任务失败不影响后面的任务，但最后退出码非零（summary.json 里标出失败的任务）。
# 结束时删掉资料库里输入带来的权重文件（控制端已有，不用再下载回去）；结果由 vm_pack.py 打包。
# 扫参（ttsapi/compute/colab_sweep.py）逐组跑、逐组取回：python3 vm_run.py <任务序号>... 只跑这几个任务，summary.json
# 按序号并进之前几次的；这时不删输入权重（扫参只有训练任务，没有输入权重）。job.json 里可选：
#   keep：训练任务训后评测前 keep 个组合用到的权重才带回（summary 里记 run 和 keep，vm_pack.py 按它挑权重文件）
#   samples、sample_texts：训练任务给前 samples 个组合各生成 sample_texts（同一参考、同一 seed），放进资料库
#     runs/<run>/samples/<gpt>+<sovits>/<序号>.wav，清单 runs/<run>/samples/items.json（随 run 的小文件导进控制端资料库）
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/content/job")
from vm_lib import API, CFG, DATA, GSV, GSV_V5, JOB, LIB, PY, PY_V5, mark, wants_v5  # noqa: E402

H = "http://127.0.0.1:9880"
ONLY = [int(a) for a in sys.argv[1:]]
SUMMARY = JOB / "summary.json"
SAMPLE_SEED = 1234
ENDPOINTS = {
    "train": "/train",
    "eval": "/weights/eval",
    "candidates": "/tts/candidates/jobs",
    "compare": "/compare/jobs",
}


def call(method, path, body=None, timeout=60):
    req = urllib.request.Request(H + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code} {e.read().decode(errors='replace')[:500]}") from None


def wait_job(job_id, label):
    """轮询到任务结束；stage 每变一次记一条（训练的各步、评测各组合），用来算阶段耗时。"""
    last = None
    while True:
        j = call("GET", f"/jobs/{job_id}")
        if j["stage"] != last:
            last = j["stage"]
            mark(f"{label}.stage", "event", stage=last, progress=j["progress"])
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(5)


def gpu_mem():
    out = subprocess.run("nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits", shell=True,
                         capture_output=True, text=True).stdout.split(",")
    return dict(used_mb=int(out[0]), total_mb=int(out[1])) if len(out) == 2 else None


def dump(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def adopt(ids):
    """VM 任务 id -> 控制端任务 id（gateway 已停）。"""
    conn = sqlite3.connect(DATA / "tts.db")
    for vm_id, job_id in ids.items():
        d = json.loads(conn.execute("SELECT data FROM jobs WHERE id=?", (vm_id,)).fetchone()[0])
        d["id"] = job_id
        for r in ((d.get("train_result") or {}).get("eval"), d.get("eval_result")):
            if r:
                r["job_id"] = job_id
        conn.execute("UPDATE jobs SET id=?, data=? WHERE id=?", (job_id, json.dumps(d, ensure_ascii=False), vm_id))
        log = DATA / "jobs" / f"{vm_id}.log"
        if log.exists():
            log.rename(log.with_name(f"{job_id}.log"))
    conn.commit()
    conn.close()
    for f in LIB.glob("runs/*/run.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d["job_id"] in ids:
            dump(f, dict(d, job_id=ids[d["job_id"]]))
    for f in LIB.glob("evals/*/*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d["job_id"] in ids:
            job_id = ids[d["job_id"]]
            dump(f.with_name(f"{job_id}.json"), dict(d, id=job_id, job_id=job_id))
            f.unlink()


def samples(tr, ev):
    """训练任务训后评测前 samples 个组合各生成 sample_texts，返回资料库里的样本目录。"""
    out = LIB / "runs" / tr["run_name"] / "samples"
    items = []
    for c in ev["combos"][:CFG["samples"]]:
        d = out / f"{c['gpt']}+{c['sovits']}"
        d.mkdir(parents=True, exist_ok=True)
        for k, text in enumerate(CFG["sample_texts"]):
            a = call("POST", "/tts", dict(text=text, character=tr["slug"], engine=tr["engine"], gpt=c["gpt"],
                                          sovits=c["sovits"], ref_id=ev["ref_id"], seed=SAMPLE_SEED, return_json=True,
                                          compute="local"), timeout=600)
            shutil.copy2(DATA / "audio" / f"{a['id']}.wav", d / f"{k:02d}.wav")
            items.append(dict(gpt=c["gpt"], sovits=c["sovits"], k=k, file=f"{d.name}/{k:02d}.wav", seed=a["seed"],
                              duration=a.get("duration"), infer_sec=a.get("infer_sec")))
    dump(out / "items.json", dict(texts=CFG["sample_texts"], ref_id=ev["ref_id"], items=items))
    return f"runs/{tr['run_name']}/samples"


# 输入带来的权重文件（评测用的控制端权重）：结束时删掉，meta.json 留着
INPUT_WEIGHTS = [f for f in LIB.glob("weights/*/*/*/*") if f.name != "meta.json"]

# ---------------------------------------------------------------- gateway
# Colab kernel 的环境变量会传给子进程：PYTHONPATH=/env/python（Colab 自己的包）、MPLBACKEND=inline。gateway 不要它们；
# gateway 再起的 worker、训练进程由 tts-core 自己设环境（osutil.child_env）
env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "MPLBACKEND")}
env.update(TTSAPI_GSV_ROOT=str(GSV), TTSAPI_LIBRARY=str(LIB), TTSAPI_DATA_DIR=str(DATA),
           TTSAPI_MACHINE_NAME=f"colab-{CFG['run_id']}", PYTHONUNBUFFERED="1")
if wants_v5() or GSV_V5.exists():
    env.update(TTSAPI_GSV_V5_ROOT=str(GSV_V5), TTSAPI_V5_PYTHON=str(PY_V5))
mark("gateway", "start")
gw = subprocess.Popen([str(PY), "-m", "ttsapi.gateway"], cwd=API, env=env,
                      stdout=open(JOB / "gateway.out", "ab"), stderr=subprocess.STDOUT)
for _ in range(300):
    try:
        h = call("GET", "/health", timeout=3)
        break
    except Exception:
        if gw.poll() is not None:
            raise RuntimeError("gateway 起不来，见 /content/job/gateway.out") from None
        time.sleep(1)
lib = call("POST", "/library/claim")
mark("gateway", version=h["version"], commit=h["commit"], library=lib["id"], counts=lib["counts"])

results = [r for r in json.loads(SUMMARY.read_text(encoding="utf-8")) if r["index"] not in ONLY] \
    if ONLY and SUMMARY.exists() else []
ids = {}
try:
    for i, task in enumerate(CFG["tasks"]):
        if ONLY and i not in ONLY:
            continue
        tag = task.get("slug") or task.get("gpt") or task.get("character") or task.get("engine") or "job"
        label = f"task{i:02d}-{task['kind']}-{tag}"
        body = {k: v for k, v in task.items() if k not in ("kind", "job_id") and v is not None}
        mark(label, "start", job_id=task["job_id"], **body)
        try:
            j = call("POST", ENDPOINTS[task["kind"]], body)
            ids[j["id"]] = task["job_id"]
            j = wait_job(j["id"], label)
        except Exception as e:  # 提交就失败（参数不对等）或 gateway 没了：记下来，接着跑下一个
            j = dict(status="failed", error=str(e))
        ev = (j.get("train_result") or {}).get("eval") or j.get("eval_result") or {}
        best = ev.get("best")
        mark(label, status=j["status"], error=j.get("error"), ref_id=ev.get("ref_id"),
             best={k: best.get(k) for k in ("gpt", "sovits", "score_median", "score_mean")} if best else None)
        mark("gpu", "event", after=label, **(gpu_mem() or {}))
        res = dict(index=i, task=label, job=task["job_id"], status=j["status"], error=j.get("error"))
        tr = j.get("train_result")
        if tr and (LIB / "runs" / tr["run_name"] / "run.json").exists():  # 训完登记了（训后评测失败时权重也带回）
            res["run"] = tr["run_name"]
        if tr and j["status"] == "succeeded":
            if CFG.get("keep"):
                res["keep"] = sorted({c[k] for c in ev["combos"][:CFG["keep"]] for k in ("gpt", "sovits")})
            if CFG.get("samples"):
                try:  # 样本没生成出来不算训练失败，记下原因
                    res["samples"] = samples(tr, ev)
                except Exception as e:
                    res["samples_error"] = str(e)
                mark(label + ".samples", "event", samples=res.get("samples"), error=res.get("samples_error"))
        results.append(res)
finally:
    mark("stop", "start")
    gw.terminate()
    try:
        gw.wait(15)
    except subprocess.TimeoutExpired:
        gw.kill()
    adopt(ids)
    if not ONLY:
        for f in INPUT_WEIGHTS:
            f.unlink()
    dump(SUMMARY, sorted(results, key=lambda r: r["index"]))
    mark("stop")
print(json.dumps(results, ensure_ascii=False, indent=1))
failed = [r["task"] for r in results if r["status"] != "succeeded" and (not ONLY or r["index"] in ONLY)]
if failed:
    print(f"FAILED TASKS: {failed}", flush=True)
    sys.exit(1)
