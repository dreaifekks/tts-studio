"""阻止系统睡眠。SetThreadExecutionState 按线程生效，所以只在事件循环线程上调用（中间件和任务都在这个线程上）。
macOS 上是 IOKit 电源断言（按进程）；Linux 上不做（osutil.keep_awake 是空操作）。"""
from ttsapi.osutil import keep_awake

_holders = 0


def hold():
    global _holders
    _holders += 1
    if _holders == 1:
        keep_awake(True)


def release():
    global _holders
    _holders -= 1
    if _holders == 0:
        keep_awake(False)
