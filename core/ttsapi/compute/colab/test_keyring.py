"""Run with the locked CLI interpreter; private directories and a fake native keyring."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

if importlib.util.find_spec("keyring") is None:
    raise unittest.SkipTest("run keyring tests with the bundled CLI interpreter")

from keyring.errors import KeyringLocked, PasswordDeleteError  # noqa: E402

from .keyring_backend import AccountKeyring, forget  # noqa: E402


class IsolationTest(unittest.TestCase):
    def setUp(self):
        self.directory = self.enter_dir()
        self.values = {}
        self.native = Mock()
        self.native.get_password.side_effect = lambda s, u: self.values.get((s, u))
        self.native.set_password.side_effect = lambda s, u, v: self.values.update(
            {(s, u): v}
        )
        self.patch(
            AccountKeyring.__module__ + ".system_backend", return_value=self.native
        )
        self.env = patch.dict(
            os.environ, TTS_COLAB_ACCOUNT="default", TTS_COLAB_KEYRING=self.directory
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.backend = AccountKeyring()

    def enter_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def patch(self, *args, **kwargs):
        item = patch(*args, **kwargs)
        self.addCleanup(item.stop)
        return item.start()

    def test_existing_login_readonly(self):
        self.values[("service", "user")] = "test-fixture"
        self.assertEqual(self.backend.get_password("service", "user"), "test-fixture")
        self.backend.set_password("service", "user", "unused")
        self.native.set_password.assert_not_called()

    def test_linux_files(self):
        self.patch("sys.platform", "linux")
        os.environ["TTS_COLAB_ACCOUNT"] = "first"
        self.backend.set_password("service", "user", "test-fixture")
        path = Path(self.directory, "first")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b"test-fixture", path.read_bytes())
        self.assertEqual(self.backend.get_password("service", "user"), "test-fixture")
        os.environ["TTS_COLAB_ACCOUNT"] = "second"
        self.assertIsNone(self.backend.get_password("service", "user"))

    def test_mac_names(self):
        self.patch("sys.platform", "darwin")
        os.environ["TTS_COLAB_ACCOUNT"] = "first"
        self.backend.set_password("service", "user", "test-fixture")
        self.assertIn(("service", "user/first"), self.values)
        os.environ["TTS_COLAB_ACCOUNT"] = "second"
        self.assertIsNone(self.backend.get_password("service", "user"))


class ForgetTest(IsolationTest):
    """移除账号：只删 core 给这个账号写的那一条（假的系统密钥环，不碰本机钥匙串）。"""

    def setUp(self):
        super().setUp()

        class NotFound(Exception):
            pass

        def delete(service, username):  # 和真的后端一样：macOS 把钥匙串的 NotFound 包起来，Secret Service 直接抛
            if (service, username) not in self.values:
                if sys.platform == "darwin":
                    raise PasswordDeleteError("Can't delete password in keychain") from NotFound(-25300)
                raise PasswordDeleteError("No such password!")
            del self.values[service, username]
        self.native.delete_password.side_effect = delete

    def test_mac_deletes_only_the_mapped_entry(self):
        self.patch("sys.platform", "darwin")
        for account in ("first", "second"):
            os.environ["TTS_COLAB_ACCOUNT"] = account
            self.backend.set_password("google-colab-cli", "oauth_token", "test-fixture")
        self.values[("google-colab-cli", "oauth_token")] = "standalone-cli-login"
        forget("first", self.directory)
        self.assertEqual(
            set(self.values),
            {("google-colab-cli", "oauth_token/second"), ("google-colab-cli", "oauth_token")},
        )
        forget("first", self.directory)  # 已经没有了：跳过

    def test_linux_deletes_file_and_file_key(self):
        self.patch("sys.platform", "linux")
        for account in ("first", "second"):
            os.environ["TTS_COLAB_ACCOUNT"] = account
            self.backend.set_password("google-colab-cli", "oauth_token", "test-fixture")
        forget("first", self.directory)
        self.assertFalse(Path(self.directory, "first").exists())
        self.assertFalse(Path(self.directory, "first.lock").exists())
        self.assertEqual(set(self.values), {("tts-core-colab-file-key", "second")})
        self.assertEqual(self.backend.get_password("google-colab-cli", "oauth_token"), "test-fixture")

    def test_only_missing_entries_are_skipped(self):
        """钥匙串拒绝删除（macOS 后端也包成 PasswordDeleteError）要报错；只有「条目不存在」跳过。"""
        class NotFound(Exception):
            pass

        class KeychainDenied(Exception):
            pass

        def fail(cause):
            def delete(service, username):
                raise PasswordDeleteError("Can't delete password in keychain") from cause
            return delete
        self.patch("sys.platform", "darwin")
        self.native.delete_password.side_effect = fail(NotFound(-25300))
        forget("first", self.directory)
        self.native.delete_password.side_effect = fail(KeychainDenied(-128))
        with self.assertRaises(PasswordDeleteError):
            forget("first", self.directory)
        self.patch("sys.platform", "linux")
        self.native.delete_password.side_effect = PasswordDeleteError("No such password!")
        forget("first", self.directory)
        self.native.delete_password.side_effect = KeyringLocked("locked")
        with self.assertRaises(KeyringLocked):
            forget("first", self.directory)

    def test_default_is_never_deleted(self):
        with self.assertRaises(ValueError):
            forget("default", self.directory)
        self.native.delete_password.assert_not_called()


class WorkerTest(unittest.TestCase):
    def test_login_staging(self):
        from . import worker
        from contextlib import nullcontext

        for actual in ("wrong", "selected"):
            with self.subTest(actual=actual):
                permanent = Mock()

                def credentials():
                    worker.keyring.get_keyring().set_password(
                        "test", "test", "synthetic"
                    )
                    return nullcontext(object())

                with (
                    patch.object(
                        worker.keyring, "get_keyring", side_effect=lambda: current[0]
                    ),
                    patch.object(
                        worker.keyring,
                        "set_keyring",
                        side_effect=lambda v: current.__setitem__(0, v),
                    ),
                    patch.object(worker, "get_credentials", side_effect=credentials),
                    patch.object(worker, "email_hash", return_value=actual),
                ):
                    current = [permanent]
                    if actual == "wrong":
                        with self.assertRaises(SystemExit):
                            worker.verify_login("selected")
                        permanent.set_password.assert_not_called()
                    else:
                        worker.verify_login("selected")
                        permanent.set_password.assert_called_once_with(
                            "test", "test", "synthetic"
                        )
                    self.assertIs(current[0], permanent)

    def test_release_checks_presence(self):
        from .worker import release

        client, store = Mock(), Mock()
        target = Mock(endpoint="owned")
        client.list_assignments.side_effect = [[target], [], []]
        release(client, store, "run", "owned")
        release(client, store, "run", "owned")
        client.unassign.assert_called_once_with("owned")
        self.assertEqual(client.list_assignments.call_count, 3)

    def test_rebuild_session_from_exact_endpoint(self):
        import sqlite3
        from colab_cli.client import ListedAssignment, RuntimeProxyInfo
        from .worker import restore_session

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, TTS_COLAB_ACCOUNT="default"),
        ):
            ledger = Path(folder) / "usage.sqlite"
            with sqlite3.connect(ledger) as db:
                db.execute("CREATE TABLE usage(name,account,endpoint,stopped)")
                db.execute("INSERT INTO usage VALUES ('run','default','owned',NULL)")
            state = Mock()
            owned = ListedAssignment(
                endpoint="owned",
                runtimeProxyInfo=RuntimeProxyInfo(
                    token="synthetic",
                    tokenExpiresInSeconds=120,
                    url="https://example.invalid",
                ),
                variant=1,
                accelerator="T4",
                machineShape=0,
            )
            state.client.list_assignments.return_value = [Mock(endpoint="other"), owned]
            restored = restore_session(state, ledger, "run")
            self.assertEqual((restored.endpoint, restored.accelerator), ("owned", "T4"))
            state.store.add.assert_called_once_with(restored)

    def test_uncertain_worker_exit_status(self):
        from requests.exceptions import ReadTimeout
        from . import worker
        from .cli import AllocationUnavailable, ColabCLI
        from subprocess import CompletedProcess

        name = "12345678123456781234567812345678"
        for error in [
            ReadTimeout(),
            *(
                worker.ColabRequestError(
                    "test", Mock(method="POST"), Mock(status_code=status)
                )
                for status in (500, 502, 503, 504, 599)
            ),
        ]:
            with self.subTest(error=error), patch.dict(os.environ, TTS_COLAB_JOB=name):
                client = worker.JobClient.__new__(worker.JobClient)
                with (
                    patch.object(worker.Client, "assign", side_effect=error),
                    patch.object(worker.signal, "alarm"),
                ):
                    with self.assertRaises(SystemExit) as outcome:
                        client.assign(None)
                self.assertEqual(outcome.exception.code, os.EX_TEMPFAIL)
                with (
                    tempfile.TemporaryDirectory() as root,
                    patch(
                        "subprocess.run",
                        return_value=CompletedProcess(
                            [], outcome.exception.code, "", ""
                        ),
                    ),
                ):
                    cli = ColabCLI(Path(root), "unused")
                    with self.assertRaises(AllocationUnavailable):
                        cli.call("default", "new", job=name)

    def test_patch_kernel_client_handshake_timeout_and_stop(self):
        import socket
        import threading
        import jupyter_kernel_client.wsclient as wsclient
        import colab_cli.runtime as colab_runtime
        from . import worker

        worker.patch_kernel_client(bound_prelude=True)
        client = wsclient.KernelWebSocketClient("ws://127.0.0.1:1/channels", timeout=0.05)
        blocked = threading.Event()
        raw_sock = Mock()

        def stall_run():
            blocked.wait(2)

        with patch.object(client, "_run_websocket", side_effect=stall_run):
            with self.assertRaises(TimeoutError):
                client.start_channels()
            self.assertTrue(client.shutting_down)
        blocked.set()

        # Verify stop_channels shuts down raw socket before closing ws.
        client2 = wsclient.KernelWebSocketClient("ws://127.0.0.1:1/channels", timeout=0.2)
        ws = Mock()
        ws.sock = Mock(sock=raw_sock)
        client2.kernel_socket = ws
        client2.stop_channels()
        raw_sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        ws.close.assert_called_once()

        # Verify prelude timeout=None is bounded while explicit timeout=0.0 is preserved.
        seen = []
        rt = colab_runtime.ColabRuntime("http://127.0.0.1:1", "tok")
        with patch.object(
            rt, "_execute_streaming", side_effect=lambda code, hook, timeout: seen.append(timeout) or {"content": {"status": "ok"}}
        ):
            rt.execute_code("chdir()")
            rt.execute_code("main()", timeout=0.0)
        self.assertEqual(seen, [wsclient.REQUEST_TIMEOUT, 0.0])

    def test_restart_kernel_http_only_and_clears_gone(self):
        from colab_cli.state import SessionState
        from .worker import restart_kernel

        for has_kernel in (True, False):
            with self.subTest(has_kernel=has_kernel):
                session = SessionState(
                    name="run1",
                    token="tok",
                    url="http://127.0.0.1:1",
                    endpoint="ep1",
                    kernel_id="k1",
                    session_id="s1",
                )
                st = Mock()
                st.get_session.return_value = session
                mgr = Mock(has_kernel=has_kernel)
                with patch("jupyter_kernel_client.manager.KernelHttpManager", return_value=mgr):
                    restart_kernel(st, Path("/nonexistent"), "run1")
                if has_kernel:
                    mgr.restart_kernel.assert_called_once_with(timeout=20)
                    st.store.add.assert_not_called()
                else:
                    mgr.restart_kernel.assert_not_called()
                    self.assertIsNone(session.kernel_id)
                    self.assertIsNone(session.session_id)
                    st.store.add.assert_called_once_with(session)

    def test_first_handshake_timeout_records_created_kernel_id(self):
        import http.server
        import json
        import threading
        import time
        import colab_cli.runtime as colab_runtime
        import jupyter_kernel_client.wsclient as wsclient
        from colab_cli.state import SessionState
        from . import worker

        worker.patch_kernel_client(bound_prelude=True)
        events = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                if length:
                    self.rfile.read(length)
                if self.path == "/api/kernels":
                    events.append("create")
                    body = json.dumps(
                        {
                            "id": "k-new-1",
                            "name": "python3",
                            "execution_state": "idle",
                            "last_activity": "2026-11-21T00:00:00.000000Z",
                        }
                    ).encode()
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/api/kernels/k-new-1/restart":
                    events.append("restart")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                self.send_response(404)
                self.end_headers()

            def do_GET(self):
                if self.path == "/api/kernels/k-new-1":
                    body = json.dumps(
                        {
                            "id": "k-new-1",
                            "name": "python3",
                            "execution_state": "idle",
                            "last_activity": "2026-11-21T00:00:00.000000Z",
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/api/kernels/k-new-1/channels"):
                    # Stall handshake past timeout so start_channels() fails.
                    time.sleep(0.5)
                    return
                self.send_response(404)
                self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            session = SessionState(
                name="run1",
                token="tok",
                url=f"http://127.0.0.1:{port}",
                endpoint="ep1",
            )
            recorded = []
            rt = colab_runtime.ColabRuntime(
                session.url,
                session.token,
                kernel_id=session.kernel_id,
                on_kernel_started=lambda kid: (
                    setattr(session, "kernel_id", kid),
                    recorded.append(kid),
                ),
            )
            orig_init = wsclient.KernelWebSocketClient.__init__

            def short_ws_init(self, *args, **kwargs):
                kwargs["timeout"] = 0.08
                return orig_init(self, *args, **kwargs)

            with patch.object(wsclient.KernelWebSocketClient, "__init__", short_ws_init):
                with self.assertRaises(TimeoutError):
                    _ = rt.kernel_client
            self.assertEqual(recorded, ["k-new-1"])
            self.assertEqual(session.kernel_id, "k-new-1")

            st = Mock()
            st.get_session.return_value = session
            worker.restart_kernel(st, Path("/nonexistent"), "run1")
            self.assertEqual(events, ["create", "restart"])
        finally:
            srv.shutdown()
            srv.server_close()
