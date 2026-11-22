"""训练、评测任务放到 Colab 上跑（控制端）。gateway 在任务协程里调用 run_colab：

1. 导出输入资料库：控制端资料库里这次作业用到的子集（export_inputs），加 tts-core（tts-studio 的 core/）和 GPT-SoVITS 补丁，
   展开成 job.json（job_spec，VM 上 colab/vm_run.py 读）。
2. 从账号池开 VM（开不出来就转为「等待云端 GPU」，到时重试），ttsapi.compute.colab.run_job 上传输入、跑 colab/vm_setup.py、
   colab/vm_run.py，取回 colab/vm_pack.py 打的结果 tar。
3. 结果 tar 用 ttsapi.importer 写进控制端资料库（训练 run、权重、评测），VM 上的任务记录填回这个任务的 train_result /
   eval_result，VM 的任务日志接到这个任务的日志后面。

VM 上的任务 id 在打包前已换成控制端的任务 id（vm_run.adopt），所以 run 和评测记在这个任务名下，不会多出一条任务记录。
每个任务的作业目录 DATA/colab/<任务 id>/：job/ 是 VM 的时间线和日志（留着排查）；results.tar 在结果写进资料库、
任务结果存进 tts.db 之后删掉；导入失败、训练 run 被跳过或写库时任务被取消时留着，可以用 POST /runs/import 重试，
重试时按原任务 id 补上这个任务的结果。
写库用任务开始时的资料库（library.pinned）；写库时取消任务，等写库线程做完任务才结束（之前不能换库）。
"""
import asyncio
import hashlib
import json
import logging
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ttsapi import datasets, importer, library, settings, version
from ttsapi.catalog import char_rel, run_rel, weight_dir, weight_id
from ttsapi.compute import registry
from ttsapi.compute.colab import RunState, WaitingForGPU, run_job
from ttsapi.compute.colab.cli import CliError
from ttsapi.jobs import ACTIVE, UserError
from ttsapi.library import now_iso, sha256_of
from ttsapi.library.store import FORMAT
from ttsapi.models import CandidatesResult, CompareResult, JobItemDone, TrainEval, TrainResult, parse_version

log = logging.getLogger("tts-api")

# 能在 Colab 上跑的引擎：和算力列表、调度用同一份（settings.CLOUD_ENGINES，按底模清单）
ENGINES = settings.CLOUD_ENGINES
# VM 和局域网设备从公开上游取基点，控制端只分发补丁。
GSV_BASE = "bf81cdb14a38b674b6e9996dabc97340bc9978d2"
GSV_BRANCH = "tts-studio"
GSV_PATCH = Path(__file__).resolve().parents[2] / "patches" / "gptsovits-bf81cdb.patch"
STAGES = {
    "upload": ("上传输入（数据集、权重、代码）", 0.04),
    "setup": ("云端装环境、下底模（约 6 分钟）", 0.08),
    "run": {
        "train": ("云端训练，训完自动评测", 0.2),
        "eval": ("云端评测", 0.2),
        "candidates": ("云端合成", 0.2),
        "compare": ("云端对比合成", 0.2),
    },
    "download": ("取回结果", 0.9),
}


def _collect_weight_rels(rels: set, engine: str, character: str, gpt: str, sovits: str, version: str, catalog):
    """Collect library relative paths for resolved non-pretrained weights (and their run.json)."""
    picked = catalog.pick(engine, character, gpt, sovits, remote=True, version=version)
    for wkind in ("gpt", "sovits"):
        x = picked[wkind]
        if x["meta"]:
            d = weight_dir(x["character"], engine, x["name"])
            rels |= {f"{d}/meta.json", f"{d}/{x['meta']['file']}"}
            if x["run"]:
                rels.add(run_rel(x["run"]))


def _gen_ref_ids(reqs: list, ref_id: str = None, aux_ref_ids: list = None) -> list[str]:
    """Deduplicated ordered list of primary + auxiliary ref IDs for cloud candidates/compare."""
    ids = []
    if ref_id:
        ids.append(ref_id)
    if aux_ref_ids is not None:
        ids.extend(aux_ref_ids)
    else:
        for req in reqs:
            if getattr(req, "ref_id", None):
                ids.append(req.ref_id)
            ids.extend(getattr(req, "aux_ref_ids", None) or ())
    return list(dict.fromkeys(r for r in ids if r))


def _derived_refs(kind: str, reqs: list, refs, ref_id: str = None, aux_ref_ids: list = None) -> list[dict]:
    """Dataset-derived candidate refs (not registered under library/refs/) needed for cloud synthesis."""
    if kind not in ("candidates", "compare"):
        return []
    lib = library.current()
    registered = {r["id"] for r in lib.items("refs").values()}
    out = []
    for rid in _gen_ref_ids(reqs, ref_id, aux_ref_ids):
        if rid not in registered:
            if rid not in refs.index:
                raise KeyError(f"参考 {rid}")
            out.append(refs.index[rid])
    return out


def _inputs(kind: str, reqs: list, catalog, refs, character: str = None, ref_id: str = None, aux_ref_ids: list = None):
    """这次作业要带的资料库文件：(相对路径的集合, {数据集记录的相对路径: (记录, 要用的角色文件夹)})。见 export_inputs。"""
    lib = library.current()
    chars = catalog.characters()
    registered = {r["id"]: rel for rel, r in lib.items("refs").items()}
    if kind in ("candidates", "compare"):
        rels = set()
        char = character or (reqs[0].character if reqs else None)
        if char in chars:
            rels.add(char_rel(char))
        for rid in _gen_ref_ids(reqs, ref_id, aux_ref_ids):
            if rid in registered:
                rel = registered[rid]
                rels |= {rel, f"{rel.rsplit('/', 1)[0]}/{lib.get(rel)['audio']}"}
            if rid in refs.index and refs.index[rid]["character"] in chars:
                rels.add(char_rel(refs.index[rid]["character"]))
        for req in reqs:
            if kind == "candidates":
                _collect_weight_rels(rels, req.engine, char, req.gpt, req.sovits, req.version, catalog)
            else:
                for eng in dict.fromkeys(req.engines):
                    if eng == req.engine:
                        _collect_weight_rels(rels, eng, char, req.gpt, req.sovits, req.version, catalog)
                    else:
                        _collect_weight_rels(rels, eng, char, None, None, None, catalog)
        return rels, {}
    rels, ref_ids, slugs, dataset_ids = set(), set(), set(), set()
    for req in reqs:
        if kind == "train":
            slugs |= {req.slug} | {s for s, c in chars.items()
                                   if c["source"] == "builtin" and c["dataset_id"] == req.dataset_id}
            dataset_ids.add(req.dataset_id)
            ref = req.ref_id
        else:
            slugs.add(character)
            ref = ref_id
            # 按版本评测：带版本绑定的那两条记录（同名的别的角色的、底模的不算）
            bound = catalog.pick(req.engine, character, req.gpt, req.sovits, remote=True, version=req.version) \
                if req.version else {}
            for wkind, name in (("gpt", req.gpt), ("sovits", req.sovits)):
                x = bound.get(wkind) or catalog.find(weight_id(req.engine, name), remote=True)
                if x["meta"]:
                    d = weight_dir(x["character"], req.engine, name)
                    rels |= {f"{d}/meta.json", f"{d}/{x['meta']['file']}"}
                    if x["run"]:  # 目录只认已提交 run 的权重：VM 上也要有这个 run 的 run.json
                        rels.add(run_rel(x["run"]))
        if ref in refs.index:
            slugs.add(refs.index[ref]["character"])
            ref_ids.add(ref)
    for slug in slugs & set(chars):
        c = chars[slug]
        rels.add(char_rel(slug))
        dataset_ids.add(c["dataset_id"])
        if c["train_list"]:
            rels.add(c["train_list"])
        ref_ids.add(c["default_ref"])
    for rid in ref_ids & set(registered):
        rel = registered[rid]
        rels |= {rel, f"{rel.rsplit('/', 1)[0]}/{lib.get(rel)['audio']}"}
    used = {}
    for rel, d in lib.items("datasets").items():
        if folders := {f for f, c in d["characters"].items() if c["id"] in dataset_ids}:
            used[rel] = (d, folders)
    return rels, used


def export_inputs(kind: str, reqs: list, catalog, refs, folder: Path, character: str = None, ref_id: str = None,
                  aux_ref_ids: list = None) -> dict:
    """输入资料库：返回 {"library/<资料库相对路径>": 本机文件}。reqs：训练是这次的 TrainRequest（扫参一台 VM 上的几组），
    评测是 [EvalRequest]（character、ref_id 已由 gateway 定好），合成/对比是 [CandidatesParams]/[CompareParams]。
    library.json 新写在 folder 里（新 id，归本机管理，VM 上再认领）；数据集只带用到的角色（_slim_dataset，写在 folder 里）；
    其余是控制端资料库里的原文件（不复制）。合成/对比不带数据集 tar，而是把用到的参考音频（含未登记的数据集派生候选）
    直接导出到 library/refs/<角色>/。"""
    lib = library.current()
    rels, used = _inputs(kind, reqs, catalog, refs, character, ref_id, aux_ref_ids)
    derived = _derived_refs(kind, reqs, refs, ref_id, aux_ref_ids)
    for rel in sorted(rels):
        p = lib.path(rel)
        if not p.is_file():
            raise FileNotFoundError(f"输入文件不在本机：{rel}")
    info = dict(format=FORMAT, id=uuid.uuid4().hex, created=now_iso(), exported_from=lib.info["id"],
                owner=dict(machine_id=lib.machine["id"], name=lib.machine["name"]), claimed_at=now_iso())
    (folder / "library.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    out = {"library/library.json": folder / "library.json", **{f"library/{rel}": lib.path(rel) for rel in sorted(rels)}}
    for r in derived:
        src = Path(r["path"])
        if not src.is_file():
            raise FileNotFoundError(f"参考音频 {r['id']} 文件不在本机：{src}")
        audio_name = f"{r['id']}{src.suffix}"
        ref_dir = folder / "refs" / r["character"]
        ref_dir.mkdir(parents=True, exist_ok=True)
        meta_path = ref_dir / f"{r['id']}.json"
        meta = dict(id=r["id"], character=r["character"], text=r["text"], lang=r["lang"], duration=r["duration"],
                    tags=refs.tags.get(r["id"], []), audio=audio_name, source=None)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        out[f"library/refs/{r['character']}/{r['id']}.json"] = meta_path
        out[f"library/refs/{r['character']}/{audio_name}"] = src
    for rel, (d, folders) in used.items():
        with _verified_tar(lib, d) as src:  # 开卡前核对：和资料库登记的大小、sha256 一致（整个上传时 VM 上还会再核对）
            if folders == set(d["characters"]):
                out.update({f"library/{rel}": lib.path(rel),
                            f"library/datasets/{d['tar']}": lib.path(f"datasets/{d['tar']}")})
            else:
                out.update(_slim_dataset(src, rel, d, folders, folder))
    return out


@contextmanager
def _verified_tar(lib, doc: dict):
    """打开数据集 doc 的 tar，核对大小和 sha256 与资料库登记的一致（对不上抛 DatasetUnavailable），返回读到开头的
    同一个文件对象：子集从这份核对过的内容生成，核对之后 tar 被换掉也不受影响。"""
    with open(lib.path(f"datasets/{doc['tar']}"), "rb") as f:
        h = hashlib.sha256()
        while chunk := f.read(1 << 20):
            h.update(chunk)
        if f.tell() != doc["size"] or h.hexdigest() != doc["sha256"]:
            raise datasets.DatasetUnavailable(f"数据集 {doc['name']} 的 {doc['tar']} 和资料库登记的大小、sha256 对不上"
                                              "（还在同步，或被换过），同步完再提交")
        f.seek(0)
        yield f


def _slim_dataset(src, rel: str, doc: dict, folders: set, folder: Path) -> dict:
    """从 src（核对过的原 tar 的文件对象）打只含 folders 这几个角色（<name>/characters/<文件夹>/）的数据集 tar 和它的
    记录（角色、sha256、大小换成这个 tar 的），写在 folder/datasets/ 下，上传用；资料库里的原 tar 不动。"""
    out = folder / "datasets"
    out.mkdir(exist_ok=True)
    tar = out / doc["tar"]
    keep = tuple(f"{doc['name']}/characters/{f}" for f in folders)
    with tarfile.open(fileobj=src) as tf, tarfile.open(tar, "w") as dst:
        for m in tf:
            if m.name in keep or m.name.startswith(tuple(f"{k}/" for k in keep)):
                dst.addfile(m, tf.extractfile(m) if m.isfile() else None)
    sub = dict(doc, characters={f: c for f, c in doc["characters"].items() if f in folders},
               sha256=sha256_of(tar), size=tar.stat().st_size)
    record = out / rel.rsplit("/", 1)[1]
    record.write_text(json.dumps(sub, ensure_ascii=False, indent=1), encoding="utf-8")
    return {f"library/{rel}": record, f"library/datasets/{doc['tar']}": tar}


def upload_mb(kind: str, reqs: list, catalog, refs, character: str = None, ref_id: str = None,
              aux_ref_ids: list = None) -> float:
    """这次作业要上传的输入约多少 MB（估云端开机准备用，scheduler.Task.upload_mb）：资料库文件按大小；数据集只算用到的角色，
    按本机解压目录里这几个角色文件夹的大小（全部角色、或本机用不了时按整个 tar）。代码 bundle 不算。"""
    lib = library.current()
    rels, used = _inputs(kind, reqs, catalog, refs, character, ref_id, aux_ref_ids)
    derived = _derived_refs(kind, reqs, refs, ref_id, aux_ref_ids)
    total = sum(lib.path(rel).stat().st_size for rel in rels)
    for r in derived:
        total += Path(r["path"]).stat().st_size + 256
    for d, folders in used.values():
        try:
            root = datasets.local_root(d, lib) if folders != set(d["characters"]) else None
        except datasets.DatasetUnavailable:
            root = None
        total += (d["size"] or 0) if root is None else \
            sum(f.stat().st_size for x in folders for f in (root / "characters" / x).rglob("*") if f.is_file())
    return total / 2**20


def plan_gen_upload_mb(body, catalog, refs) -> float:
    """Estimate cloud upload size (weights + reference audio) in MB for POST /plan (kind='gen')."""
    lib = library.current()
    chars = catalog.characters()
    registered = {r["id"]: rel for rel, r in lib.items("refs").items()}
    rels = set()
    engines = list(dict.fromkeys(e for e, _ in body.task_parts()))
    char = body.character
    if char and char in chars:
        rels.add(char_rel(char))
    ver_engine, ver_gpt, ver_sovits = parse_version(body.version) if body.version else (None, None, None)
    primary_engine = ver_engine or body.engine or (engines[0] if engines else None)
    for eng in engines:
        if body.version and eng == ver_engine:
            _collect_weight_rels(rels, eng, char or "", ver_gpt, ver_sovits, body.version, catalog)
        elif eng == primary_engine and (body.gpt or body.sovits):
            _collect_weight_rels(rels, eng, char or "", body.gpt, body.sovits, None, catalog)
        elif char:
            _collect_weight_rels(rels, eng, char, None, None, None, catalog)
    if body.version and ver_engine not in engines:
        _collect_weight_rels(rels, ver_engine, char or "", ver_gpt, ver_sovits, body.version, catalog)
    ref_ids = []
    if body.ref_id:
        ref_ids.append(body.ref_id)
    elif char:
        if default_ref := refs.default_or_none(char):
            ref_ids.append(default_ref)
    ref_ids.extend(body.aux_ref_ids or ())
    extra_bytes = 0
    for rid in dict.fromkeys(r for r in ref_ids if r):
        if rid in registered:
            rel = registered[rid]
            rels |= {rel, f"{rel.rsplit('/', 1)[0]}/{lib.get(rel)['audio']}"}
        elif rid in refs.index:
            extra_bytes += Path(refs.index[rid]["path"]).stat().st_size + 256
        else:
            raise FileNotFoundError(f"参考音频不存在：{rid}")
    total = sum(lib.path(rel).stat().st_size for rel in rels) + extra_bytes
    return total / 2**20


def _git(repo: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def bundle_code(folder: Path):
    """打包已提交的 tts-core 版本和 GPT-SoVITS 补丁。
    tts-core 在 tts-studio 仓库的 core/ 下，VM 只要这一部分：用 HEAD 里 core/ 的树另做一个提交（消息里记仓库的 commit），
    带一个和主控版本号同名的 tag（VM 上 version.py 的 git describe 得到和主控一样的版本号）。这个提交在一个临时的
    bare 仓库里做（借用本仓库的对象），本仓库里不留 ref。
    返回 (输入 {"code/<文件名>": 本机文件}, job.json 里代码的字段, 提示：tts-core 有未提交的改动时)。"""
    commit = _git(settings.ROOT, "rev-parse", "HEAD")
    tree = _git(settings.ROOT, "rev-parse", "HEAD:./")
    dirty = _git(settings.ROOT, "status", "--porcelain", "--", "ttsapi", "scripts", "requirements", "colab", "patches")
    patch = GSV_PATCH.read_bytes()
    patch_sha = hashlib.sha256(patch).hexdigest()
    tb, gp = f"tts-core-{commit[:12]}.bundle", f"gsv-{patch_sha[:12]}.patch"
    ver = version.describe()[0]
    repo = folder / "tts-core.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "core", str(repo)], check=True, capture_output=True)
    objects = Path(_git(settings.ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "objects"
    (repo / "objects" / "info" / "alternates").write_text(f"{objects}\n", encoding="utf-8")
    core = _git(repo, "-c", "user.name=tts-core", "-c", "user.email=tts-core@localhost", "commit-tree", tree,
                "-m", f"tts-core {ver} (tts-studio {commit})")
    _git(repo, "update-ref", "refs/heads/core", core)
    _git(repo, "tag", f"v{ver}", core)
    _git(repo, "bundle", "create", str(folder / tb), "HEAD", "--tags")
    shutil.rmtree(repo)
    (folder / gp).write_bytes(patch)
    code = dict(ttsapi_bundle=f"code/{tb}", ttsapi_commit=core, gsv_patch=f"code/{gp}",
                gsv_patch_sha256=patch_sha, gsv_base=GSV_BASE, gsv_branch=GSV_BRANCH)
    note = f"注意：tts-core 有未提交的改动，云端用的是已提交的 {commit[:7]}" if dirty else None
    return {f"code/{tb}": folder / tb, f"code/{gp}": folder / gp}, code, note


def control_tz() -> str:
    """主控现在的时区（POSIX TZ 写法，UTC+9 是 <+0900>-09:00）：job.json 的 tz，VM 上的脚本、gateway 按它记时间，
    run 名里的时间戳、任务日志和主控一致（VM 本身是 UTC）。"""
    m = round(datetime.now().astimezone().utcoffset().total_seconds() / 60)
    hh, mm = divmod(abs(m), 60)
    return f"<{'+' if m >= 0 else '-'}{hh:02d}{mm:02d}>{'-' if m >= 0 else '+'}{hh:02d}:{mm:02d}"


def job_spec(job_id: str, kind: str, req, code: dict, character: str = None, ref_id: str = None,
             aux_ref_ids: list = None) -> dict:
    """job.json（colab/vm_run.py 读）：一个任务，带控制端的任务 id；评测、合成、对比带上 gateway 定好的角色和参考。
    VM 上的 gateway 在本机（VM 的 GPU）跑，不再选算力。"""
    task = dict(kind=kind, job_id=job_id,
                **req.model_dump(exclude={"compute", "approved", "approved_device", "approved_units"}), compute="local")
    if kind == "eval":
        task.update(character=character, ref_id=ref_id)
    elif kind in ("candidates", "compare"):
        task.update(character=character or req.character, ref_id=ref_id, emotion=None)
        if aux_ref_ids is not None:
            task["aux_ref_ids"] = list(aux_ref_ids)
    engines = list(dict.fromkeys(req.engines)) if kind == "compare" else [req.engine]
    return dict(run_id=job_id, engines=engines, tasks=[task], tz=control_tz(), **code)


async def _stop_vm_best_effort(vm) -> bool:
    """Release a VM without letting a stop failure override cancellation; reaper retries on failure."""
    try:
        await asyncio.to_thread(vm.stop)
        return True
    except Exception:
        log.exception("failed to release Colab VM %s; reaper will retry", getattr(vm, "name", "?"))
        return False


async def start_vm(run, pool, gpu: str, estimated_minutes: float, approved: bool, max_minutes: int = None):
    """开 VM（max_minutes：设置里的「最长用多久」，不设时按预计时间的 2 倍）；有做完先别关、同型号的 VM 时直接接手。
    所有账号都开不出这种卡时任务转为「等待云端 GPU」，retry_after 秒后再试，直到开出来。
    开卡时被取消：开卡线程停不下来，等它返回，开出来的 VM 接过来释放掉再结束任务。"""
    while True:
        start = asyncio.ensure_future(asyncio.to_thread(
            pool.start, gpu, max_minutes, task_id=run.job.id, estimated_minutes=estimated_minutes, approved=approved))
        try:
            return await asyncio.shield(start)
        except asyncio.CancelledError:
            await asyncio.wait({start})
            if start.exception() is None:
                await _stop_vm_best_effort(start.result())
            raise
        except WaitingForGPU as e:
            run.job.status = "queued"
            run.stage(f"等待云端 GPU（{gpu} 暂时开不出来）")
            await asyncio.sleep(e.retry_after)


# 结果下载失败时 VM 留着（ColabRun.keep_for_retry，最多 30 分钟），隔一会儿再取，最多取这么多次
RESULT_RETRIES = 5
RESULT_RETRY_SEC = 120


def _on_vm(vm, fn, keep_minutes=0, cancel=None):
    """在 with vm 里跑 fn（任务线程）：结果下载失败（vm.pending_result 不为空）时留机等重试；做完、VM 还在并且设了
    keep_minutes（做完先别关）时留着等下一个任务接手（只留做成功的）；其余情况退出时释放 VM。"""
    with vm:
        try:
            state = fn()
        except CliError:
            if vm.pending_result is not None and not (cancel is not None and cancel.is_set()):
                vm.keep_for_retry()
            raise
        if keep_minutes and state == RunState.DONE and not (cancel is not None and cancel.is_set()):  # 失败的不留：可能是 VM 本身坏了
            vm.keep_idle(keep_minutes)
        return state


async def on_vm_thread(vm, fn, keep_minutes=0, cancel=None):
    """_on_vm(vm, fn) 放进任务线程。任务被取消时通知线程停止后续阶段、控制协程马上释放 VM（线程里等着的 CLI 调用、
    轮询随之失败退出），等线程退出再把取消抛出去。"""
    job = asyncio.ensure_future(asyncio.to_thread(_on_vm, vm, fn, keep_minutes, cancel))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        if cancel is not None:
            cancel.set()
        stopped = await _stop_vm_best_effort(vm)
        if stopped:
            await asyncio.wait({job})
        else:
            await asyncio.wait({job}, timeout=5)
        if job.done() and not job.cancelled():
            job.exception()  # 线程因 VM 被释放而出的错：取走，取消照常抛出
        elif not job.done():
            job.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        raise


async def run_and_fetch(run, vm, spec, inputs, tar: Path, stage, keep_minutes: int = 0,
                        cancel: threading.Event = None) -> RunState:
    """跑作业、取回结果 tar。下载失败时不丢结果：VM 留着，RESULT_RETRY_SEC 秒后重新下载，最多 RESULT_RETRIES 次，
    都失败（或任务被取消）才释放 VM。keep_minutes：做完先别关（见 _on_vm）。"""
    cancel = cancel or threading.Event()
    try:
        return await on_vm_thread(
            vm, lambda: run_job(vm, spec, inputs, tar, stage, cancel=cancel), keep_minutes, cancel=cancel
        )
    except CliError:
        if vm.pending_result is None:
            raise
    for i in range(RESULT_RETRIES):
        run.stage(f"取回结果失败，{RESULT_RETRY_SEC} 秒后重试（{i + 1}/{RESULT_RETRIES}）")
        try:
            await asyncio.sleep(RESULT_RETRY_SEC)
        except asyncio.CancelledError:
            cancel.set()
            await _stop_vm_best_effort(vm)
            raise
        try:
            return await on_vm_thread(
                vm, lambda: vm.download(*vm.pending_result, timeout=1800) or RunState.DONE,
                keep_minutes, cancel=cancel,
            )
        except CliError:
            if i == RESULT_RETRIES - 1:
                await _stop_vm_best_effort(vm)
                raise


def collect(tar: Path, work: Path, job_id: str, catalog, refs, into_builtin: bool = False, kind: str = None,
            device_id: str = None):
    """解开结果 tar：job/ 和 training/<run>/runner.out 放进作业诊断目录；训练/评测有资料库（vm_setup 走完了）就导入控制端资料库
    （into_builtin 同 importer），合成/对比导入音频到本机 AUDIO_DIR 和 tts.db。返回 (导入摘要或 None, vm_run 的任务汇总或 None,
    VM 上这个任务的记录或 None, VM 上这个任务的日志)。"""
    with tempfile.TemporaryDirectory(prefix="ttscore-colab-result-") as tmp:
        src = Path(tmp)
        with tarfile.open(tar) as tf:
            # filter="data"（拒绝绝对路径、..、设备文件）在 3.10.12+ 才有
            tf.extractall(src, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        shutil.rmtree(work / "job", ignore_errors=True)
        shutil.move(src / "job", work / "job")
        if (src / "training").is_dir():
            for out in sorted((src / "training").glob("*/runner.out")):
                dst = work / "training" / out.parent.name / "runner.out"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(out, dst)
        summary = work / "job" / "summary.json"
        summary = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else None
        record = None
        if (src / "data" / "tts.db").exists():
            conn = sqlite3.connect(src / "data" / "tts.db")
            row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            conn.close()
            record = row and json.loads(row[0])
        if kind in ("candidates", "compare"):
            id_map = importer.import_gen_audio(src, record=record, device_id=device_id, job_id=job_id) if record else {}
            imported = dict(imported=[], skipped=[], evals=0, jobs=[], filled=[], audio=len(id_map), audio_id_map=id_map)
        else:
            imported = importer.import_runs(src, catalog, refs, into_builtin=into_builtin) \
                if (src / "library" / "library.json").exists() else None
        log_file = src / "data" / "jobs" / f"{job_id}.log"
        return imported, summary, record, log_file.read_text(encoding="utf-8") if log_file.exists() else ""


def _shutdown_note(pool, vm, job_id: str) -> str | None:
    """Return a shutdown/cost suffix only when this task's own usage row is confirmed stopped."""
    if not hasattr(pool, "db"):
        return None
    try:
        with pool.db() as conn:
            row = conn.execute(
                "SELECT gpu, task_id, started, stopped, actual_units, adopted, late_checked "
                "FROM usage WHERE name=?",
                (vm.name,),
            ).fetchone()
    except Exception:
        return None
    if row is None or row["stopped"] is None or row["task_id"] != job_id:
        return None
    if row["late_checked"] and row["actual_units"] is not None:
        units = row["actual_units"]
        if units < 0.005:
            return "已关机，用了 0 单元"
        if not row["adopted"]:
            return f"已关机，用了 {units:.2f} 单元"
    start = row["adopted"] or row["started"]
    rate = pool.rates().get(row["gpu"])
    est = (row["stopped"] - start) / 3600 * rate if rate and start else None
    if est is not None and est >= 0.005:
        return f"已关机，约用了 {est:.2f} 单元"
    return "已关机"


async def run_colab(run, kind, req, *, gpu: str, estimated_minutes: float, approved: bool, pool, catalog, refs,
                    character: str = None, ref_id: str = None, aux_ref_ids: list = None, max_minutes: int = None,
                    keep_minutes: int = 0, device_id: str = None) -> dict:
    """在 Colab 上跑一个训练（kind="train"）、评测（kind="eval"）、候选合成（kind="candidates"）或对比合成（kind="compare"）
    任务，结果写进资料库（或合成音频写进本机 AUDIO_DIR 与 tts.db），填 run.job.*_result。
    VM 上的任务失败时，取回的部分照样导入（比如训练完了、评测失败：权重进资料库），然后抛错。
    max_minutes、keep_minutes：设置里的「最长用多久」「做完先别关」（PUT /cloud/settings）。"""
    req_engines = list(dict.fromkeys(req.engines)) if kind == "compare" else [req.engine]
    if bad := [e for e in req_engines if e not in settings.CLOUD_ENGINES]:
        raise ValueError(f"Colab 上只能跑 {', '.join(settings.CLOUD_ENGINES)}，不能跑 {', '.join(bad)}")
    job_id = run.job.id
    did = device_id or f"colab-{gpu}"
    lib = library.current()  # 结果写进任务开始时的资料库
    work = settings.DATA / "colab" / job_id
    work.mkdir(parents=True, exist_ok=True)
    tar = work / "results.tar"
    loop = asyncio.get_running_loop()
    texts = dict(STAGES, run=STAGES["run"][kind])
    cancel = threading.Event()

    def stage(step):  # run_job 在任务线程里调用
        if cancel.is_set() or run.cancel_requested or run.job.status not in ACTIVE:
            return
        loop.call_soon_threadsafe(run.stage, *texts[step])

    vm = None
    registry.assigned.setdefault(job_id, (did, time.time() + estimated_minutes * 60))
    try:
        with tempfile.TemporaryDirectory(prefix="ttscore-colab-") as tmp:
            tmp = Path(tmp)
            run.stage("准备输入：资料库子集、代码", 0.01)

            def prepare():
                inputs = export_inputs(kind, [req], catalog, refs, tmp, character, ref_id, aux_ref_ids)
                code_inputs, code, note = bundle_code(tmp)
                return {**inputs, **code_inputs}, job_spec(job_id, kind, req, code, character, ref_id, aux_ref_ids), note

            inputs, spec, note = await asyncio.to_thread(prepare)
            if note:
                run.log(note)
            run.stage(f"开云端 GPU（{gpu}）", 0.02)
            vm = await start_vm(run, pool, gpu, estimated_minutes, approved, max_minutes)
            run.job.status = "running"
            run.log(f"VM {vm.name}（账号 {vm.account_id}{'，接手上一个任务留下的' if getattr(vm, 'reused', False) else ''}）")
            state = await run_and_fetch(run, vm, spec, inputs, tar, stage, keep_minutes, cancel=cancel)
        if state == RunState.GONE:
            raise UserError("云端 VM 中途没了（被回收），没有取回结果")
        run.stage("写入音频与历史记录" if kind in ("candidates", "compare") else "写进资料库", 0.97)
        retry = f"结果留在 {tar}，可以用 POST /runs/import 重试（会补上这个任务的结果）"
        with library.pinned(lib):  # 写库线程和下面读写资料库的地方都用任务开始时的资料库
            write = asyncio.ensure_future(asyncio.to_thread(
                collect, tar, work, job_id, catalog, refs, kind=kind, device_id=device_id))
            try:
                imported, summary, record, vm_log = await asyncio.shield(write)
            except asyncio.CancelledError:
                # 写库线程停不下来：等它写完再结束任务，这期间任务还在 jobs.runs 里（换库、删角色、导入都被挡住）
                log.info("job %s cancelled while writing results; %s", job_id, retry)
                run.log("取消：等云端结果写完资料库再结束")
                await asyncio.wait({write})
                raise
            except Exception as e:
                err = RuntimeError(f"云端结果没能写进资料库（{type(e).__name__}: {e}）；{retry}")
                err.user_message = "云端结果写入资料库失败，详细信息在主控日志里"
                raise err from e
            if vm_log:
                offset = run.log_path.stat().st_size if run.log_path.exists() else 0
                with open(run.log_path, "a", encoding="utf-8") as f:
                    f.write(f"---- 云端任务日志 ----\n{vm_log.rstrip()}\n")
                run.follow(offset)
            # VM 的任务记录对上控制端资料库（只导进来部分权重时，评测结果只剩导进来的组合、最好的重选）；
            # 训练 run 没写进资料库时不填，重试导入时补
            tr = record and kind == "train" and record.get("train_result")
            if tr and catalog.has_run(tr["run_name"]):
                run.job.train_result = TrainResult(**importer.job_record(record)["train_result"])
            if record and kind == "eval" and record.get("eval_result"):
                ev = importer.job_record(record)["eval_result"]
                run.job.eval_result = ev and TrainEval(**ev)
            id_map = (imported or {}).get("audio_id_map") or {}
            if record and kind in ("candidates", "compare"):
                if record.get("completed"):
                    run.job.completed = [
                        JobItemDone(**importer.rewrite_item_done(d, id_map))
                        for d in record["completed"]
                        if d.get("audio_id") in id_map
                    ]
                if record.get("latest") and record["latest"].get("audio_id") in id_map:
                    run.job.latest = JobItemDone(**importer.rewrite_item_done(record["latest"], id_map))
            if record and kind == "candidates" and record.get("candidates_result"):
                cr = importer.rewrite_gen_result("candidates_result", record["candidates_result"], id_map, device_id)
                run.job.candidates_result = CandidatesResult(**cr)
            if record and kind == "compare" and record.get("compare_result"):
                cmp = importer.rewrite_gen_result("compare_result", record["compare_result"], id_map, device_id)
                run.job.compare_result = CompareResult(**cmp)
            run.save()  # 任务结果先落进 tts.db，再删结果 tar
            if tr and run.job.train_result is None:
                err = RuntimeError(f"训练 run {tr['run_name']} 没写进资料库：{imported['skipped']}；{retry}")
                err.user_message = "训练结果写入资料库失败，详细信息在主控日志里"
                raise err
            if record and kind == "eval" and record.get("eval_result") and run.job.eval_result is None:
                err = RuntimeError(f"云端评测的成绩没写进资料库（两半对不上资料库里的权重）；{retry}")
                err.user_message = "云端评测结果写入资料库失败，详细信息在主控日志里"
                raise err
            tar.unlink()
        task = summary[0] if summary else None
        if task is None or task["status"] != "succeeded":
            raise RuntimeError(task["error"] if task else f"云端作业没跑完（{state.value}），日志在 {work / 'job'}")
        return imported
    except Exception:
        if vm is not None:
            run.failure_note = await asyncio.to_thread(_shutdown_note, pool, vm, job_id)
        raise
    finally:
        registry.assigned.pop(job_id, None)
