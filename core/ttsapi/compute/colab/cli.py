"""Launch an account-scoped Colab command and return only its result to the caller."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_KNOWN_ERRORS = (
    ("websocket handshake timed out", "websocket handshake timed out"),
    ("timeout waiting for output", "kernel execution timed out"),
    ("timeout waiting for reply", "kernel execution timed out"),
    ("connection was lost", "kernel connection lost"),
    ("kernel dead", "kernel dead"),
    ("kernel restarting", "kernel restarting"),
    ("session not found", "session not found"),
    ("appears to be lost", "session lost"),
    ("file or directory not found", "remote file not found"),
    ("read timed out", "HTTP read timed out"),
    ("connect timed out", "HTTP connect timed out"),
)
_HTTP_STATUS_RE = re.compile(
    r"(?:\b([45]\d{2})\s+(?:client|server)\s+error\b|\bhttp\s+([45]\d{2})\b)",
    re.IGNORECASE,
)
_EXCEPTION_RE = re.compile(
    r"(?m)^\s*(TimeoutError|ConnectionError|ConnectionResetError|SSLError|"
    r"WebSocketConnectionClosedException|WebSocketTimeoutException|"
    r"FileNotFoundError|RuntimeError)\s*:"
)


def _classify_error(*chunks):
    """Map subprocess output to a fixed safe label; never returns raw stderr/stdout text."""
    parts = []
    for chunk in chunks:
        if not chunk:
            continue
        parts.append(chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk)
    text = "\n".join(parts)
    if not text:
        return ""
    lower = text.lower()
    for needle, label in _KNOWN_ERRORS:
        if needle in lower:
            return label
    if match := _HTTP_STATUS_RE.search(text):
        code = match.group(1) or match.group(2)
        return f"HTTP {code}"
    matches = _EXCEPTION_RE.findall(text)
    if matches:
        return matches[-1]
    return ""


class CliError(RuntimeError):
    pass


class AllocationUnavailable(CliError):
    pass


class AllocationRefused(AllocationUnavailable):
    pass


class LoginRequired(CliError):
    pass


class ColabCLI:
    def __init__(self, root, python):
        self.root = root
        self.python = python

    def command(self, account, action, args, job=""):
        home = self.root / "colab" / account
        directory = home / ".config" / "colab-cli"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        settings = directory / "settings.json"
        if not settings.exists():
            settings.write_text('{"enable_update_check": false}')
        prefs = home / "Library" / "Preferences"
        if sys.platform == "darwin" and not (prefs / "com.apple.security.plist").exists():
            # macOS 的 Security 框架按 HOME 找默认钥匙串，这个 HOME 里没有：存凭据时弹「Keychain Not Found」，
            # 读也读不到。设成本机用户的登录钥匙串（Preferences 目录不在时 security 什么都不写，照样返回 0）
            prefs.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "/usr/bin/security",
                    "default-keychain",
                    "-d",
                    "user",
                    "-s",
                    str(Path.home() / "Library/Keychains/login.keychain-db"),
                ],
                env=dict(os.environ, HOME=str(home)),
                check=True,
            )
        config = str(home / "sessions.json")
        env = dict(
            os.environ,
            HOME=str(home),
            TTS_COLAB_ACCOUNT=account,
            TTS_COLAB_KEYRING=str(self.root / "keyring"),
            TTS_COLAB_JOB=job,
            PYTHONPATH=str(HERE.parents[2]),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHON_KEYRING_BACKEND="ttsapi.compute.colab.keyring_backend.AccountKeyring",
            COLAB_CLI_ASSIGN_RETRIES="0",
            COLAB_CLI_REQUEST_TIMEOUT="90",
            # jupyter_kernel_client reads REQUEST_TIMEOUT at import for kernel HTTP calls
            # and websocket handshake wait (default 10s is too short for Colab proxy spikes).
            REQUEST_TIMEOUT="30",
            TTS_COLAB_OWNER=str(os.getpid()),
            BROWSER="/bin/false",
            NO_COLOR="1",
        )
        argv = [
            str(self.python),
            str(HERE / "worker.py"),
            config,
            action,
            *map(str, args),
        ]
        return argv, env

    def login_process(self, slot):
        """后台登录子进程（worker.py login）：stderr 打出授权链接，stdin 收授权码；核对出邮箱后 stdout 打一行
        {"email", "account"}，再等 stdin 的 commit / abort。slot 是临时 HOME 的名字（账号 id 要等授权后才知道）。"""
        argv, env = self.command(slot, "login", ())
        return subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def authorize(self, account):
        argv, env = self.command(account, "verify-login", ())
        subprocess.run(argv, env=env, check=True)

    def call(self, account, action, *args, timeout=90, job="", lock_fd=None):
        argv, env = self.command(account, action, args, job)
        try:
            p = subprocess.run(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                text=True,
                pass_fds=() if lock_fd is None else (lock_fd,),
            )
        except subprocess.TimeoutExpired as exc:
            if action == "new" or (
                action == "run" and self.endpoint(account, job) is None
            ):
                raise AllocationUnavailable(f"{action} allocation timed out") from None
            detail = _classify_error(exc.stderr, exc.stdout)
            suffix = f" ({detail})" if detail else ""
            raise CliError(f"{action} timed out{suffix}") from None
        if p.returncode:
            message = (p.stderr + p.stdout).lower()
            if "to authorize colab-cli" in message or "keyring is locked" in message:
                raise LoginRequired(f"Colab login required for {account}")
            if (
                action in ("new", "run")
                and p.returncode in (-14, os.EX_TEMPFAIL)
                and self.endpoint(account, job) is None
            ):
                raise AllocationUnavailable("GPU allocation not confirmed")
            if (
                action in ("new", "run")
                and self.endpoint(account, job) is None
                and any(
                    text in message
                    for text in ("allocation refused", "backend rejected accelerator")
                )
            ):
                raise AllocationRefused("GPU allocation refused")
            if (
                action in ("new", "run")
                and self.endpoint(account, job) is None
                and any(
                    s in message
                    for s in (
                        "allocation refused",
                        "quota",
                        "service unavailable",
                        "503",
                        "read timed out",
                    )
                )
            ):
                raise AllocationUnavailable("GPU allocation unavailable")
            detail = _classify_error(p.stderr, p.stdout)
            suffix = f" ({detail})" if detail else ""
            raise CliError(f"colab {action} exited {p.returncode}{suffix}")
        return p.stdout

    def api(self, account, action, *args):
        return json.loads(self.call(account, action, *args))

    def endpoint(self, account, name):
        history = (
            self.root
            / "colab"
            / account
            / ".config/colab-cli/history"
            / f"{name}.jsonl"
        )
        if history.exists():
            with history.open() as stream:
                for line in stream:
                    if '"session_created"' in line:
                        event = json.loads(line)
                        if event["event_type"] == "session_created":
                            return event["endpoint"]
        return None

    def execute(self, account, name, code, timeout):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "command.py"
            script.write_text(code + "\nprint('TTS_COLAB_OK')\n")
            output = self.call(
                account,
                "exec",
                "-s",
                name,
                "--timeout",
                "0",
                "-f",
                script,
                timeout=timeout,
            )
            if "TTS_COLAB_OK" not in [line.strip() for line in output.splitlines()]:
                raise CliError("Colab kernel did not confirm execution")
            return output

    def restart_kernel(self, account, name, timeout=30):
        self.call(account, "restart-kernel", name, timeout=timeout)
