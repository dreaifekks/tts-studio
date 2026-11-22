"""Install the CLI sidecar selected for this control host."""

import argparse
import subprocess
import sys
from pathlib import Path


def ready(root: Path) -> bool:
    """装好了：venv 在、依赖能导入（创建 venv 后同步依赖失败的半成品不算，下次照样重装）。"""
    python = root / "colab-venv" / "bin" / "python"
    return python.exists() and subprocess.run(
        [str(python), "-c", "import colab_cli, keyring"], capture_output=True
    ).returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.home() / ".config/tts-core")
    root = parser.parse_args().root
    platform = {"linux": "linux", "darwin": "macos"}[sys.platform]
    lock = (
        Path(__file__).resolve().parents[3]
        / "requirements"
        / f"colab-{platform}.lock.txt"
    )
    venv = root / "colab-venv"
    python = venv / "bin/python"
    if not python.exists():
        subprocess.run(["uv", "venv", "--python", "3.12", str(venv)], check=True)
    subprocess.run(
        ["uv", "pip", "sync", "--python", str(python), str(lock)], check=True
    )


if __name__ == "__main__":
    main()
