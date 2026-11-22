"""One account, one VM and one active background command."""

import hashlib
import json
import os
import shlex
import tempfile
import textwrap
import time
import uuid
from enum import Enum
from pathlib import Path, PurePosixPath

from .cli import CliError

CHUNK_BYTES = 70 * 1024 * 1024
RETRY_SECONDS = 5
DEFAULT_RETRY_MINUTES = 30
MERGE_BYTES_PER_SECOND = 100 * 1024 * 1024


class RunState(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    GONE = "gone"


class ColabRun:
    def __init__(self, pool, name, account_id, endpoint, deadline):
        self.pool = pool
        self.name = name
        self.account_id = account_id
        self.endpoint = endpoint
        self.deadline = deadline
        self.marker = None
        self.next_ping = pool.clock() + 300
        self.closed = False
        self.pending_result = None
        self._keep_on_exit = False
        self.task_id = None  # 开它的任务；接手后换成新任务，旧句柄的 stop 不再释放它
        self.idle = False  # keep_idle：做完先别关，等下一个任务接手
        self.reused = False  # 接手的是上一个任务留下的 VM（环境已装好，开头要清掉上次的作业目录）

    def __enter__(self):
        if self._keep_on_exit:
            with self.pool._job_lock(self.name), self.pool.db() as db:
                row = db.execute(
                    """SELECT retry_ping FROM usage WHERE name=? AND closing=0
                       AND retry_deadline>?""",
                    (self.name, self.pool.clock()),
                ).fetchone()
                if not row or row[0] is None:
                    raise RuntimeError("run is no longer retained")
                self.next_ping = row[0]
                db.execute(
                    "UPDATE usage SET retry_ping=NULL, retry_deadline=NULL WHERE name=?",
                    (self.name,),
                )
            self._keep_on_exit = False
        return self

    def __exit__(self, *exc):
        if not (self._keep_on_exit and (self.pending_result or self.idle)):
            self.stop()

    def keep_idle(self, minutes):
        """任务做完、结果已取回：VM 先别关，minutes 分钟内同型号的下一个任务直接接手（ColabPool.start）。
        这期间由账号池的维护循环保活，到时没人接手就释放、结算。留下了返回 True（快到时限、已关时不留，调用方照常释放）。"""
        if self.pending_result is not None:
            raise RuntimeError("retry the pending result download first")
        if self.closed or self.deadline - self.pool.clock() < 60:
            return False  # 快到时限了：照常释放
        with self.pool.db() as db:
            updated = db.execute(
                """UPDATE usage SET idle=1, retry_ping=?, retry_deadline=min(deadline, ?)
                WHERE name=? AND stopped IS NULL AND closing=0""",
                (self.next_ping, self.pool.clock() + minutes * 60, self.name),
            )
        if updated.rowcount:
            self._keep_on_exit = self.idle = True
        return self.idle

    def keep_for_retry(self, max_minutes=DEFAULT_RETRY_MINUTES):
        """Hand an unfinished download to the core maintenance loop until its deadline."""
        self._timeout(60)
        if self.pending_result is None:
            raise RuntimeError("no pending result download")
        with self.pool.db() as db:
            updated = db.execute(
                """UPDATE usage SET retry_ping=?, retry_deadline=coalesce(retry_deadline, min(deadline, ?))
                WHERE name=? AND stopped IS NULL AND closing=0""",
                (self.next_ping, self.pool.clock() + max_minutes * 60, self.name),
            )
            if not updated.rowcount:
                raise RuntimeError("run is closing")
        self._keep_on_exit = True

    def _timeout(self, seconds):
        if self.closed:
            raise RuntimeError("run is closed")
        remaining = self.deadline - self.pool.clock()
        if remaining <= 0:
            self.pool.stop(self.account_id, self.name, self.task_id)
            self.closed = True
            raise TimeoutError("Colab VM time limit reached")
        return min(seconds, remaining)

    def execute(self, code, *, timeout=90, restart_on_retry=False):
        for attempt in range(3):
            try:
                return self.pool.cli.execute(
                    self.account_id, self.name, code, self._timeout(timeout)
                )
            except CliError:
                if attempt == 2:
                    raise
                if (
                    restart_on_retry
                    and attempt == 1
                    and hasattr(self.pool.cli, "restart_kernel")
                ):
                    try:
                        self.pool.cli.restart_kernel(
                            self.account_id, self.name, self._timeout(30)
                        )
                    except CliError:
                        pass
                time.sleep(self._timeout(RETRY_SECONDS))

    def _keepalive(self):
        if self.pool.clock() >= self.next_ping:
            self.next_ping = self.pool.clock() + 300
            try:
                self.pool.cli.execute(
                    self.account_id, self.name, "pass", self._timeout(60)
                )
            except CliError:
                pass

    def _transfer(self, cmd, src, dst, timeout=180):
        self._keepalive()
        self.pool.cli.call(
            self.account_id,
            cmd,
            "-s",
            self.name,
            src,
            dst,
            timeout=self._timeout(timeout),
        )
        self._keepalive()

    def _upload_part(self, local, remote):
        for attempt in range(3):
            try:
                self._transfer("upload", local, remote)
                return
            except CliError:
                if attempt == 2:
                    raise
                time.sleep(self._timeout(RETRY_SECONDS))

    def upload(self, local, remote):
        local = Path(local)
        target = str(PurePosixPath("/") / remote)
        prefix = target + "." + uuid.uuid4().hex
        self.execute(
            f"from pathlib import Path\nPath({target!r}).parent.mkdir(parents=True, exist_ok=True)",
            restart_on_retry=True,
        )
        digest = hashlib.sha256()
        count = 0
        size = 0
        with tempfile.TemporaryDirectory() as folder, local.open("rb") as stream:
            part = Path(folder) / "part"
            while block := stream.read(CHUNK_BYTES):
                digest.update(block)
                size += len(block)
                part.write_bytes(block)
                self._upload_part(part, f"{prefix}.{count}")
                count += 1
        self.execute(
            textwrap.dedent(f"""
            import hashlib
            from pathlib import Path
            prefix = {prefix!r}
            target = Path({target!r})
            def file_digest(path):
                actual = hashlib.sha256()
                with path.open('rb') as stream:
                    while block := stream.read(1024*1024):
                        actual.update(block)
                return actual.hexdigest()
            if not target.exists() or file_digest(target) != {digest.hexdigest()!r}:
                actual = hashlib.sha256()
                with open(prefix, 'wb') as output:
                    for i in range({count}):
                        with open(f'{{prefix}}.{{i}}', 'rb') as stream:
                            while block := stream.read(1024*1024):
                                actual.update(block)
                                output.write(block)
                if actual.hexdigest() != {digest.hexdigest()!r}:
                    raise IOError('upload sha256 mismatch')
                Path(prefix).replace(target)
            for i in range({count}):
                Path(f'{{prefix}}.{{i}}').unlink(missing_ok=True)
        """),
            timeout=max(300, 60 + 3 * size / MERGE_BYTES_PER_SECOND),
            restart_on_retry=True,
        )

    def download(self, remote, local, *, timeout=180):
        """Publish only a verified result and expose the unfinished request for a retry."""
        local = Path(local)
        request = (remote, local)
        if self.pending_result and self.pending_result != request:
            raise RuntimeError("retry the pending result download first")
        self.pending_result = request
        remote = str(PurePosixPath("/") / remote)
        stage = remote + ".tts-" + self.name
        self.execute(
            textwrap.dedent(f"""
            import hashlib, json
            from pathlib import Path
            stage = Path({stage!r})
            stage.mkdir(exist_ok=True)
            manifest = stage / 'manifest'
            if not manifest.exists():
                digest = hashlib.sha256()
                parts = []
                with open({remote!r}, 'rb') as source:
                    while block := source.read({CHUNK_BYTES}):
                        (stage / str(len(parts))).write_bytes(block)
                        parts.append(hashlib.sha256(block).hexdigest())
                        digest.update(block)
                tmp = stage / 'manifest.tmp'
                tmp.write_text(json.dumps([digest.hexdigest(), parts]))
                tmp.replace(manifest)
        """),
            timeout=600,
        )
        with tempfile.TemporaryDirectory(dir=local.parent) as folder:
            part = Path(folder) / "part"
            self._fetch_part(stage + "/manifest", part, timeout)
            expected, checksums = json.loads(part.read_text())
            assembled = Path(folder) / "assembled"
            digest = hashlib.sha256()
            with assembled.open("wb") as dest:
                for i, checksum in enumerate(checksums):
                    data = self._fetch_part(f"{stage}/{i}", part, timeout, checksum)
                    digest.update(data)
                    dest.write(data)
            if digest.hexdigest() != expected:
                raise CliError("result sha256 mismatch")
            os.replace(assembled, local)
        self.pending_result = None
        try:
            self.execute(f"import shutil\nshutil.rmtree({stage!r}, ignore_errors=True)")
        except CliError:
            pass  # VM teardown also removes the temporary parts; the result is published.

    def _fetch_part(self, remote, local, timeout, checksum=None):
        for attempt in range(3):
            try:
                self._transfer("download", remote, local, timeout)
                if checksum is None:
                    return
                block = local.read_bytes()
                if hashlib.sha256(block).hexdigest() != checksum:
                    raise CliError("result part sha256 mismatch")
                return block
            except CliError:
                if attempt == 2:
                    raise
                time.sleep(self._timeout(RETRY_SECONDS))

    def run_background(self, cmd, done_marker, *, heartbeat=None):
        self.heartbeat = heartbeat
        self.marker = str(PurePosixPath("/") / done_marker)
        marker = shlex.quote(self.marker)
        # Publish a complete exit status, including a shell parse failure.
        shell = f"bash -c {shlex.quote(cmd)}; rc=$?; echo $rc > {marker}.tmp && mv {marker}.tmp {marker}"
        self.launch = textwrap.dedent(f"""
            import os, subprocess
            from pathlib import Path
            mark = Path({self.marker!r})
            mark.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(str(mark)+'.started', os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                try:
                    with open(str(mark)+'.log', 'ab') as log:
                        subprocess.Popen(['bash', '-c', {shell!r}], stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log, start_new_session=True)
                except OSError:
                    Path(str(mark)+'.started').unlink()
                    raise
        """)
        self.confirmed = False
        self.download_failed_since = None
        self._launch()

    def _launch(self):
        self.next_launch = self.pool.clock() + 60
        try:
            self.execute(self.launch)
        except CliError:
            return
        self.confirmed = True

    def poll(self):
        self._timeout(90)
        if not self.marker:
            return RunState.RUNNING
        now = self.pool.clock()
        if not self.confirmed and now >= self.next_launch:
            self._launch()
        self._keepalive()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "marker"
            try:
                self._transfer("download", self.marker, target, timeout=20)
            except CliError:
                pass
            else:
                self.download_failed_since = None
                return (
                    RunState.DONE
                    if target.read_text().strip() == "0"
                    else RunState.FAILED
                )
            try:
                self._transfer("download", self.marker + ".started", target, timeout=20)
            except CliError:
                if self.download_failed_since is None:
                    self.download_failed_since = now
                if self.heartbeat:
                    try:
                        self._transfer("download", self.heartbeat, target, timeout=20)
                    except CliError:
                        pass
                    else:
                        self.download_failed_since = None
            else:
                self.confirmed = True
                self.download_failed_since = None
        if (
            self.download_failed_since is not None
            and now - self.download_failed_since >= 900
        ):
            try:
                endpoints = self.pool.cli.api(self.account_id, "ps")
            except CliError:
                return RunState.RUNNING
            if self.endpoint not in endpoints:
                return RunState.GONE
        return RunState.RUNNING

    def wait(self, cancel=None):
        while (state := self.poll()) == RunState.RUNNING:
            delay = self._timeout(30)
            if cancel is not None:
                if cancel.wait(delay):
                    raise RuntimeError("job cancelled")
            else:
                time.sleep(delay)
        return state

    def stop(self):
        self.pool.stop(self.account_id, self.name, self.task_id)
        self.closed = True
