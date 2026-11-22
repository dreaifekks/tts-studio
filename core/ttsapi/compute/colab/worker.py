"""Runs in the bundled Python 3.12 CLI environment."""

import hashlib
import json
import logging
import os
import sys
import signal
import socket
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urljoin
from uuid import UUID

import keyring
from requests.exceptions import Timeout
from keyring.backend import KeyringBackend
from colab_cli.state import SessionState
from colab_cli import cli
from colab_cli.client import Assignment, Client, ColabRequestError, Prod, TUN_ENDPOINT
from colab_cli.commands.session import resolve_runtime_options
from colab_cli.common import get_credentials, state


class JobClient(Client):
    """Reconcile an uncertain allocation using the same notebook UUID."""

    def assign(self, notebook_hash, *args, **kwargs):
        signal.alarm(120)
        try:
            name = os.environ["TTS_COLAB_JOB"]
            try:
                result = super().assign(UUID(name), *args, **kwargs)
            except Timeout:
                raise SystemExit(os.EX_TEMPFAIL) from None
            except ColabRequestError as exc:
                if (
                    exc.request.method == "POST"
                    and 500 <= exc.response.status_code < 600
                ):
                    raise SystemExit(os.EX_TEMPFAIL) from None
                if exc.request.method == "GET" and exc.response.status_code == 503:
                    # Assignment needs a POST. A rejected preflight can safely fall back.
                    raise SystemExit("Allocation refused: no GPU capacity") from None
                raise
            # colab run can execute for hours before the parent sees it return.
            # Include that VM in concurrent billing as soon as assignment succeeds.
            with sqlite3.connect(
                Path(state.config_path).parents[2] / "usage.sqlite"
            ) as db:
                db.execute(
                    "UPDATE usage SET endpoint=? WHERE name=?", (result.endpoint, name)
                )
            return result
        finally:
            signal.alarm(0)


def email_hash(session):
    response = session.get(
        "https://openidconnect.googleapis.com/v1/userinfo", timeout=20
    )
    response.raise_for_status()
    return hashlib.sha256(
        response.json()["email"].strip().casefold().encode()
    ).hexdigest()


class LoginStage(KeyringBackend):
    """Private to this authorization subprocess, including the CLI's keyring I/O thread."""

    priority = 0

    def __init__(self):
        super().__init__()
        self.entries = {}

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def set_password(self, service, username, password):
        self.entries[service, username] = password


def verify_login(account):
    if account == "default":
        raise ValueError("default uses the existing system login")
    destination = keyring.get_keyring()
    stage = LoginStage()
    keyring.set_keyring(stage)
    try:
        with get_credentials() as session:
            if email_hash(session) != account:
                raise SystemExit(
                    "The signed-in account differs from the selected account."
                )
        for (service, username), value in stage.entries.items():
            destination.set_password(service, username, value)
    finally:
        stage.entries.clear()
        keyring.set_keyring(destination)


def login():
    """后台登录（ColabPool.begin_login）：CLI 的粘贴授权码流程（链接打到 stderr，授权码从 stdin 读），
    授权结果先放在本进程内存里；拿到邮箱后告诉主控，主控回 commit 才按这个邮箱的账号 id 写进凭据。"""
    import builtins

    # CLI 用 input() 读授权码，提示语会写到 stdout；stdout 只留给给主控的那一行
    builtins.input = lambda prompt="": sys.stdin.readline().strip()
    destination = keyring.get_keyring()
    stage = LoginStage()
    keyring.set_keyring(stage)
    try:
        with get_credentials() as session:
            response = session.get(
                "https://openidconnect.googleapis.com/v1/userinfo", timeout=20
            )
            response.raise_for_status()
            email = response.json()["email"].strip()
        account = hashlib.sha256(email.casefold().encode()).hexdigest()
        print(json.dumps({"email": email, "account": account}), flush=True)
        if sys.stdin.readline().strip() != "commit":
            raise SystemExit("login aborted")
        os.environ["TTS_COLAB_ACCOUNT"] = account  # AccountKeyring 按它选凭据条目
        for (service, username), value in stage.entries.items():
            destination.set_password(service, username, value)
    finally:
        stage.entries.clear()
        keyring.set_keyring(destination)


def release(client, store, name, endpoint):
    if any(a.endpoint == endpoint for a in client.list_assignments()):
        try:
            client.unassign(endpoint)
        except ColabRequestError as error:
            if error.response.status_code not in (404, 410):
                raise
        if any(a.endpoint == endpoint for a in client.list_assignments()):
            raise SystemExit(1)
    local = store.get(name)
    if local and local.endpoint == endpoint:
        store.remove(name)


def restore_session(state, ledger, name):
    with sqlite3.connect(ledger) as db:
        row = db.execute(
            "SELECT endpoint FROM usage WHERE name=? AND account=? AND stopped IS NULL",
            (name, os.environ["TTS_COLAB_ACCOUNT"]),
        ).fetchone()
    if row:
        for assignment in state.client.list_assignments():
            if assignment.endpoint == row[0]:
                info = assignment.runtime_proxy_info
                session = SessionState(
                    name=name,
                    endpoint=assignment.endpoint,
                    **info.model_dump(include={"url", "token"}),
                    token_expires_at=info.expires_at(),
                    variant=assignment.variant.name,
                    accelerator=assignment.accelerator.value,
                    machine_shape=assignment.machine_shape.name,
                )
                state.store.add(session)
                return session


def patch_kernel_client(*, bound_prelude=False):
    """Fix upstream jupyter-kernel-client 0.9 / colab-cli 0.8 exec hangs:
    1. Daemonize the websocket listener thread so a failed prelude or stalled
       connect cannot block interpreter shutdown until the outer timeout.
    2. Fail start_channels() when the websocket handshake exceeds REQUEST_TIMEOUT
       instead of sending execute_request on an unready socket.
    3. Record a newly created kernel_id even when the initial websocket handshake
       fails so retries and restart_kernel() reuse it instead of leaking orphans.
    4. Shut down the raw socket before closing in stop_channels() so macOS kqueue
       selectors wake immediately instead of stalling 10s per exec.
    5. Bound the chdir prelude (timeout=None in colab exec; --timeout 0 passes 0.0)
       to REQUEST_TIMEOUT so a wedged kernel fails fast and triggers retry.
    """
    import colab_cli.runtime as colab_runtime
    import jupyter_kernel_client.wsclient as wsclient

    if not getattr(wsclient.KernelWebSocketClient, "_tts_patched", False):
        orig_thread = wsclient.Thread
        orig_start = wsclient.KernelWebSocketClient.start_channels

        def daemon_thread(*args, **kwargs):
            kwargs.setdefault("daemon", True)
            return orig_thread(*args, **kwargs)

        def checked_start_channels(self, *args, **kwargs):
            orig_start(self, *args, **kwargs)
            if not self.connection_ready.is_set():
                self.stop_channels()
                raise TimeoutError(
                    f"Colab kernel websocket handshake timed out after {self.timeout}s"
                )

        def fast_stop_channels(self):
            if self.shutting_down:
                return
            self.shutting_down = True
            ws = self.kernel_socket
            self.kernel_socket = None
            if ws is not None:
                ws.keep_running = False
                raw = getattr(getattr(ws, "sock", None), "sock", None)
                if raw is not None:
                    try:
                        raw.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                try:
                    ws.close()
                except Exception:
                    pass
            thread = self.connection_thread
            self.connection_thread = None
            if thread is not None and thread.is_alive():
                thread.join(min(self.timeout, 2.0))

        wsclient.Thread = daemon_thread
        wsclient.KernelWebSocketClient.start_channels = checked_start_channels
        wsclient.KernelWebSocketClient.stop_channels = fast_stop_channels
        wsclient.KernelWebSocketClient._tts_patched = True

    if not getattr(colab_runtime.ColabRuntime, "_tts_kernel_record_patched", False):
        orig_kernel_client = colab_runtime.ColabRuntime.kernel_client.fget

        def recording_kernel_client(self):
            try:
                return orig_kernel_client(self)
            except Exception:
                kc = self._kernel_client
                if kc is not None and kc.id and kc.id != self.kernel_id:
                    self.kernel_id = kc.id
                    if self.on_kernel_started:
                        self.on_kernel_started(self.kernel_id)
                raise

        colab_runtime.ColabRuntime.kernel_client = property(recording_kernel_client)
        colab_runtime.ColabRuntime._tts_kernel_record_patched = True

    if bound_prelude:
        if not getattr(colab_runtime.ColabRuntime, "_tts_prelude_patched", False):
            orig_execute = colab_runtime.ColabRuntime.execute_code

            def bounded_execute_code(self, code, *args, timeout=None, **kwargs):
                if timeout is None:
                    timeout = wsclient.REQUEST_TIMEOUT
                return orig_execute(self, code, *args, timeout=timeout, **kwargs)

            colab_runtime.ColabRuntime.execute_code = bounded_execute_code
            colab_runtime.ColabRuntime._tts_prelude_patched = True


def restart_kernel(state, ledger, name):
    """Restart the session's kernel over HTTP without opening a websocket."""
    from jupyter_kernel_client.manager import KernelHttpManager

    session = state.get_session(name, ignore_missing_session=True)
    if session is None:
        session = restore_session(state, ledger, name)
    if session is None or not session.kernel_id:
        return
    headers = {
        "X-Colab-Client-Agent": "colab-cli",
        "X-Colab-Runtime-Proxy-Token": session.token,
    }
    manager = KernelHttpManager(
        server_url=session.url,
        token=session.token,
        kernel_id=session.kernel_id,
        headers=headers,
    )
    if manager.has_kernel:
        manager.restart_kernel(timeout=20)
    else:
        session.kernel_id = None
        session.session_id = None
        state.store.add(session)


def main():
    logging.disable(logging.CRITICAL)  # upstream DEBUG logs include HTTP bodies
    config, action, *args = sys.argv[1:]
    if action in ("new", "run"):
        owner = int(os.environ["TTS_COLAB_OWNER"])

        def watch_owner():
            # A long colab run holds the allocation lock. Leave promptly when
            # the core closes it so the waiting reaper can settle the account.
            ledger = Path(config).parents[2] / "usage.sqlite"
            with sqlite3.connect(ledger) as db:
                while os.getppid() == owner:
                    row = db.execute(
                        "SELECT closing FROM usage WHERE name=?",
                        (os.environ["TTS_COLAB_JOB"],),
                    ).fetchone()
                    if row is None or row[0]:
                        break
                    time.sleep(1)
            os._exit(125)

        threading.Thread(target=watch_owner, daemon=True).start()
    state.config_path = config
    if action == "verify-login":
        verify_login(os.environ["TTS_COLAB_ACCOUNT"])
        return
    if action == "login":
        login()
        return
    if action == "forget":
        from ttsapi.compute.colab.keyring_backend import forget

        forget(os.environ["TTS_COLAB_ACCOUNT"], os.environ["TTS_COLAB_KEYRING"])
        return
    state._client = JobClient(Prod(), get_credentials())
    client = state.client
    if action == "identity":
        print(json.dumps(email_hash(client.session)))
    elif action == "usage":
        raw = client._issue_request(
            urljoin(client.colab_domain, TUN_ENDPOINT + "/ccu-info"), schema=dict
        )
        print(
            json.dumps({"balance": raw["currentBalance"], "gpus": raw["eligibleGpus"]})
        )
    elif action == "ps":
        print(json.dumps([a.endpoint for a in client.list_assignments()]))
    elif action == "recover":
        name, gpu = args
        variant, acc, shape = resolve_runtime_options(gpu)
        res = client._get_assignment(UUID(name), variant, acc, shape)
        print(json.dumps(res.endpoint if isinstance(res, Assignment) else None))
    elif action == "release":
        release(client, state.store, *args)
    elif action == "restart-kernel":
        restart_kernel(state, Path(config).parents[2] / "usage.sqlite", args[0])
    else:
        if action in ("exec", "upload", "download"):
            original = state.get_session

            def get_session(name, ignore_missing_session=False):
                session = original(name, ignore_missing_session=True)
                if session is None:
                    session = restore_session(
                        state, Path(config).parents[2] / "usage.sqlite", name
                    )
                if session is None and not ignore_missing_session:
                    raise SystemExit("Session not found")
                return session

            state.get_session = get_session
        if action in ("exec", "run"):
            patch_kernel_client(bound_prelude=(action == "exec"))
        cli.app(args=["--config", config, action, *args])


if __name__ == "__main__":
    main()
