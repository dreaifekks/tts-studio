"""把别处（Colab VM 等）跑出来的训练结果写进资料库：训练 run、权重文件、评测、角色、训练任务记录。

来源是一个目录或 .tar（colab/ 的作业打的 results.tar），里面有
  另一台机器的资料库（含 library.json 的那层，格式同 ttsapi/library）
  data/tts.db、data/jobs/<job>.log（可选：训练任务记录和日志，进本机的 tts.db）
都按最浅的一层找。

规则：
- 按训练 run 导入（来源资料库的 runs/<run>/run.json）；资料库里已有同名 run 的跳过，所以重复导入同一份结果没有副作用。
  run.json 最后写，是这个 run 的提交记录：之前放进资料库的权重目录里看不到（catalog 只认已提交的 run 的权重），
  中途失败时这个 run 不算导入，重试会完整重来（覆盖上次放了一半的）。
- 先整批核对要导入的全部权重的 sha256（来源文件和来源 meta.json 里的比），有对不上的就报错、整批不导入，资料库里
  什么都不写；都对上才复制进资料库 weights/<角色>/<engine>/<id>/（复制时再算一遍）。显示名、备注、隐藏跟着 meta.json 过来。
- 和这个引擎的底模同名的权重改名成「<角色>-<原名>」（catalog.stored_name，原名记在 meta 的 original_name）；run.json、
  best、手动默认、评测组合里这个角色的权重跟着用新名字，导入摘要里也是新名字。
- 来源只带了部分权重（Colab 扫参只传评测前几名）时导入在的那些，缺的不登记；GPT 或 SoVITS 一个都没有才跳过。
- runs/<run>/ 的小文件（train.list、配置、日志）复制过来，train.list 里的音频路径换算成本机数据集路径；
  Colab 扫参生成的样本 runs/<run>/samples/（items.json + wav）也复制过来。
- 角色：资料库里没有就新建（训练出来的角色），有就把 run 加进去；数据集不一样时跳过。来源里这个角色在这个引擎下
  手动设的默认，在资料库里还没手动设时跟过来。
- 评测：只导 GPT、SoVITS 两半在导入后的资料库里都有的组合：这些 run 的权重的评测（来源 evals/ 里的组合，另一半是
  这次导入的或本库已有的），加上只用到本库导入前已有权重（底模、7 月权重、以前导入的 run）的评测（Colab 评测作业评的是
  控制端资料库里的权重，不属于任何新 run）；同一任务的评测并进同一个文件，已有的组合不重复。
- 训练任务记录（tts.db 的 jobs、日志）本机没有才导入，导入后 GET /train/{job_id}/eval 能看。记录里的角色、权重换成
  资料库里登记的，评测结果按导入的评测记录重算（job_record）：缺失权重的组合去掉，最好的组合重新选。
  本机已有、已结束、结果为空的任务（Colab 作业写库失败、训练 run 被跳过、写库时取消后用结果 tar 重试）按任务 id 补上
  结果（train_result / eval_result，同样过 job_record），摘要里是 filled。
- into_builtin=True：用某个内置角色的数据集训练的角色（Colab 扫参的 alice-v2proplus-s16g20 这种）并到那个内置角色名下，
  run 名和权重文件名不变，评测和默认组合跟着记到内置角色。
用法：gateway 在跑时用 POST /runs/import；gateway 没跑时可用（用配置的资料库）
  python -m ttsapi.importer <目录或 .tar> [--run <run_name> ...] [--into-builtin]
"""
import argparse
import errno
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

from ttsapi import db, library, settings, synth
from ttsapi.catalog import KINDS, Catalog, copy_small_files, rank, run_rel, stored_name, weight_dir
from ttsapi.jobs import ACTIVE
from ttsapi.library import Library, sha256_of
from ttsapi.models import ComboEval


def _shallowest(src: Path, pattern: str):
    hits = sorted(src.rglob(pattern), key=lambda p: len(p.parts))
    return hits[0] if hits else None


def import_runs(src, catalog=None, refs=None, runs: list = None, into_builtin: bool = False) -> dict:
    """导入 src（目录或 .tar）里的训练 run；runs 给了就只导这些 run_name；into_builtin 见模块说明。
    refs 是 gateway 的参考库（导入后重建），CLI 下为 None。返回导入摘要。"""
    src = Path(src)
    catalog = catalog or Catalog()
    with tempfile.TemporaryDirectory(prefix="ttsapi-import-") as tmp:
        if src.is_file():
            with tarfile.open(src) as tf:
                # filter="data"（拒绝绝对路径、..、设备文件）在 3.10.12+ 才有
                tf.extractall(tmp, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
            src = Path(tmp)
        info = _shallowest(src, "library.json")
        if info is None:
            raise FileNotFoundError(f"{src} 里没有资料库（library.json）")
        # 来源资料库只读：本机身份和它的 owner 不同，写入会被挡住；索引放临时目录
        source = Library(info.parent, Path(tmp) / ".index", dict(id="import", name="import"))
        out = _import(source, _shallowest(src, "data/tts.db"), catalog, runs, into_builtin)
    if refs is not None:
        refs.reload()
    return out


def _source_file(source: Library, engine: str, m: dict) -> Path:
    return source.path(weight_dir(m["character"], engine, m["name"])) / m["file"]


def _import(source: Library, sdb_path, catalog: Catalog, only: list, into_builtin: bool) -> dict:
    lib = library.current()
    lib.check_owner()
    sdb = sqlite3.connect(f"file:{sdb_path}?mode=ro", uri=True) if sdb_path else None
    if sdb:
        sdb.row_factory = sqlite3.Row
    src_chars = {c["slug"]: c for c in source.docs("characters")}
    builtin_by_dataset = {c["dataset_id"]: s for s, c in catalog.characters().items()
                          if c["source"] == "builtin"} if into_builtin else {}
    summary = dict(imported=[], skipped=[], evals=0, jobs=[], filled=[])
    # 导入前本库已有的权重记录 (engine, 角色, kind, 文件名)：底模按引擎配置认（角色 None），不看控制端装没装
    before = {(x["engine"], x["character"], x["kind"], x["name"]) for x in catalog.entries(remote=True)}
    todo = []  # 要导入的 run：(来源的 run.json, 资料库里登记用的角色名, 来源的 meta, 导入的权重, 缺的权重)
    for run in sorted(source.docs("runs"), key=lambda r: (r["created"], r["run_name"])):
        name, engine, src_slug = run["run_name"], run["engine"], run["character"]
        if only and name not in only:
            continue
        if catalog.has_run(name):
            summary["skipped"].append(dict(run=name, reason="资料库里已有"))
            continue
        # 资料库里登记用的角色名：并到内置角色时换成内置角色名，来源里查东西仍用 src_slug
        slug = builtin_by_dataset.get(run["dataset_id"], src_slug)
        mine = catalog.characters().get(slug)
        if mine and mine["dataset_id"] != run["dataset_id"]:
            summary["skipped"].append(dict(run=name, reason=f"资料库里的 {slug} 用的是数据集 {mine['dataset_id']}"))
            continue
        metas = {m["name"]: m for m in source.docs("weights") if m["run"] == name
                 and _source_file(source, engine, m).exists()}
        missing = sorted({n for k in KINDS for n in run[k]} - set(metas))
        kept = {k: [n for n in run[k] if n in metas] for k in KINDS}
        if not kept["gpt"] or not kept["sovits"]:
            summary["skipped"].append(dict(run=name, reason=f"缺权重文件 {missing}"))
            continue
        todo.append((run, slug, metas, kept, missing))

    # 先整批核对 sha256：来源就是暂存（tar 解在临时目录里），全部对上才往资料库里放；有对不上的整批不导入，资料库里不写东西
    bad = []
    for run, _, metas, kept, _ in todo:
        for n in (*kept["gpt"], *kept["sovits"]):
            sha = sha256_of(_source_file(source, run["engine"], metas[n]))
            if sha != metas[n]["sha256"]:
                bad.append(f"{n}（{sha} ≠ {metas[n]['sha256']}）")
    if bad:
        raise RuntimeError(f"sha256 和来源记录的不一致，传输可能损坏，这次什么都没导入：{'、'.join(bad)}")

    # 导入后资料库里能找到的权重（资料库里的名字）：评测组合两半都在这里面才导
    have = before | {(run["engine"], slug, k, stored_name(slug, run["engine"], n))
                     for run, slug, _, kept, _ in todo for k in KINDS for n in kept[k]}
    for run, slug, metas, kept, missing in todo:
        name, engine, src_slug = run["run_name"], run["engine"], run["character"]
        names = {k: [stored_name(slug, engine, n) for n in kept[k]] for k in KINDS}  # 资料库里的名字（add_weight 同样改）
        # 先放文件（权重、小文件），再写记录，run.json 最后写（提交：之前放进去的权重不算数，中途失败重试会完整重来）
        if source.path(f"runs/{name}").is_dir():
            copy_small_files(source.path(f"runs/{name}"), f"runs/{name}", skip={"run.json"})
            # Colab 扫参生成的样本（盲听素材）：清单 items.json 随小文件过来，音频在这里复制
            for f in sorted(source.path(f"runs/{name}/samples").rglob("*.wav")):
                lib.put_file(f"runs/{name}/{f.relative_to(source.path(f'runs/{name}')).as_posix()}", f)
        if slug not in catalog.characters():
            has_list = lib.path(f"runs/{name}/train.list").exists()
            catalog.add_character(slug, "user", run["dataset_id"],
                                  train_list=f"runs/{name}/train.list" if has_list else None)
        for n in (*kept["gpt"], *kept["sovits"]):
            m = metas[n]
            # 来源登记时已经改过名的（VM 跑的是同一套代码），原名跟着过来
            doc = catalog.add_weight(slug, engine, m["kind"], _source_file(source, engine, m), m["source"], run=name,
                                     name=n, display_name=m["display_name"], note=m["note"], hidden=m["hidden"],
                                     created=m["created"],
                                     **({"original_name": m["original_name"]} if "original_name" in m else {}))
            if doc["sha256"] != m["sha256"]:  # 核对之后来源文件又变了
                raise RuntimeError(f"{n} 的 sha256 和来源记录的不一致（{doc['sha256']} ≠ {m['sha256']}），来源在导入中被改动")
        n_evals = _import_evals(source, catalog, engine, set(kept["gpt"]) | set(kept["sovits"]), have, src_slug, slug)
        manual = src_chars.get(src_slug, {}).get("default_weights", {}).get(engine) or {}
        for k in KINDS:
            if manual.get(k) in kept[k] and not catalog.manual_default(engine, slug, k):
                catalog.set_default(engine, slug, k, stored_name(slug, engine, manual[k]))
        best = run.get("best") and {k: stored_name(slug, engine, run["best"][k]) for k in KINDS}
        if best and not all((engine, slug, k, best[k]) in have for k in KINDS):
            best = None
        doc = dict(run, character=slug, best=best, **names)
        job = sdb.execute("SELECT * FROM jobs WHERE id=?", (run["job_id"],)).fetchone() if sdb else None
        if job and not db.one("SELECT 1 FROM jobs WHERE id=?", job["id"]):
            log = Path(sdb_path).parent / "jobs" / f"{job['id']}.log"
            if log.exists():
                settings.JOBS_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(log, settings.JOBS_DIR / log.name)
            db.put_doc("jobs", job["id"], job["created"], job_record(json.loads(job["data"]), doc))
            summary["jobs"].append(job["id"])
        lib.put(run_rel(name), doc)
        summary["evals"] += n_evals
        summary["imported"].append(dict(character=slug, run=name, engine=engine, gpt=names["gpt"],
                                        sovits=names["sovits"], evals=n_evals, missing=missing,
                                        **({"source_character": src_slug} if slug != src_slug else {})))
    summary["evals"] += _import_existing_evals(source, catalog, before)
    if sdb:
        summary["filled"] = _fill_jobs(sdb, catalog, Path(sdb_path).parent.parent)
        sdb.close()
    return summary


def _record_audio_refs(record: dict) -> list[tuple[str, dict | None]]:
    """Collect ordered deduplicated (vm_audio_id, fallback_AudioItem_or_None) from results and completed items."""
    out = []
    seen = set()
    if not record:
        return out

    def add(aid: str | None, item: dict | None):
        if aid and aid not in seen:
            seen.add(aid)
            out.append((aid, item))

    cr = record.get("candidates_result") or {}
    for item in cr.get("candidates") or ():
        add(item.get("id"), item)
    cmp = record.get("compare_result") or {}
    for part in cmp.get("results") or ():
        for item in part.get("candidates") or ():
            add(item.get("id"), item)
    for done in record.get("completed") or ():
        add(done.get("audio_id"), None)
    latest = record.get("latest") or {}
    add(latest.get("audio_id"), None)
    return out


def _rewrite_audio_item(c: dict, id_map: dict[str, str], device_id: str | None) -> dict:
    c = dict(c)
    old_id = c.get("id")
    new_id = id_map.get(old_id, old_id)
    if new_id and new_id != old_id:
        c["id"] = new_id
        c["audio_url"] = f"/audio/{new_id}"
        if c.get("mel_url"):
            c["mel_url"] = f"/audio/{new_id}/mel.png"
    if device_id:
        c["compute"] = device_id
    return c


def rewrite_gen_result(field: str, result: dict, id_map: dict[str, str], device_id: str | None = None) -> dict:
    """Rewrite AudioItem IDs/URLs from id_map and stamp AudioItem.compute on candidates/compare results."""
    if not result:
        return result
    if field == "candidates_result":
        return dict(result, candidates=[_rewrite_audio_item(c, id_map, device_id) for c in result.get("candidates") or ()])
    if field == "compare_result":
        return dict(result, results=[
            dict(r, candidates=[_rewrite_audio_item(c, id_map, device_id) for c in r.get("candidates") or ()])
            for r in result.get("results") or ()
        ])
    return result


def rewrite_item_done(d: dict, id_map: dict[str, str]) -> dict:
    """Rewrite JobItemDone audio_id and audio_url according to id_map."""
    d = dict(d)
    old_id = d.get("audio_id")
    new_id = id_map.get(old_id, old_id)
    if new_id and new_id != old_id:
        d["audio_id"] = new_id
        d["audio_url"] = f"/audio/{new_id}"
    return d


def _atomic_copy_exclusive(src: Path, dst: Path) -> bool:
    """Atomically copy src to dst without overwriting any existing file. Returns False if dst already exists."""
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.stem}-", suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        shutil.copy2(src, tmp_path)
        try:
            os.link(tmp_path, dst)
            return True
        except FileExistsError:
            return False
        except OSError as e:
            if e.errno not in (errno.EPERM, errno.ENOTSUP, errno.EXDEV, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
                raise
            try:
                out_fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                return False
            with os.fdopen(out_fd, "wb") as out_f, open(tmp_path, "rb") as in_f:
                shutil.copyfileobj(in_f, out_f)
            return True
    finally:
        tmp_path.unlink(missing_ok=True)


def _audio_id_in_use(audio_id: str) -> bool:
    if db.one("SELECT 1 FROM audio WHERE id=?", audio_id) is not None:
        return True
    if db.one("SELECT 1 FROM cloud_audio_map WHERE local_audio_id=?", audio_id) is not None:
        return True
    return (settings.AUDIO_DIR / f"{audio_id}.wav").exists() or (settings.AUDIO_DIR / f"{audio_id}.png").exists()


def _import_one_gen_audio(source_job_id: str, aid: str, base_meta: dict, created: float, audio_src: Path,
                          device_id: str | None) -> str:
    wav = audio_src / f"{aid}.wav"
    if not wav.is_file():
        raise FileNotFoundError(f"云端产物缺少音频文件：{wav.name}")
    png = audio_src / f"{aid}.png"

    if source_job_id:
        mapped = db.one("SELECT local_audio_id FROM cloud_audio_map WHERE job_id=? AND vm_audio_id=?",
                        source_job_id, aid)
        if mapped is not None:
            local_id = mapped["local_audio_id"]
            if not (settings.AUDIO_DIR / f"{local_id}.wav").exists():
                _atomic_copy_exclusive(wav, settings.AUDIO_DIR / f"{local_id}.wav")
            if png.is_file() and not (settings.AUDIO_DIR / f"{local_id}.png").exists():
                _atomic_copy_exclusive(png, settings.AUDIO_DIR / f"{local_id}.png")
            if db.one("SELECT 1 FROM audio WHERE id=?", local_id) is None:
                meta = _rewrite_audio_item(base_meta, {aid: local_id}, device_id)
                meta.pop("review", None)
                synth.insert_audio(meta, created)
            return local_id

    first = True
    while True:
        cand_id = aid if first and not _audio_id_in_use(aid) else synth.new_audio_id()
        first = False
        if _audio_id_in_use(cand_id):
            continue
        dst_wav = settings.AUDIO_DIR / f"{cand_id}.wav"
        if not _atomic_copy_exclusive(wav, dst_wav):
            continue
        dst_png = settings.AUDIO_DIR / f"{cand_id}.png"
        created_png = False
        if png.is_file():
            if not _atomic_copy_exclusive(png, dst_png):
                dst_wav.unlink(missing_ok=True)
                continue
            created_png = True
        meta = _rewrite_audio_item(base_meta, {aid: cand_id}, device_id)
        meta.pop("review", None)
        try:
            synth.insert_audio(meta, created)
        except sqlite3.IntegrityError:
            dst_wav.unlink(missing_ok=True)
            if created_png:
                dst_png.unlink(missing_ok=True)
            continue
        if source_job_id:
            db.run("INSERT OR IGNORE INTO cloud_audio_map(job_id, vm_audio_id, local_audio_id) VALUES(?,?,?)",
                   source_job_id, aid, cand_id)
        return cand_id


def import_gen_audio(src: Path, record: dict = None, device_id: str = None, job_id: str = None) -> dict[str, str]:
    """Import cloud-generated candidates/compare audio (including partial completed items) into AUDIO_DIR and tts.db
    without ever overwriting existing WAVs or audio rows. Returns {vm_audio_id: local_audio_id}."""
    sdb_path = src / "data" / "tts.db"
    audio_src = src / "data" / "audio"
    rows_by_id = {}
    entries: list[tuple[str, str, dict | None]] = []
    sdb = sqlite3.connect(f"file:{sdb_path}?mode=ro", uri=True) if sdb_path.is_file() else None
    try:
        if sdb is not None:
            sdb.row_factory = sqlite3.Row
            tables = {r[0] for r in sdb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "audio" in tables:
                for row in sdb.execute("SELECT id, created, meta FROM audio"):
                    rows_by_id[row["id"]] = (row["created"], json.loads(row["meta"]))
            if record is None and "jobs" in tables:
                for row in sdb.execute("SELECT id, data FROM jobs"):
                    doc = json.loads(row["data"])
                    for aid, fallback in _record_audio_refs(doc):
                        entries.append((job_id or row["id"], aid, fallback))
            else:
                jid = job_id or (record or {}).get("id") or ""
                entries = [(jid, aid, fallback) for aid, fallback in _record_audio_refs(record)]
        else:
            jid = job_id or (record or {}).get("id") or ""
            entries = [(jid, aid, fallback) for aid, fallback in _record_audio_refs(record)]
    finally:
        if sdb is not None:
            sdb.close()
    if not entries:
        return {}
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    id_map: dict[str, str] = {}
    for source_job_id, aid, fallback in entries:
        if aid in id_map:
            continue
        if aid in rows_by_id:
            created, base_meta = rows_by_id[aid]
        elif fallback is not None:
            base_meta = dict(fallback)
            try:
                created = datetime.fromisoformat(base_meta["created"]).timestamp() if base_meta.get("created") else time.time()
            except ValueError:
                created = time.time()
        else:
            # Completed entry not persisted in VM audio table; skip.
            continue
        id_map[aid] = _import_one_gen_audio(source_job_id, aid, base_meta, created, audio_src, device_id)
    return id_map


def _fill_jobs(sdb, catalog: Catalog, src_root: Path = None) -> list:
    """本机 tts.db 里已有、已经结束、结果（train_result / eval_result / candidates_result / compare_result）为空的任务
    （Colab 作业的原任务：写库失败、训练 run 被跳过、写库时被取消），用来源里同一任务的记录补上结果。返回补上的任务 id。"""
    out = []
    for row in sdb.execute("SELECT id, data FROM jobs"):
        mine = db.one("SELECT created, data FROM jobs WHERE id=?", row["id"])
        if mine is None:
            continue
        job, data = json.loads(mine["data"]), json.loads(row["data"])
        if job.get("status") in ACTIVE:
            continue
        field = {
            "train": "train_result",
            "eval": "eval_result",
            "candidates": "candidates_result",
            "compare": "compare_result",
        }.get(job["kind"], "eval_result")
        if field in ("candidates_result", "compare_result"):
            need_result = bool(data.get(field) and not job.get(field))
            need_completed = bool(data.get("completed") and not job.get("completed"))
            if not (need_result or need_completed):
                continue
            dev = (job.get("compute") or {}).get("device")
            id_map = import_gen_audio(src_root, record=data, device_id=dev, job_id=row["id"]) \
                if src_root is not None else {}
            patch = {}
            if need_result:
                patch[field] = rewrite_gen_result(field, data[field], id_map, dev)
            if need_completed:
                patch["completed"] = [rewrite_item_done(d, id_map) for d in data["completed"]
                                      if d.get("audio_id") in id_map]
            if data.get("latest") and not job.get("latest") and data["latest"].get("audio_id") in id_map:
                patch["latest"] = rewrite_item_done(data["latest"], id_map)
            if patch:
                db.put_doc("jobs", row["id"], mine["created"], dict(job, **patch))
                out.append(row["id"])
            continue
        if not data.get(field) or job.get(field) \
                or field == "train_result" and not catalog.has_run(data[field]["run_name"]):
            continue
        result = job_record(data)[field]
        if result:
            db.put_doc("jobs", row["id"], mine["created"], dict(job, **{field: result}))
            out.append(row["id"])
    return out


def job_record(data: dict, run: dict = None) -> dict:
    """来源（VM）的任务记录（Job 的 dict）对上资料库：
    - 训练任务的角色、权重换成资料库里这个 run 登记的（run 是它的 run.json 内容，不给就读资料库里的）：并到内置角色、
      只导入了部分权重时和来源不同
    - 训练后评测（train_result.eval）、手动评测（eval_result）按资料库里这个任务的评测记录重算：只剩导进来的组合（两半
      都在资料库里），最好的组合重新选，角色用评测记录里的（并到内置角色时是内置角色）；一组都没导进来时为 None
    Colab 作业把 VM 的任务记录填回 train_result / eval_result 前也要过一遍。"""
    tr = data.get("train_result")
    if tr:
        run = run or Catalog.run(tr["run_name"])
        tr = dict(tr, slug=run["character"], gpt_weights=run["gpt"], sovits_weights=run["sovits"],
                  eval=tr["eval"] and _imported_eval(tr["eval"]))
        data = dict(data, target=run["character"], train_result=tr)
    if data.get("eval_result"):
        data = dict(data, eval_result=_imported_eval(data["eval_result"]))
    return data


def _imported_eval(ev: dict):
    """TrainEval 的 dict 换成资料库里同一任务的评测记录里的组合（按 score 排序）、最好的组合和角色。"""
    lib = library.current()
    rel = Catalog.eval_rel(ev["job_id"])
    if not lib.has(rel):
        return None
    doc = lib.get(rel)
    combos = sorted((ComboEval(**c) for c in doc["combos"]), key=rank, reverse=True)
    return dict(ev, character=doc["character"], combos=[c.model_dump() for c in combos], best=combos[0].model_dump())


def _merge_eval(catalog: Catalog, doc: dict, combos: list, character: str) -> int:
    """评测组合并进资料库里同一任务（job_id）的评测文件，已有的组合（gpt、sovits、评测时间都一样）不重复。返回新增数。"""
    lib = library.current()
    rel = catalog.eval_rel(doc["job_id"])
    mine = lib.get(rel) if lib.has(rel) else dict(doc, character=character, combos=[])
    have = {(c["gpt"], c["sovits"], c["evaluated_at"]) for c in mine["combos"]}
    new = [c for c in combos if (c["gpt"], c["sovits"], c["evaluated_at"]) not in have]
    if new:
        lib.put(rel, dict(mine, combos=[*mine["combos"], *new]))
    return len(new)


def _accepted(engine: str, c: dict, have: set) -> bool:
    """评测组合的两半在本库都有对应的权重记录（have：(engine, 角色, kind, 文件名)，底模角色 None）：记了归属
    （gpt_owner / sovits_owner）的按归属认，没记的旧记录按文件名。"""
    names = {(e, k, n) for e, _, k, n in have}
    return all((engine, c[f"{k}_owner"], k, c[k]) in have if f"{k}_owner" in c else (engine, k, c[k]) in names
               for k in KINDS)


def _as_stored(c: dict, engine: str, src_slug: str, slug: str) -> dict:
    """来源的一个评测组合换成资料库里的写法：记的归属是来源角色的那几半，归属改成资料库里登记用的角色名，文件名换成
    资料库里的（和底模同名的改过名，见 stored_name）。"""
    c = dict(c)
    for k in KINDS:
        if c.get(f"{k}_owner") == src_slug:
            c[f"{k}_owner"], c[k] = slug, stored_name(slug, engine, c[k])
    return c


def _import_evals(source: Library, catalog: Catalog, engine: str, names: set, have: set, src_slug: str,
                  slug: str) -> int:
    """来源里涉及这些权重、两半都在资料库里（have：导入前已有的和这次导入的）的评测组合，并进资料库里同一任务的
    评测文件。返回新增的组合数。"""
    n = 0
    for doc in source.docs("evals"):
        if doc["engine"] != engine:
            continue
        # 来源里记的权重所属角色（gpt_owner / sovits_owner）跟着改名，再按归属验收
        combos = [_as_stored(c, engine, src_slug, slug) for c in doc["combos"] if {c["gpt"], c["sovits"]} & names]
        combos = [c for c in combos if _accepted(engine, c, have)]
        if combos:
            n += _merge_eval(catalog, doc, combos, slug if doc["character"] == src_slug else doc["character"])
    return n


def _import_existing_evals(source: Library, catalog: Catalog, existing: set) -> int:
    """来源里 GPT、SoVITS 都是本库导入前已有权重的评测组合（Colab 评测作业的结果），并进同一任务的评测文件。"""
    n = 0
    for doc in source.docs("evals"):
        combos = [c for c in doc["combos"] if _accepted(doc["engine"], c, existing)]
        if combos:
            n += _merge_eval(catalog, doc, combos, doc["character"])
    return n


def main():
    ap = argparse.ArgumentParser(description="把另一台机器（Colab 等）的训练结果（目录或 .tar）写进配置的资料库。"
                                             "gateway 在跑时请用 POST /runs/import。")
    ap.add_argument("src")
    ap.add_argument("--run", action="append", help="只导入这个 run（可多次）")
    ap.add_argument("--into-builtin", action="store_true", help="用内置角色数据集训练的角色并到内置角色名下")
    a = ap.parse_args()
    library.open_configured()
    print(json.dumps(import_runs(a.src, runs=a.run, into_builtin=a.into_builtin), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
