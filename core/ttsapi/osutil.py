"""和操作系统有关的几件事，Windows 上的行为和原来一样，Linux（Colab）和 macOS 上用等价做法
（macOS 除阻止睡眠外都和 Linux 相同）：
- 子进程不弹窗（CREATE_NO_WINDOW）、训练进程脱离 gateway 的 Job Object（Linux / macOS：新会话）
- 阻止系统睡眠（Windows：SetThreadExecutionState；macOS：IOKit 电源断言 PreventUserIdleSystemSleep；Linux 不做）
- worker 跟随 gateway 退出（Windows：等父进程句柄；Linux / macOS：轮询父进程是否还在）
- 训练工作目录里的目录联接（Windows：mklink /J；Linux / macOS：符号链接）
- 杀进程树（Windows：taskkill /T；Linux / macOS：psutil 逐个杀）
- 子进程环境：PATH / PYTHONPATH 用 os.pathsep 拼，固定 MPLBACKEND=Agg，不继承外部的 PYTHONPATH；
  Linux 上把 venv 里 pip 装的 CUDA 库（nvidia-cublas-cu12 等）加进 LD_LIBRARY_PATH（打分用的 ctranslate2 要找
  libcublas / libcudnn；Windows 上这些 DLL 由 torch 加载，不用管；macOS 的 venv 里没有这些包，是空列表）
"""
import ctypes
import glob
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
SYNCHRONIZE, INFINITE = 0x00100000, 0xFFFFFFFF
# macOS：IOPMAssertionCreateWithName 的参数（IOKit/pwr_mgt/IOPMLib.h）
_CF_UTF8, _ASSERTION_ON = 0x08000100, 255
_assertion = None  # macOS 上当前持有的电源断言 id（按进程生效，power.py 保证只在 0↔1 个持有者时切换）
if IS_MACOS:
    _cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    _cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]
    _iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
    _iokit.IOPMAssertionCreateWithName.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                                   ctypes.POINTER(ctypes.c_uint32)]
    _iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]


def detached_popen_kwargs() -> dict:
    """训练进程：gateway 退出或被杀时不跟着退出。"""
    if IS_WINDOWS:
        return dict(creationflags=NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB)
    return dict(start_new_session=True)


def keep_awake(on: bool):
    """on=True 阻止系统睡眠，False 放开。按线程生效（Windows）；macOS 上按进程，屏幕照常熄灭，进程退出时自动失效。"""
    global _assertion
    if IS_WINDOWS:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    elif IS_MACOS and on and _assertion is None:
        kind, reason = (_cf.CFStringCreateWithCString(None, s.encode(), _CF_UTF8)
                        for s in ("PreventUserIdleSystemSleep", "tts-api: inference / training in progress"))
        aid = ctypes.c_uint32(0)
        if _iokit.IOPMAssertionCreateWithName(kind, _ASSERTION_ON, reason, ctypes.byref(aid)) == 0:
            _assertion = aid.value
        _cf.CFRelease(kind)
        _cf.CFRelease(reason)
    elif IS_MACOS and not on and _assertion is not None:
        _iokit.IOPMAssertionRelease(_assertion)
        _assertion = None


def exit_with_parent(parent_pid: int):
    """父进程（gateway）退出时本进程跟着退出，避免留下占着显存和端口的孤儿 worker。"""
    if IS_WINDOWS:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)

        def wait():
            kernel32.WaitForSingleObject(handle, INFINITE)
            os._exit(0)
    else:
        def wait():
            while psutil.pid_exists(parent_pid):
                time.sleep(2)
            os._exit(0)

    threading.Thread(target=wait, daemon=True).start()


def link_dir(link: Path, target: Path):
    """建目录联接 / 符号链接 link -> target（已存在就不动）。"""
    if os.path.lexists(link):
        return
    if IS_WINDOWS:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True,
                       creationflags=NO_WINDOW)
    else:
        os.symlink(target, link, target_is_directory=True)


def kill_tree(pid: int):
    """杀掉进程和它的整棵子进程树。"""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
        return
    try:
        proc = psutil.Process(pid)
        procs = proc.children(recursive=True) + [proc]
    except psutil.NoSuchProcess:
        return
    for p in procs:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass


def cuda_lib_dirs(python: Path) -> list:
    """Linux: nvidia-* lib directories in the python venv (also following .pth site-packages for lightweight V5 venvs)."""
    if IS_WINDOWS:
        return []
    sites = glob.glob(str(Path(python).parent.parent / "lib" / "python3*" / "site-packages"))
    extra = []
    for site in sites:
        for pth in sorted(Path(site).glob("*.pth")):
            for line in pth.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith(("#", "import ")) and (Path(line) / "nvidia").is_dir():
                    extra.append(line)
    out = []
    for base in dict.fromkeys([*sites, *extra]):
        out.extend(glob.glob(str(Path(base) / "nvidia" / "*" / "lib")))
    return sorted(set(out))


def child_env(pythonpath: list, path_prepend: list = (), ld_library_path: list = (), **extra) -> dict:
    """给 worker / 训练脚本用的环境：PYTHONPATH 只用给定的几项（不继承外部的），PATH 前面加 path_prepend，
    LD_LIBRARY_PATH 前面加 ld_library_path（只在给了时设），
    MPLBACKEND 固定 Agg（外面若是 Jupyter 的 inline 后端，子进程里 import matplotlib 会报错）。"""
    env = dict(os.environ)
    if ld_library_path:
        env["LD_LIBRARY_PATH"] = os.pathsep.join([*map(str, ld_library_path), env.get("LD_LIBRARY_PATH", "")])
    env.update(
        PYTHONPATH=os.pathsep.join(str(p) for p in pythonpath),
        PATH=os.pathsep.join([*(str(p) for p in path_prepend if p), env.get("PATH", "")]),
        MPLBACKEND="Agg",
        **{k: str(v) for k, v in extra.items()},
    )
    return env
