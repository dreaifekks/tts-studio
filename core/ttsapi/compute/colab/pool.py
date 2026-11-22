"""Colab account selection and the durable VM lifecycle journal."""

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .cli import (
    AllocationRefused,
    AllocationUnavailable,
    CliError,
    ColabCLI,
    LoginRequired,
)
from .run import DEFAULT_RETRY_MINUTES, ColabRun

RATES = {"T4": 1.15, "L4": 1.94, "A100": 6.73, "G4": 10.05}
PREMIUM = {"A100", "G4", "H100"}


@dataclass(frozen=True)
class Account:
    id: str
    label: str
    balance: float | None
    month_used: float | None
    gpus: tuple[str, ...]
    recorded_month_used: float
    error: str | None = None
    email: str | None = None
    disabled: bool = False


@dataclass(frozen=True)
class GpuType:
    gpu: str
    account_count: int
    hours: float | None
    reason: str | None

    @property
    def label(self):
        return "RTX PRO 6000" if self.gpu == "G4" else self.gpu


class WaitingForGPU(RuntimeError):
    retry_after = 600

    def __init__(self, gpu):
        self.gpu = gpu
        super().__init__(f"{gpu} 暂时开不出来")


LOGIN_TIMEOUT = 900  # 授权链接发出后这么久还没完成就结束登录子进程
PROBE_WAIT = 180  # 移除刚登录的账号时等它正在试的那张卡释放（开卡最多 120 秒，加释放）


@dataclass
class Login:
    """一次后台登录（POST /cloud/accounts/login）：CLI 子进程打印授权链接，用户在浏览器同意后把页面上的授权码交回来。"""

    id: str
    label: str | None
    proc: subprocess.Popen
    slot: str
    auth_url: str | None = None
    state: str = "waiting"  # waiting / done / failed
    account: str | None = None
    added: bool = False
    error: str | None = None
    code_sent: bool = False
    cancelled: bool = False
    committing: bool = False
    cleaned: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    done_commit: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None  # 核对、登记、试卡的后台线程


def limits(estimated_minutes, rate):
    return min(1440, max(30, math.ceil(2 * estimated_minutes))), max(
        0.25, estimated_minutes * rate / 60
    )


class ColabPool:
    def __init__(self, root=None, *, python=None, cli=None, clock=time.time):
        self.root = Path(root or Path.home() / ".config/tts-core").resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cli = cli or ColabCLI(
            self.root, python or self.root / "colab-venv/bin/python"
        )
        self.clock = clock
        self.closing = threading.Event()  # lifespan 收尾中：不再试卡（登录后的 _probe）
        self._probe_changed = threading.Condition()
        self.probing = set()  # accounts currently in post-login probe; remove waits until probe exits and ledger rows stop
        self.logins = {}
        self._default_absent_until = 0
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts(
                    id TEXT PRIMARY KEY, label TEXT, enabled INTEGER DEFAULT 1,
                    balance REAL, gpus TEXT, refreshed REAL);
                INSERT OR IGNORE INTO accounts(id,label) VALUES ('default','默认账号');
                CREATE TABLE IF NOT EXISTS usage(
                    name TEXT PRIMARY KEY, account TEXT, gpu TEXT, task_id TEXT,
                    started REAL, deadline REAL, pid INTEGER, pid_start REAL,
                    estimated_units REAL, before_balance REAL, endpoint TEXT,
                    closing INTEGER DEFAULT 0, stopped REAL, actual_units REAL);
                DROP INDEX IF EXISTS one_vm_per_account;
                CREATE TABLE IF NOT EXISTS identities(account TEXT PRIMARY KEY, digest TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS meter(account TEXT PRIMARY KEY, at REAL, balance REAL);
                CREATE TABLE IF NOT EXISTS cooldown(
                    account TEXT, gpu TEXT, until REAL, PRIMARY KEY(account,gpu));
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(usage)")}
            if "retry_ping" not in columns:
                db.execute("ALTER TABLE usage ADD COLUMN retry_ping REAL")
            if "retry_deadline" not in columns:
                db.execute("ALTER TABLE usage ADD COLUMN retry_deadline REAL")
                db.execute(
                    """UPDATE usage SET retry_deadline=min(deadline, ?)
                           WHERE retry_ping IS NOT NULL AND stopped IS NULL""",
                    (self.clock() + DEFAULT_RETRY_MINUTES * 60,),
                )
            if "idle" not in columns:  # 做完先别关（keep_minutes）：留着等下一个任务接手
                db.execute("ALTER TABLE usage ADD COLUMN idle INTEGER DEFAULT 0")
                db.execute("ALTER TABLE usage ADD COLUMN adopted REAL")  # 接手时间：这一行里后来那个任务从这里算
            account_columns = {r["name"] for r in db.execute("PRAGMA table_info(accounts)")}
            if "email" not in account_columns:
                db.execute("ALTER TABLE accounts ADD COLUMN email TEXT")
            if "removed" not in account_columns:  # 移除了：不再列出；账本行保留，重新登录就回来
                db.execute("ALTER TABLE accounts ADD COLUMN removed INTEGER DEFAULT 0")
            if "recover_after" not in columns:
                db.execute("ALTER TABLE usage ADD COLUMN recover_after REAL")
                db.execute("ALTER TABLE usage ADD COLUMN allocated REAL DEFAULT 0")
                db.execute(
                    "ALTER TABLE usage ADD COLUMN late_checked INTEGER DEFAULT 0"
                )
                db.execute(
                    "UPDATE usage SET allocated=actual_units, late_checked=1 WHERE actual_units IS NOT NULL"
                )
                # The previous schema allowed one open or unsettled row per account.
                db.execute("""INSERT OR IGNORE INTO meter SELECT account,started,before_balance
                              FROM usage WHERE actual_units IS NULL""")

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.root / "usage.sqlite", timeout=15)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn

    @contextmanager
    def lock(self, account, *, wait=True):
        import fcntl

        with (self.root / (account + ".lock")).open("a") as fd:
            try:
                fcntl.flock(
                    fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except BlockingIOError:
                yield None
                return
            yield fd.fileno()

    @contextmanager
    def _job_lock(self, name):
        with self.lock(name) as fd:
            try:
                yield fd
            finally:
                # UUIDs are never reused. A waiter for a terminal UUID can only
                # observe a settled or deleted row and cannot release a new VM.
                with self.db() as db:
                    pending = db.execute(
                        "SELECT 1 FROM usage WHERE name=? AND actual_units IS NULL",
                        (name,),
                    ).fetchone()
                if not pending:
                    (self.root / (name + ".lock")).unlink(missing_ok=True)

    def _default_identity(self):
        with self.db() as db:
            row = db.execute(
                "SELECT digest FROM identities WHERE account='default'"
            ).fetchone()
            if row:
                return row[0]
        if self.clock() < self._default_absent_until:
            return None
        try:
            digest = self.cli.api("default", "identity")
        except LoginRequired:
            # 本机 colab CLI 没登录：10 分钟内不再起子进程查（GET /compute 一次会调好几次 accounts）
            self._default_absent_until = self.clock() + 600
            return None
        with self.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO identities VALUES ('default',?)", (digest,)
            )
            db.execute("UPDATE accounts SET enabled=0 WHERE id=?", (digest,))
        return digest

    def _check_new(self, account):
        default = self._default_identity()
        with self.db() as db:
            if (
                account == default
                or db.execute(
                    "SELECT 1 FROM accounts WHERE id=? AND enabled", (account,)
                ).fetchone()
            ):
                raise ValueError("这个账号已经在池里")

    def register(self, email, label=None):
        account = hashlib.sha256(email.strip().casefold().encode()).hexdigest()
        with self.lock("enrollment"):
            self._check_new(account)
            self._register(account, label or email)
        return account

    def _register(self, account, label, email=None):
        with self.db() as db:
            db.execute(
                """INSERT INTO accounts(id,label,email) VALUES (?,?,?)
                ON CONFLICT(id) DO UPDATE SET enabled=1,removed=0,label=excluded.label,
                    email=coalesce(excluded.email,email)""",
                (account, label, email),
            )
        return account

    def login(self, email, label=None, *, approved=False):
        """Interactive enrollment. Probe eligible priced cards after explicit login."""
        account = hashlib.sha256(email.strip().casefold().encode()).hexdigest()
        with self.lock("enrollment"):
            if account == self._default_identity():
                raise ValueError("这个账号已经在池里")
            with self.db() as db:
                existing = db.execute(
                    "SELECT label FROM accounts WHERE id=? AND enabled", (account,)
                ).fetchone()
            self.cli.authorize(account)
            self._register(account, label or (existing["label"] if existing else email))
            if existing:
                return account
        self._probe(account, approved)
        return account

    def _enabled(self, account):
        with self.db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM accounts WHERE id=? AND enabled AND NOT removed",
                    (account,),
                ).fetchone()
                is not None
            )

    def _probe(self, account, approved=False):
        """新账号：每种有报价的卡试申请一次就释放（高费率卡要显式批准）。账号停用后（移除的第一步）立即退出试卡。"""
        if self.closing.is_set() or not self._enabled(account):
            return
        for gpu in self._usage(account)["gpus"]:
            if self.closing.is_set() or not self._enabled(account):
                return
            if gpu not in RATES or (gpu in PREMIUM and not approved):
                continue
            try:
                run = self._allocate(
                    gpu, 30, "account-probe", approved, 1, only_account=account
                )
            except WaitingForGPU:
                continue
            run.stop()

    # ------------------------------------------------------------ 后台登录（Studio 用）

    def begin_login(self, label=None):
        """起登录子进程，等它打印授权链接后返回 Login。CLI 0.8.0 走「粘贴授权码」：用户在浏览器同意后，
        Google 的页面显示授权码，交给 submit_code。授权结果先留在子进程内存里，核对邮箱后才写进这个账号的凭据。"""
        login_id = uuid.uuid4().hex[:12]
        slot = f"login-{login_id}"
        login = Login(login_id, label, self.cli.login_process(slot), slot)
        timer = threading.Timer(LOGIN_TIMEOUT, login.proc.kill)
        timer.daemon = True
        timer.start()
        tail = []
        for line in login.proc.stderr:
            tail = (tail + [line.strip()])[-20:]
            if line.strip().startswith("https://"):
                login.auth_url = line.strip()
                break
        if login.auth_url is None:
            timer.cancel()
            login.proc.wait()
            self._cleanup_login(login)
            raise CliError(f"colab 登录没给出授权链接：{' / '.join(t for t in tail if t)[-300:]}")
        self.logins[login_id] = login
        login.thread = threading.Thread(
            target=self._finish_login, args=(login, tail, timer), daemon=True
        )
        login.thread.start()
        return login

    def submit_code(self, login_id, code):
        login = self.logins[login_id]
        if login.state != "waiting" or login.code_sent or login.cancelled:
            raise ValueError("这次登录已经交过授权码或已经结束")
        login.code_sent = True
        try:
            login.proc.stdin.write(code.strip() + "\n")
            login.proc.stdin.flush()
        except BrokenPipeError:
            raise ValueError("这次登录已经结束") from None

    def _finish_login(self, login, tail, timer):
        drain = threading.Thread(
            target=lambda: [tail.append(x.strip()) for x in login.proc.stderr],
            daemon=True,
        )
        drain.start()
        probe_account = None
        try:
            info = None
            for line in login.proc.stdout:  # CLI 自己往 stdout 打的别的行跳过，只认给主控的那一行
                if line.startswith("{") and '"account"' in line:
                    info = json.loads(line)
                    break
            if info is None:
                login.proc.wait()
                if login.cancelled:
                    raise ValueError("已取消登录")
                reason = next((t for t in reversed(tail) if t), "")
                raise CliError(
                    "授权超时，重新添加"
                    if login.proc.returncode == -9
                    else f"授权没成功（{reason[-200:]}）"
                )
            account, email = info["account"], info["email"]
            with self.lock("enrollment"):
                if login.cancelled:
                    raise ValueError("已取消登录")
                if account == self._default_identity():
                    # 本机 colab CLI 原有的登录：不另存凭据；停用过的话重新启用
                    with login.lock:
                        if login.cancelled:
                            raise ValueError("已取消登录")
                        login.committing = True
                    login.proc.stdin.write("abort\n")
                    login.proc.stdin.flush()
                    with self.db() as db:
                        revived = db.execute(
                            "UPDATE accounts SET enabled=1 WHERE id='default' AND NOT enabled"
                        ).rowcount
                    if not revived:
                        raise ValueError("这个账号已经在池里（默认账号）")
                    login.account, login.added, login.state = "default", False, "done"
                    return
                with self.db() as db:
                    existing = db.execute(
                        "SELECT label, removed FROM accounts WHERE id=?", (account,)
                    ).fetchone()
                with login.lock:
                    if login.cancelled:
                        raise ValueError("已取消登录")
                    login.committing = True
                login.proc.stdin.write("commit\n")
                login.proc.stdin.flush()
                if login.proc.wait():
                    raise CliError("保存授权失败")
                self._register(
                    account,
                    login.label or (existing["label"] if existing else email),
                    email,
                )
                new = existing is None or bool(existing["removed"])
                login.account, login.added = account, new
                with login.lock:
                    if new and not login.cancelled:
                        # Register before marking done so an immediate cancel/remove waits for the probe.
                        with self._probe_changed:
                            self.probing.add(account)
                        probe_account = account
                login.state = "done"
        except Exception as exc:
            login.state, login.error = "failed", str(exc)
        finally:
            timer.cancel()
            if login.proc.poll() is None:
                login.proc.kill()
            login.proc.wait()
            drain.join(5)
            for pipe in (login.proc.stdin, login.proc.stdout, login.proc.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
            self._cleanup_login(login)
            login.done_commit.set()
        if probe_account is not None:
            try:
                self._probe(probe_account)
            except Exception:
                logging.getLogger(__name__).exception("Colab account probe failed")
            finally:
                with self._probe_changed:
                    self.probing.discard(probe_account)
                    self._probe_changed.notify_all()

    def cancel_login(self, login_id):
        """Cancel a background login: kill the process before commit, or remove the account if this login added it."""
        login = self.logins.get(login_id)
        if login is None:
            return
        with login.lock:
            login.cancelled = True
            if not login.committing and login.proc.poll() is None:
                login.proc.kill()
        login.done_commit.wait()
        if login.added and login.account and not login.cleaned:
            with self.db() as db:
                row = db.execute(
                    "SELECT removed FROM accounts WHERE id=?", (login.account,)
                ).fetchone()
            if row and not row["removed"]:
                self.remove(login.account)
            login.cleaned = True

    def _cleanup_login(self, login):
        shutil.rmtree(self.root / "colab" / login.slot, ignore_errors=True)

    def login_status(self, login_id):
        return self.logins[login_id]

    def disable(self, account):
        """停用：不再参与调度；它做完先别关的 VM 马上释放（不会再被接手）。"""
        with self.db() as db:
            db.execute("UPDATE accounts SET enabled=0 WHERE id=?", (account,))
            idle = db.execute(
                "SELECT name FROM usage WHERE account=? AND idle=1 AND stopped IS NULL AND actual_units IS NULL",
                (account,),
            ).fetchall()
        for row in idle:
            self.stop(account, row["name"])

    def enable(self, account):
        """重新启用停用的账号（移除了的要重新登录）。没有这个账号或已移除时 KeyError。"""
        with self.db() as db:
            if not db.execute(
                "UPDATE accounts SET enabled=1 WHERE id=? AND NOT removed", (account,)
            ).rowcount:
                raise KeyError(f"账号 {account}")

    def _probe_pending(self, account):
        if account in self.probing:
            return True
        with self.db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM usage WHERE account=? AND task_id='account-probe' AND stopped IS NULL",
                    (account,),
                ).fetchone()
                is not None
            )

    def remove(self, account):
        """移除：停用，删掉 core 给这个账号写的那条凭据和它的配置目录（colab/<id>）；账本行保留（每月用量照算）。
        default（本机 colab CLI 原有的登录）不删凭据，只停用。它的 VM 还在跑任务时 ValueError。
        删凭据和开卡登记（_allocate）在同一把账号锁里：停用之后、锁里再查一遍，开卡那边登记前也复核账号还启用，
        不会删掉刚开出的 VM 要用的凭据。删凭据失败（CliError）时账号只是停用了，没标移除，可以再删一次。
        还没等到余额扣款的 VM（late_checked=0）按「运行时长 × 费率」记估值（已到账的更多时用到账的），之后不再补结算。
        Studio 登录刚加的账号还在后台试卡（登错了马上「换一个」「取消」）：试卡的 VM 不算在跑任务，停用后不再试下一张，
        等这次试卡在账本里的记录确认已释放（含回收器重试释放，整体最多 PROBE_WAIT 秒，超时 ValueError）再删。"""
        busy = "SELECT 1 FROM usage WHERE account=? AND stopped IS NULL"  # VM 还没释放（释放了没结算的下面记估值）
        with self.db() as db:
            if db.execute(busy + " AND NOT idle AND task_id IS NOT 'account-probe'", (account,)).fetchone():
                raise ValueError("这个账号的 VM 还在跑任务，等任务结束再移除")
        self.disable(account)  # 做完先别关的 VM 这里释放、结算
        deadline = time.monotonic() + PROBE_WAIT
        with self._probe_changed:
            while self._probe_pending(account):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("这个账号登录后试开的卡还没释放完，稍后再移除")
                self._probe_changed.wait(min(remaining, 1.0))
        if account == "default":
            return
        with self.lock(account):
            with self.db() as db:
                if db.execute(busy, (account,)).fetchone():  # 停用前刚开上的
                    raise ValueError("这个账号的 VM 还没释放完，等任务结束再移除")
            self.cli.call(account, "forget")
            shutil.rmtree(self.root / "colab" / account, ignore_errors=True)
            with self.db() as db:
                rows = db.execute(
                    """SELECT name, gpu, started, stopped, max(coalesce(actual_units, 0), coalesce(allocated, 0)) AS known
                    FROM usage WHERE account=? AND stopped IS NOT NULL AND late_checked=0""",
                    (account,),
                ).fetchall()
                db.executemany(
                    "UPDATE usage SET actual_units=?, late_checked=1 WHERE name=?",
                    [
                        (max(r["known"], RATES[r["gpu"]] * (r["stopped"] - r["started"]) / 3600), r["name"])
                        for r in rows
                    ],
                )
                db.execute(
                    "UPDATE accounts SET removed=1, balance=NULL, gpus=NULL WHERE id=?",
                    (account,),
                )

    def month_used(self):
        """本月已用合计（本机账本，含停用、移除了的账号），提醒线用。"""
        with self.db() as db:
            return db.execute(
                """SELECT COALESCE(SUM(actual_units),0) FROM usage
                WHERE strftime('%Y-%m',started,'unixepoch','localtime')=
                    strftime('%Y-%m',?,'unixepoch','localtime')""",
                (self.clock(),),
            ).fetchone()[0]

    def labels(self):
        """账本里出现过的所有账号（含移除了的）的显示名：{账号 id: 显示名}，每月用量图用。"""
        with self.db() as db:
            return {r["id"]: r["label"] for r in db.execute("SELECT id, label FROM accounts")}

    def account(self, account_id, *, refresh=True):
        """一个账号（停用的也算）；refresh 时先查一次余额和能开的卡。没有这个账号时 KeyError。"""
        if refresh:
            with self.db() as db:
                row = db.execute(
                    "SELECT enabled FROM accounts WHERE id=? AND NOT removed", (account_id,)
                ).fetchone()
            if row is None:
                raise KeyError(f"账号 {account_id}")
            try:
                self._usage(account_id)
            except CliError:
                pass  # 错误照常出现在 accounts() 的 error 里
        found = next(
            (a for a in self.accounts(refresh=False, include_disabled=True) if a.id == account_id),
            None,
        )
        if found is None:
            raise KeyError(f"账号 {account_id}")
        return found

    def usage_monthly(self):
        """[{month: "2026-11", by_account: {账号 id: 单元}}]，按本机账本（已结算的实际单元），月份从早到晚。"""
        with self.db() as db:
            rows = db.execute(
                """SELECT strftime('%Y-%m',started,'unixepoch','localtime') AS month, account,
                       SUM(actual_units) AS units FROM usage WHERE actual_units IS NOT NULL
                   GROUP BY month, account ORDER BY month"""
            ).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["month"], {})[r["account"]] = round(r["units"], 4)
        return [dict(month=m, by_account=b) for m, b in out.items()]

    def idle_vms(self):
        """做完先别关、还在等下一个任务的 VM：{卡型: 台数}。"""
        with self.db() as db:
            rows = db.execute(
                """SELECT gpu, COUNT(*) FROM usage WHERE idle=1 AND closing=0 AND stopped IS NULL
                   AND retry_deadline>? GROUP BY gpu""",
                (self.clock(),),
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def _usage(self, account):
        info = self.cli.api(account, "usage")
        with self.db() as db:
            db.execute(
                "UPDATE accounts SET balance=?,gpus=?,refreshed=? WHERE id=?",
                (info["balance"], json.dumps(info["gpus"]), self.clock(), account),
            )
        return info

    def accounts(self, *, refresh=True, include_disabled=False, only_account=None):
        """启用的账号（include_disabled 时连停用的，停用的不刷新；移除了的不列）。默认账号（本机 colab CLI 原有的登录）没登录时不列出。"""
        default_logged_in, default_error = True, None
        if only_account in (None, "default"):
            try:
                default_logged_in = self._default_identity() is not None
            except CliError as exc:
                default_error = str(exc)
        now = self.clock()
        with self.db() as db:
            sql = "SELECT * FROM accounts WHERE NOT removed"
            params = ()
            if not include_disabled:
                sql += " AND enabled"
            if only_account is not None:
                sql += " AND id=?"
                params = (only_account,)
            rows = db.execute(sql + " ORDER BY id", params).fetchall()
            accounts = []
            for row in rows:
                account = row["id"]
                error = None
                balance, gpus = row["balance"], json.loads(row["gpus"] or "[]")
                if account == "default" and not default_logged_in:
                    continue
                if account == "default" and default_error:
                    error, balance, gpus = default_error, None, []
                elif not row["enabled"]:
                    pass
                elif (
                    refresh
                    or row["refreshed"] is None
                    or now - row["refreshed"] >= 1800
                ):
                    try:
                        info = self._usage(account)
                        balance, gpus = info["balance"], info["gpus"]
                    except CliError as exc:
                        error, balance, gpus = str(exc), None, []
                unavailable = {
                    r[0]
                    for r in db.execute(
                        "SELECT gpu FROM cooldown WHERE account=? AND until>?",
                        (account, now),
                    )
                }
                total = db.execute(
                    """SELECT COALESCE(SUM(actual_units),0) FROM usage WHERE account=?
                    AND strftime('%Y-%m',started,'unixepoch','localtime')=
                        strftime('%Y-%m',?,'unixepoch','localtime')""",
                    (account, now),
                ).fetchone()[0]
                accounts.append(
                    Account(
                        account,
                        row["label"],
                        balance,
                        None,
                        tuple(g for g in gpus if g not in unavailable),
                        total,
                        error,
                        row["email"],
                        not row["enabled"],
                    )
                )
        return accounts

    def rates(self):
        return dict(RATES)

    def gpu_types(self):
        accounts = self.accounts(refresh=False)
        result = []
        for gpu in [*RATES, "H100"]:
            eligible = [a for a in accounts if gpu in a.gpus]
            rate = RATES.get(gpu)
            reason = None if eligible else "没有可用账号"
            if rate is None:
                reason = "费率未测"
            hours = sum(a.balance for a in eligible) / rate if rate else None
            result.append(GpuType(gpu, len(eligible), hours, reason))
        return result

    def start(
        self, gpu, max_minutes=None, *, task_id, approved=False, estimated_minutes=None
    ):
        return self._allocate(gpu, max_minutes, task_id, approved, estimated_minutes)

    def run_script(
        self, gpu, script, args=(), *, task_id, estimated_minutes, approved=False
    ):
        """Run a local script with colab run, then release and settle the balance."""
        script = Path(script).resolve(strict=True)
        if any(arg == "--env" or arg.startswith("--env=") for arg in args):
            raise ValueError("colab run reserves --env; rename that script argument")
        return self._allocate(
            gpu, None, task_id, approved, estimated_minutes, (script, *args)
        )

    def _allocate(
        self,
        gpu,
        max_minutes,
        task_id,
        approved,
        estimated_minutes,
        script=None,
        only_account=None,
    ):
        import psutil

        if gpu == "RTX PRO 6000":
            gpu = "G4"
        if gpu in PREMIUM and not approved:
            raise PermissionError(f"{gpu} requires approval")
        if gpu not in RATES:
            raise ValueError(f"unknown rate for {gpu}")
        if estimated_minutes is None:
            if max_minutes is None:
                raise ValueError("supply estimated_minutes or max_minutes")
            estimated_minutes = max_minutes / 2
        cap, cost = limits(estimated_minutes, RATES[gpu])
        if max_minutes is not None:
            cap = min(1440, max(30, max_minutes))
        with self.db() as db:
            if db.execute(
                """SELECT 1 FROM usage WHERE task_id=? AND closing=1
                   AND stopped IS NULL AND endpoint IS NOT NULL""",
                (task_id,),
            ).fetchone():
                raise WaitingForGPU(gpu)
        if script is None and only_account is None:
            if run := self._adopt(gpu, task_id, cap, cost, estimated_minutes):
                return run
        candidates = [
            a
            for a in self.accounts(only_account=only_account)
            if gpu in a.gpus and a.balance >= cost
        ]
        for a in sorted(candidates, key=lambda a: -a.balance):
            name = uuid.uuid4().hex
            with self.lock(a.id, wait=len(candidates) == 1) as account_lock:
                if account_lock is None or not self._enabled(a.id):
                    continue
                try:
                    info = self._usage(a.id)
                except CliError:
                    continue
                if info["balance"] < cost or gpu not in info["gpus"]:
                    continue
                now = self.clock()
                with self.db() as db:
                    if not db.execute(  # 等锁、查余额期间被停用或移除了（ColabPool.remove 在同一把锁里删凭据）
                        "SELECT 1 FROM accounts WHERE id=? AND enabled AND NOT removed", (a.id,)
                    ).fetchone():
                        continue
                    self._meter(db, a.id, now, info["balance"])
                    db.execute(
                        """INSERT INTO usage
                        (name,account,gpu,task_id,started,deadline,pid,pid_start,estimated_units,before_balance)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (
                            name,
                            a.id,
                            gpu,
                            task_id,
                            now,
                            now + cap * 60,
                            os.getpid(),
                            psutil.Process().create_time(),
                            cost,
                            info["balance"],
                        ),
                    )
            with self._job_lock(name) as fd:
                try:
                    if script is not None:
                        output = self.cli.call(
                            a.id,
                            "run",
                            "-s",
                            name,
                            "--gpu",
                            gpu,
                            "--keep",
                            "--timeout",
                            "0",
                            "--",
                            *script,
                            job=name,
                            timeout=cap * 60,
                            lock_fd=fd,
                        )
                        self._stop(name)
                        return output
                    self.cli.call(
                        a.id,
                        "new",
                        "-s",
                        name,
                        "--gpu",
                        gpu,
                        job=name,
                        timeout=120,
                        lock_fd=fd,
                    )
                    endpoint = self.cli.endpoint(a.id, name)
                    if not endpoint:
                        raise CliError("missing session_created endpoint")
                    with self.db() as db:
                        db.execute(
                            "UPDATE usage SET endpoint=? WHERE name=?", (endpoint, name)
                        )
                    run = ColabRun(self, name, a.id, endpoint, now + cap * 60)
                    run.task_id = task_id
                    return run
                except AllocationUnavailable as error:
                    with self.db() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO cooldown VALUES (?,?,?)",
                            (a.id, gpu, self.clock() + 600),
                        )
                    if isinstance(error, AllocationRefused):
                        with self.db() as db:
                            db.execute("DELETE FROM usage WHERE name=?", (name,))
                    else:
                        with self.db() as db:
                            db.execute(
                                "UPDATE usage SET closing=1,recover_after=? WHERE name=?",
                                (self.clock() + 10, name),
                            )
                except BaseException:
                    self._stop(name)
                    raise
        raise WaitingForGPU(gpu)

    # 能接手的空闲 VM：账号还启用、开机 24 小时的硬期限前剩下的时间够这次预计的
    ADOPTABLE = """idle=1 AND closing=0 AND stopped IS NULL AND retry_deadline>:now
        AND started + 86400 - :now >= :need
        AND (SELECT enabled FROM accounts WHERE accounts.id=usage.account)"""

    def adopt(self, gpu, *, task_id, estimated_minutes, max_minutes=None, approved=False):
        """只接手、不新开：有能接手的同型号空闲 VM 就返回它（ColabRun.reused），否则 None。扫参按热启动报价的那一段用。"""
        if gpu in PREMIUM and not approved:
            raise PermissionError(f"{gpu} requires approval")
        cap, cost = limits(estimated_minutes, RATES[gpu])
        if max_minutes is not None:
            cap = min(1440, max(30, max_minutes))
        return self._adopt(gpu, task_id, cap, cost, estimated_minutes)

    def _adopt(self, gpu, task_id, cap, cost, estimated_minutes):
        """接手一台做完先别关（ColabRun.keep_idle）的同型号 VM：不用重新开卡、装环境。账号余额（当场查的）要够这次，
        账号没停用，开机 24 小时的硬期限前剩下的时间要够这次预计的（正式接手时在 VM 锁里再核一遍）。
        账本里还是同一行（同一个 notebook UUID），任务 id 换成这次的，时限从现在起算（不超过开机后 24 小时）。"""
        need = estimated_minutes * 60
        with self.db() as db:
            rows = db.execute(
                f"SELECT name,account,endpoint,started FROM usage WHERE gpu=:gpu AND {self.ADOPTABLE}"
                " ORDER BY retry_deadline DESC",
                dict(gpu=gpu, now=self.clock(), need=need),
            ).fetchall()
        for row in rows:
            try:  # 余额按现在的查（上一个任务刚花过）
                if self._usage(row["account"])["balance"] < cost:
                    continue
            except CliError:
                continue
            now = self.clock()
            deadline = min(row["started"] + 1440 * 60, now + cap * 60)
            with self._job_lock(row["name"]), self.db() as db:
                adopted = db.execute(
                    f"""UPDATE usage SET idle=0, retry_ping=NULL, retry_deadline=NULL, task_id=:task, deadline=:deadline,
                       adopted=:now WHERE name=:name AND {self.ADOPTABLE}""",
                    dict(task=task_id, deadline=deadline, now=now, name=row["name"], need=need),
                ).rowcount
            if adopted:
                run = ColabRun(self, row["name"], row["account"], row["endpoint"], deadline)
                run.reused, run.task_id = True, task_id
                return run
        return None

    def _stop(self, name):
        with self.db() as db:
            row = db.execute("SELECT * FROM usage WHERE name=?", (name,)).fetchone()
            if row is None or row["actual_units"] is not None:
                return
            db.execute("UPDATE usage SET closing=1 WHERE name=?", (name,))
        account = row["account"]
        if row["stopped"] is None:
            ep = row["endpoint"] or self.cli.endpoint(account, name)
            if not ep:
                now = self.clock()
                if row["recover_after"] is None:
                    with self.db() as db:
                        db.execute(
                            "UPDATE usage SET recover_after=? WHERE name=?",
                            (now + 10, name),
                        )
                    return
                if now < row["recover_after"]:
                    return
                ep = self.cli.api(account, "recover", name, row["gpu"])
                if not ep:
                    deleted = now >= row["started"] + 600
                    with self.db() as db:
                        if deleted:
                            db.execute("DELETE FROM usage WHERE name=?", (name,))
                        else:
                            db.execute(
                                "UPDATE usage SET recover_after=? WHERE name=?",
                                (now + 10, name),
                            )
                    if deleted:
                        with self._probe_changed:
                            self._probe_changed.notify_all()
                    return
            with self.db() as db:
                db.execute("UPDATE usage SET endpoint=? WHERE name=?", (ep, name))
            self.cli.call(account, "release", name, ep)
            with self.db() as db:
                db.execute(
                    "UPDATE usage SET stopped=? WHERE name=?", (self.clock(), name)
                )
        try:
            self._settle(account)
        finally:
            with self._probe_changed:
                self._probe_changed.notify_all()

    @staticmethod
    def _meter(db, account, now, balance):
        """Consume each observed debit once, weighted by VM time in the measured interval."""
        previous = db.execute(
            "SELECT at,balance FROM meter WHERE account=?", (account,)
        ).fetchone()
        rows = db.execute(
            """SELECT name,gpu,started,stopped FROM usage WHERE account=?
            AND late_checked=0 AND (endpoint IS NOT NULL OR stopped IS NOT NULL)""",
            (account,),
        ).fetchall()
        if previous and rows:
            debit = previous["balance"] - balance
            if debit == 0:
                return  # retain elapsed VM time until the provider posts a charge
            if debit > 0:
                weights = []
                for r in rows:
                    start, end = previous["at"], now
                    if (
                        r["stopped"] is not None
                        and r["stopped"] <= start
                        and now >= r["stopped"] + 300
                    ):
                        # A recently closed VM can still contribute to this CU posting
                        # while a later VM is running. Use the preceding billing interval.
                        start, end = start - 300, end - 300
                    seconds = max(
                        0, min(end, r["stopped"] or now) - max(start, r["started"])
                    )
                    weights.append(RATES[r["gpu"]] * seconds)
                if not any(weights):
                    # A delayed debit after all eligible VMs stopped.
                    weights = [
                        RATES[r["gpu"]] * max(1, (r["stopped"] or now) - r["started"])
                        for r in rows
                    ]
                total = sum(weights)
                db.executemany(
                    "UPDATE usage SET allocated=allocated+? WHERE name=?",
                    [
                        (debit * w / total, r["name"])
                        for r, w in zip(rows, weights)
                        if w
                    ],
                )
        db.execute(
            "INSERT OR REPLACE INTO meter VALUES (?,?,?)", (account, now, balance)
        )

    def _settle(self, account):
        with self.lock(account):
            balance = self._usage(account)["balance"]
            now = self.clock()
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                self._meter(db, account, now, balance)
                db.execute(
                    """UPDATE usage SET actual_units=allocated,
                    late_checked=CASE WHEN stopped<=? THEN 1 ELSE 0 END
                    WHERE account=? AND stopped IS NOT NULL AND late_checked=0""",
                    (now - 300, account),
                )

    def stop(self, account, name, task_id=None):
        """释放 VM。task_id：只在这台还归这个任务时释放（做完先别关的 VM 可能已被下一个任务接手）。"""
        with self.db() as db:
            changed = db.execute(
                """UPDATE usage SET closing=1 WHERE account=? AND name=? AND actual_units IS NULL
                   AND (? IS NULL OR task_id=?)""",
                (account, name, task_id, task_id),
            )
            if not changed.rowcount:
                return
        with self._job_lock(name):
            self._stop(name)

    def reap(self):
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM usage WHERE actual_units IS NULL"
            ).fetchall()
        errors = []
        for row in rows:
            alive = self._owner_alive(row)
            if row["closing"] or not alive or self.clock() >= row["deadline"]:
                try:
                    self._reap_stop(row["name"], alive)
                except Exception as exc:
                    errors.append(exc)
            elif row["retry_deadline"] is not None and self.clock() >= min(
                row["retry_ping"], row["retry_deadline"]
            ):
                try:
                    self._maintain_retained(row)
                except Exception as exc:
                    errors.append(exc)
        with self.db() as db:
            due = db.execute(
                """SELECT DISTINCT account FROM usage WHERE stopped<=?
                             AND late_checked=0""",
                (self.clock() - 300,),
            ).fetchall()
        for row in due:
            try:
                self._settle(row["account"])
            except Exception as exc:
                errors.append(exc)
        # Sweep only journaled, settled jobs from previous versions or a crash.
        with self.db() as db:
            for path in self.root.glob("?" * 32 + ".lock"):
                if db.execute(
                    "SELECT 1 FROM usage WHERE name=? AND actual_units IS NOT NULL",
                    (path.stem,),
                ).fetchone():
                    path.unlink(missing_ok=True)
        if errors:
            raise errors[0]

    @staticmethod
    def _owner_alive(row):
        import psutil

        try:
            owner = psutil.Process(row["pid"])
            return owner.create_time() == row["pid_start"] and owner.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def _reap_stop(self, name, alive):
        """回收：在这台 VM 的锁里按现在的记录再判一次（前面的行要调 CLI，慢，这期间它可能已被接手、时限延长），
        还该关才标 closing 并释放。"""
        with self._job_lock(name):
            with self.db() as db:
                row = db.execute("SELECT * FROM usage WHERE name=?", (name,)).fetchone()
                if row is None or row["actual_units"] is not None:
                    return
                if not (row["closing"] or not alive or self.clock() >= row["deadline"]):
                    return
                db.execute("UPDATE usage SET closing=1 WHERE name=?", (name,))
            self._stop(name)

    def _maintain_retained(self, row):
        with self._job_lock(row["name"]):
            with self.db() as db:
                now = self.clock()
                expired = db.execute(
                    """UPDATE usage SET closing=1 WHERE name=? AND retry_deadline<=?
                    AND stopped IS NULL""",
                    (row["name"], now),
                ).rowcount
                claimed = db.execute(
                    """UPDATE usage SET retry_ping=? WHERE name=? AND retry_ping<=?
                    AND closing=0 AND stopped IS NULL AND retry_deadline=?""",
                    (now + 300, row["name"], now, row["retry_deadline"]),
                ).rowcount
            if expired:
                self._stop(row["name"])
            elif claimed:
                self.cli.execute(
                    row["account"],
                    row["name"],
                    "pass",
                    min(60, row["retry_deadline"] - now),
                )

    @asynccontextmanager
    async def lifespan(self, interval=60):
        """Mount on the core control process. Reap on start, periodically and at shutdown."""

        async def tick():
            try:
                await asyncio.to_thread(self.reap)
                await asyncio.to_thread(self.accounts, refresh=False)
            except Exception:
                logging.getLogger(__name__).exception("Colab maintenance failed")

        stop = asyncio.Event()

        async def maintain():
            while not stop.is_set():
                await tick()
                try:
                    await asyncio.wait_for(stop.wait(), interval)
                except asyncio.TimeoutError:
                    pass

        task = asyncio.create_task(maintain())
        try:
            yield self
        finally:
            stop.set()
            await task
            # 登录后的试卡在后台线程里：先不许再开，等正在开的那台进入释放，再收最后一轮
            self.closing.set()
            for login in list(self.logins.values()):
                if login.thread is not None:
                    await asyncio.to_thread(login.thread.join)
            with self.db() as db:
                rows = db.execute(
                    "SELECT account,name FROM usage WHERE pid=? AND actual_units IS NULL",
                    (os.getpid(),),
                ).fetchall()
            for row in rows:
                await asyncio.to_thread(self.stop, row["account"], row["name"])
