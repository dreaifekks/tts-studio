"""worker 角色（python -m ttsapi.gateway --role worker）的接口：只执行主控派来的合成、打分、测速，不碰资料库。
除 POST /pair 外每个请求都要带 Authorization: Bearer <令牌>。令牌：TTSAPI_WORKER_TOKEN，不设时配对时生成，存在
DEVICE_FILE（0600）。还没配对时启动在终端打印连接地址和配对码（ttsapi.pair），主控用 POST /pair 拿配对码换令牌；
安装脚本已经记下了一个配对码（自动连回主控没连上）时不另生成。自动连回配对的每 30 秒向主控报一次心跳（heartbeat）。

文件按 sha256 缓存在 BLOB_DIR：主控 PUT /worker/blobs/{sha} 传过来（核对 sha256），/worker/call 里按 sha 引用；
缺的返回 409 {"code": "missing_blobs", "missing": [...]}。合成出的音频也按 sha 存一份，主控随后打分时不用再传。
用到时更新修改时间；7 天没用过的自动删掉，总量超过 BLOB_MAX_GB（默认 10GB）时从最久没用的开始删。
"""
import asyncio
import base64
import hmac
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional, Union

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ttsapi import library, pair, settings, storage, version, workers
from ttsapi.bench import run_bench
from ttsapi.compute import hw
from ttsapi.library.store import sha256_of

log = logging.getLogger("tts-api")
BLOB_KEEP_DAYS = 7
CALLS = {"/synth", "/score", "/mel"}
HEARTBEAT_SEC = 30
CAPS = ("aux_refs", "fragment_interval")


def check_token(request: Request):
    if request.url.path == "/pair":
        return
    auth = request.headers.get("authorization", "")
    if app.state.token is None or not hmac.compare_digest(auth.encode(), f"Bearer {app.state.token}".encode()):
        raise HTTPException(401, "令牌不对")


def blob(sha: str):
    return settings.BLOB_DIR / sha


# 请求要用的 blob（sha -> 保留到的时间）：执行期间、报了缺文件之后主控补传再重发期间，清理时不删，先删别的
HELD = {}
HOLD_SEC = 900  # 主控调 /worker/call 的超时


def hold(shas):
    until = time.time() + HOLD_SEC
    for sha in shas:
        HELD[sha] = max(HELD.get(sha, 0), until)


def cleanup_blobs():
    """删掉 7 天没用过的；剩下的总量超过上限时，从最久没用的开始删。正在保留的（HELD）不删。"""
    now = time.time()
    for sha in [s for s, t in HELD.items() if t <= now]:
        del HELD[sha]
    cutoff = now - BLOB_KEEP_DAYS * 86400
    files = sorted(((f.stat(), f) for f in settings.BLOB_DIR.iterdir() if not f.name.startswith(".")),
                   key=lambda x: x[0].st_mtime)
    total = sum(st.st_size for st, _ in files)
    for st, f in files:
        if st.st_mtime >= cutoff and total <= settings.BLOB_MAX_GB * 2**30:
            break
        if f.name in HELD:
            continue
        f.unlink()
        total -= st.st_size


async def housekeeping():
    while True:
        cleanup_blobs()
        for w in workers.ALL:
            if settings.IDLE_UNLOAD_MIN and w.running and not w.busy and \
                    time.time() - w.last_used > settings.IDLE_UNLOAD_MIN * 60:
                w.stop()
        await asyncio.sleep(600)


async def heartbeat():
    """自动连回主控配对的（DEVICE_FILE 里有 control、device_id）：每 HEARTBEAT_SEC 秒向主控报一次心跳，带上这台现在的地址
    （pair.address_for；主控据此判断在不在线，地址变了跟着改）。每次重读 DEVICE_FILE：改成手动配对（POST /pair 换了令牌）后就不报了。
    报不上只在状态变化时记日志（主控睡着时不刷屏）。"""
    ok = None
    while (dev := pair.device()).get("control"):
        try:
            address = await asyncio.to_thread(pair.address_for, dev["control"])  # 要解析主控的名字，不卡事件循环
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{dev['control']}/devices/{dev['device_id']}/heartbeat",
                                      json=dict(address=address), headers={"Authorization": f"Bearer {dev['token']}"})
            r.raise_for_status()
            err = None
        except (httpx.HTTPError, OSError) as e:  # OSError：断网时主控的名字解析不了
            err = f"{type(e).__name__}: {e}"
        if ok != (err is None):
            ok = err is None
            log.log(logging.INFO if ok else logging.WARNING, "heartbeat to %s: %s", dev["control"], err or "ok")
        await asyncio.sleep(HEARTBEAT_SEC)


@asynccontextmanager
async def lifespan(app):
    app.state.token = pair.token()
    if app.state.token is None:
        if pair.pending():
            print(f"等主控配对：连接地址 {pair.address()}（配对码是安装时显示的那个，10 分钟内有效）", flush=True)
        else:
            pair.announce()
    settings.BLOB_DIR.mkdir(parents=True, exist_ok=True)
    tasks = [asyncio.create_task(housekeeping()), asyncio.create_task(heartbeat())]
    yield
    for t in tasks:
        t.cancel()
    for w in workers.ALL:
        w.stop()


# 不开 /openapi.json、/docs、/redoc：worker 上每个请求都要带令牌，FastAPI 自带的这三个不走 dependencies
app = FastAPI(title="tts-core worker", version=version.VERSION, lifespan=lifespan,
              dependencies=[Depends(check_token)], openapi_url=None, docs_url=None, redoc_url=None)


@app.middleware("http")
async def access_log(request: Request, call_next):
    t = time.perf_counter()
    response = await call_next(request)
    log.info("%s %s %s %s %.2fs", request.client.host, request.method, request.url.path, response.status_code,
             time.perf_counter() - t)
    return response


class PairBody(BaseModel):
    code: str = Field(..., description="worker 终端里显示的配对码，XXX-XXX（大小写、横线、空格都行）")


@app.post("/pair")
async def pair_device(body: PairBody):
    """主控用配对码换这台 worker 的长期令牌（不用带令牌）。配对码不对、过期、输错太多次作废时 403。
    配对成功后之前配对的主控（旧令牌）连不上了。回复里一并带上主控登记要的（本机 id、机器名、硬件、引擎），主控拿到就存，
    不用再连一次 /worker/health。这些先备好再换令牌：换了令牌之后不再做可能慢、可能出错的事。"""
    me = dict(machine=settings.MACHINE_NAME, machine_id=library.machine()["id"], hw=hw.hardware(),
              engines=settings.installed_engines(), caps=list(CAPS))
    tok = pair.redeem(body.code)
    if tok is None:
        raise HTTPException(403, "配对码不对或已过期：在这台机器上运行 python -m ttsapi.pair 重新生成")
    app.state.token = tok
    return dict(me, token=tok)


@app.get("/worker/health")
async def health():
    """硬件、装好的引擎、各 worker 进程的状态、是否在忙、是否在训练（这台机器自己的训练）。machine_id 是这台 core 的本机 id
    （主控按它认出同一台设备，重新配对时不另加）。"""
    return dict(role="worker", machine=settings.MACHINE_NAME, machine_id=library.machine()["id"],
                version=version.VERSION, hw=hw.hardware(),
                engines=settings.installed_engines(), caps=list(CAPS),
                workers={w.name: w.status() for w in workers.ALL},
                loaded=[w.name for w in workers.ALL if w.running], busy=any(w.busy for w in workers.ALL),
                training=workers.training_job, **storage.device_disk_stats())


@app.put("/worker/blobs/{sha}")
async def put_blob(sha: str, request: Request):
    """传一个文件（请求体是原始字节），核对 sha256 后缓存。已有时直接返回。"""
    if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha):
        raise HTTPException(400, "sha 不对")
    dst = blob(sha)
    if dst.exists():
        return dict(sha=sha, cached=True)
    tmp = settings.BLOB_DIR / f".{sha}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    if (got := await asyncio.to_thread(sha256_of, tmp)) != sha:
        tmp.unlink()
        raise HTTPException(400, f"sha256 对不上：{got}")
    os.replace(tmp, dst)
    cleanup_blobs()
    return dict(sha=sha, cached=False)


class CallBody(BaseModel):
    worker: str = Field(..., description="引擎名或 scorer")
    path: Literal["/synth", "/score", "/mel"]
    payload: dict
    blobs: dict[str, Union[str, list[str]]] = Field({}, description="payload field -> blob sha256 or list of sha256")
    pretrained: dict[str, Literal["pretrained_gpt", "pretrained_sovits"]] = Field(
        {}, description="payload 字段 -> 用本机这个引擎的底模")
    outputs: dict[str, str] = Field({}, description="payload 里要写出文件的字段 -> 扩展名（如 .wav），文件随响应带回（base64）")


class BenchBody(BaseModel):
    ref: str = Field(..., description="参考音频的 sha256（先 PUT /worker/blobs）")
    text: str
    lang: str
    engines: Optional[list[str]] = None
    quick: bool = False


@app.post("/worker/bench")
async def bench(body: BenchBody):
    """在这台设备上测速，返回测速结果（ttsapi.bench.run_bench）；最佳设备和线程数写回这台设备的 tuning。"""
    if not blob(body.ref).exists():
        return JSONResponse(status_code=409, content=dict(detail="缺文件", code="missing_blobs", missing=[body.ref]))
    return await run_bench(dict(path=str(blob(body.ref)), text=body.text, lang=body.lang), body.engines, body.quick)


@app.post("/worker/call")
async def call(body: CallBody):
    if body.worker != "scorer" and body.worker not in settings.installed_engines():
        raise HTTPException(400, f"这台设备没装 {body.worker}")
    all_shas = [s for v in body.blobs.values() for s in (v if isinstance(v, list) else [v])]
    hold(all_shas)  # 缺文件时主控会补传再重发：这期间已有的不被补传挤掉
    missing = list(dict.fromkeys(s for s in all_shas if not blob(s).exists()))
    if missing:
        return JSONResponse(status_code=409, content=dict(detail="缺文件", code="missing_blobs", missing=missing))
    payload = dict(body.payload)
    for k, v in body.blobs.items():
        if isinstance(v, list):
            payload[k] = [str(blob(s)) for s in v]
            for s in v:
                os.utime(blob(s))
        else:
            payload[k] = str(blob(v))
            os.utime(blob(v))
    for k, key in body.pretrained.items():
        payload[k] = str(settings.ENGINES[body.worker][key])
    out_dir = settings.DATA / "worker-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    outs = {k: out_dir / f"{uuid.uuid4().hex}{suffix}" for k, suffix in body.outputs.items()}
    payload.update({k: str(p) for k, p in outs.items()})
    w = workers.SCORER if body.worker == "scorer" else workers.ENGINES[body.worker]
    result = await w.call(body.path, payload)
    data = {}
    for k, p in outs.items():
        data[k] = base64.b64encode(p.read_bytes()).decode()
        os.replace(p, blob(await asyncio.to_thread(sha256_of, p)))
    if outs:
        cleanup_blobs()
    return dict(result=result, outputs=data)
