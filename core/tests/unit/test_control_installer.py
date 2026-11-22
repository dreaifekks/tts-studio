"""Unit tests for macOS local control core install and uninstall scripts."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SCRIPT = SCRIPTS_DIR / "install_control_macos.sh"
UNINSTALL_SCRIPT = SCRIPTS_DIR / "uninstall_control_macos.sh"
MAC_INSTALL_SCRIPT = REPO_ROOT / "mac" / "script" / "install.sh"
MAC_UPDATE_SCRIPT = REPO_ROOT / "mac" / "script" / "update.sh"
MAC_STAMP_SCRIPT = REPO_ROOT / "mac" / "script" / "stamp-version.sh"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


PATCH_REL = "patches/gptsovits-bf81cdb.patch"
SAMPLE_PATCH = b"""diff --git a/base.txt b/base.txt
--- a/base.txt
+++ b/base.txt
@@ -1 +1 @@
-base
+studio patch
"""


def _make_bundle(
    tmp_path: Path,
    version: str,
    *,
    with_web: bool = True,
    with_gsv_patch: bool = True,
    legacy_manifest: bool = False,
    gsv_base: str = "bf81cdb14a38b674b6e9996dabc97340bc9978d2",
) -> tuple[Path, str]:
    """Create a Core bundle with an optional embedded patch and Web archive."""
    bundle_dir = tmp_path / f"bundle-{version}-{int(with_web)}-{int(with_gsv_patch)}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    src_repo = tmp_path / f"src-{version}-{int(with_web)}-{int(with_gsv_patch)}"
    src_repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "core", str(src_repo)], check=True)
    (src_repo / "ttsapi").mkdir(parents=True, exist_ok=True)
    (src_repo / "ttsapi" / "marker.txt").write_text(f"version={version}\n", encoding="utf-8")
    (src_repo / "scripts").mkdir(parents=True, exist_ok=True)
    (src_repo / "scripts" / "pretrained.sha256").write_text(
        "# engine sha256 rel url\n",
        encoding="utf-8",
    )
    shutil.copy2(UNINSTALL_SCRIPT, src_repo / "scripts" / "uninstall_control_macos.sh")

    shutil.copy2(SCRIPTS_DIR / "gptsovits.sh", src_repo / "scripts" / "gptsovits.sh")
    if with_gsv_patch:
        (src_repo / PATCH_REL).parent.mkdir()
        (src_repo / PATCH_REL).write_bytes(SAMPLE_PATCH)

    _git(src_repo, "add", ".")
    _git(
        src_repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "-q",
        "-m",
        f"tts-core {version}",
    )
    commit = _git(src_repo, "rev-parse", "HEAD")
    _git(src_repo, "tag", f"v{version}", commit)
    bundle_name = "core.bundle" if legacy_manifest else "core-head.bundle"
    _git(src_repo, "bundle", "create", str(bundle_dir / bundle_name), "HEAD", "--tags")

    has_web = "0"
    if with_web:
        web_stage = tmp_path / f"web-stage-{version}-{int(with_gsv_patch)}"
        web_stage.mkdir(parents=True, exist_ok=True)
        (web_stage / "index.html").write_text(f"<h1>TTS Studio {version}</h1>\n", encoding="utf-8")
        with tarfile.open(bundle_dir / "web-dist.tar.gz", "w:gz") as tar:
            tar.add(web_stage / "index.html", arcname="index.html")
        has_web = "1"

    version_field = "CORE_VERSION" if legacy_manifest else "VERSION"
    patch_sha = hashlib.sha256(SAMPLE_PATCH).hexdigest() if with_gsv_patch else ""
    manifest = "code.env" if legacy_manifest else "bundle.env"
    (bundle_dir / manifest).write_text(
        f"{version_field}={version}\nCORE_COMMIT={commit}\nGSV_BASE={gsv_base}\nGSV_BRANCH=tts-studio\n"
        f"GSV_PATCH_REL={PATCH_REL}\nGSV_PATCH_SHA256={patch_sha}\nHAS_WEB_DIST={has_web}\n",
        encoding="utf-8",
    )
    return bundle_dir, commit


def _run_install(
    bundle_dir: Path,
    root: Path,
    launch_agents: Path,
    *,
    extra_args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["TTS_LAUNCH_AGENTS_DIR"] = str(launch_agents)
    if extra_env:
        env.update(extra_env)
    cmd = [
        "bash",
        str(INSTALL_SCRIPT),
        "--bundle",
        str(bundle_dir),
        "--root",
        str(root),
        "--skip-heavy",
        *(extra_args or []),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        result[k.strip()] = v.strip()
    return result


def test_scripts_bash_syntax():
    for script in (INSTALL_SCRIPT, UNINSTALL_SCRIPT, MAC_INSTALL_SCRIPT, MAC_UPDATE_SCRIPT, MAC_STAMP_SCRIPT):
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert res.returncode == 0, f"{script.name} syntax error: {res.stderr}"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS control installer test")
def test_first_install_atomic_layout_and_progress(tmp_path: Path):
    root = tmp_path / "Application Support" / "TTSStudio" / "core"
    launch_agents = tmp_path / "LaunchAgents"
    bundle_dir, commit = _make_bundle(tmp_path, "0.9.1", with_web=True)

    res = _run_install(bundle_dir, root, launch_agents)
    assert res.returncode == 0, f"stderr:\n{res.stderr}\nstdout:\n{res.stdout}"

    lines = res.stdout.splitlines()
    progress_lines = [line for line in lines if line.startswith("@@PROGRESS ")]
    assert len(progress_lines) == 6
    for idx, line in enumerate(progress_lines, start=1):
        match = re.fullmatch(r"@@PROGRESS (\d+) 6 (0\.\d+|1\.0) (.+)", line)
        assert match is not None, f"unexpected progress line: {line}"
        assert int(match.group(1)) == idx

    assert "@@SWITCHING" in lines
    assert lines.index("@@SWITCHING") < lines.index("@@DONE 0.9.1 9880")
    assert not (root / ".rollback.json").exists()

    current = root / "current"
    assert current.is_symlink()
    rel = Path(os.readlink(current))
    assert rel.parent == root / "releases"
    assert (rel / "core" / "ttsapi" / "marker.txt").read_text(encoding="utf-8") == "version=0.9.1\n"
    assert _git(rel / "core", "rev-parse", "HEAD") == commit
    assert _git(rel / "core", "rev-parse", "--abbrev-ref", "HEAD") == "core"
    assert _git(rel / "core", "describe", "--tags", "--exact-match") == "v0.9.1"
    assert _git(rel / "core", "remote", "get-url", "origin") == "https://github.com/dreaifekks/tts-studio.git"
    assert (rel / "web" / "dist" / "index.html").is_file()
    assert (root / "uninstall.sh").is_file()
    assert os.access(root / "uninstall.sh", os.X_OK)

    env_map = _parse_env(root / "tts-core.env")
    assert env_map["TTSAPI_ROLE"] == "control"
    assert env_map["TTSAPI_HOST"] == "0.0.0.0"
    assert env_map["TTSAPI_PORT"] == "9880"
    assert env_map["TTSAPI_WORKER_PORT_SHIFT"] == "100"
    assert env_map["TTSAPI_DATA_DIR"] == str(root / "data")
    assert env_map["TTSAPI_COLAB_ROOT"] == str(root / "colab")
    assert env_map["TTSAPI_WEB_DIR"] == str(rel / "web" / "dist")
    assert env_map["TTSAPI_MACHINE_NAME"]

    plist_path = launch_agents / "com.dreaife.tts-core.plist"
    assert plist_path.is_file()
    with plist_path.open("rb") as fh:
        plist = plistlib.load(fh)
    assert plist["Label"] == "com.dreaife.tts-core"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ThrottleInterval"] == 5
    assert plist["WorkingDirectory"] == str(rel / "core")
    assert "/opt/homebrew/bin" in plist["EnvironmentVariables"]["PATH"]
    assert str(root / "bin") in plist["EnvironmentVariables"]["PATH"]
    assert plist["EnvironmentVariables"]["TTSAPI_PORT"] == "9880"
    assert plist["EnvironmentVariables"]["PYTHONPYCACHEPREFIX"] == str(rel / "runtime" / "pycache")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS control installer test")
def test_takeover_existing_trial_plist_preserves_all_user_settings_and_rejects_corrupt_plist(tmp_path: Path):
    root = tmp_path / "Application Support" / "TTSStudio" / "core"
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / "com.dreaife.tts-core.plist"

    # Corrupt existing plist must abort BEFORE making any changes.
    plist_path.write_bytes(b"<<<not a valid plist>>>")
    bundle_dir, _ = _make_bundle(tmp_path, "0.9.5", with_web=False)
    bad = _run_install(bundle_dir, root, launch_agents)
    assert bad.returncode != 0
    assert plist_path.read_bytes() == b"<<<not a valid plist>>>"
    assert not (root / "current").exists()

    # Simulate an existing ~/tts-core-trial service on port 9980 with custom paths & tuning.
    ext_web = tmp_path / "trial-web" / "dist"
    ext_web.mkdir(parents=True, exist_ok=True)
    (ext_web / "index.html").write_text("<h1>Trial Web</h1>", encoding="utf-8")

    trial_plist = {
        "Label": "com.dreaife.tts-core",
        "ProgramArguments": ["/Users/demo/dev/GPT-SoVITS/.venv/bin/python", "-m", "ttsapi.gateway"],
        "WorkingDirectory": "/Users/demo/tts-core-trial/core",
        "EnvironmentVariables": {
            "TTSAPI_ROLE": "control",
            "TTSAPI_PORT": "9980",
            "TTSAPI_HOST": "100.64.0.10",
            "TTSAPI_LIBRARY": "/Users/demo/My Drive/ai/tts-library-dev",
            "TTSAPI_DATA_DIR": "/Users/demo/tts-core-trial/data",
            "TTSAPI_COLAB_ROOT": "/Users/demo/tts-core-trial/colab",
            "TTSAPI_DATASET_ROOT": "/Users/demo/tts-core-trial/datasets",
            "TTSAPI_TRAIN_DIR": "/Users/demo/tts-core-trial/training",
            "TTSAPI_WEIGHTS_DIR": "/Users/demo/tts-core-trial/weights",
            "TTSAPI_DEVICE_V2PRO": "cpu",
            "TTSAPI_TORCH_THREADS": "6",
            "TTSAPI_WEB_DIR": str(ext_web),
            "TTSAPI_GSV_ROOT": "/Users/demo/dev/GPT-SoVITS",
            "TTSAPI_PYTHON": "/Users/demo/dev/GPT-SoVITS/.venv/bin/python",
            "PYTHONPATH": "/Users/demo/tts-core-trial/core",
        },
        "RunAtLoad": True,
    }
    with plist_path.open("wb") as fh:
        plistlib.dump(trial_plist, fh)

    probe_file = tmp_path / "probe_host.txt"
    res = _run_install(
        bundle_dir,
        root,
        launch_agents,
        extra_env={"TTS_HEALTH_CHECK_CMD": f'printf "%s:%s" "$PROBE_HOST" "$PORT" > "{probe_file}"'},
    )
    assert res.returncode == 0, f"stderr:\n{res.stderr}\nstdout:\n{res.stdout}"
    assert "@@DONE 0.9.5 9980" in res.stdout.splitlines()
    # Health check must probe the configured listen host (not hardcode 127.0.0.1 when bound to Tailscale IP).
    assert probe_file.read_text(encoding="utf-8") == "100.64.0.10:9980"

    new_rel = Path(os.readlink(root / "current"))
    env_map = _parse_env(root / "tts-core.env")
    assert env_map["TTSAPI_PORT"] == "9980"
    assert env_map["TTSAPI_HOST"] == "100.64.0.10"
    assert env_map["TTSAPI_LIBRARY"] == "/Users/demo/My Drive/ai/tts-library-dev"
    assert env_map["TTSAPI_DATA_DIR"] == "/Users/demo/tts-core-trial/data"
    assert env_map["TTSAPI_COLAB_ROOT"] == "/Users/demo/tts-core-trial/colab"
    assert env_map["TTSAPI_DATASET_ROOT"] == "/Users/demo/tts-core-trial/datasets"
    assert env_map["TTSAPI_TRAIN_DIR"] == "/Users/demo/tts-core-trial/training"
    assert env_map["TTSAPI_WEIGHTS_DIR"] == "/Users/demo/tts-core-trial/weights"
    assert env_map["TTSAPI_DEVICE_V2PRO"] == "cpu"
    assert env_map["TTSAPI_TORCH_THREADS"] == "6"
    # Existing web dist from old plist TTSAPI_WEB_DIR was copied into the new release.
    assert (new_rel / "web" / "dist" / "index.html").read_text(encoding="utf-8") == "<h1>Trial Web</h1>"
    assert env_map["TTSAPI_WEB_DIR"] == str(new_rel / "web" / "dist")
    # Code/runtime fields must point to the new release.
    assert env_map["TTSAPI_GSV_ROOT"] == str(new_rel / "GPT-SoVITS")
    assert env_map["TTSAPI_PYTHON"] == str(new_rel / "GPT-SoVITS" / ".venv" / "bin" / "python")

    with plist_path.open("rb") as fh:
        new_plist = plistlib.load(fh)
    assert new_plist["EnvironmentVariables"]["PYTHONPATH"] == str(new_rel / "core")
    assert new_plist["EnvironmentVariables"]["TTSAPI_LIBRARY"] == "/Users/demo/My Drive/ai/tts-library-dev"
    assert new_plist["EnvironmentVariables"]["TTSAPI_PORT"] == "9980"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS control installer test")
def test_takeover_inherits_implicit_legacy_data_web_and_gsv_fork_from_old_plist(tmp_path: Path):
    root = tmp_path / "Application Support" / "TTSStudio" / "core"
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)

    # Create a legacy checkout layout: <legacy>/core/data/tts.db + <legacy>/web/dist/index.html + external GSV fork.
    legacy = tmp_path / "legacy-studio"
    legacy_core = legacy / "core"
    (legacy_core / "data").mkdir(parents=True, exist_ok=True)
    (legacy_core / "data" / "tts.db").write_text("legacy-db", encoding="utf-8")
    (legacy / "web" / "dist").mkdir(parents=True, exist_ok=True)
    (legacy / "web" / "dist" / "index.html").write_text("<h1>Legacy Sibling Web</h1>", encoding="utf-8")

    gsv_old = tmp_path / "legacy-gsv"
    gsv_old.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "tts-studio", str(gsv_old)], check=True)
    (gsv_old / "base.txt").write_text("base\n", encoding="utf-8")
    _git(gsv_old, "add", ".")
    _git(gsv_old, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "base")
    base_sha = _git(gsv_old, "rev-parse", "HEAD")
    (gsv_old / "fork.txt").write_text("fork\n", encoding="utf-8")
    _git(gsv_old, "add", ".")
    _git(gsv_old, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "fork")

    # Legacy plist omits TTSAPI_DATA_DIR and TTSAPI_WEB_DIR (relying on core's implicit defaults).
    plist_path = launch_agents / "com.dreaife.tts-core.plist"
    legacy_plist = {
        "Label": "com.dreaife.tts-core",
        "WorkingDirectory": str(legacy_core),
        "EnvironmentVariables": {
            "TTSAPI_PORT": "9982",
            "TTSAPI_GSV_ROOT": str(gsv_old),
        },
    }
    with plist_path.open("wb") as fh:
        plistlib.dump(legacy_plist, fh)

    # Upgrade without an embedded Web archive and with the earlier manifest filename.
    bundle_dir, _ = _make_bundle(tmp_path, "0.9.6", with_web=False, legacy_manifest=True, gsv_base=base_sha)
    res = _run_install(bundle_dir, root, launch_agents)
    assert res.returncode == 0, f"stderr:\n{res.stderr}\nstdout:\n{res.stdout}"

    new_rel = Path(os.readlink(root / "current"))
    env_map = _parse_env(root / "tts-core.env")
    assert env_map["TTSAPI_DATA_DIR"] == str(legacy_core / "data")
    assert (new_rel / "web" / "dist" / "index.html").read_text(encoding="utf-8") == "<h1>Legacy Sibling Web</h1>"
    assert env_map["TTSAPI_WEB_DIR"] == str(new_rel / "web" / "dist")
    new_gsv = new_rel / "GPT-SoVITS"
    assert _git(new_gsv, "rev-parse", "HEAD^") == base_sha
    assert _git(new_gsv, "branch", "--show-current") == "tts-studio"
    assert (new_gsv / "base.txt").read_text() == "studio patch\n"
    assert not (new_gsv / "fork.txt").exists()
    assert (gsv_old / "fork.txt").is_file()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS control installer test")
def test_upgrade_strips_quotes_preserves_old_web_and_hardlinks_models(tmp_path: Path):
    root = tmp_path / "Application Support" / "TTSStudio" / "core"
    launch_agents = tmp_path / "LaunchAgents"
    bundle_v1, _ = _make_bundle(tmp_path, "0.9.1", with_web=True)
    res1 = _run_install(bundle_v1, root, launch_agents)
    assert res1.returncode == 0, res1.stderr

    old_rel = Path(os.readlink(root / "current"))

    # Write quoted and bare user env values, data files, and a G2PWModel file.
    env_file = root / "tts-core.env"
    env_text = env_file.read_text(encoding="utf-8").replace("TTSAPI_PORT=9880", "TTSAPI_PORT='9980'")
    env_file.write_text(
        env_text
        + 'TTSAPI_LIBRARY="/Users/demo/My Drive/ai/tts-library"\n'
        + "TTSAPI_TORCH_THREADS='6'\n",
        encoding="utf-8",
    )
    (root / "data" / "keep.txt").write_text("user-data", encoding="utf-8")
    (root / "colab" / "accounts.json").write_text("{}", encoding="utf-8")
    g2pw_file = old_rel / "GPT-SoVITS" / "GPT_SoVITS" / "text" / "G2PWModel" / "model.onnx"
    g2pw_file.parent.mkdir(parents=True, exist_ok=True)
    g2pw_file.write_bytes(b"g2pw-weights")
    old_inode = g2pw_file.stat().st_ino

    # Upgrade with a bundle that omits web-dist.tar.gz: old web/dist MUST be copied forward.
    bundle_v2, _ = _make_bundle(tmp_path, "0.9.2", with_web=False)
    res2 = _run_install(bundle_v2, root, launch_agents)
    assert res2.returncode == 0, f"stderr:\n{res2.stderr}\nstdout:\n{res2.stdout}"
    assert "@@DONE 0.9.2 9980" in res2.stdout.splitlines()

    new_rel = Path(os.readlink(root / "current"))
    assert new_rel != old_rel
    assert not old_rel.exists()
    assert list((root / "releases").iterdir()) == [new_rel]

    # Preserved user data & stripped quoted values.
    assert (root / "data" / "keep.txt").read_text(encoding="utf-8") == "user-data"
    assert (root / "colab" / "accounts.json").read_text(encoding="utf-8") == "{}"
    env_map = _parse_env(env_file)
    assert env_map["TTSAPI_PORT"] == "9980"
    assert env_map["TTSAPI_LIBRARY"] == "/Users/demo/My Drive/ai/tts-library"
    assert env_map["TTSAPI_TORCH_THREADS"] == "6"
    assert (new_rel / "web" / "dist" / "index.html").read_text(encoding="utf-8") == "<h1>TTS Studio 0.9.1</h1>\n"
    assert env_map["TTSAPI_WEB_DIR"] == str(new_rel / "web" / "dist")

    # Bare (unquoted) TTSAPI_LIBRARY works identically on a subsequent upgrade.
    bundle_v3, _ = _make_bundle(tmp_path, "0.9.3", with_web=False)
    res3 = _run_install(bundle_v3, root, launch_agents)
    assert res3.returncode == 0, res3.stderr
    assert _parse_env(env_file)["TTSAPI_LIBRARY"] == "/Users/demo/My Drive/ai/tts-library"

    v3_rel = Path(os.readlink(root / "current"))
    new_g2pw = v3_rel / "GPT-SoVITS" / "GPT_SoVITS" / "text" / "G2PWModel" / "model.onnx"
    assert new_g2pw.is_file()
    assert new_g2pw.stat().st_ino == old_inode


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS control installer test")
def test_flock_blocks_concurrent_process_and_auto_releases_after_sigkill(tmp_path: Path):
    root = tmp_path / "Application Support" / "TTSStudio" / "core"
    launch_agents = tmp_path / "LaunchAgents"
    bundle_dir, _ = _make_bundle(tmp_path, "0.9.1", with_web=False)

    # Start installer #1 and pause it inside TTS_HEALTH_CHECK_CMD while holding kernel flock on fd 9.
    started_flag = tmp_path / "lock-held.flag"
    release_flag = tmp_path / "lock-release.flag"
    env1 = dict(os.environ)
    env1["TTS_LAUNCH_AGENTS_DIR"] = str(launch_agents)
    env1["TTS_HEALTH_CHECK_CMD"] = (
        f'touch "{started_flag}"; while [ ! -f "{release_flag}" ]; do sleep 0.05; done'
    )
    proc1 = subprocess.Popen(
        [
            "bash",
            str(INSTALL_SCRIPT),
            "--bundle",
            str(bundle_dir),
            "--root",
            str(root),
            "--skip-heavy",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env1,
        start_new_session=True,
    )
    try:
        for _ in range(100):
            if started_flag.exists():
                break
            threading.Event().wait(0.05)
        assert started_flag.exists(), "First installer did not reach health check"

        # Second installer must fail immediately due to kernel flock contention.
        res2 = _run_install(bundle_dir, root, launch_agents)
        assert res2.returncode != 0
        assert "已有另一安装或卸载正在进行" in res2.stderr

        # Uninstaller must also respect the same kernel flock.
        un_env = dict(os.environ)
        un_env["TTS_LAUNCH_AGENTS_DIR"] = str(launch_agents)
        un_env["TTS_SKIP_LAUNCHCTL"] = "1"
        res_un = subprocess.run(
            ["bash", str(UNINSTALL_SCRIPT), "--root", str(root)],
            capture_output=True,
            text=True,
            env=un_env,
        )
        assert res_un.returncode != 0
        assert "已有另一安装或卸载正在进行" in res_un.stderr

        # Hard-kill the first installer's process group (SIGKILL): kernel releases flock immediately.
        os.killpg(proc1.pid, signal.SIGKILL)
        proc1.wait(timeout=10)
    finally:
        if proc1.poll() is None:
            os.killpg(proc1.pid, signal.SIGKILL)
            proc1.wait(timeout=5)

    # Next installer recovers the pending rollback marker left by SIGKILL and succeeds immediately!
    res3 = _run_install(bundle_dir, root, launch_agents)
    assert res3.returncode == 0, f"stderr:\n{res3.stderr}\nstdout:\n{res3.stdout}"
    assert "@@DONE 0.9.1 9880" in res3.stdout.splitlines()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS control installer test")
def test_rollback_preserves_backups_on_bootstrap_failure_and_recovers_on_next_run(tmp_path: Path):
    root = tmp_path / "Application Support" / "TTSStudio" / "core"
    launch_agents = tmp_path / "LaunchAgents"
    bundle_v1, _ = _make_bundle(tmp_path, "0.9.1", with_web=True)
    res1 = _run_install(bundle_v1, root, launch_agents, extra_args=["--port", "9981"])
    assert res1.returncode == 0, res1.stderr

    plist_path = launch_agents / "com.dreaife.tts-core.plist"
    v1_plist_bytes = plist_path.read_bytes()
    v1_env_text = (root / "tts-core.env").read_text(encoding="utf-8")
    v1_rel = Path(os.readlink(root / "current"))

    # Create a mock launchctl: fails bootstrap when fail_flag exists (after v2 starts & fails health).
    fail_bootstrap_flag = tmp_path / "fail_rollback_bootstrap.flag"
    bootstrap_calls_file = tmp_path / "bootstrap_calls.log"
    mock_launchctl = tmp_path / "mock-launchctl"
    mock_launchctl.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
cmd="${{1:-}}"
if [ "$cmd" = "bootstrap" ]; then
  echo "bootstrap:$3" >> "{bootstrap_calls_file}"
  count="$(wc -l < "{bootstrap_calls_file}" | tr -d ' ')"
  if [ -f "{fail_bootstrap_flag}" ] && [ "$count" -ge 2 ]; then
    echo "simulated bootstrap failure" >&2
    exit 1
  fi
  exit 0
elif [ "$cmd" = "bootout" ]; then
  exit 0
elif [ "$cmd" = "print" ]; then
  exit 1
fi
exit 0
""",
        encoding="utf-8",
    )
    mock_launchctl.chmod(0o755)
    fail_bootstrap_flag.write_text("1", encoding="utf-8")

    # Attempt v2 upgrade: health check fails -> rollback tries to bootstrap v1 -> mock fails bootstrap!
    bundle_v2, _ = _make_bundle(tmp_path, "0.9.2", with_web=True)
    res_fail = _run_install(
        bundle_v2,
        root,
        launch_agents,
        extra_env={
            "TTS_LAUNCHCTL_BIN": str(mock_launchctl),
            "TTS_HEALTH_CHECK_CMD": "exit 1",
        },
    )
    assert res_fail.returncode != 0

    # Because rollback bootstrap failed, .rollback.json AND its backup files MUST remain intact!
    rollback_file = root / ".rollback.json"
    assert rollback_file.is_file()
    rb_doc = json.loads(rollback_file.read_text(encoding="utf-8"))
    assert Path(rb_doc["service_prev"]).is_file()
    assert Path(rb_doc["env_prev"]).is_file()
    assert Path(rb_doc["service_prev"]).read_bytes() == v1_plist_bytes
    assert Path(rb_doc["env_prev"]).read_text(encoding="utf-8") == v1_env_text

    # Corrupt live plist to prove next run actually restores from backups in .rollback.json first!
    plist_path.write_bytes(b"corrupted-by-interrupted-switch")
    fail_bootstrap_flag.unlink()

    res_recover = _run_install(
        bundle_v2,
        root,
        launch_agents,
        extra_env={"TTS_LAUNCHCTL_BIN": str(mock_launchctl)},
    )
    assert res_recover.returncode == 0, f"stderr:\n{res_recover.stderr}\nstdout:\n{res_recover.stdout}"
    assert not rollback_file.exists()
    assert not Path(rb_doc["release"]).exists()
    assert "@@DONE 0.9.2 9981" in res_recover.stdout.splitlines()
    assert Path(os.readlink(root / "current")) != v1_rel


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS control installer test")
def test_non_symlink_current_directory_is_rejected(tmp_path: Path):
    root = tmp_path / "Application Support" / "TTSStudio" / "core"
    launch_agents = tmp_path / "LaunchAgents"
    current_dir = root / "current"
    current_dir.mkdir(parents=True, exist_ok=True)
    sentinel = current_dir / "important.txt"
    sentinel.write_text("do-not-touch", encoding="utf-8")

    bundle_dir, _ = _make_bundle(tmp_path, "0.9.1", with_web=False)
    res = _run_install(bundle_dir, root, launch_agents)
    assert res.returncode != 0
    assert "non-symlink" in res.stderr
    assert current_dir.is_dir() and not current_dir.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "do-not-touch"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS control installer test")
def test_health_check_validates_status_version_and_7char_commit(tmp_path: Path):
    root = tmp_path / "Application Support" / "TTSStudio" / "core"
    launch_agents = tmp_path / "LaunchAgents"
    bundle_v1, _ = _make_bundle(tmp_path, "0.9.1", with_web=True)
    res1 = _run_install(bundle_v1, root, launch_agents, extra_args=["--port", "9981"])
    assert res1.returncode == 0, res1.stderr

    plist_path = launch_agents / "com.dreaife.tts-core.plist"
    v1_plist_bytes = plist_path.read_bytes()
    v1_env_text = (root / "tts-core.env").read_text(encoding="utf-8")
    v1_rel = Path(os.readlink(root / "current"))

    bundle_v2, v2_commit = _make_bundle(tmp_path, "0.9.2", with_web=True)
    payload_holder: dict[str, dict] = {"body": {}}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = json.dumps(payload_holder["body"]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # noqa: A002
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Reject: wrong commit, 1-char commit prefix, missing version, wrong version.
        bad_payloads = [
            {"status": "ok", "commit": "0000000", "version": "0.9.2"},
            {"status": "ok", "commit": v2_commit[:1], "version": "0.9.2"},
            {"status": "ok", "commit": v2_commit[:7]},
            {"status": "ok", "commit": v2_commit[:7], "version": "0.9.1"},
        ]
        for bad in bad_payloads:
            payload_holder["body"] = bad
            res_bad = _run_install(
                bundle_v2,
                root,
                launch_agents,
                extra_env={
                    "TTS_HEALTH_URL": f"http://127.0.0.1:{port}/health",
                    "TTS_HEALTH_ATTEMPTS": "1",
                },
            )
            assert res_bad.returncode != 0, f"payload should have been rejected: {bad}"
            assert "@@DONE" not in res_bad.stdout
            assert plist_path.read_bytes() == v1_plist_bytes
            assert (root / "tts-core.env").read_text(encoding="utf-8") == v1_env_text
            assert Path(os.readlink(root / "current")) == v1_rel
            assert list((root / "releases").iterdir()) == [v1_rel]
            assert not (root / ".rollback.json").exists()

        # Accept: matching status, exact version, and >=7-char commit prefix.
        payload_holder["body"] = {"status": "ok", "commit": v2_commit[:7], "version": "0.9.2"}
        res_ok = _run_install(
            bundle_v2,
            root,
            launch_agents,
            extra_env={
                "TTS_HEALTH_URL": f"http://127.0.0.1:{port}/health",
                "TTS_HEALTH_ATTEMPTS": "2",
            },
        )
        assert res_ok.returncode == 0, f"stderr:\n{res_ok.stderr}\nstdout:\n{res_ok.stdout}"
        assert "@@DONE 0.9.2 9981" in res_ok.stdout.splitlines()
        assert Path(os.readlink(root / "current")) != v1_rel
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS control installer test")
def test_missing_gsv_patch_requires_explicit_upstream_fallback(tmp_path: Path):
    root = tmp_path / "Application Support" / "TTSStudio" / "core"
    launch_agents = tmp_path / "LaunchAgents"

    bundle_no_gsv, _ = _make_bundle(tmp_path, "0.9.1", with_web=False, with_gsv_patch=False)
    res_fail = _run_install(bundle_no_gsv, root, launch_agents)
    assert res_fail.returncode != 0
    assert "gptsovits-bf81cdb.patch" in res_fail.stderr

    res_allow = _run_install(bundle_no_gsv, root, launch_agents, extra_args=["--allow-upstream-gsv"])
    assert res_allow.returncode == 0, res_allow.stderr
    assert "Skipped remote GPT-SoVITS fetch" in res_allow.stdout
    old_rel = Path(os.readlink(root / "current"))
    gsv = old_rel / "GPT-SoVITS"
    (gsv / "base.txt").write_text("base\n", encoding="utf-8")
    _git(gsv, "add", ".")
    _git(gsv, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-qm", "base")
    base_sha = _git(gsv, "rev-parse", "HEAD")

    bundle_v2, _ = _make_bundle(tmp_path, "0.9.2", with_web=False, with_gsv_patch=False, gsv_base=base_sha)
    res_upg = _run_install(bundle_v2, root, launch_agents)
    assert res_upg.returncode != 0 and "gptsovits-bf81cdb.patch" in res_upg.stderr
    assert Path(os.readlink(root / "current")) == old_rel

    bad_bundle, _ = _make_bundle(tmp_path, "0.9.3", with_web=False, gsv_base=base_sha)
    env = bad_bundle / "bundle.env"
    env.write_text(env.read_text().replace(hashlib.sha256(SAMPLE_PATCH).hexdigest(), "0" * 64))
    bad_upgrade = _run_install(bad_bundle, root, launch_agents)
    assert bad_upgrade.returncode != 0 and "patch SHA-256 mismatch" in bad_upgrade.stderr
    assert Path(os.readlink(root / "current")) == old_rel

    res_allow = _run_install(bundle_v2, root, launch_agents, extra_args=["--allow-upstream-gsv"])
    assert res_allow.returncode == 0, res_allow.stderr
    new_rel = Path(os.readlink(root / "current"))
    assert _git(new_rel / "GPT-SoVITS", "rev-parse", "HEAD") == base_sha


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS control installer test")
def test_uninstall_checks_ownership_missing_plist_launchd_state_and_purge(tmp_path: Path):
    root = tmp_path / "Application Support" / "TTSStudio" / "core"
    launch_agents = tmp_path / "LaunchAgents"
    bundle_dir, _ = _make_bundle(tmp_path, "0.9.1", with_web=False)
    res = _run_install(bundle_dir, root, launch_agents)
    assert res.returncode == 0, res.stderr

    (root / "data" / "tts.db").write_text("db-content", encoding="utf-8")
    (root / "colab" / "state.json").write_text("{}", encoding="utf-8")
    (root / "python").mkdir(parents=True, exist_ok=True)
    (root / "python" / "stub").write_text("py", encoding="utf-8")

    # Mock launchctl backed by a state file so we can test live launchd service when plist is missing.
    loaded_flag = tmp_path / "service-loaded.flag"
    mock_launchctl = tmp_path / "mock-launchctl-un"
    mock_launchctl.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
cmd="${{1:-}}"
if [ "$cmd" = "print" ]; then
  [ -f "{loaded_flag}" ] && exit 0 || exit 1
elif [ "$cmd" = "bootout" ]; then
  rm -f "{loaded_flag}"
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    mock_launchctl.chmod(0o755)

    env = dict(os.environ)
    env["TTS_LAUNCH_AGENTS_DIR"] = str(launch_agents)
    env["TTS_LAUNCHCTL_BIN"] = str(mock_launchctl)
    env.pop("TTS_CORE_ROOT", None)
    env.pop("TTS_SKIP_LAUNCHCTL", None)

    plist_path = launch_agents / "com.dreaife.tts-core.plist"
    saved_plist = plist_path.read_bytes()

    # 1. External plist -> refuse without --force.
    external_plist = {
        "Label": "com.dreaife.tts-core",
        "WorkingDirectory": str(tmp_path / "tts-studio-run" / "core"),
        "EnvironmentVariables": {"PYTHONPATH": str(tmp_path / "tts-studio-run" / "core")},
    }
    with plist_path.open("wb") as fh:
        plistlib.dump(external_plist, fh)

    refused_ext = subprocess.run(["bash", str(root / "uninstall.sh")], capture_output=True, text=True, env=env)
    assert refused_ext.returncode != 0
    assert "Refusing to stop service" in refused_ext.stderr
    assert plist_path.is_file()
    assert (root / "current").exists()

    # 2. Missing plist while launchd still reports service loaded -> refuse without --force and preserve files!
    plist_path.unlink()
    loaded_flag.write_text("running", encoding="utf-8")
    refused_missing = subprocess.run(["bash", str(root / "uninstall.sh")], capture_output=True, text=True, env=env)
    assert refused_missing.returncode != 0
    assert "still loaded in launchd" in refused_missing.stderr
    assert loaded_flag.exists()
    assert (root / "current").exists()

    # 3. Restore owned plist; running $ROOT/uninstall.sh stops service, removes runtime, preserves data/colab.
    plist_path.write_bytes(saved_plist)
    un1 = subprocess.run(["bash", str(root / "uninstall.sh")], capture_output=True, text=True, env=env)
    assert un1.returncode == 0, un1.stderr
    assert not loaded_flag.exists()
    assert not plist_path.exists()
    assert not (root / "releases").exists()
    assert not (root / "current").exists()
    assert not (root / "bin").exists()
    assert not (root / "python").exists()
    assert (root / "data" / "tts.db").read_text(encoding="utf-8") == "db-content"
    assert (root / "colab" / "state.json").read_text(encoding="utf-8") == "{}"

    # 4. --purge removes $ROOT completely.
    un2 = subprocess.run(
        ["bash", str(UNINSTALL_SCRIPT), "--root", str(root), "--purge"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert un2.returncode == 0, un2.stderr
    assert not root.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS control installer test")
def test_control_installer_v5_engines_opt_in_and_env_plist_wiring(tmp_path: Path):
    """Installing without V5 creates no GPT-SoVITS-v5 and omits V5 env vars; installing with --engines containing
    v5turbo creates GPT-SoVITS-v5 and writes TTSAPI_GSV_V5_ROOT & TTSAPI_V5_PYTHON into tts-core.env and plist;
    upgrading later without V5 strips stale release-scoped V5 paths."""
    root = tmp_path / "Application Support" / "TTSStudio" / "core"
    launch_agents = tmp_path / "LaunchAgents"
    plist_path = launch_agents / "com.dreaife.tts-core.plist"

    # 1. Default engines (no V5): zero V5 directory or env vars.
    bundle_v1, _ = _make_bundle(tmp_path, "0.9.1", with_web=False)
    res1 = _run_install(bundle_v1, root, launch_agents, extra_args=["--port", "9981", "--engines", "v2pro,v2proplus"])
    assert res1.returncode == 0, res1.stderr
    rel1 = Path(os.readlink(root / "current"))
    assert not (rel1 / "GPT-SoVITS-v5").exists()
    env1 = (root / "tts-core.env").read_text(encoding="utf-8")
    assert "TTSAPI_GSV_V5_ROOT" not in env1 and "TTSAPI_V5_PYTHON" not in env1

    # 2. Install with v5turbo: creates release-scoped GPT-SoVITS-v5 and wires env + plist.
    bundle_v2, _ = _make_bundle(tmp_path, "0.9.2", with_web=False)
    res2 = _run_install(bundle_v2, root, launch_agents, extra_args=["--engines", "v2pro,v5turbo"])
    assert res2.returncode == 0, res2.stderr
    rel2 = Path(os.readlink(root / "current"))
    v5_root = rel2 / "GPT-SoVITS-v5"
    v5_py = v5_root / ".venv" / "bin" / "python"
    assert (v5_root / ".git").is_dir()
    env2 = (root / "tts-core.env").read_text(encoding="utf-8")
    assert f"TTSAPI_GSV_V5_ROOT={v5_root}" in env2
    assert f"TTSAPI_V5_PYTHON={v5_py}" in env2
    plist2 = plistlib.loads(plist_path.read_bytes())
    assert plist2["EnvironmentVariables"]["TTSAPI_GSV_V5_ROOT"] == str(v5_root)
    assert plist2["EnvironmentVariables"]["TTSAPI_V5_PYTHON"] == str(v5_py)

    # 3. Upgrade without V5: strips stale release-scoped V5 env vars.
    bundle_v3, _ = _make_bundle(tmp_path, "0.9.3", with_web=False)
    res3 = _run_install(bundle_v3, root, launch_agents, extra_args=["--engines", "v2pro"])
    assert res3.returncode == 0, res3.stderr
    rel3 = Path(os.readlink(root / "current"))
    assert not (rel3 / "GPT-SoVITS-v5").exists()
    env3 = (root / "tts-core.env").read_text(encoding="utf-8")
    assert "TTSAPI_GSV_V5_ROOT" not in env3 and "TTSAPI_V5_PYTHON" not in env3
    plist3 = plistlib.loads(plist_path.read_bytes())
    assert "TTSAPI_GSV_V5_ROOT" not in plist3["EnvironmentVariables"]
    assert "TTSAPI_V5_PYTHON" not in plist3["EnvironmentVariables"]
