"""资料库：一个文件夹（Google Drive 电脑版同步的文件夹，或本地文件夹），放角色、参考音频、数据集、权重、评测、盲听、作品、
训练记录。生成历史、评分、任务队列不在这里，留在本机的 tts.db。格式见 README「资料库」：

  library.json                                   格式版本、库 id、管理它的控制端（owner）
  characters/<slug>.json                         角色：来源（builtin / user）、数据集、默认参考、各引擎的默认权重
  refs/<slug>/<ref_id>.json + 音频               参考音频：文本、语言、时长、情绪标签
  datasets/<name>.json + <name>.tar              训练数据：tar 里是 <name>/characters/<文件夹>/{*.list, audio/}
  weights/<slug>/<engine>/<weight_id>/           一个权重文件：gpt.ckpt 或 sovits.pth，加 meta.json（sha256、来源 run、显示名…）
  evals/<yyyy-mm>/<job_id>.json                  一次评测（一个任务，可能评了几组 GPT × SoVITS）
  blind/<test_id>.json                           一场盲听：题目、投票、揭晓
  works/<work_id>.json + works/<work_id>/        作品；目录里是渲染产物和用到的音频（audio/）
  runs/<run>/run.json + 训练的小文件             一次训练：参数、数据过滤、产出的权重
  compute/bench/<machine>/<date>.json            测速结果（第 2 轮）

只有管理它的控制端（library.json 的 owner）写；别的机器打开时可以读，写入返回 409，POST /library/claim 改归属。
选哪个资料库存在本机的 DATA/core.json（POST /library 写）；没选过时用环境变量 TTSAPI_LIBRARY。都没有时，用到资料库的接口
返回 409「未选资料库」。
"""
import contextvars
import json
import uuid
from contextlib import contextmanager
from pathlib import Path

from ttsapi import settings
from ttsapi.library.store import (Library, LibraryError, NotALibrary, NotOwner, NotSelected, is_conflict, kind_of,
                                  now_iso, sha256_of)

__all__ = ["Library", "LibraryError", "NotALibrary", "NotOwner", "NotSelected", "current", "pinned", "selected", "select",
           "use", "open_or_create", "open_configured", "error", "machine", "is_conflict", "kind_of", "now_iso",
           "sha256_of"]

_current = None
_error = None  # 配置了资料库但打不开时的原因
# pinned 设的资料库：设了 current() 就返回它，不跟着 POST /library 换库
_pinned = contextvars.ContextVar("pinned_library", default=None)


def machine() -> dict:
    """本机身份 {"id", "name"}：id 第一次用时生成，存在 DATA/machine.json；name 用 TTSAPI_MACHINE_NAME，没设是主机名。"""
    f = settings.MACHINE_FILE
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(dict(id=uuid.uuid4().hex)), encoding="utf-8")
    return dict(id=json.loads(f.read_text(encoding="utf-8"))["id"], name=settings.MACHINE_NAME)


def configured_path():
    if settings.CORE_CONFIG.exists():
        p = json.loads(settings.CORE_CONFIG.read_text(encoding="utf-8")).get("library")
        if p:
            return Path(p)
    return Path(settings.LIBRARY).expanduser() if settings.LIBRARY else None


def open_configured():
    """服务启动时：打开配置的资料库并扫描。路径不在（比如 Drive 还没挂上）时不报错，GET /library 里给出原因。"""
    global _current, _error
    _current, _error = None, None
    path = configured_path()
    if path is None:
        return None
    if not (path / "library.json").exists():
        _error = f"配置的资料库 {path} 不存在或不是资料库"
        return None
    _current = Library(path, settings.LIBRARY_INDEX_DIR, machine())
    return _current


def open_or_create(path: Path) -> Library:
    """path 里有 library.json 就打开；是空文件夹或不存在就在那里新建（本机管理）；别的情况（有文件但不是资料库）400。"""
    path = Path(path).expanduser().absolute()
    if (path / "library.json").exists():
        return Library(path, settings.LIBRARY_INDEX_DIR, machine())
    if not path.exists() or not any(p for p in path.iterdir() if not p.name.startswith(".")):
        return Library.create(path, settings.LIBRARY_INDEX_DIR, machine())
    raise NotALibrary(f"{path} 不是资料库（没有 library.json），也不是空文件夹")


def use(lib):
    """设为当前资料库（不记进配置；供离线工具和测试使用）。None 表示未选。"""
    global _current, _error
    _current, _error = lib, None


def select(path: Path) -> Library:
    """选一个资料库（open_or_create），路径写进 DATA/core.json，下次启动直接打开。"""
    lib = open_or_create(path)
    settings.CORE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(settings.CORE_CONFIG.read_text(encoding="utf-8")) if settings.CORE_CONFIG.exists() else {}
    settings.CORE_CONFIG.write_text(json.dumps(dict(cfg, library=str(lib.root)), ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    use(lib)
    return lib


def selected():
    """当前资料库；没选时 None。"""
    return _current


def error():
    return _error


@contextmanager
def pinned(lib: Library):
    """这段里（含其中 asyncio.to_thread 开的线程）current() 固定是 lib：后台任务（Colab 回收结果）写进任务开始时的资料库。"""
    token = _pinned.set(lib)
    try:
        yield lib
    finally:
        _pinned.reset(token)


def current() -> Library:
    """当前资料库（pinned 里是固定的那个）；没选时抛 NotSelected（API 里是 409「未选资料库」）。"""
    if (lib := _pinned.get()) is not None:
        return lib
    if _current is None:
        raise NotSelected(_error or "未选资料库：用 POST /library 选一个文件夹（Drive 同步的文件夹或本地文件夹）")
    return _current
