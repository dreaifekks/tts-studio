"""异步任务：立即返回 job_id，进度、日志尾部、结果存数据库，客户端轮询 GET /jobs/{id} 或订阅 SSE。"""
import asyncio
import json
import logging
import re
import secrets
import time
from collections import deque
from datetime import datetime

from fastapi import HTTPException

from ttsapi import db, power, settings
from ttsapi.compute.colab.cli import AllocationUnavailable, CliError, LoginRequired
from ttsapi.library.store import LibraryError
from ttsapi.models import Job, JobItemDone

log = logging.getLogger("tts-api")
ACTIVE = ("queued", "running")
_SAFE_FOLLOW_PATTERNS = (
    re.compile(r"^\[\d{2}:\d{2}:\d{2}\] \S"),
    re.compile(r"^(?:SoVITS|GPT) epoch \d+/\d+$"),
    re.compile(
        r"^eval [^/\\\r\n]+ \+ [^/\\\r\n]+: "
        r"\{['\"]gpt['\"]: [^/\\\r\n]*['\"]score_median['\"]: -?\d+(?:\.\d+)?[^/\\\r\n]*\}$"
    ),
    re.compile(r"^---- 云端任务日志(?:：[^\s/\\]+)? ----$"),
)
_SAFE_FOLLOW_EXACT = frozenset({
    "已取消，训练进程已结束",
    "取消：等云端结果写完资料库再结束",
})


def _is_safe_followed_line(line: str) -> bool:
    return line in _SAFE_FOLLOW_EXACT or any(p.match(line) for p in _SAFE_FOLLOW_PATTERNS)


class UserError(RuntimeError):
    """Job failure carrying an explicit user-facing Chinese message safe for API/SSE/UI."""


def iso(ts: float):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


class JobRun:
    def __init__(self, job: Job, created: float, involves=()):
        self.job, self.created = job, created
        # 这个任务用到的角色：有训练、评测、盲听生成任务在用某个角色时，不能删这个角色
        self.involves = set(involves)
        self.sweep_keys = set()  # 扫参：要跑的组的完整参数键（colab_sweep.key），同样的组不能同时在两个扫参任务里跑
        self.version = 0
        self.tail = deque(maxlen=200)
        self.task = None
        self.cancel_requested = False
        # 训练进程已经独立运行：服务退出时任务保持 running，下次启动时接管（见 training.py）
        self.detached = False
        # Optional suffix attached by cloud runners on failure (e.g., VM shutdown and units spent).
        self.failure_note: str | None = None
        self.log_path = settings.JOBS_DIR / f"{job.id}.log"
        self._saved = 0.0

    def _bump(self, force_save=False):
        self.version += 1
        if force_save or time.time() - self._saved > 2:
            self.save()

    def save(self):
        self.job.log_tail = list(self.tail)[-40:]
        db.put_doc("jobs", self.job.id, self.created, self.job.model_dump())
        self._saved = time.time()

    def log(self, line: str):
        line = line.rstrip()
        if not line:
            return
        self.tail.append(line)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._bump()

    def stage(self, stage: str, progress: float = None):
        if self.job.status not in ACTIVE or self.cancel_requested:
            return
        self.job.stage = stage
        if progress is not None:
            self.job.progress = round(progress, 4)
        self.log(f"[{datetime.now():%H:%M:%S}] {stage}")
        self._bump(force_save=True)

    def item_done(self, item, done: int, total: int, line_id: str = None):
        """候选、对比、作品生成任务每完成一条候选调用一次：progress 按完成数算，事件里带上这一条。"""
        d = JobItemDone(audio_id=item.id, audio_url=item.audio_url, score=item.score, seed=item.seed,
                        line_id=line_id, done=done, total=total)
        self.job.latest = d
        self.job.completed.append(d)
        self.job.progress = round(done / total, 4)
        self.log(f"{done}/{total} {item.id} score={item.score}")

    def progress(self, progress: float):
        self.job.progress = round(progress, 4)
        self._bump()

    def follow(self, offset: int) -> int:
        """Read new lines appended to log_path into tail using an explicit safe-line allowlist."""
        with open(self.log_path, "rb") as f:
            f.seek(offset)
            data = f.read()
        end = data.rfind(b"\n") + 1
        added = False
        for line in data[:end].decode("utf-8", "replace").splitlines():
            text = line.rstrip()
            if _is_safe_followed_line(text):
                self.tail.append(text)
                added = True
        if added:
            self._bump()
        return offset + end


runs: dict[str, JobRun] = {}
# 一键更新期间（git 操作到进程退出）为 409 的说明，这期间不接新任务
closed: str = None


def check_open():
    if closed:
        raise HTTPException(409, closed)


def recover(resumable) -> list:
    """服务启动时调用：上次没跑完的任务里，resumable(job) 为真的（训练进程还在独立运行或已经跑完的训练）
    返回 [(job, created)] 交给调用方接管，其余标记为中断，结果保留。"""
    out = []
    for row in db.query("SELECT id, created, data FROM jobs"):
        job = Job(**json.loads(row["data"]))
        if job.status not in ACTIVE:
            continue
        if resumable(job):
            out.append((job, row["created"]))
        else:
            job.status, job.stage, job.finished = "interrupted", "服务重启，任务中断", iso(time.time())
            db.put_doc("jobs", job.id, row["created"], job.model_dump())
    return out


def start(kind: str, target: str, fn, involves=()) -> Job:
    """fn(run) 是协程函数，负责更新 run.stage/progress 并把结果写进 run.job 的对应字段。"""
    check_open()
    settings.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    job = Job(id=f"{kind}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}", kind=kind, target=target,
              status="queued", progress=0.0, stage="排队中", created=iso(now))
    run = runs[job.id] = JobRun(job, now, involves)
    run.save()
    run.task = asyncio.create_task(_run(run, fn))
    return job


def resume(job: Job, created: float, fn, involves=()) -> JobRun:
    """接管上次服务退出时还在跑的任务：沿用原来的 job（started 不变），fn 从当前状态继续。"""
    run = runs[job.id] = JobRun(job, created, involves)
    run.task = asyncio.create_task(_run(run, fn, resumed=True))
    return run


def _cloud_stage_prefix(stage: str) -> str:
    if "上传" in stage:
        return "上传到云端时"
    if "取回" in stage or "下载" in stage:
        return "从云端取回结果时"
    if "开云端" in stage:
        return "开云端 GPU 时"
    return "云端"


def _cli_error_message(exc: CliError, stage: str) -> str:
    if isinstance(exc, LoginRequired):
        return "云端账号需要重新登录"
    if isinstance(exc, AllocationUnavailable):
        return "云端 GPU 开不出来"
    text = str(exc).lower()
    prefix = _cloud_stage_prefix(stage)
    if "kernel execution timed out" in text:
        return f"{prefix}执行超时"
    if any(
        k in text
        for k in (
            "websocket handshake timed out",
            "http read timed out",
            "http connect timed out",
            "timed out",
        )
    ):
        return f"{prefix}连接超时" if prefix != "云端" else "连接云端超时"
    if any(
        k in text
        for k in (
            "kernel connection lost",
            "connectionerror",
            "connectionreseterror",
            "websocketconnectionclosedexception",
            "websockettimeoutexception",
            "sslerror",
        )
    ):
        return f"{prefix}连接中断"
    if any(k in text for k in ("kernel dead", "kernel restarting", "did not confirm execution")):
        return "云端运行环境异常，请重试"
    if "session not found" in text or "session lost" in text:
        return "云端会话已失效"
    if "remote file not found" in text:
        return "云端文件不存在"
    if "sha256 mismatch" in text:
        return "云端文件校验失败，请重试"
    if "http 5" in text:
        return "云端服务暂时不可用，请稍后重试"
    if "http 4" in text:
        return "云端请求被拒绝，请检查账号状态"
    return f"{prefix}出错"


def format_error(exc: Exception, run: JobRun) -> str:
    """Convert a job exception into a safe user-facing Chinese message; never echoes raw text."""
    user_message = getattr(exc, "user_message", None)
    if isinstance(exc, UserError):
        msg = str(exc)
    elif isinstance(user_message, str) and user_message:
        msg = user_message
    elif isinstance(exc, HTTPException):
        if exc.status_code == 503:
            msg = "设备暂时不可用，请稍后重试"
        elif exc.status_code >= 500:
            msg = "设备运行出错，详细信息在主控日志里"
        else:
            msg = "请求未能完成，请重试"
    elif isinstance(exc, CliError):
        msg = _cli_error_message(exc, run.job.stage)
    elif isinstance(exc, TimeoutError):
        msg = "云端使用时间已到上限" if "time limit" in str(exc).lower() else "操作超时，请重试"
    elif isinstance(exc, PermissionError):
        msg = "需要确认使用高费率云端 GPU" if "approval" in str(exc).lower() else "权限不足，无法继续"
    elif isinstance(exc, LibraryError):
        msg = (
            "训练资料尚未同步完成或已变更，同步后再试"
            if type(exc).__name__ == "DatasetUnavailable"
            else "资料库读取失败，详细信息在主控日志里"
        )
    else:
        lower = str(exc).lower()
        name = type(exc).__name__
        if "out of memory" in lower:
            msg = "显存不足，请调小参数或换显存更大的卡"
        elif "colab result archive failed" in lower:
            msg = "云端结果打包失败"
        elif "upload sha256 mismatch" in lower:
            msg = "上传文件校验失败，请重试"
        elif "TimeoutException" in name or name in ("ReadTimeout", "ConnectTimeout"):
            msg = "连接超时，请重试"
        elif name == "ConnectError":
            msg = "连接失败，请检查设备是否在线"
        elif isinstance(exc, RuntimeError) and run.job.kind == "train":
            msg = "训练脚本出错，详细信息在主控日志里"
        elif isinstance(exc, RuntimeError) and run.job.kind == "eval":
            msg = "评测脚本出错，详细信息在主控日志里"
        else:
            msg = "出错了，详细信息在主控日志里"

    note = getattr(run, "failure_note", None)
    if note:
        if msg.endswith("）"):
            msg = f"{msg}；{note}"
        else:
            msg = f"{msg}（{note}）"
    return msg


async def _run(run: JobRun, fn, resumed=False):
    job = run.job
    power.hold()  # 任务期间阻止系统睡眠
    # 外层 finally：存盘（开头或收尾的 SQLite 写入）失败也要放开防睡眠断言、把任务移出 runs
    try:
        job.status = "running"
        if not resumed:
            job.started = iso(time.time())
        run._bump(force_save=True)
        try:
            await fn(run)
            job.status, job.progress, job.stage = "succeeded", 1.0, "完成"
        except asyncio.CancelledError:
            # 用户取消 vs 服务正常退出（uvicorn 退出时会取消所有任务）。训练进程独立运行时，服务退出不影响它，
            # 任务保持 running，下次启动时接管
            if run.cancel_requested:
                job.status, job.stage = "cancelled", "已取消"
            elif run.detached:
                job.stage = "服务重启中，训练进程继续运行，服务起来后自动接管"
            else:
                job.status, job.stage = "interrupted", "服务重启，任务中断"
        except Exception as e:
            job.status = "failed"
            job.error = format_error(e, run)
            job.stage = "失败"
            run.log(job.error)
            log.exception("job %s failed: %s", job.id, job.error)
        finally:
            if job.status not in ACTIVE:
                job.finished = iso(time.time())
            run._bump(force_save=True)
    finally:
        power.release()
        runs.pop(job.id)  # 结果已存库，之后从数据库读；正在订阅 SSE 的连接手里还有 run 的引用


def busy(kinds=("train", "eval", "blind"), character: str = None) -> list:
    """在跑或排队的训练、评测、盲听生成任务（它们按文件名用权重）；给了 character 时只算用到这个角色的。"""
    return [r.job.id for r in runs.values()
            if r.job.kind in kinds and (character is None or character in r.involves)]


def get(job_id: str) -> Job:
    if job_id in runs:
        return runs[job_id].job
    return Job(**db.get_doc("jobs", job_id))


def list_jobs(kind: str = None, status: str = None, target: str = None, limit: int = 50, offset: int = 0,
              sort: str = "created", order: str = "desc"):
    """返回 (过滤后的总数, 这一页)。过滤和排序在库里做，正在跑的任务再用内存里的最新状态。"""
    conds, args = [], []
    for field, val in (("kind", kind), ("status", status), ("target", target)):
        if val is not None:
            conds.append(f"json_extract(data, '$.{field}')=?")
            args.append(val)
    where = " WHERE " + " AND ".join(conds) if conds else ""
    total = db.one(f"SELECT COUNT(*) AS n FROM jobs{where}", *args)["n"]
    col = "created" if sort == "created" else f"json_extract(data, '$.{sort}')"
    rows = db.query(f"SELECT id FROM jobs{where} ORDER BY {col} IS NULL, {col} {order.upper()}, created DESC "
                    "LIMIT ? OFFSET ?", *args, limit, offset)
    return total, [get(r["id"]) for r in rows]


async def cancel(job_id: str) -> Job:
    job = get(job_id)
    if job.status not in ACTIVE:
        raise HTTPException(409, f"任务已结束（{job.status}），不能取消")
    run = runs[job_id]
    # 重复取消只等结果，不再向任务注入取消：训练任务处理取消时还要等训练进程写出 pid 再杀，不能被第二次取消打断
    if not run.cancel_requested:
        run.cancel_requested = True
        run.task.cancel()
    await asyncio.wait({run.task}, timeout=10)  # 等子进程被杀、状态变成 cancelled 再返回
    return job


async def events(job_id: str):
    """SSE：状态有变化就推一条 `event: job`，15 秒没变化推一行注释保活，任务结束后推最后一条并关闭。"""
    run = runs.get(job_id)
    if run is None:
        yield f"event: job\ndata: {get(job_id).model_dump_json()}\n\n"
        return
    last, last_sent = -1, time.time()
    while True:
        if run.version != last:
            last, last_sent = run.version, time.time()
            yield f"event: job\ndata: {run.job.model_dump_json()}\n\n"
        if run.job.status not in ACTIVE:
            return
        if time.time() - last_sent > 15:
            last_sent = time.time()
            yield ": keepalive\n\n"
        await asyncio.sleep(0.2)
