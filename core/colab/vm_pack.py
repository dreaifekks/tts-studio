# 在 Colab VM 上跑（vm_run.py 之后；vm_setup 或 vm_run 失败时也跑）：结果打成一个 tar 给控制端下载，
# 布局就是 ttsapi.importer 认的来源：
#   library/        VM 的资料库（含 library.json；训练出的 run、权重、评测；不带输入的数据集 tar，输入的权重文件已删掉）
#   data/tts.db     VM gateway 的任务记录（任务 id 已换成控制端的）
#   data/jobs/      任务日志 <job_id>.log
#   training/       训练子进程原始输出 <run>/runner.out（供控制端本地排查，不进入客户端 log_tail）
#   job/            job.json、timeline.jsonl、gateway.out、各脚本的日志、summary.json、venv-freeze
# 还没走到的部分（vm_setup 失败时没有 library/、data/）就不带。用法：python3 vm_pack.py <输出 tar> [任务序号...]
# 给了任务序号（扫参逐组取回）时只带这几个任务的东西：按它们的控制端任务 id（job.json）找资料库里登记的 run（run.json 的
# job_id，不靠 summary.json），只带这些 run 的目录（含样本）和权重、这些任务的评测、任务记录和日志；之前取回过的组不再带。
# summary.json 里任务记了 keep（训后评测排过名）时，它的 run 只带 keep 里的权重；没排名（评测失败、结果没取到）时全带。
import json
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, "/content/job")
from vm_lib import API, CFG, DATA, JOB, LIB  # noqa: E402

ONLY = {int(a) for a in sys.argv[2:]}
JOBS = {CFG["tasks"][i]["job_id"] for i in ONLY}
summary = json.loads((JOB / "summary.json").read_text(encoding="utf-8")) if (JOB / "summary.json").exists() else []
KEEP = {r["run"]: r["keep"] for r in summary if r.get("run") and r.get("keep")}
WANTED = {d["run_name"] for d in (json.loads(f.read_text(encoding="utf-8")) for f in LIB.glob("runs/*/run.json"))
          if d["job_id"] in JOBS}
TRAIN = API / "training"
WANTED_RUNS = set(WANTED)
AUDIO_IDS = set()
if (DATA / "tts.db").exists():
    conn = sqlite3.connect(DATA / "tts.db")
    rows = conn.execute(
        f"SELECT data FROM jobs WHERE id IN ({','.join('?' * len(JOBS))})",
        sorted(JOBS),
    ).fetchall() if ONLY else conn.execute("SELECT data FROM jobs").fetchall()
    for (raw,) in rows:
        doc = json.loads(raw) or {}
        tr = doc.get("train_result") or {}
        if ONLY and tr.get("run_name"):
            WANTED_RUNS.add(tr["run_name"])
        cr = doc.get("candidates_result") or {}
        for item in cr.get("candidates") or ():
            if item.get("id"):
                AUDIO_IDS.add(item["id"])
        cmp = doc.get("compare_result") or {}
        for part in cmp.get("results") or ():
            for item in part.get("candidates") or ():
                if item.get("id"):
                    AUDIO_IDS.add(item["id"])
        for done in doc.get("completed") or ():
            if done.get("audio_id"):
                AUDIO_IDS.add(done["audio_id"])
        latest = doc.get("latest") or {}
        if latest.get("audio_id"):
            AUDIO_IDS.add(latest["audio_id"])
    conn.close()


def exclude(info):
    """不带回的（输入的数据集 tar；给了任务序号时别的任务的 run、评测；没挑中的权重）：返回 None（tarfile 的 filter）。"""
    rel = info.name.split("/")
    if rel[1:2] == ["datasets"] and len(rel) == 3 and rel[2].endswith(".tar"):
        return None
    if ONLY and rel[1:2] == ["runs"] and len(rel) >= 3 and rel[2] not in WANTED:
        return None
    if ONLY and rel[1:2] == ["evals"] and len(rel) == 4 and Path(rel[3]).stem not in JOBS:
        return None
    if not (ONLY or KEEP) or rel[1:2] != ["weights"] or len(rel) != 6 or rel[-1] == "meta.json" or not info.isfile():
        return info
    meta = json.loads((LIB.joinpath(*rel[1:-1]) / "meta.json").read_text(encoding="utf-8"))
    run = meta.get("run")
    if ONLY and run not in WANTED or run in KEEP and meta["name"] not in KEEP[run]:
        return None
    return info


with tarfile.open(sys.argv[1], "w") as tf, tempfile.TemporaryDirectory() as tmp:
    tf.add(JOB, "job")
    if (LIB / "library.json").exists():
        tf.add(LIB, "library", filter=exclude)
    if (DATA / "tts.db").exists():
        db = DATA / "tts.db"
        if ONLY:  # 只带这几个任务的记录
            db = Path(tmp) / "tts.db"
            src, dst = sqlite3.connect(DATA / "tts.db"), sqlite3.connect(db)
            src.backup(dst)
            src.close()
            dst.execute(f"DELETE FROM jobs WHERE id NOT IN ({','.join('?' * len(JOBS))})", sorted(JOBS))
            dst.commit()
            dst.close()
        tf.add(db, "data/tts.db")
    if (DATA / "jobs").is_dir():
        tf.add(DATA / "jobs", "data/jobs",
               filter=lambda i: None if ONLY and i.isfile() and Path(i.name).stem not in JOBS else i)
    if AUDIO_IDS and (DATA / "audio").is_dir():
        for aid in sorted(AUDIO_IDS):
            for ext in (".wav", ".png"):
                f = DATA / "audio" / f"{aid}{ext}"
                if f.is_file():
                    tf.add(f, f"data/audio/{aid}{ext}")
    if TRAIN.is_dir():
        for out in sorted(TRAIN.glob("*/runner.out")):
            if not ONLY or out.parent.name in WANTED_RUNS:
                tf.add(out, f"training/{out.parent.name}/runner.out")
