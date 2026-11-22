"""tts-api 对外服务：0.0.0.0:9880。本进程不碰 GPU，推理、打分、训练都在子进程里。

python -m ttsapi.gateway
"""
import asyncio
import io
import ipaddress
import itertools
import json
import logging
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional, Union

import httpx
import soundfile as sf
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ttsapi import (blind, datasets, db, importer, jobs, library, pair, power, reviews, settings, storage, synth,
                    training, version, works, workers)
from ttsapi.catalog import _UNSET, Catalog, VersionUnavailable, delete_run_dirs, engine_infos, half, rank, versions
from ttsapi.compute import dispatch, enroll, registry, scheduler, speeds
from ttsapi.compute.scheduler import Task
from ttsapi.bench import bench_ref, run_bench
from ttsapi.compute.remote import Remote
from ttsapi.models import (AddLineRequest, AttachRequest, AudioFormat, AudioItem, AudioPage, AutoPatch, AutoSettings,
                           BenchList, BenchRequest, BlindCreate, BlindGenerateRequest, BlindList, BlindQuestion,
                           BlindResult, BlindTest, BlindVote, CandidatesParams, CharacterPrefs, CharacterPrefsPatch,
                           CharacterSynthParams, CloudAccount, CloudGpu, CloudInfo, CloudLoginCode, CloudLoginRequest,
                           CloudLoginStart, CloudLoginStatus, CloudSettings, CloudSettingsPatch, CloudUsageMonth,
                           SystemInfo, SystemVersions, CandidatesResult, CompareParams, CompareResult, ComputeDevice,
                           ComputeInvite, ComputeList, DatasetList, DatasetPreview, DefaultChange, DeletedCharacter,
                           DeletedWeight, DeviceAdd, DeviceHeartbeat, DevicePair, DevicePaired, DevicePatch,
                           InviteCommands, InviteCreate, InviteManual, InviteStatus, Engine, ErrorResponse, EvalRequest,
                           EvalSet, EvalSetList, EvalSetName, GenerateRequest, Health, Job, JobKind, JobList, JobStatus,
                           LibraryCounts, LibraryInfo, LibraryScan, LibrarySelect, MachineRef, ModelsResponse,
                           ModelVersion, Order, PickRequest, Plan, PlanRequest, Ref, RefList, RegenerateRequest, Review,
                           ReviewIn, ReviewStats, RunImportRequest, RunImportResult, StorageOverview, SweepPlan,
                           SweepRequest, SweepResult, SystemVersion, TagsBody, TrainEval, TrainRequest, TTSParams,
                           UpdateRequest, UpdateResult, WeightEntry, WeightList, WeightPatch, WeightSort, WeightSource,
                           Work, WorkCreate, WorkerNames, WorkList, WorkPatch)
from ttsapi.osutil import IS_WINDOWS
from ttsapi.refs import RefLibrary

log = logging.getLogger("tts-api")
# 路径里的 id 只允许这些字符，防止 ..%5C 之类的路径穿越
AudioId = PathParam(..., pattern=r"^[0-9A-Za-z-]+$")
WorkId = PathParam(..., pattern=r"^w-[0-9a-f]+$")
BlindId = PathParam(..., pattern=r"^b-[0-9a-f]+$")
warmups = set()
ALLOWED = [ipaddress.ip_network(n) for n in settings.ALLOWED_NETS]

catalog = synth.catalog = Catalog()
refs = synth.refs = RefLibrary(catalog)  # 资料库在 lifespan 里打开后 reload


async def housekeeping():
    while True:
        synth.cleanup_audio(log)
        for w in workers.ALL:
            idle = time.time() - w.last_used
            if settings.IDLE_UNLOAD_MIN and w.running and not w.busy and idle > settings.IDLE_UNLOAD_MIN * 60:
                w.stop()
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app):
    synth.import_legacy_meta()
    if library.open_configured():
        refs.reload()
    # 上次退出时还在跑的训练：训练进程是独立的，重新接管；其他任务标记为中断
    for job, created in jobs.recover(training.resumable):
        log.info("resuming train job %s (%s)", job.id, job.train_result.run_name)
        spec = json.loads((settings.TRAIN_DIR / job.train_result.run_name / "runner_spec.json").read_text(
            encoding="utf-8"))
        jobs.resume(job, created, lambda run: training.resume_train_job(run, catalog, refs),
                    train_involves(job.target, spec["req"].get("ref_id")))
    # 数据集时长第一次要读几千个音频头，后台先算好
    if library.selected():
        threading.Thread(target=lambda: [datasets.rows(p) for p, _ in datasets.scan().values()], daemon=True).start()
    task = asyncio.create_task(housekeeping())
    global _stack
    async with AsyncExitStack() as stack:
        _stack = stack
        # 装了 colab CLI（python -m ttsapi.compute.colab.install）才有云端 GPU：账号池在主控上定期回收、刷新余额。
        # 没装时第一次「添加云端 GPU」（POST /cloud/accounts/login）先装
        if colab_installed():
            await start_pool()
        yield
        _stack = None
    task.cancel()
    for w in workers.ALL:
        w.stop()


_stack = None  # lifespan 的 AsyncExitStack：后装的账号池也挂在上面，服务退出时一起收尾
_pool_lock = asyncio.Lock()


def colab_installed() -> bool:
    from ttsapi.compute.colab.install import ready
    return ready(settings.COLAB_ROOT)


def make_pool():
    from ttsapi.compute.colab import ColabPool
    return ColabPool(settings.COLAB_ROOT)


def install_colab():
    """装 colab CLI 的环境（uv、Python 3.12，按锁文件），约 1 分钟。"""
    r = subprocess.run([sys.executable, "-m", "ttsapi.compute.colab.install", "--root", str(settings.COLAB_ROOT)],
                       cwd=settings.ROOT, capture_output=True, text=True)
    if r.returncode:
        raise HTTPException(500, f"装云端 GPU 组件失败：{(r.stderr or r.stdout).strip()[-500:]}")


async def start_pool():
    registry.pool = make_pool()
    await _stack.enter_async_context(registry.pool.lifespan())


async def ensure_pool():
    """账号池；没装 colab CLI 时先装（第一次添加云端 GPU 时）。"""
    async with _pool_lock:
        if registry.pool is None:
            if not colab_installed():
                await asyncio.to_thread(install_colab)
            await start_pool()
    return registry.pool


app = FastAPI(
    title="tts-api",
    description="GPT-SoVITS（v1–V5）的常驻 HTTP 接口：合成、候选打分、review、作品、训练、评测、盲听。"
                "角色、权重、评测、盲听和作品存于资料库（GET /library）。",
    version=version.VERSION,
    lifespan=lifespan,
)
ERRORS = {code: {"model": ErrorResponse} for code in (400, 403, 404, 409, 500, 503)}
api = APIRouter(responses=ERRORS)
# 另外绑上、只提供安装脚本和代码包的端口（settings.INSTALL_PORT，一般是 80）；绑不上时 None，安装命令带 core 端口
install_port = None
INSTALL_PATHS = ("/install", "/install/code.tar")
HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")  # 安装脚本里认的主控名：DNS 名、IPv4（Host 头已经转成小写）


@app.middleware("http")
async def guard(request: Request, call_next):
    ip = ipaddress.ip_address(request.client.host)
    if not any(ip in net for net in ALLOWED):
        return JSONResponse(status_code=403, content=dict(detail="forbidden"))
    if install_port and request.scope["server"][1] == install_port and request.url.path not in INSTALL_PATHS:
        return JSONResponse(status_code=404, content=dict(detail="这个端口只提供 /install"))
    power.hold()  # 有请求在处理时阻止系统睡眠
    t = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        power.release()
    log.info("%s %s %s %s %.2fs", request.client.host, request.method, request.url.path,
             response.status_code, time.perf_counter() - t)
    return response


def owned():
    """会写资料库的请求在入口判归属（在改内存、入队之前）：未选资料库 409 library_not_selected，
    不是本机管理 409 library_not_owned。"""
    library.current().check_owner()


OWNED = [Depends(owned)]


@app.exception_handler(KeyError)
async def not_found(request: Request, exc: KeyError):
    return JSONResponse(status_code=404, content=dict(detail=f"not found: {exc.args[0]}"))


@app.exception_handler(VersionUnavailable)
async def version_unavailable(request: Request, exc: VersionUnavailable):
    """给了 version，但这个角色已经没有版本里的权重了：409 version_unavailable，重新取 GET /characters/{id}/versions。"""
    return JSONResponse(status_code=409, content=dict(detail=str(exc), code="version_unavailable"))


@app.exception_handler(dispatch.PlanError)
async def plan_error(request: Request, exc: dispatch.PlanError):
    """提交任务时：云端花费超过提醒线（confirm_required）、指定的设备不能用（compute_unavailable）。plan 给弹窗用。"""
    return JSONResponse(status_code=exc.status, content=dict(detail=exc.detail, code=exc.code, plan=exc.plan))


@app.exception_handler(library.LibraryError)
async def library_error(request: Request, exc: library.LibraryError):
    """未选资料库、资料库由别的控制端管理（409）、选的文件夹不是资料库（400）：detail 是说明，code 给客户端分辨。"""
    return JSONResponse(status_code=exc.status, content=dict(detail=exc.detail, code=exc.code))


@app.exception_handler(synth.AudioElsewhere)
async def audio_elsewhere(request: Request, exc: synth.AudioElsewhere):
    """盲听的音频在出题的主控上、这台没有（资料库还没同步过来，或者是音频进资料库之前出的题）：
    404 {code: "audio_elsewhere", machine}，界面说「音频在 <machine> 上」。"""
    return JSONResponse(status_code=404, content=dict(detail=str(exc), code="audio_elsewhere", machine=exc.machine))


@app.exception_handler(synth.AuxRefsUnsupported)
async def aux_refs_unsupported(request: Request, exc: synth.AuxRefsUnsupported):
    """Reject non-empty aux_ref_ids on engines that do not support multi-reference blending."""
    return JSONResponse(status_code=422, content=dict(detail=str(exc)))


@app.exception_handler(synth.AuxRefUnavailable)
async def aux_ref_unavailable(request: Request, exc: synth.AuxRefUnavailable):
    """Reject aux_ref_ids whose metadata exists in the library but backing audio file is missing locally."""
    return JSONResponse(status_code=409, content=dict(detail=str(exc), code="aux_ref_unavailable"))


# ---------------------------------------------------------------- 基础

@api.get("/health", tags=["基础"], response_model=Health)
async def health():
    return Health(status="ok", time=datetime.now().isoformat(timespec="seconds"), training_job=workers.training_job,
                  workers={w.name: w.status() for w in workers.ALL}, version=version.VERSION, commit=version.COMMIT)


@api.get("/system/version", tags=["基础"], response_model=SystemVersion)
async def system_version():
    """正在运行的版本（服务启动时的代码）、短 sha，以及工作区是否有没提交的改动、当前分支。"""
    return await asyncio.to_thread(version.current)


@api.get("/system/versions", tags=["基础"], response_model=SystemVersions)
async def system_versions(refresh: bool = False):
    """Studio 和 core 的版本：current 是正在运行的版本，studio.latest 是 core 所在仓库 origin 上最新的 v*.*.* tag
    （git ls-remote，用机器上已有的 git 凭据）。结果缓存 10 分钟（查失败缓存 1 分钟），refresh=true 重新查。"""
    return await asyncio.to_thread(version.versions, refresh)


@api.get("/system/info", tags=["基础"], response_model=SystemInfo)
async def system_info():
    """本机在局域网里可访问的 Web 地址（mDNS 名 <主机名>.local + 实际端口 + /ui/），Studio 的「复制链接」用它。
    web_available：core 挂了 /ui/（启动时 TTSAPI_WEB_DIR，默认仓库的 web/dist，在）才是 true，false 时 Studio 隐藏「复制链接」。"""
    host = pair.mdns_host()
    return SystemInfo(web_url=f"http://{host}:{settings.PORT}/ui/", host=host, port=settings.PORT,
                      lan_reachable=settings.HOST not in ("127.0.0.1", "localhost", "::1"),
                      web_available=any(getattr(r, "path", None) == "/ui" for r in app.routes),
                      machine=settings.MACHINE_NAME)


@api.get("/system/storage", tags=["基础"], response_model=StorageOverview)
async def system_storage(refresh: bool = False):
    """Disk capacity and storage breakdown for the active library and local TTS Studio directories.
    Results are cached in memory for 60 seconds; pass refresh=true to force a rescan."""
    return await asyncio.to_thread(storage.overview, refresh)


@api.post("/system/update", tags=["基础"], response_model=UpdateResult, status_code=202,
          responses={200: {"model": UpdateResult, "description": "正在运行的就是这个版本，不更新也不重启"}})
async def system_update(body: UpdateRequest, response: Response):
    """一键更新：git fetch --tags 后把 master 快进到 tag，返回 202，随后服务自己重启（推理 worker 一起换新代码，
    冷启动照常按需加载）。客户端轮询 /health，直到 version 等于返回的 to。
    有任务在跑或排队时 409（重启会中断它们），更新期间提交的新任务也是 409；tag 不存在 404；不在 master、工作区有改动、不能快进时 409，
    detail 带 git 的原文；正在运行的就是这个 tag 时返回 200，什么都不做。"""
    global restarting
    if jobs.closed:
        raise HTTPException(409, "已经在更新")
    if active := list(jobs.runs):
        raise HTTPException(409, f"有任务在跑或排队（{', '.join(active)}），重启会中断它们，结束后再更新")
    # 检查和关门之间没有 await：从这里起新任务一律 409，不会在 git 操作期间或重启前混进来
    jobs.closed = f"服务正在更新到 {body.tag}，几秒后重启，之后再提交"
    changed = False
    try:
        changed = await asyncio.to_thread(version.fast_forward, body.tag)
    finally:
        if not changed:
            jobs.closed = None
    result = UpdateResult(**{"from": version.VERSION, "to": body.tag[1:]})
    if not changed:
        response.status_code = 200
        return result
    log.info("updated to %s (from %s), restarting", body.tag, version.VERSION)
    # 先把 202 发出去再退出：uvicorn 正常关闭（关 worker、lifespan 收尾），然后以 RESTART_EXIT_CODE 退出，
    # start.ps1 立刻拉起新代码
    restarting = True
    asyncio.get_running_loop().call_later(0.5, setattr, server, "should_exit", True)
    return result


# ---------------------------------------------------------------- 资料库

def library_info() -> LibraryInfo:
    lib, me = library.selected(), library.machine()
    this = MachineRef(machine_id=me["id"], name=me["name"])
    if lib is None:
        return LibraryInfo(selected=False, error=library.error(), this_machine=this)
    return LibraryInfo(selected=True, path=str(lib.root), kind=lib.kind, id=lib.info["id"],
                       owner=MachineRef(**lib.info["owner"]), owned=lib.owned, this_machine=this,
                       counts=LibraryCounts(**lib.counts()), conflicts=len(lib.conflicts),
                       conflict_files=lib.conflicts[:100], broken_files=lib.broken, scanned_at=lib.scanned_at,
                       scan=LibraryScan(**lib.last_scan))


@api.get("/library", tags=["资料库"], response_model=LibraryInfo)
async def get_library():
    """当前资料库：路径、类型（drive / local）、管理它的控制端和是不是本机、各类记录数、Drive 同步冲突副本数、上次扫描。
    没选资料库时 selected=false（其余用到资料库的接口返回 409，code=library_not_selected）。"""
    return library_info()


@api.post("/library", tags=["资料库"], response_model=LibraryInfo)
async def select_library(body: LibrarySelect):
    """选资料库：文件夹里有资料库就打开（由别的控制端管理时 owned=false，只能读，POST /library/claim 改归属）；
    空文件夹或不存在的路径就在那里新建一个由本机管理的资料库；有别的文件、不是资料库时 400（code=not_a_library）。
    有任务在跑或排队时 409。选好的路径记在本机，服务重启后直接打开。"""
    if active := list(jobs.runs):
        raise HTTPException(409, f"有任务在跑或排队（{', '.join(active)}），结束后再换资料库")
    library.select(Path(body.path))
    storage.invalidate_cache()
    refs.reload()
    return library_info()


@api.post("/library/rescan", tags=["资料库"], response_model=LibraryInfo)
async def rescan_library():
    """重新扫描资料库（没变的文件按 mtime + size 跳过），刷新冲突副本计数，重建参考库。"""
    library.current().scan()
    storage.invalidate_cache()
    refs.reload()
    return library_info()


@api.post("/library/claim", tags=["资料库"], response_model=LibraryInfo)
async def claim_library():
    """把资料库改为由本机管理（另一台控制端之后再写会被挡住）。"""
    library.current().claim()
    storage.invalidate_cache()
    return library_info()


@api.get("/models", tags=["基础"], response_model=ModelsResponse)
async def models(include_hidden: bool = Query(False, description="是否包含被隐藏的权重（当前默认的总是包含）")):
    """所有角色（内置 + 训练出来的）、每个引擎可用的权重、默认组合和默认参考；以及所有引擎的说明
    （能否零样本、能否训练、用哪套代码）。权重的完整信息和管理见 /weights。"""
    ents, evals = catalog.entries(), catalog.latest_evals()
    return ModelsResponse(
        characters=[catalog.info(s, ref, ents, evals, include_hidden) for s in catalog.slugs()
                    if (ref := refs.default_or_none(s))],
        engines=engine_infos())


@api.get("/characters/{slug}/versions", tags=["基础"], response_model=list[ModelVersion])
async def character_versions(slug: str):
    """角色的模型版本（一对 GPT + SoVITS，Studio 里一个角色只选一个版本），新的在前：每次训练的最佳组合、
    历史导入权重、手动设置的默认组合；一个自己的权重都没有时是底模零样本。两半都是底模的版本（零样本、手动设的底模默认）只从不指定设备
    提交生成任务时能派出去的引擎里出（固定了设备时只看那一台，自动时看本机、局域网设备，不含云端；都没有时没有这种版本）。
    recommended 的那个是推荐项。生成等接口直接收 version（id），「更多设置」里手动选权重仍用 gpt / sovits。"""
    return [ModelVersion(**v) for v in versions(catalog, slug, await registry.gen_engines())]


@api.get("/characters/{slug}/prefs", tags=["基础"], response_model=CharacterPrefs)
async def get_character_prefs(slug: str):
    """Read character-level synthesis preferences and default reference settings."""
    return catalog.prefs(slug)


@api.patch("/characters/{slug}/prefs", dependencies=OWNED, tags=["基础"], response_model=CharacterPrefs)
async def patch_character_prefs(slug: str, body: CharacterPrefsPatch):
    """Update character-level synthesis preferences and/or default primary reference."""
    doc = catalog.character(slug)
    fields = body.model_fields_set
    new_default_ref = _UNSET
    new_last_ref = _UNSET
    new_aux_refs = _UNSET
    new_synth_params = _UNSET
    try:
        if "default_ref" in fields:
            if body.default_ref in (None, ""):
                new_default_ref = None
            else:
                refs.validate_for_character(slug, body.default_ref, require_primary=True)
                new_default_ref = body.default_ref
        if "last_ref_id" in fields:
            if body.last_ref_id in (None, ""):
                new_last_ref = None
            else:
                refs.validate_for_character(slug, body.last_ref_id)
                new_last_ref = body.last_ref_id
        if "aux_ref_ids" in fields:
            if body.aux_ref_ids is None:
                new_aux_refs = []
            else:
                deduped, seen = [], set()
                for rid in body.aux_ref_ids:
                    if not rid:
                        continue
                    refs.validate_for_character(slug, rid)
                    if rid not in seen:
                        seen.add(rid)
                        deduped.append(rid)
                new_aux_refs = deduped
    except ValueError as e:
        raise HTTPException(422, str(e)) from None

    if body.reset_synth_params or "synth_params" in fields:
        stored = ((doc.get("prefs") or {}).get("synth_params") or {})
        sp = {} if body.reset_synth_params else {
            k: v for k, v in stored.items() if k in CharacterSynthParams.model_fields and v is not None
        }
        if "synth_params" in fields:
            if body.synth_params is None:
                sp = {}
            else:
                for k in body.synth_params.model_fields_set:
                    v = getattr(body.synth_params, k)
                    if v is None:
                        sp.pop(k, None)
                    else:
                        sp[k] = v
        new_synth_params = sp

    if new_default_ref is not _UNSET and new_default_ref:
        refs.register(new_default_ref)
    return catalog.update_prefs(
        slug,
        default_ref=new_default_ref,
        synth_params=new_synth_params,
        last_ref_id=new_last_ref,
        aux_ref_ids=new_aux_refs,
    )


@api.delete("/characters/{slug}", dependencies=OWNED, tags=["基础"], response_model=DeletedCharacter)
async def delete_character(slug: str):
    """删除通过 /train 训练出的角色：资料库里的角色记录、权重、训练 run、参考、这个角色的盲听，本机的训练中间产物
    （training/），以及它生成过的、不在作品里的音频。训练任务记录、评测记录保留。内置角色返回 403；
    有用到这个角色（训练它、评测或盲听它的权重、用它的参考）的训练、评测、盲听生成任务在跑或排队时返回 409。"""
    # 先查任务：第一次训练还没结束的新角色还没登记，也要返回 409 而不是 404
    if busy := jobs.busy(("train", "sweep", "eval", "blind"), character=slug):
        raise HTTPException(409, f"有用到 {slug} 的训练、评测或盲听生成任务在进行（{', '.join(busy)}），结束后再删")
    if catalog.character(slug)["source"] == "builtin":
        raise HTTPException(403, f"{slug} 是内置角色，不能删除")
    for doc in [d for d in library.current().docs("blind") if d["character"] == slug]:
        await blind.delete(doc["id"])
    runs = catalog.remove(slug)
    delete_run_dirs(runs)
    refs.reload()
    rows = db.query("SELECT id FROM audio WHERE character=? AND (work_id IS NULL OR keep=0)", slug)
    for r in rows:
        for f in settings.AUDIO_DIR.glob(r["id"] + ".*"):
            f.unlink()
        db.run("DELETE FROM reviews WHERE audio_id=?", r["id"])
        db.run("DELETE FROM audio WHERE id=?", r["id"])
    log.info("deleted character %s: %d runs, %d audio", slug, len(runs), len(rows))
    return DeletedCharacter(slug=slug, runs=[r["run_name"] for r in runs], audio_deleted=len(rows))


@api.post("/workers/unload", tags=["基础"], response_model=WorkerNames)
async def unload_workers():
    """立刻关掉所有空闲 worker，释放显存（比如要打游戏时）。下次请求会自动重新加载。"""
    return WorkerNames(workers=workers.unload_idle())


@api.post("/workers/warmup", tags=["基础"], response_model=WorkerNames, status_code=202)
async def warmup(engines: list[Union[Engine, Literal["scorer"]]] = Query(["v2pro", "scorer"])):
    """后台预加载 worker，立即返回。加载要 20–40 秒，完成后 /health 里 running=true。"""
    if workers.training_job:
        raise HTTPException(503, f"GPU 正在训练（任务 {workers.training_job}）", headers={"Retry-After": "120"})
    targets = [workers.SCORER if e == "scorer" else workers.ENGINES[e] for e in engines]
    for w in targets:
        task = asyncio.create_task(w.ensure())
        warmups.add(task)
        task.add_done_callback(warmups.discard)
    return WorkerNames(workers=[w.name for w in targets])


# ---------------------------------------------------------------- 参考音频

@api.get("/refs", tags=["参考音频"], response_model=RefList)
async def list_refs(character: Optional[str] = None, emotion: Optional[str] = None,
                    sort: Optional[Literal["id", "duration", "text", "character"]] = Query(
                        None, description="不填按索引顺序（角色、数据集顺序）"),
                    order: Order = "asc"):
    """参考：资料库里的参考（上传的、打过标签的、默认参考）和数据集里 3–10 秒的句子（候选）。可按角色、情绪标签过滤。"""
    library.current()
    items = refs.list(character, emotion, sort, order)
    return RefList(count=len(items), refs=[Ref(**r) for r in items])


@api.put("/refs/{ref_id}", dependencies=OWNED, tags=["参考音频"], response_model=Ref)
async def tag_ref(ref_id: str, body: TagsBody):
    """设置参考的情绪标签（整体替换，传空列表即清除）。数据集里的候选第一次打标签时连音频登记进资料库。"""
    return Ref(**refs.set_tags(ref_id, body.tags))


@api.get("/refs/{ref_id}/audio", tags=["参考音频"], response_class=FileResponse,
         responses={200: {"content": {"audio/ogg": {}, "audio/wav": {}, "audio/mpeg": {}, "audio/flac": {}}}})
async def ref_audio(ref_id: str, format: Optional[AudioFormat] = Query(
        None, description="不填返回原始文件（数据集里的参考是 ogg）；wav/mp3/flac 时转码")):
    """参考的原始音频（数据集里的和上传的都可以），用来试听。"""
    path = Path(refs.index[ref_id]["path"])
    if format is None:
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    return Response(audio_bytes(path, format), media_type=MEDIA[format])


@api.post("/refs/upload", dependencies=OWNED, tags=["参考音频"], response_model=Ref)
async def upload_ref(audio: UploadFile = File(...), text: str = Form(..., description="参考音频的原文"),
                     lang: str = Form("ja"), character: str = Form("custom")):
    """上传一段 3–10 秒参考音频和原文，返回 ref_id（up-xxxx）。同一文件重复上传得到同一个 id。"""
    suffix = os.path.splitext(audio.filename)[1] or ".wav"
    return Ref(**refs.add_upload(await audio.read(), suffix, text, lang, character))


# ---------------------------------------------------------------- 合成

MEDIA = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac"}
HI_BIT_SUBTYPES = {"PCM_24", "PCM_32", "FLOAT", "DOUBLE"}


def gen_caps(aux_ref_ids=None, fragment_interval=None) -> tuple[str, ...]:
    """LAN worker capabilities required for a synthesis task."""
    caps = []
    if any(aux_ref_ids or ()):
        caps.append("aux_refs")
    if fragment_interval is not None and abs(float(fragment_interval) - 0.3) > 1e-9:
        caps.append("fragment_interval")
    return tuple(caps)


def audio_bytes(path: Path, fmt: str):
    """Return raw wav/flac when suffix matches, otherwise transcode to wav/mp3/flac.
    Preserves 24-bit depth when exporting high-bit-depth sources to FLAC."""
    if fmt == "wav" and path.suffix.lower() == ".wav":
        return path.read_bytes()
    if fmt == "flac" and path.suffix.lower() == ".flac":
        return path.read_bytes()
    info = sf.info(str(path))
    data, sr = sf.read(str(path))
    buf = io.BytesIO()
    if fmt == "flac":
        subtype = "PCM_24" if info.subtype in HI_BIT_SUBTYPES else "PCM_16"
        sf.write(buf, data, sr, format="FLAC", subtype=subtype)
    else:
        sf.write(buf, data, sr, format=fmt.upper())
    return buf.getvalue()


@api.post("/tts", tags=["合成"], response_model=AudioItem,
          responses={200: {"content": {"audio/wav": {}, "audio/mpeg": {}, "audio/flac": {}}}})
async def tts(p: TTSParams):
    """合成一条（同步，热状态 2–5 秒，冷启动 20–40 秒）。默认直接返回音频，元数据在 X-* 响应头里；
    debug=true 或 return_json=true 时返回 AudioItem JSON。"""
    ref = refs.pick(p.character, p.ref_id, p.emotion)
    aux_ids, _ = synth.validate_aux_refs([p.engine], p.aux_ref_ids, ref["id"])
    task = Task("gen", [(p.engine, 1)], required_caps=gen_caps(aux_ids, p.fragment_interval), cloud_capable=False)
    token = synth.EXEC.set(dispatch.executor(await ready(task, p)))
    try:
        # 先定设备再解析权重：派到局域网设备时底模用那台的，本机没装这个引擎的底模也能跑
        weights = synth.resolve(p.engine, p.character, p.gpt, p.sovits, version=p.version)
        finish = await synth.synth(p, p.engine, ref, weights, synth.pick_seed(p.seed), score=False)
        item = await finish()
    finally:
        synth.EXEC.reset(token)
    if p.debug or p.return_json:
        return item
    headers = {"X-Audio-Id": item.id, "X-Seed": str(item.seed), "X-Ref-Id": item.ref_id,
               "X-Duration": str(item.duration), "X-Infer-Sec": str(item.infer_sec)}
    return Response(audio_bytes(settings.AUDIO_DIR / f"{item.id}.wav", p.format), media_type=MEDIA[p.format],
                    headers=headers)


async def ready(task: Task, choice) -> dict:
    """同步接口：选设备，睡着的局域网设备先唤醒（dispatch.prepare）。"""
    chosen = await dispatch.choose(task, choice)
    await dispatch.prepare(chosen)
    dispatch.ensure_not_paused(chosen)
    return chosen


def check_version(chosen: dict, p):
    """任务版提交时就核对版本（p.version，p 是生成参数或作品行）绑定的权重还在（缺了 409 version_unavailable），
    不等任务跑起来才失败。"""
    p = p if isinstance(p, dict) else p.model_dump()
    if p.get("version") is not None:
        catalog.resolve(p["engine"], p["character"], p["gpt"], p["sovits"], remote=chosen["type"] != "local",
                        version=p["version"])


def on_device(chosen: dict, fn):
    """任务函数包一层：在选中的设备上跑（本机或局域网设备），睡着的局域网设备先唤醒。synth.EXEC 只在这个任务里生效。"""
    async def run_there(run):
        await dispatch.prepare(chosen, run.stage)
        synth.EXEC.set(dispatch.executor(chosen))
        await fn(run)
    return run_there


def start_on(chosen: dict, kind: str, target: str, fn, involves=()) -> Job:
    dispatch.ensure_not_paused(chosen)
    job = jobs.start(kind, target, on_device(chosen, fn), involves)
    dispatch.assign(job, chosen)
    return job


async def _candidates(p: CandidatesParams, on_item=None, ref: dict = None):
    ref = ref or refs.pick(p.character, p.ref_id, p.emotion)
    return await synth.run_candidates(p, p.engine, p.n, ref, synth.pick_seed(p.seed), on_item=on_item)


async def _compare(p: CompareParams, on_item=None, ref: dict = None):
    ref = ref or refs.pick(p.character, p.ref_id, p.emotion)
    seed = synth.pick_seed(p.seed)
    t = time.perf_counter()
    # 指定的 gpt/sovits 只对 engine 参数那个引擎有效，其他引擎用各自的默认权重
    results = [await synth.run_candidates(p if e == p.engine else p.model_copy(update=dict(gpt=None, sovits=None,
                                                                                            version=None)),
                                          e, p.n, ref, seed, on_item=on_item) for e in p.engines]
    return CompareResult(text=p.text, ref_id=ref["id"], ref_text=ref["text"], base_seed=seed,
                         elapsed_sec=round(time.perf_counter() - t, 2), results=results)


@api.post("/tts/candidates", tags=["合成"], response_model=CandidatesResult)
async def candidates(p: CandidatesParams):
    """同步版：同一句话按不同 seed 生成 n 条，whisper 转写算 CER、和参考算说话人相似度，按 score 排序。
    6 条约 20–30 秒，经网关调用建议用 /tts/candidates/jobs。"""
    ref = refs.pick(p.character, p.ref_id, p.emotion)
    aux_ids, _ = synth.validate_aux_refs([p.engine], p.aux_ref_ids, ref["id"])
    task = Task("gen", [(p.engine, p.n)], required_caps=gen_caps(aux_ids, p.fragment_interval), cloud_capable=False)
    token = synth.EXEC.set(dispatch.executor(await ready(task, p)))
    try:
        return await _candidates(p, ref=ref)
    finally:
        synth.EXEC.reset(token)


async def _attach_gen_cloud_inputs(task: Task, kind: str, p, ref_id: str, aux_ids: list):
    """Estimate upload_mb for cloud synthesis/compare jobs; record cloud_error if local input files are missing."""
    if library.selected() is None:
        return
    from ttsapi.compute import colab_jobs
    try:
        task.upload_mb = await asyncio.to_thread(
            colab_jobs.upload_mb, kind, [p], catalog, refs,
            character=p.character, ref_id=ref_id, aux_ref_ids=aux_ids,
        )
    except (OSError, datasets.DatasetUnavailable) as e:
        task.cloud_error = f"要上传到云端的输入本机缺：{e}"


@api.post("/tts/candidates/jobs", tags=["合成"], response_model=Job)
async def candidates_job(p: CandidatesParams):
    """任务版：立即返回 job，结果在 job.candidates_result。每条候选打完分推一次进度（job.latest / job.completed）。"""
    ref = refs.pick(p.character, p.ref_id, p.emotion)
    aux_ids, _ = synth.validate_aux_refs([p.engine], p.aux_ref_ids, ref["id"])

    async def fn(run):
        run.stage(f"生成 {p.n} 个候选")
        done = itertools.count(1)
        run.job.candidates_result = await _candidates(p, lambda item: run.item_done(item, next(done), p.n), ref=ref)

    task = Task("gen", [(p.engine, p.n)], required_caps=gen_caps(aux_ids, p.fragment_interval))
    await _attach_gen_cloud_inputs(task, "candidates", p, ref["id"], aux_ids)
    chosen = await dispatch.choose(task, p)
    check_version(chosen, p)
    if chosen["type"] == "cloud":
        dispatch.ensure_not_paused(chosen)
        job = jobs.start("candidates", p.character,
                         on_cloud(chosen, "candidates", p, task, character=p.character, ref_id=ref["id"],
                                  aux_ref_ids=aux_ids))
        dispatch.assign(job, chosen)
        return job
    return start_on(chosen, "candidates", p.character, fn)


@api.post("/compare", tags=["合成"], response_model=CompareResult)
async def compare(p: CompareParams):
    """同步版：同一句话、同一参考、同一组 seed，各引擎各出 n 条候选。经网关调用建议用 /compare/jobs。"""
    ref = refs.pick(p.character, p.ref_id, p.emotion)
    aux_ids, _ = synth.validate_aux_refs(p.engines, p.aux_ref_ids, ref["id"])
    task = Task("gen", [(e, p.n) for e in p.engines], required_caps=gen_caps(aux_ids, p.fragment_interval),
                cloud_capable=False)
    token = synth.EXEC.set(dispatch.executor(await ready(task, p)))
    try:
        return await _compare(p, ref=ref)
    finally:
        synth.EXEC.reset(token)


@api.post("/compare/jobs", tags=["合成"], response_model=Job)
async def compare_job(p: CompareParams):
    """任务版：立即返回 job，结果在 job.compare_result。每条候选打完分推一次进度（job.latest / job.completed）。"""
    ref = refs.pick(p.character, p.ref_id, p.emotion)
    aux_ids, _ = synth.validate_aux_refs(p.engines, p.aux_ref_ids, ref["id"])

    async def fn(run):
        run.stage(f"对比 {', '.join(p.engines)}")
        done, total = itertools.count(1), p.n * len(p.engines)
        run.job.compare_result = await _compare(p, lambda item: run.item_done(item, next(done), total), ref=ref)

    task = Task("gen", [(e, p.n) for e in p.engines], required_caps=gen_caps(aux_ids, p.fragment_interval))
    await _attach_gen_cloud_inputs(task, "compare", p, ref["id"], aux_ids)
    chosen = await dispatch.choose(task, p)
    check_version(chosen, p)
    if chosen["type"] == "cloud":
        dispatch.ensure_not_paused(chosen)
        job = jobs.start("compare", p.character,
                         on_cloud(chosen, "compare", p, task, character=p.character, ref_id=ref["id"],
                                  aux_ref_ids=aux_ids))
        dispatch.assign(job, chosen)
        return job
    return start_on(chosen, "compare", p.character, fn)


# ---------------------------------------------------------------- 音频、review、历史

@api.get("/audio/{audio_id}", tags=["音频和 review"], response_class=FileResponse,
         responses={200: {"content": {"audio/wav": {}, "audio/mpeg": {}, "audio/flac": {}}}})
async def get_audio(audio_id: str = AudioId, format: AudioFormat = "wav"):
    wav = synth.audio_file(audio_id)  # 本机生成的，或资料库里盲听、作品的副本（换了控制端时）
    if format == "wav":
        return FileResponse(wav, media_type="audio/wav")
    return Response(audio_bytes(wav, format), media_type=MEDIA[format])


@api.get("/audio/{audio_id}/mel.png", tags=["音频和 review"], response_class=FileResponse,
         responses={200: {"content": {"image/png": {}}}})
async def get_mel(audio_id: str = AudioId):
    png = settings.AUDIO_DIR / f"{audio_id}.png"
    if not png.is_file():
        raise KeyError(audio_id)
    return FileResponse(png, media_type="image/png")


@api.get("/audio/{audio_id}/meta", tags=["音频和 review"], response_model=AudioItem)
async def get_meta(audio_id: str):
    """生成时的参数、分数、debug 数据和 review。"""
    return synth.get_audio(audio_id)


@api.post("/audio/{audio_id}/review", tags=["音频和 review"], response_model=Review)
async def review_audio(audio_id: str, body: ReviewIn):
    """打分（1–5 或 good/bad）、问题标签、备注。同一条音频重复提交会覆盖。打过 review 的音频不会被自动清理。"""
    return reviews.upsert(audio_id, body)


@api.delete("/audio/{audio_id}/review", tags=["音频和 review"], status_code=204)
async def delete_review(audio_id: str):
    """删除这条音频的 review。音频如果不在作品里，之后会按 7 天规则被自动清理。"""
    reviews.delete(audio_id)


AudioSort = Literal["created", "score", "duration", "character", "engine", "rating", "reviewed_at"]


def _filters(character, engine, ref_id, gpt, sovits, work_id, q, temperature_min, temperature_max):
    return dict(character=character, engine=engine, ref_id=ref_id, gpt=gpt, sovits=sovits, work_id=work_id, q=q,
                temperature_min=temperature_min, temperature_max=temperature_max)


@api.get("/history", tags=["音频和 review"], response_model=AudioPage)
async def history(character: Optional[str] = None, engine: Optional[str] = None, ref_id: Optional[str] = None,
                  gpt: Optional[str] = None, sovits: Optional[str] = None, work_id: Optional[str] = None,
                  q: Optional[str] = Query(None, description="文本包含"), temperature_min: Optional[float] = None,
                  temperature_max: Optional[float] = None, reviewed: Optional[bool] = None,
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                  sort: AudioSort = Query("created", description="reviewed_at 是 review 的更新时间"),
                  order: Order = "desc"):
    """所有生成过的音频（含 meta 和 review），默认新的在前。未 review 且不在作品里的音频 7 天后会被清理。"""
    return reviews.page(limit, offset, sort, order, reviewed=reviewed,
                        **_filters(character, engine, ref_id, gpt, sovits, work_id, q, temperature_min, temperature_max))


@api.get("/reviews", tags=["音频和 review"], response_model=AudioPage)
async def list_reviews(character: Optional[str] = None, engine: Optional[str] = None, ref_id: Optional[str] = None,
                       gpt: Optional[str] = None, sovits: Optional[str] = None, work_id: Optional[str] = None,
                       q: Optional[str] = None, temperature_min: Optional[float] = None,
                       temperature_max: Optional[float] = None, tag: Optional[str] = None,
                       verdict: Optional[Literal["good", "bad"]] = None, min_rating: Optional[int] = None,
                       limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                       sort: AudioSort = Query("created", description="created 是音频生成时间，reviewed_at 是 review 的更新时间"),
                       order: Order = "desc"):
    """打过 review 的音频，按角色、引擎、参考、权重、参数、标签过滤。"""
    return reviews.page(limit, offset, sort, order, reviewed=True, tag=tag, verdict=verdict, min_rating=min_rating,
                        **_filters(character, engine, ref_id, gpt, sovits, work_id, q, temperature_min, temperature_max))


@api.get("/reviews/stats", tags=["音频和 review"], response_model=ReviewStats)
async def review_stats(character: Optional[str] = None, engine: Optional[str] = None):
    """按引擎、角色、参考、权重组合、温度区间（0.2 一档）、top_k 汇总：平均分、好/坏占比、各问题标签占比、平均自动分。"""
    return reviews.stats(character=character, engine=engine)


# ---------------------------------------------------------------- 作品

@api.post("/works", dependencies=OWNED, tags=["作品"], response_model=Work)
async def create_work(body: WorkCreate):
    return works.create(body)


@api.get("/works", tags=["作品"], response_model=WorkList)
async def list_works(sort: Literal["created", "updated", "title", "n_lines"] = "created", order: Order = "desc"):
    return WorkList(works=works.list_works(sort, order))


@api.get("/works/{work_id}", tags=["作品"], response_model=Work)
async def get_work(work_id: str):
    """作品详情：每行的参数、全部候选（含分数和 review）、选中的候选、最近一次渲染结果。"""
    return works.to_model(works.load(work_id))


@api.patch("/works/{work_id}", dependencies=OWNED, tags=["作品"], response_model=Work)
async def patch_work(work_id: str, body: WorkPatch):
    """改标题，或整体替换行列表（增删改、调顺序）。带 id 的行保留候选和选择。"""
    return works.patch(work_id, body)


@api.delete("/works/{work_id}", dependencies=OWNED, tags=["作品"], status_code=204)
async def delete_work(work_id: str):
    works.delete(work_id)


@api.post("/works/{work_id}/generate", dependencies=OWNED, tags=["作品"], response_model=Job)
async def generate_work(work_id: str, body: GenerateRequest):
    """任务：每行出 n 个候选并自动选最高分。不指定 line_ids 时只生成还没有候选的行。结果在 job.generate_result。"""
    works.check_lines(work_id, body.line_ids)
    lines = works.targets(works.load(work_id), body.line_ids)
    if not lines:  # 每行都有候选了：不选设备，任务直接以空结果完成（generate_result.lines 为空）
        return jobs.start("work_generate", work_id, lambda run: works.generate_job(run, work_id, body))
    caps = check_work_lines_aux(lines)
    chosen = await dispatch.choose(work_task(lines, body.n, caps), body)
    for line in lines:
        check_version(chosen, line)
    return start_on(chosen, "work_generate", work_id, lambda run: works.generate_job(run, work_id, body))


def check_work_lines_aux(lines: list) -> tuple[str, ...]:
    """Validate aux_ref_ids on target work lines before enqueueing and collect required LAN worker caps."""
    caps = []
    for line in lines:
        aux = line.get("aux_ref_ids") or []
        aux_ids = []
        if aux:
            main_ref = line.get("ref_id") or ("" if line.get("emotion") else refs.default_or_none(line["character"]) or "")
            aux_ids, _ = synth.validate_aux_refs([line["engine"]], aux, main_ref)
        for c in gen_caps(aux_ids, line.get("fragment_interval", 0.3)):
            if c not in caps:
                caps.append(c)
    return tuple(caps)


def work_task(lines: list, n: int, required_caps: tuple = ()) -> Task:
    per = {}
    for line in lines:
        per[line["engine"]] = per.get(line["engine"], 0) + n
    return Task("gen", list(per.items()), required_caps=required_caps, cloud_capable=False)


@api.post("/works/{work_id}/lines", dependencies=OWNED, tags=["作品"], response_model=Work)
async def add_work_line(work_id: str, body: AddLineRequest):
    """新增一行。带 audio_id 时，这条已有音频（比如「生成」页挑中的候选）直接成为这一行的候选并被选中；
    不填 line 时按这条音频的角色、文本、参考、引擎、权重和采样参数建行。"""
    return works.add_line(work_id, body)


@api.post("/works/{work_id}/lines/{line_id}/candidates", dependencies=OWNED, tags=["作品"], response_model=Work)
async def attach_candidate(work_id: str, line_id: str, body: AttachRequest):
    """把已有的生成音频挂到这一行上作为候选（默认同时选中）。挂上去的音频不会被自动清理。"""
    return works.attach(work_id, line_id, body.audio_id, body.pick)


@api.put("/works/{work_id}/lines/{line_id}/pick", dependencies=OWNED, tags=["作品"], response_model=Work)
async def pick_line(work_id: str, line_id: str, body: PickRequest):
    """手动换这一行选中的候选。"""
    return works.pick(work_id, line_id, body.audio_id)


@api.post("/works/{work_id}/lines/{line_id}/regenerate", dependencies=OWNED, tags=["作品"], response_model=Job)
async def regenerate_line(work_id: str, line_id: str, body: RegenerateRequest):
    """任务：给这一行再出 n 个候选（追加，旧候选保留），自动选新候选里的最高分。"""
    req = GenerateRequest(line_ids=[line_id], n=body.n)
    works.check_lines(work_id, req.line_ids)
    lines = works.targets(works.load(work_id), req.line_ids)
    caps = check_work_lines_aux(lines)
    chosen = await dispatch.choose(work_task(lines, body.n, caps), body)
    for line in lines:
        check_version(chosen, line)
    return start_on(chosen, "work_generate", work_id, lambda run: works.generate_job(run, work_id, req))


@api.post("/works/{work_id}/render", dependencies=OWNED, tags=["作品"], response_model=Job)
async def render_work(work_id: str):
    """任务：按行后停顿把选中的候选拼成整轨，响度归一，输出 wav/mp3/srt/lrc。结果在 job.render_result。"""
    return jobs.start("work_render", work_id, works.render_task(work_id))


@api.get("/works/{work_id}/files/{name}", tags=["作品"], response_class=FileResponse,
         responses={200: {"content": {"audio/wav": {}, "audio/mpeg": {}, "text/plain": {}}}})
async def work_file(name: str, work_id: str = WorkId):
    """渲染产物（render_result 里的 URL 指向这里）。"""
    path = library.current().path(f"works/{work_id}/{name}")
    if Path(name).name != name or not path.is_file():
        raise KeyError(name)
    media = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".srt": "text/plain; charset=utf-8",
             ".lrc": "text/plain; charset=utf-8"}[path.suffix]
    return FileResponse(path, media_type=media, filename=name)


# ---------------------------------------------------------------- 训练

@api.get("/datasets", tags=["训练"], response_model=DatasetList)
async def list_datasets():
    """可用的训练数据（GPTSoVits\\datasets\\*\\characters\\*\\*.list），带句数和总时长。"""
    found = datasets.scan()
    return DatasetList(datasets=await asyncio.to_thread(
        lambda: [datasets.info(i, p, c) for i, (p, c) in found.items()]))


@api.post("/datasets/{dataset_id}/preview", tags=["训练"], response_model=DatasetPreview)
async def preview_dataset(dataset_id: str, min_sec: float = 1.0, max_sec: float = 12.0,
                          exclude_interjections: bool = True):
    """按时长和「纯语气词」过滤后还剩多少句、多少分钟。纯语气词：去掉标点后不超过 4 个字且全是
    あいうえおんっーはひふへほやゆよわ 及其小写/片假名，比如「ん？」「……ふう」「はぁ」。"""
    return await asyncio.to_thread(datasets.preview, dataset_id, min_sec, max_sec, exclude_interjections)


@api.post("/train", dependencies=OWNED, tags=["训练"], response_model=Job)
async def train(req: TrainRequest):
    """任务：训练一个角色。训练期间所有推理 worker 卸载，推理接口返回 503；多个训练任务排队依次执行。
    结束后自动评测每个 checkpoint 组合，最好的组合成为这个角色的默认权重。
    训练在独立进程里跑：服务重启或崩溃不影响训练，服务起来后自动接管这个任务；只有 POST /jobs/{id}/cancel 会停掉训练。"""
    datasets.get(req.dataset_id)  # 没登记 404；tar 还没同步到等本机用不了 409（code=dataset_unavailable）
    if req.ref_id and req.ref_id not in refs.index and not req.ref_id.startswith(f"{req.slug}-"):
        raise KeyError(f"参考 {req.ref_id}")
    kept = (await asyncio.to_thread(datasets.preview, req.dataset_id, req.min_sec, req.max_sec,
                                    req.exclude_interjections)).lines_kept
    lines = min(kept, req.max_lines or kept)
    task = training.train_task(req, lines)
    from ttsapi.compute import colab_jobs
    try:  # 云端的开机准备按上传量算
        task.upload_mb = await asyncio.to_thread(colab_jobs.upload_mb, "train", [req], catalog, refs)
    except OSError as e:  # 云端要带的输入本机缺（比如角色的 train.list 还没同步）：只是云端不能用，本机照样能训
        task.cloud_error = f"要上传到云端的输入本机缺：{e}"
    chosen = await dispatch.choose(task, req)
    involves = train_involves(req.slug, req.ref_id)
    if chosen["type"] == "cloud":
        dispatch.ensure_not_paused(chosen)
        job = jobs.start("train", req.slug, on_cloud(chosen, "train", req, task), involves)
        dispatch.assign(job, chosen)
        return job
    return start_on(chosen, "train", req.slug, lambda run: training.train_job(run, req, catalog, refs), involves)


def on_cloud(chosen: dict, kind: str, req, task: Task, **kw):
    """合成、对比、评测、训练派到云端 GPU：控制端准备输入、开 VM、取回结果（colab_jobs.run_colab）。
    开卡的预计时长和提交时的估算是同一个（chosen）。做完按实际耗时修正这张卡的速度。"""
    async def fn(run):
        from ttsapi.compute import colab_jobs
        t = time.time()
        cloud = registry.cloud_settings()
        await colab_jobs.run_colab(run, kind, req, gpu=chosen["gpu"], estimated_minutes=chosen["seconds"] / 60,
                                   approved=req.approved, pool=registry.pool,
                                   catalog=catalog, refs=refs, max_minutes=cloud["max_minutes"],
                                   keep_minutes=cloud["keep_minutes"], device_id=chosen["device"], **kw)
        if kind == "train" and task.lines:
            sec = training.train_seconds(run.job.train_result.run_name)
            speeds.observe(chosen["device"], "train", sec / (task.lines * task.epochs))
        elif kind == "eval" and task.parts[0][1]:
            spent = time.time() - t - scheduler.PREP_CLOUD
            speeds.observe(chosen["device"], req.engine, max(1.0, spent) / task.parts[0][1])
        elif kind == "candidates" and run.job.candidates_result:
            for item in run.job.candidates_result.candidates:
                if item.infer_sec > 0:
                    speeds.observe(chosen["device"], item.engine, speeds.per_hard(item.infer_sec, item.text))
        elif kind == "compare" and run.job.compare_result:
            for part in run.job.compare_result.results:
                for item in part.candidates:
                    if item.infer_sec > 0:
                        speeds.observe(chosen["device"], item.engine, speeds.per_hard(item.infer_sec, item.text))
    return fn


async def sweep_groups(req: SweepRequest):
    """扫参展开的组、要跑的组的估时任务（每组一个，云端按账号余额分段）。只对要跑的组查数据集和参考：
    已在资料库里的组（skipped）不用本机有它的数据集。"""
    from ttsapi.compute import colab_jobs, colab_sweep
    if bad := [e for e in req.engines if e not in settings.CLOUD_ENGINES]:
        raise HTTPException(422, f"云端不能训 {', '.join(bad)}（能训 {', '.join(settings.CLOUD_ENGINES)}）")
    groups = colab_sweep.groups(req, catalog)
    pending = [x for x in groups if x[0].status == "pending"]
    for c in {g.character for g, _ in pending}:
        if c in req.ref_ids and req.ref_ids[c] not in refs.index:
            raise KeyError(f"参考 {req.ref_ids[c]}")
    lines = {}
    for _, tr in pending:
        if tr.dataset_id not in lines:
            datasets.get(tr.dataset_id)
            kept = (await asyncio.to_thread(datasets.preview, tr.dataset_id, req.min_sec, req.max_sec,
                                            req.exclude_interjections)).lines_kept
            lines[tr.dataset_id] = min(kept, req.max_lines or kept)
    task = colab_sweep.task(pending, lines, req.samples)
    if pending:  # 每台 VM 都按整批的输入算上传
        task.upload_mb = await asyncio.to_thread(colab_jobs.upload_mb, "train", [tr for _, tr in pending], catalog, refs)
    return groups, task


def _sweep_running(pending: set):
    """pending：要跑的组的完整参数键（colab_sweep.key）。"""
    if running := [r.job.id for r in jobs.runs.values() if r.job.kind == "sweep" and r.sweep_keys & pending]:
        raise HTTPException(409, f"有扫参任务在跑同样的组（{', '.join(running)}），结束后再提交")


@api.post("/train/sweep/plan", tags=["训练"], response_model=SweepPlan)
async def train_sweep_plan(req: SweepRequest):
    """扫参的「在哪跑」：展开的组（资料库里已有同样参数的 run 的组是 skipped），要跑的组整批的推荐设备、预计耗时、单元、
    要不要先问（同 POST /plan）。云端按账号余额分几台 VM（vms），每台各算一次开机准备和花费；组都在资料库里时 plan 为 null
    （提交时不开 VM）。"""
    groups, task = await sweep_groups(req)
    pending = any(g.status == "pending" for g, _ in groups)
    return SweepPlan(plan=await dispatch.plan(task, req.compute) if pending else None, groups=[g for g, _ in groups])


@api.post("/train/sweep", dependencies=OWNED, tags=["训练"], response_model=Job)
async def train_sweep(req: SweepRequest):
    """任务（kind=sweep）：角色 × 引擎 × 训练参数（trains），在云端 VM 上依次训练、训后评测。每组训完就把训后评测
    前 keep 个组合的权重、评测、训练任务记录（<任务 id>-<组序号>）写进资料库，前 samples 个组合各生成几句样本放进
    runs/<run>/samples/。资料库里已有同样参数的 run 的组跳过（重复提交同一批只跑没完成的组）；VM 中途被回收时，
    已写进资料库的组不重跑，剩下的换一台 VM 接着跑。花费按分段计划（几台 VM）算，超过提醒线时 409 confirm_required。
    只在云端 GPU 上跑。每组的状态在 job.sweep_result。"""
    from ttsapi.compute import colab_sweep
    groups, task = await sweep_groups(req)
    pending = {colab_sweep.key(tr) for g, tr in groups if g.status == "pending"}
    _sweep_running(pending)
    chosen = await dispatch.choose(task, req) if pending else None  # 本机、局域网是 cloud_only
    # 等 choose 时别的请求可能已经登记了同样的组、或者有组写进了资料库：重查，查完到登记任务之间不再 await
    _sweep_running(pending)
    if {colab_sweep.key(tr) for g, tr in colab_sweep.groups(req, catalog) if g.status == "pending"} != pending:
        raise HTTPException(409, "这一批里有组刚写进资料库，重新提交（会跳过这些组）")
    involves = set(req.characters) | {g.slug for g, _ in groups} | \
        {refs.index[r]["character"] for r in req.ref_ids.values() if r in refs.index}
    target = ",".join(req.characters)
    if not pending:  # 都在资料库里了：不开 VM，任务直接完成
        job = jobs.start("sweep", target, lambda run: asyncio.sleep(0))
    else:
        dispatch.ensure_not_paused(chosen)
        cloud = registry.cloud_settings()
        job = jobs.start("sweep", target, lambda run: colab_sweep.run_sweep(
            run, groups, chosen, approved=req.approved, pool=registry.pool, catalog=catalog, refs=refs,
            keep=req.keep, samples=req.samples, into_builtin=req.into_builtin, max_minutes=cloud["max_minutes"],
            keep_minutes=cloud["keep_minutes"]), involves)
        jobs.runs[job.id].sweep_keys = pending
        dispatch.assign(job, chosen)
    job.sweep_result = SweepResult(groups=[g for g, _ in groups])
    return job


def train_involves(slug: str, ref_id: Optional[str]) -> set:
    """训练任务占用的角色：训练的角色本身，加上 ref_id 指定的参考所属的角色（训后评测要读那条参考音频，
    那个角色在训练期间被删掉的话评测会失败）。ref_id 是这个角色自己训练列表里的句子（训完才建索引）时只有它自己。"""
    return {slug} | ({refs.index[ref_id]["character"]} if ref_id in refs.index else set())


@api.post("/runs/import", dependencies=OWNED, tags=["训练"], response_model=RunImportResult)
async def import_runs(body: RunImportRequest):
    """把别处（Colab 等）训练的结果写进资料库：训练 run、权重文件（带 sha256 核对）、训练的小文件、评测、新角色；
    训练任务记录进本机 tts.db。资料库里已有的 run 跳过，可以重复调用；本机已有、结果为空的原任务（Colab 作业写库失败后
    用结果 tar 重试）按任务 id 补上结果（filled）。规则见 ttsapi/importer.py。
    有训练、评测、盲听生成任务时返回 409。"""
    if busy := jobs.busy():
        raise HTTPException(409, f"有训练、评测或盲听生成任务在进行（{', '.join(busy)}），结束后再导入")
    if not Path(body.path).exists():
        raise HTTPException(404, f"{body.path} 不存在")
    return importer.import_runs(body.path, catalog, refs, body.runs, body.into_builtin)


@api.get("/train/{job_id}/eval", tags=["训练"], response_model=TrainEval)
async def train_eval(job_id: str):
    """训练后自动评测：每个 GPT×SoVITS 组合在评测集（这一轮起是 hard，之前是 mini）上的 CER、说话人相似度、score
    的中位数、均值、最差值和逐句结果。"""
    job = jobs.get(job_id)
    if job.train_result is None or job.train_result.eval is None:
        raise HTTPException(404, f"任务 {job_id} 没有评测结果（状态 {job.status}，阶段 {job.stage}）")
    return job.train_result.eval


# ---------------------------------------------------------------- 权重管理

def _weight(wid: str, eval_set: str = settings.EVAL_DEFAULT_SET) -> WeightEntry:
    for w in catalog.weight_entries(eval_set):
        if w.id == wid:
            return w
    raise KeyError(f"权重 {wid}")


EvalSetQuery = Query(settings.EVAL_DEFAULT_SET, description="best_eval 和 sort=score 用哪个评测集的分数（不同集不混着比）")


@api.get("/weights", tags=["权重"], response_model=WeightList)
async def list_weights(character: Optional[str] = Query(None, description="只看属于这个角色的（底模不属于任何角色）"),
                       engine: Optional[Engine] = None, source: Optional[WeightSource] = None,
                       type: Optional[Literal["gpt", "sovits"]] = None,
                       hidden: Optional[bool] = Query(None, description="不填全部；true 只看隐藏的；false 只看没隐藏的"),
                       sort: WeightSort = Query("created_at", description="score 按 best_eval.score_median，没评测过的排最后"),
                       order: Order = "desc", eval_set: EvalSetName = EvalSetQuery):
    """权重级别的列表：底模、7 月的旧权重、训练出的权重，每个 GPT / SoVITS 文件一条。
    带来源、训练任务、数据集和过滤条件、评测分数、显示名、备注、是否默认、是否隐藏。
    evals 列出所有评测集的结果（每条带 eval_set）；best_eval 只在 eval_set 指定的集（默认 hard）里挑。"""
    items = [w for w in catalog.weight_entries(eval_set)
             if (character is None or w.character == character) and (engine is None or w.engine == engine)
             and (source is None or w.source == source) and (type is None or w.type == type)
             and (hidden is None or w.hidden == hidden)]
    key = {"created_at": lambda w: w.created_at, "score": lambda w: w.best_eval and rank(w.best_eval),
           "epoch": lambda w: w.epoch, "engine": lambda w: w.engine, "character": lambda w: w.character,
           "size": lambda w: w.size_bytes, "name": lambda w: w.name, "type": lambda w: w.type,
           "source": lambda w: w.source}[sort]
    # 没有值的（没评测过、底模没有角色和 epoch）不管升序降序都排最后
    present = sorted((w for w in items if key(w) is not None), key=key, reverse=order == "desc")
    return WeightList(total=len(items), weights=present + [w for w in items if key(w) is None])


@api.get("/weights/{weight_id}", tags=["权重"], response_model=WeightEntry)
async def get_weight(weight_id: str, eval_set: EvalSetName = EvalSetQuery):
    return _weight(weight_id, eval_set)


@api.patch("/weights/{weight_id}", dependencies=OWNED, tags=["权重"], response_model=WeightEntry)
async def patch_weight(weight_id: str, body: WeightPatch):
    """改显示名、备注，隐藏或取消隐藏，设为（或取消）某个角色在这个引擎下默认的 GPT / SoVITS。所有来源都可以改。
    设默认只改这个权重对应的那一半：要换整个组合，对 GPT 和 SoVITS 各 PATCH 一次。"""
    x = catalog.find(weight_id)
    # 先检查设默认的条件，出错时什么都不改
    if body.is_default is not None:
        char = body.character or x["character"]
        if char is None:
            raise HTTPException(400, "底模是所有角色共用的，设默认时要用 character 指定角色")
        if char not in catalog.slugs():
            raise KeyError(f"角色 {char}")
        if x["character"] not in (None, char):
            raise HTTPException(400, f"这个权重属于 {x['character']}，不能设为 {char} 的默认")
    fields = {k: v for k, v in dict(display_name=body.display_name, note=body.note, hidden=body.hidden).items()
              if v is not None}
    if fields and x["meta"] is None:
        raise HTTPException(400, "底模不在资料库里，不能改显示名、备注、隐藏（设默认可以）")
    if "display_name" in fields:
        fields["display_name"] = fields["display_name"] or None
    if fields:
        catalog.set_meta(weight_id, **fields)
    if body.is_default is not None:
        # 记下选的是底模还是训练出的权重（同名时分得开，推荐版本按它生成 id）
        if body.is_default:
            catalog.set_default(x["engine"], char, x["kind"], half(x))
        elif catalog.manual_default(x["engine"], char, x["kind"]) == half(x):
            catalog.set_default(x["engine"], char, x["kind"], None)
    return _weight(weight_id)


@api.delete("/weights/{weight_id}", dependencies=OWNED, tags=["权重"], response_model=DeletedWeight)
async def delete_weight(weight_id: str):
    """删除一个训练出的权重文件（只允许 user 来源；7 月的旧权重和底模返回 403；有训练、评测或盲听生成任务在进行时返回 409）。
    如果它是某个角色的默认，这个角色自动换成剩下的权重里评测分最高的组合（没评测过就用最近一次训练的最后一个），
    在 new_defaults 里返回。"""
    w = _weight(weight_id)
    if not w.deletable:
        raise HTTPException(403, f"{weight_id} 是{'7 月训练的旧权重' if w.source == 'july' else '底模'}，不能删除")
    # 训练后评测、手动评测、盲听生成会按名字用权重，正在跑或排队时不删
    if busy := jobs.busy():
        raise HTTPException(409, f"有训练、评测或盲听生成任务在进行（{', '.join(busy)}），结束后再删")
    catalog.delete_weight(catalog.find(weight_id))
    log.info("deleted weight %s (default for %s)", weight_id, w.default_for)
    return DeletedWeight(id=w.id, path=w.path, was_default_for=w.default_for, new_defaults=[
        DefaultChange(character=c, gpt=(d := catalog.defaults(w.engine, c))[0], sovits=d[1]) for c in w.default_for])


@api.post("/weights/eval", dependencies=OWNED, tags=["权重"], response_model=Job)
async def eval_weights(body: EvalRequest):
    """任务：对任意一组 GPT + SoVITS 跑一次评测：评测集（eval_set，默认 hard，见 GET /eval-sets）每句各出
    candidates 个候选（seed 固定），统计 CER、说话人相似度和 score 的中位数、均值、最差值和逐句结果。结果在 job.eval_result，
    同时记进这两个权重的 evals（带 eval_set），参与「评测分最高」的默认选择。训练期间会排队等训练结束。
    给 version（和 character）时评测这个版本绑定的权重，同生成的 version：绑定的权重没有了 409 version_unavailable。"""
    # 按版本评测时，训练出的那一半都是 character 的（下面 involves 里有它），不按文件名找归属（同名的可能是别的角色的或底模）
    owners = set() if body.version else \
        {catalog.owner(body.engine, body.gpt), catalog.owner(body.engine, body.sovits)} - {None}
    char = body.character or catalog.owner(body.engine, body.gpt) or catalog.owner(body.engine, body.sovits)
    if char is None:
        raise HTTPException(400, "两个都是底模，要用 character 指定用哪个角色的参考")
    ref_id = body.ref_id or refs.default_for(char)
    involves = owners | {char, refs.index[ref_id]["character"]}
    per = body.candidates or settings.EVAL_CANDIDATES[body.eval_set]
    items = len(settings.EVAL_SETS[body.eval_set]["sentences"]) * per
    task = Task("eval", [(body.engine, items)])
    chosen = await dispatch.choose(task, body)
    # 选完设备再核对权重名：不在本机跑时底模用那台的（本机没装也行）
    catalog.resolve(body.engine, char, body.gpt, body.sovits, remote=chosen["type"] != "local", version=body.version)
    if chosen["type"] == "cloud":
        dispatch.ensure_not_paused(chosen)
        job = jobs.start("eval", char, on_cloud(chosen, "eval", body, task, character=char, ref_id=ref_id), involves)
        dispatch.assign(job, chosen)
        return job
    return start_on(chosen, "eval", char, lambda run: training.eval_job(run, body, char, ref_id, catalog, refs),
                    involves)


@api.get("/eval-sets", tags=["权重"], response_model=EvalSetList)
async def eval_sets():
    """评测集：每句的文本和考点。评测（POST /weights/eval、训练后自动评测）和盲听按 name 选；不同集的分数不能放在一起比。"""
    return EvalSetList(sets=[EvalSet(name=n, description=s["description"], default=n == settings.EVAL_DEFAULT_SET,
                                     candidates=settings.EVAL_CANDIDATES[n], sentences=s["sentences"])
                             for n, s in settings.EVAL_SETS.items()])


# ---------------------------------------------------------------- 盲听 A/B

@api.post("/blind", dependencies=OWNED, tags=["盲听"], response_model=BlindTest)
async def create_blind(body: BlindCreate):
    """创建一次盲听并开始生成（任务，kind=blind，进度看 GET /jobs/{job_id}）。评测集的每句、每次重复是一道题，
    A、B 各念一遍，左右顺序随机。训练期间生成任务排队。生成中途已经好的题就能听、能投。"""
    if body.character not in catalog.slugs():
        raise KeyError(f"角色 {body.character}")
    jobs.check_open()  # 先查，免得更新期间建出一个没有生成任务的盲听
    n = len(settings.EVAL_SETS[body.eval_set]["sentences"]) * body.repeats
    chosen = await dispatch.choose(Task("gen", [(body.a.engine, n), (body.b.engine, n)], cloud_capable=False), body)
    doc = blind.create(body, catalog, refs, remote=chosen["type"] != "local")
    blind_start(doc, chosen)
    return blind.to_model(doc)


def blind_start(doc: dict, chosen: dict):
    dispatch.ensure_not_paused(chosen)
    job = jobs.start("blind", doc["id"], on_device(chosen, lambda run: blind.generate_job(run, doc["id"])),
                     blind.involves(doc, catalog))
    dispatch.assign(job, chosen)
    doc["job_id"] = job.id
    blind.save(doc)


@api.get("/blind", tags=["盲听"], response_model=BlindList)
async def list_blind(character: Optional[str] = None, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    """盲听列表，新的在前。没揭晓的 a / b 为 null。"""
    total, items = blind.list_tests(character, limit, offset)
    return BlindList(total=total, tests=items)


@api.get("/blind/{test_id}", tags=["盲听"], response_model=BlindTest)
async def get_blind(test_id: str = BlindId):
    """盲听详情和题目。揭晓前 a / b 为 null；题目里只有左右两段音频的地址、投票和备注，没有模型信息。"""
    return blind.to_model(blind.load(test_id))


@api.get("/blind/{test_id}/questions/{qid}/{side}", tags=["盲听"], response_class=FileResponse,
         responses={200: {"content": {"audio/wav": {}, "audio/mpeg": {}, "audio/flac": {}}}})
async def blind_audio(side: Literal["left", "right"], qid: str, test_id: str = BlindId,
                      format: AudioFormat = "wav"):
    """一道题左边或右边的音频。"""
    wav = blind.audio_path(test_id, qid, side)
    if format == "wav":
        return FileResponse(wav, media_type="audio/wav")
    return Response(audio_bytes(wav, format), media_type=MEDIA[format])


@api.put("/blind/{test_id}/questions/{qid}/vote", dependencies=OWNED, tags=["盲听"], response_model=BlindQuestion)
async def blind_vote(qid: str, body: BlindVote, test_id: str = BlindId):
    """投票：left / right / tie（差不多），可带备注。重复提交即改票（揭晓后也能改，结果跟着变）。题目没生成好时 409。"""
    return blind.vote(test_id, qid, body.choice, body.note)


@api.post("/blind/{test_id}/reveal", dependencies=OWNED, tags=["盲听"], response_model=BlindResult)
async def blind_reveal(test_id: str = BlindId):
    """主动揭晓（没投完也可以），返回结果。全部投完时不用调，直接 GET /result。"""
    return blind.reveal(test_id)


@api.get("/blind/{test_id}/result", tags=["盲听"], response_model=BlindResult)
async def blind_result(test_id: str = BlindId):
    """揭晓后的结果：每个组合的胜 / 负 / 平、胜率，逐题揭晓左边是哪个组合、谁赢、两边的音频和自动打分。
    还没投完、也没调过 reveal 时 409。"""
    return blind.result(test_id)


@api.post("/blind/{test_id}/generate", dependencies=OWNED, tags=["盲听"], response_model=BlindTest)
async def blind_generate(test_id: str = BlindId, body: Optional[BlindGenerateRequest] = None):
    """补齐没生成好的题（生成任务失败、被取消或服务重启中断后用）。请求体可以不带（自动选设备）；compute、approved……同其他
    提交任务的接口：超过提醒线时 409 confirm_required 附 plan，确认后带 approved=true、approved_device、approved_units 再提交。
    已经在生成或全部生成好时 409；两边的版本绑定的权重没有了（删了、文件不在这台机器上）时 409 version_unavailable，不建任务。"""
    def check():
        doc = blind.load(test_id)
        if (status := blind.summary(doc).status) != "failed":
            raise HTTPException(409, f"盲听 {test_id} 不需要补生成（{status}）")
        return doc
    doc = check()
    left = sum(1 for q in doc["questions"] if not (q["audio"]["A"] and q["audio"]["B"]))
    chosen = await dispatch.choose(Task("gen", [(c["engine"], left) for c in doc["combos"].values()],
                                        cloud_capable=False), body)
    # 选设备时让出过事件循环：重新读一遍再查状态，查、建任务、记 job_id 之间不再 await，两个并发请求只有一个能开始
    doc = check()
    for c in doc["combos"].values():
        check_version(chosen, dict(c, character=doc["character"]))
    blind_start(doc, chosen)
    return blind.to_model(doc)


@api.delete("/blind/{test_id}", dependencies=OWNED, tags=["盲听"], status_code=204)
async def delete_blind(test_id: str = BlindId):
    """删除盲听。生成任务还在跑就先取消。它的音频（不在作品里、没打 review 的）之后按 7 天规则清理。"""
    await blind.delete(test_id)


# ---------------------------------------------------------------- 算力

def device_model(dev: dict) -> ComputeDevice:
    return ComputeDevice(**{k: v for k, v in dev.items() if k in ComputeDevice.model_fields and k != "speeds"},
                         speeds={k: dict(sec_per_item=v[0], source=v[1]) for k, v in dev["speeds"].items()})


@api.get("/compute", tags=["算力"], response_model=ComputeList)
async def list_compute():
    """设备列表：本机、局域网设备、云端 GPU（每种卡一项）。每项有状态、能跑的引擎、硬件、已加载的引擎，
    以及每个引擎（另有 scorer、train）的每句秒数和来源（已实测 / 参考值 / 未测）。局域网设备当场查一次状态（3 秒超时）。"""
    devices, auto = await registry.snapshot()
    return ComputeList(devices=[device_model(d) for d in devices], auto=AutoSettings(**auto),
                       engines=settings.installed_engines(), month_used=round(registry.month_used(), 2))


@api.post("/compute/devices", tags=["算力"], response_model=ComputeDevice)
async def add_device(body: DeviceAdd):
    """手填连接地址和配对码加一台局域网设备（那台机器上直接运行 python -m ttsapi.gateway --role worker，没配对时终端里
    显示连接地址和配对码；「添加算力」里的手动填用 POST /compute/invites/{id}/manual）。core 用配对码换长期令牌，存在主控
    本地，界面看不到。name 不给时用它报的机器名；同一台（按它的本机 id）已经在列表里时更新那一项，不另加。
    连不上、配对码不对或过期时 400，不登记；换到令牌就登记，之后连不上只是显示离线。"""
    try:
        dev = await registry.add_device(body.address, body.pair_code, body.name, body.wake and body.wake.model_dump())
    except registry.PairError as e:
        raise HTTPException(400, str(e)) from None
    except (httpx.HTTPError, OSError) as e:  # 连不上：原因原样给界面
        raise HTTPException(400, f"连不上 {body.address}：{type(e).__name__}: {e}") from None
    return await _device(dev["id"])


@api.post("/compute/invites", tags=["算力"], response_model=ComputeInvite,
          responses={409: {"model": ErrorResponse, "description": "选了 Tailscale，主控没连 Tailscale（code=tailscale_unavailable）"}})
async def create_invite(body: InviteCreate):
    """「添加算力 → 局域网或远程电脑」：生成配对码（10 分钟有效、只核销一次）和一行安装命令，那台电脑跑完命令会自己连回来，
    轮询 GET /compute/invites/{id} 等它连上。再调一次就是「重新生成配对码」：没核销的旧码马上作废。
    命令里的主控名：局域网是 mDNS 名，Tailscale 是 MagicDNS 名（没有时 Tailscale IP）；主控在 80 端口提供 /install 时
    命令不带端口，80 绑不上时带 core 端口。URL 在单引号里。Windows 的命令还没做（null）。"""
    try:
        host = await asyncio.to_thread(enroll.host_for, body.network)
    except pair.TailscaleUnavailable as e:
        return JSONResponse(status_code=409, content=dict(detail=f"主控没连 Tailscale：{e}", code="tailscale_unavailable"))
    inv = enroll.create(body.network, host)
    port = "" if install_port == 80 else f":{install_port or settings.PORT}"
    return ComputeInvite(id=inv["id"], code=inv["code"], network=inv["network"], host=host,
                         expires_at=enroll.expires_at(inv), expires_in=enroll.expires_in(inv), commands=InviteCommands(
                             unix=f"curl -fsSL 'http://{host}{port}/install' | sh -s -- --pair {inv['code']}"))


async def invite_status(invite_id: str) -> InviteStatus:
    inv = enroll.get(invite_id)
    if inv is None:
        return InviteStatus(id=invite_id, state="expired", expires_in=0, redeemed=False)
    device = await _device(inv["device_id"]) if inv["state"] == "connected" else None
    return InviteStatus(id=inv["id"], state=inv["state"], expires_at=enroll.expires_at(inv),
                        expires_in=enroll.expires_in(inv), redeemed=inv["device_id"] is not None, device=device)


@api.get("/compute/invites/{invite_id}", tags=["算力"], response_model=InviteStatus)
async def get_invite(invite_id: str):
    """邀请的状态：waiting（等那台电脑装好、连回来；用配对码连回来后到它的服务起来、报第一次心跳之前也是 waiting）、
    connected（device 是连上的设备，可以改名）、expired（过了 10 分钟没核销、被重新生成作废、配对码输错太多次）。
    查不到（取消了、主控重启过）也是 200 expired，device 为 null。"""
    return await invite_status(invite_id)


@api.delete("/compute/invites/{invite_id}", tags=["算力"], status_code=204)
async def cancel_invite(invite_id: str):
    """取消（关掉「添加算力」弹窗时）：配对码作废。重复调、查不到都是 204；已经连上的设备不受影响（不删设备）。"""
    enroll.cancel(invite_id)


@api.post("/compute/invites/{invite_id}/manual", tags=["算力"], response_model=InviteStatus,
          responses={409: {"model": ErrorResponse, "description": "邀请已经核销（自动连回或手动填先配上了）或正在手动配对（code=invite_used）、"
                                                                  "过期、查不到（code=invite_expired）"}})
async def invite_manual(invite_id: str, body: InviteManual):
    """「没有自动连上？手动填」：那台电脑连不上主控时终端里显示连接地址和同一个配对码，用户只填地址。主控拿这个邀请的配对码
    调那台的 POST /pair 换长期令牌，换到就存下、邀请 connected（之后连不上只是设备显示离线），返回值里有 device（名字是它报的
    机器名；同一台已经在列表里时更新那一项、不另加）。一个邀请只核销一次：自动连回和手动填谁先到算谁，后到的 409。
    POST /pair 连不上、那台不认配对码：400，邀请还能再用。"""
    try:
        inv = enroll.begin_manual(invite_id)
    except KeyError:
        return JSONResponse(status_code=409, content=dict(detail="配对码过期了：重新生成", code="invite_expired"))
    except enroll.Used:
        return JSONResponse(status_code=409, content=dict(detail="这个配对码已经用过了（那台电脑已经连上，或者正在手动配对）",
                                                          code="invite_used"))
    try:
        dev = await registry.add_device(body.address, inv["code"])
    except registry.PairError as e:
        raise HTTPException(400, str(e)) from None
    except (httpx.HTTPError, OSError) as e:
        raise HTTPException(400, f"连不上 {body.address}：{type(e).__name__}: {e}") from None
    finally:
        inv["busy"] = False
    inv.update(device_id=dev["id"], state="connected")
    return await invite_status(invite_id)


@api.post("/devices/pair", tags=["算力（设备连回主控）"], response_model=DevicePaired)
async def device_pair(body: DevicePair):
    """局域网设备装好后自动连回主控（安装脚本里的 python -m ttsapi.pair join，Studio 不调）：核对配对码，登记设备，
    发长期令牌。之后主控派任务、设备报心跳都带这个令牌。同一台机器重装时更新原来那项，旧令牌用到新版本带新令牌报第一次
    心跳为止（重装失败、旧服务接着跑时照样能用；换回旧服务时它带旧令牌报心跳又换回来）。配对码不对、过期：403；这个邀请已经核销（用户先手动填配上了）或正在
    手动配对：409。"""
    try:
        inv = enroll.redeem(body.code)
    except enroll.Used:
        return JSONResponse(status_code=409, content=dict(detail="这个配对码已经用过了", code="invite_used"))
    if inv is None:
        raise HTTPException(403, "配对码不对或过期了：在 Studio 里重新生成")
    dev, token = registry.join(body.machine_id, body.address, body.machine, body.hardware.model_dump(), body.engines)
    inv.update(device_id=dev["id"], token=token)
    return DevicePaired(device_id=dev["id"], token=token, heartbeat_sec=30)


@api.post("/devices/{device_id}/heartbeat", tags=["算力（设备连回主控）"], status_code=204)
async def device_heartbeat(device_id: str, body: DeviceHeartbeat, authorization: str = Header("")):
    """设备每 30 秒报一次心跳（带配对时发的令牌）：记下时间（GET /compute 的 last_seen，超过 90 秒没来算离线），
    地址变了跟着改；带的是重装时发的新令牌时换上它（旧令牌留着，换回旧服务时用）；发这个令牌的邀请变成 connected。令牌不对 401；
    没有这台设备（被移除了）404。"""
    token = authorization.removeprefix("Bearer ")
    try:
        registry.heartbeat(device_id, token, body.address)
    except registry.BadToken:
        raise HTTPException(401, "令牌不对") from None
    enroll.connect(device_id, token)


@api.get("/install", tags=["算力（设备连回主控）"], response_class=Response,
         responses={200: {"content": {"text/x-shellscript": {}}}})
async def install_script(request: Request):
    """局域网设备的安装脚本（macOS / Linux，sh），写好了主控地址（请求里用的主机名 + core 端口）和取脚本的地址
    （同一个主机名 + 这次连的端口，代码包也从那里取）。主机名只认 DNS 名和 IPv4，别的（比如 Host 头里带了 shell 的字符）
    换成主控的 mDNS 名。主控另外绑上 80 端口时那里也提供（只有这个和 /install/code.tar）。用法见 POST /compute/invites
    的 commands.unix。"""
    host = request.url.hostname or ""
    host = host if HOSTNAME.fullmatch(host) else pair.mdns_host()
    port = request.scope["server"][1]
    return Response(enroll.script(f"http://{host}:{settings.PORT}", f"http://{host}" + ("" if port == 80 else f":{port}")),
                    media_type="text/x-shellscript; charset=utf-8")


@api.get("/install/code.tar", tags=["算力（设备连回主控）"], response_class=Response,
         responses={200: {"content": {"application/x-tar": {}}}})
async def install_code():
    """安装脚本取的代码：core（主控已提交的版本，带和主控同名的版本 tag）的 Git bundle、GPT-SoVITS 补丁及 install.env。
    80 端口上也提供（设备只连得上 80 时照样装好，等手动填）。"""
    return Response(await asyncio.to_thread(enroll.code_tar), media_type="application/x-tar")


async def _device(device_id: str) -> ComputeDevice:
    devices, _ = await registry.snapshot()
    dev = next((d for d in devices if d["id"] == device_id), None)
    if dev is None:
        raise KeyError(f"设备 {device_id}")
    return device_model(dev)


@api.patch("/compute/devices/{device_id}", tags=["算力"], response_model=ComputeDevice)
async def patch_device(device_id: str, body: DevicePatch):
    """改名（局域网设备和本机；本机给空字符串回到机器名）、改地址、重新配对（pair_code）、设唤醒（局域网设备；
    wake 给 null 去掉唤醒），暂停使用 / 恢复（所有设备，包括本机和云端卡）。
    配了唤醒的设备离线时照样是候选，派任务前 core 先发唤醒包、等它上线（状态 waking），超时算离线。"""
    lan_only = body.address is not None or body.pair_code is not None or "wake" in body.model_fields_set
    if device_id.startswith("colab-") and (lan_only or body.name is not None):
        raise HTTPException(400, "云端卡只能暂停或恢复")
    if device_id == "local" and lan_only:
        raise HTTPException(400, "本机只能改名、暂停或恢复")
    await _device(device_id)
    fields = body.model_dump(exclude={"wake", "pair_code"})
    if "wake" in body.model_fields_set:
        fields["wake"] = body.wake and body.wake.model_dump()
    if body.pair_code is not None:
        address = body.address or registry.lan_device(device_id)["address"]
        try:
            fields["token"] = (await registry.pair(address, body.pair_code))["token"]
        except registry.PairError as e:
            raise HTTPException(400, str(e)) from None
        except (httpx.HTTPError, OSError) as e:
            raise HTTPException(400, f"连不上 {address}：{type(e).__name__}: {e}") from None
    registry.patch_device(device_id, **fields)
    return await _device(device_id)


@api.delete("/compute/devices/{device_id}", tags=["算力"], status_code=204)
async def delete_device(device_id: str):
    """移除一台局域网设备（它缓存的权重留在那台机器上，7 天没用自动删）。"""
    registry.remove_device(device_id)


# ---------------------------------------------------------------- 云端 GPU（账号池）

def cloud_account(a) -> CloudAccount:
    return CloudAccount(id=a.id, label=a.label, email=a.email, balance=a.balance,
                        month_used_estimate=round(a.recorded_month_used, 4), gpus=list(a.gpus), disabled=a.disabled,
                        error=a.error)


def _cloud_info(refresh: bool, activities: dict = None) -> CloudInfo:
    pool, data = registry.pool, registry.load()
    accounts = pool.accounts(refresh=refresh, include_disabled=True) if pool else []
    gpus = [CloudGpu(gpu=d["gpu"], label=registry.GPU_LABEL.get(d["gpu"], d["gpu"]), account_count=d["accounts"],
                     units_per_hour=d["rate"], hours_left=d["hours"] and round(d["hours"], 2),
                     available=not d.get("paused") and not d["status_reason"]
                     and d["status"] in ("idle", "busy", "training"),
                     reason="已暂停" if d.get("paused") or d["status"] == "paused" else d["status_reason"],
                     premium=d["premium"], speeds={k: dict(sec_per_item=v[0], source=v[1]) for k, v in d["speeds"].items()})
            for d in registry.cloud_states(data["paused"], data["cloud"], activities)]
    return CloudInfo(installed=pool is not None, accounts=[cloud_account(a) for a in accounts], gpus=gpus,
                     total_balance=round(sum(a.balance or 0 for a in accounts if not a.disabled), 2),
                     month_used_estimate=round(pool.month_used() if pool else 0.0, 2),
                     usage_monthly=[CloudUsageMonth(**m) for m in (pool.usage_monthly() if pool else [])],
                     account_labels=pool.labels() if pool else {},
                     settings=CloudSettings(**data["cloud"]))


@api.get("/cloud", tags=["云端 GPU"], response_model=CloudInfo)
async def get_cloud(refresh: bool = False):
    """云端 GPU：账号（按账号视角：剩余、本月已用（估计）、能开的卡、是否停用）、卡（按 GPU 视角：速度、每小时单元、
    能开的账号数、按余额还能用几小时）、合计、每月用量（本机账本）、设置。refresh=true 时先查一遍每个账号的余额
    （打开设置页时用；每个账号约 1–2 秒），否则用缓存（超过 30 分钟的自动刷新）。"""
    activities = registry.cloud_activities()
    return await asyncio.to_thread(_cloud_info, refresh, activities)


@api.put("/cloud/settings", tags=["云端 GPU"], response_model=CloudSettings)
async def put_cloud_settings(body: CloudSettingsPatch):
    """开哪张卡（gpu_pref）、最长用多久（max_minutes，null = 自动）、做完先别关（keep_minutes）。只改给了的字段。
    gpu_pref 用在推荐（训练、云端更快时开这张卡）；max_minutes 是开卡时 VM 的时限，预计超过它的卡不可用；
    keep_minutes 是任务做完后 VM 留多久等同型号的下一个任务接手。"""
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None or k == "max_minutes"}
    return CloudSettings(**registry.set_cloud(**fields))


@api.post("/cloud/accounts/login", tags=["云端 GPU"], response_model=CloudLoginStart)
async def cloud_login(body: Optional[CloudLoginRequest] = None):
    """添加云端 GPU 账号（或重新登录已有账号）：后台起 colab CLI 的登录，返回授权链接。Studio 在浏览器里打开它，
    用户同意后 Google 的页面显示授权码，交给 POST /cloud/accounts/login/{login_id}；之后轮询
    GET /cloud/accounts/login/{login_id} 直到 done / failed。15 分钟没完成自动结束。
    没装云端 GPU 组件时先装（第一次约 1 分钟）。"""
    from ttsapi.compute.colab.cli import CliError
    pool = await ensure_pool()
    try:
        login = await asyncio.to_thread(pool.begin_login, body.label if body else None)
    except CliError as e:
        raise HTTPException(500, str(e)) from None
    return CloudLoginStart(login_id=login.id, auth_url=login.auth_url, needs_code=True)


def _login_status(login) -> CloudLoginStatus:
    account = (
        cloud_account(registry.pool.account(login.account, refresh=False))
        if login.state == "done" and not login.cleaned
        else None
    )
    return CloudLoginStatus(state=login.state, account=account, added=login.added, error=login.error)


@api.get("/cloud/accounts/login/{login_id}", tags=["云端 GPU"], response_model=CloudLoginStatus)
async def cloud_login_status(login_id: str):
    """登录进行到哪了：waiting（等授权码、正在核对）/ done（account 是加进池的账号，added 表示是否由这次登录新加）/ failed（error）。"""
    if registry.pool is None:
        raise KeyError(f"登录 {login_id}")
    return await asyncio.to_thread(_login_status, registry.pool.login_status(login_id))


@api.post("/cloud/accounts/login/{login_id}", tags=["云端 GPU"], response_model=CloudLoginStatus)
async def cloud_login_code(login_id: str, body: CloudLoginCode):
    """交授权码（needs_code=true 时）。之后照常轮询 GET。已经交过或登录已结束时 409。"""
    if registry.pool is None:
        raise KeyError(f"登录 {login_id}")
    try:
        registry.pool.submit_code(login_id, body.code)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return _login_status(registry.pool.login_status(login_id))


@api.delete("/cloud/accounts/login/{login_id}", tags=["云端 GPU"], status_code=204)
async def cloud_login_cancel(login_id: str):
    """取消这次登录（204）：还没把账号加进池子时终止登录进程；已经加进池子且 added 为真时，按 DELETE /cloud/accounts/{id}
    的方式等试卡在账本里确认释放后删掉这个账号（最多等约 3 分钟，超时 409；删授权失败 502）；
    added 为假、已失败、已取消或查不到时什么都不删。"""
    if registry.pool is None:
        return
    from ttsapi.compute.colab.cli import CliError
    try:
        await asyncio.to_thread(registry.pool.cancel_login, login_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    except CliError as e:
        raise HTTPException(502, f"删除授权失败，账号已停用、没有移除，可以再试一次：{e}") from None


@api.post("/cloud/accounts/{account_id}/refresh", tags=["云端 GPU"], response_model=CloudAccount)
async def cloud_refresh(account_id: str):
    """马上查一次这个账号的余额和能开的卡。"""
    if registry.pool is None:
        raise KeyError(f"账号 {account_id}")
    return cloud_account(await asyncio.to_thread(registry.pool.account, account_id))


def _pool_call(fn, account_id):
    if registry.pool is None:
        raise KeyError(f"账号 {account_id}")
    registry.pool.account(account_id, refresh=False)  # 没有这个账号（或已移除）时 404
    fn(registry.pool, account_id)
    return registry.pool


@api.post("/cloud/accounts/{account_id}/disable", tags=["云端 GPU"], response_model=CloudAccount)
async def cloud_disable(account_id: str):
    """停用账号：不再参与调度，它做完先别关的 VM 马上释放；授权和账本保留，POST .../enable 恢复。"""
    pool = await asyncio.to_thread(_pool_call, lambda p, a: p.disable(a), account_id)
    return cloud_account(await asyncio.to_thread(pool.account, account_id, refresh=False))


@api.post("/cloud/accounts/{account_id}/enable", tags=["云端 GPU"], response_model=CloudAccount)
async def cloud_enable(account_id: str):
    """重新启用停用的账号。"""
    pool = await asyncio.to_thread(_pool_call, lambda p, a: p.enable(a), account_id)
    return cloud_account(await asyncio.to_thread(pool.account, account_id, refresh=False))


@api.delete("/cloud/accounts/{account_id}", tags=["云端 GPU"], status_code=204)
async def cloud_remove(account_id: str):
    """移除账号：删掉这个账号的配置目录和 core 为它存的授权（钥匙串条目），不再出现在列表里；账本保留
    （每月用量图里照算，名字见 GET /cloud 的 account_labels），重新登录这个账号就回来。
    default（本机 colab CLI 原有的登录）不删授权，只停用。它的 VM 还在跑任务时 409；删授权失败时 502（账号停用、没移除，可重试）。
    刚登录加进来、还在后台试开各种卡的账号（「添加云端 GPU」里「换一个」「取消」）：不再试下一张，等这次试卡在账本里确认释放（含回收器重试）后再删，
    整体最多等约 3 分钟。"""
    from ttsapi.compute.colab.cli import CliError
    try:
        await asyncio.to_thread(_pool_call, lambda p, a: p.remove(a), account_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    except CliError as e:  # 删凭据失败（钥匙串拒绝等）：账号只是停用了，没标移除，可以再删一次
        raise HTTPException(502, f"删除授权失败，账号已停用、没有移除，可以再试一次：{e}") from None


@api.put("/compute/auto", tags=["算力"], response_model=AutoSettings)
async def set_auto(body: AutoPatch):
    """自动选择：在哪跑（auto 按推荐 / fixed 固定一台）、先问我的提醒线（一次超过 ask_over 单元、本月超过 ask_month
    单元，0 = 每次都问）。只改给了的字段。"""
    if body.mode == "fixed" and body.device is None:
        raise HTTPException(400, "固定一台要给 device")
    fields = body.model_dump(exclude_none=True)
    if body.mode == "auto":
        fields["device"] = None
    return AutoSettings(**registry.set_auto(**fields))


@api.post("/plan", tags=["算力"], response_model=Plan)
async def plan(body: PlanRequest):
    """「在哪跑」：给任务类型、引擎、句数（训练给训练句数），返回推荐的设备、理由代码，以及每台设备的预计耗时、
    单元、能不能用和原因。混合引擎（对比、盲听）给 engines 或 parts，按各引擎分别估时再累加。
    chosen 是 compute（没给时同推荐），confirm 不为空时提交前要先问（带 approved=true、
    approved_device=chosen、approved_units=那个候选的 units 提交）。"""
    if body.kind == "train":
        task = training.train_task(body, body.lines)
    else:
        caps = gen_caps(body.aux_ref_ids, body.fragment_interval) if body.kind == "gen" else ()
        task = Task(body.kind, body.task_parts(), required_caps=caps, cloud_capable=body.cloud_capable)
        if body.kind in ("gen", "eval") and task.cloud_capable and library.selected() is not None and any(
            (body.character, body.version, body.gpt, body.sovits, body.ref_id, body.aux_ref_ids)
        ):
            from ttsapi.compute import colab_jobs
            try:
                task.upload_mb = await asyncio.to_thread(colab_jobs.plan_gen_upload_mb, body, catalog, refs)
            except VersionUnavailable as e:
                task.cloud_error = str(e)
            except (OSError, datasets.DatasetUnavailable, KeyError, ValueError) as e:
                task.cloud_error = f"要上传到云端的输入本机缺：{e}"
    return await dispatch.plan(task, body.compute)


@api.post("/bench", dependencies=OWNED, tags=["算力"], response_model=Job)
async def start_bench(body: BenchRequest):
    """任务（kind=bench）：在本机或一台局域网设备上测速（完整约 8–15 分钟，quick 约 2 分钟，engines 只测几个引擎）。
    固定 6 句 hard 日语、固定 seed、底模 + 资料库中首个可用的角色默认参考；CPU 试几档线程，MPS / CUDA 也试，挑最快的。
    结果写进资料库 compute/bench/<机器>/<时间>.json 和这台设备的实测速度，最佳设备和线程数写回那台机器、之后推理按它跑。
    结果在 job.bench_result。"""
    dev = None if body.device == "local" else registry.lan_device(body.device)
    ref = bench_ref(refs)

    async def fn(run):
        if dev is None:
            result = await run_bench(ref, body.engines, body.quick, lambda stage, f: run.stage(stage, f))
        else:
            run.stage(f"在 {dev['name']} 上测速", 0.0)
            result = await Remote(dev).bench(ref, body.engines, body.quick)
        library.current().put(f"compute/bench/{result['machine']}/{datetime.now():%Y-%m-%d-%H%M%S}.json", result)
        speeds.set_bench(body.device, result)
        run.job.bench_result = result
    return jobs.start("bench", body.device, fn)


@api.get("/bench", tags=["算力"], response_model=BenchList)
async def list_bench(machine: Optional[str] = Query(None, description="只看这台机器的（机器名，GET /compute 的 machine）")):
    """资料库里的测速结果，新的在前。"""
    items = [dict(doc, path=rel) for rel, doc in library.current().items("bench").items()
             if machine is None or rel.split("/")[2] == machine]
    return BenchList(results=sorted(items, key=lambda d: d["at"], reverse=True))


# ---------------------------------------------------------------- 任务

@api.get("/jobs", tags=["任务"], response_model=JobList)
async def list_jobs(kind: Optional[JobKind] = Query(None, description="比如 kind=train 只看训练任务"),
                    status: Optional[JobStatus] = None, target: Optional[str] = Query(None, description="角色 slug 或作品 id"),
                    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                    sort: Literal["created", "started", "finished", "kind", "status", "progress"] = "created",
                    order: Order = "desc"):
    """任务列表，默认新的在前。total 是过滤后的总数，用 offset 翻页。"""
    total, items = jobs.list_jobs(kind, status, target, limit, offset, sort, order)
    return JobList(jobs=items, total=total)


@api.get("/jobs/{job_id}", tags=["任务"], response_model=Job)
async def get_job(job_id: str):
    """状态、进度（0–1）、阶段、日志尾部、结果。建议每 1–2 秒轮询一次，或者用 /events。"""
    return jobs.get(job_id)


@api.post("/jobs/{job_id}/cancel", tags=["任务"], response_model=Job)
async def cancel_job(job_id: str):
    return await jobs.cancel(job_id)


@api.get("/jobs/{job_id}/events", tags=["任务"], response_class=StreamingResponse,
         responses={200: {"content": {"text/event-stream": {}}}})
async def job_events(job_id: str):
    """SSE：每次状态变化推一条 `event: job`，data 是 Job JSON；15 秒无变化推 `: keepalive`；任务结束后关闭连接。"""
    jobs.get(job_id)
    return StreamingResponse(jobs.events(job_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.include_router(api)


class WebUI(StaticFiles):
    """Web 界面（单页应用）：/ui/ 下找不到的路径里不带扩展名的是前端的路由，回 index.html；找不到的资源文件照样 404。"""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code != 404 or "." in path.rsplit("/", 1)[-1]:
                raise
            return await super().get_response("index.html", scope)


def mount_web(directory: Path) -> bool:
    """directory（web/ 构建出来的 dist）在就挂到 /ui/，不在不挂（GET /system/info 的 web_available 跟着）。"""
    if not directory.is_dir():
        return False
    app.mount("/ui", WebUI(directory=directory, html=True), name="ui")
    return True


mount_web(settings.WEB_DIR)


def listen(port: int):
    """绑好 HOST:port 的监听 socket（给 uvicorn 用）。SO_REUSEADDR 同 asyncio 自己绑时：只在 POSIX 上设（Windows 上它让第二个
    进程也能绑上同一个端口）。"""
    sock = socket.socket(socket.AF_INET6 if ":" in settings.HOST else socket.AF_INET)
    if os.name == "posix":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((settings.HOST, port))
    return sock


# SSE 连接不会自己结束，退出时最多等 3 秒
if settings.ROLE == "worker":
    from ttsapi.compute.worker_api import app as worker_app
server = uvicorn.Server(uvicorn.Config(worker_app if settings.ROLE == "worker" else app, host=settings.HOST,
                                       port=settings.PORT, log_level="warning", timeout_graceful_shutdown=3))
restarting = False


if __name__ == "__main__":
    for d in (settings.AUDIO_DIR, settings.LOG_DIR, settings.RUNTIME, settings.JOBS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 每次调 worker 一行，太吵
    if IS_WINDOWS:  # 「关闭即杀」Job Object；Linux 上 worker 自己轮询 gateway 是否还在（osutil.exit_with_parent）
        from ttsapi.jobobject import kill_children_on_exit
        kill_children_on_exit()
    (settings.RUNTIME / "gateway.pid").write_text(str(os.getpid()))
    sockets = [listen(settings.PORT)]
    # 主控另外在 80 端口（INSTALL_PORT）只提供 /install，局域网设备的安装命令不用带端口；被占、没权限时不开，命令带 core 端口
    if settings.ROLE == "control" and settings.INSTALL_PORT:
        try:
            sockets.append(listen(settings.INSTALL_PORT))
            install_port = settings.INSTALL_PORT
        except OSError as e:
            log.info("install port %s not available (%s): install commands carry port %s", settings.INSTALL_PORT, e,
                     settings.PORT)
    server.run(sockets=sockets)
    raise SystemExit(version.RESTART_EXIT_CODE if restarting else 0)
