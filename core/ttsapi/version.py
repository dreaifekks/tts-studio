"""版本号（从 git tag 算出来）和一键更新（快进到某个 tag；重启由 gateway 负责）。"""
import os
import re
import subprocess
import time
from datetime import datetime

from fastapi import HTTPException

from ttsapi import settings
from ttsapi.osutil import NO_WINDOW
from ttsapi.models import CoreVersion, StudioVersion, SystemVersion, SystemVersions

# gateway 以这个退出码退出时，start.ps1 不等 5 秒，立刻拉起新代码
RESTART_EXIT_CODE = 75
# 服务没有窗口：git 要凭据时不弹窗、不等终端输入，直接失败
ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
FETCH_TIMEOUT = 60


def run(*args, timeout=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=settings.ROOT, capture_output=True, text=True, encoding="utf-8", env=ENV,
                          creationflags=NO_WINDOW, timeout=timeout)


def git(*args) -> str:
    r = run(*args)
    r.check_returncode()
    return r.stdout.strip()


def describe():
    """(版本号, 短 sha)。在 tag v0.1.0 上是 0.1.0；之后的提交是 0.1.0-dev+<短 sha>；没有 tag 时基于 0.0.0。"""
    d = git("describe", "--tags", "--long", "--always", "--match", "v[0-9]*")
    m = re.fullmatch(r"v(.+)-(\d+)-g([0-9a-f]+)", d)
    if m is None:
        return f"0.0.0-dev+{d}", d
    base, ahead, sha = m.groups()
    return (base if ahead == "0" else f"{base}-dev+{sha}"), sha


# 启动时的代码版本：之后磁盘上的代码变了（更新了还没重启），这里仍是正在运行的版本
VERSION, COMMIT = describe()
HEAD = git("rev-parse", "HEAD")


def current() -> SystemVersion:
    dirty = git("status", "--porcelain", "--untracked-files=no") != ""
    return SystemVersion(version=VERSION, commit=COMMIT, dirty=dirty, branch=git("rev-parse", "--abbrev-ref", "HEAD"))


def fast_forward(tag: str) -> bool:
    """fetch 后把 master 快进到 tag。正在运行的就是这个版本时什么都不做，返回 False；快进成功（或磁盘上已经是
    这个 tag）返回 True，调用方随后重启。tag 不存在 404；不在 master、工作区脏、不能快进时 409，带 git 的原文。"""
    # 连上 GitHub 后对端不再响应时 git 会一直等，更新请求和「更新中」状态就一直卡着
    try:
        r = run("fetch", "--tags", "origin", timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise HTTPException(500, f"git fetch 超过 {FETCH_TIMEOUT} 秒没有完成")
    if r.returncode:
        raise HTTPException(500, f"git fetch 失败：{(r.stderr or r.stdout).strip()}")
    r = run("rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}")
    if r.returncode:
        raise HTTPException(404, f"tag {tag} 不存在")
    target = r.stdout.strip()
    # 提交相同但版本号不同（tag 是服务启动后才打的）时也要重启，不然 /health 的 version 一直是旧的
    if target == HEAD and VERSION == tag[1:]:
        return False
    st = current()
    if st.branch != "master":
        raise HTTPException(409, f"当前在 {st.branch} 分支上，只能在 master 上更新")
    if st.dirty:
        raise HTTPException(409, "工作区有没提交的改动，不能快进：\n" + git("status", "--short", "--untracked-files=no"))
    r = run("merge", "--ff-only", tag)
    if r.returncode:
        raise HTTPException(409, f"不能快进到 {tag}：\n{(r.stderr + r.stdout).strip()}")
    if git("rev-parse", "HEAD") != target:
        raise HTTPException(409, f"{tag} 比当前代码旧，快进不会改变代码：{r.stdout.strip()}")
    return True


# ---------------------------------------------------------------- 最新版本（GET /system/versions）

LATEST_TTL, LATEST_FAIL_TTL, LS_REMOTE_TIMEOUT = 600, 60, 20
_latest: dict = {}  # expires、checked_at、latest、notes_url、error


def release_url(tag: str):
    """origin 是 GitHub 时这个 tag 的 Release 页面，否则 None。"""
    r = run("remote", "get-url", "origin")
    m = re.match(r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$",
                 r.stdout.strip())
    return f"https://github.com/{m.group(1)}/releases/tag/{tag}" if r.returncode == 0 and m else None


def _check() -> dict:
    """git ls-remote --tags origin 取最新的 v*.*.*（用机器上已有的 git 凭据；不弹窗，失败直接返回原因）。"""
    try:
        r = run("ls-remote", "--tags", "origin", timeout=LS_REMOTE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return dict(latest=None, notes_url=None, error=f"git ls-remote 超过 {LS_REMOTE_TIMEOUT} 秒没有完成")
    if r.returncode:
        return dict(latest=None, notes_url=None, error=f"git ls-remote 失败：{(r.stderr or r.stdout).strip()[-300:]}")
    tags = {tuple(map(int, m)) for m in re.findall(r"refs/tags/v(\d+)\.(\d+)\.(\d+)$", r.stdout, re.M)}
    if not tags:
        return dict(latest=None, notes_url=None, error=None)
    tag = "v" + ".".join(map(str, max(tags)))
    return dict(latest=tag[1:], notes_url=release_url(tag), error=None)


def versions(refresh: bool = False) -> SystemVersions:
    """Studio 和 core 的版本。最新版本缓存 LATEST_TTL 秒（查失败时 LATEST_FAIL_TTL 秒），refresh 时重新查。"""
    if refresh or time.time() >= _latest.get("expires", 0):
        found = _check()
        _latest.update(found, checked_at=datetime.now().isoformat(timespec="seconds"),
                       expires=time.time() + (LATEST_FAIL_TTL if found["error"] else LATEST_TTL))
    return SystemVersions(studio=StudioVersion(current=VERSION, latest=_latest["latest"], notes_url=_latest["notes_url"],
                                               error=_latest["error"]),
                          core=CoreVersion(current=VERSION), checked_at=_latest["checked_at"])
