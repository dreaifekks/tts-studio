"""Disk and storage usage overview for the control host and worker devices.

Scans the active library and local TTS Studio directories using iterative
``os.scandir`` (skipping symlinks and ignoring unreadable entries or mid-scan
I/O errors) with a 60-second generation-tracked in-memory cache. Distinguishes
logical size (``st_size``) from on-disk allocated size (POSIX ``st_blocks * 512``,
Windows fallback ``st_size``) so macOS Google Drive FileProvider streaming
placeholders report 0 local bytes. Directories are deduplicated by filesystem
identity ``(st_dev, st_ino)``, and the active library subtree is excluded from
local categories.
"""
import os
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Optional

from ttsapi import library, settings
from ttsapi.library.store import now_iso
from ttsapi.models import DiskUsage, LibraryStorage, LocalStorage, StorageCategory, StorageOverview

CACHE_TTL = 60.0

LIBRARY_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("weights", "模型权重"),
    ("datasets", "训练数据集"),
    ("runs", "训练产出与样本"),
    ("works", "作品音频"),
    ("refs", "参考音频"),
    ("other", "评测、盲听与配置"),
)
_LIBRARY_NAMED = frozenset(k for k, _ in LIBRARY_CATEGORIES if k != "other")

LOCAL_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("engine", "语音引擎与基础模型"),
    ("dataset_cache", "数据集解包缓存"),
    ("audio_history", "历史音频与运行数据"),
    ("training_tmp", "训练暂存"),
    ("blobs", "缓存模型"),
)
# More specific directories claim ownership first when two categories resolve to
# the exact same directory inode.
_LOCAL_SPECIFICITY = ("blobs", "dataset_cache", "training_tmp", "audio_history", "engine")

_cache_lock = threading.Lock()
_cache: Optional[tuple[tuple, float, StorageOverview]] = None
_cache_gen: int = 0


def invalidate_cache() -> None:
    """Bump cache generation and clear the in-memory storage overview cache."""
    global _cache, _cache_gen
    with _cache_lock:
        _cache_gen += 1
        _cache = None


def _file_sizes(st: os.stat_result) -> tuple[int, int]:
    """Return ``(logical_bytes, local_bytes)`` for a stat result."""
    logical = st.st_size
    if os.name == "nt":
        return logical, logical
    blocks = getattr(st, "st_blocks", None)
    if blocks is None:
        return logical, logical
    return logical, blocks * 512


def _inode_key(st: os.stat_result, path: str):
    """Return filesystem-unique directory identity ``(st_dev, st_ino)`` (fallback to normcase realpath)."""
    if st.st_ino:
        return (st.st_dev, st.st_ino)
    return os.path.normcase(os.path.realpath(path))


def _dir_id(path: Path | str):
    """Return directory identity for an existing directory (following symlinks on the root), or None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    return _inode_key(st, str(path))


def scan_dir(path: Path, stop_inodes: Optional[set] = None) -> tuple[int, int, int]:
    """Recursively scan ``path`` using ``os.scandir`` without following symlinks.

    ``stop_inodes`` is a set of ``(st_dev, st_ino)`` directory keys that must
    not be descended into (used to avoid double-counting nested categories or
    the active library subtree). Both opening and iterating ``os.scandir`` catch
    ``OSError`` so unreadable directories or mid-scan I/O errors skip cleanly.
    Returns ``(logical_bytes, local_bytes, file_count)``.
    """
    root_id = _dir_id(path)
    if root_id is None:
        return 0, 0, 0
    stop = stop_inodes or set()
    if root_id in stop:
        return 0, 0, 0
    try:
        root_real = os.path.realpath(path)
    except OSError:
        return 0, 0, 0

    total_logical = 0
    total_local = 0
    file_count = 0
    visited = {root_id}
    stack = [root_real]

    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            child_st = entry.stat(follow_symlinks=False)
                            child_id = _inode_key(child_st, entry.path)
                            if child_id not in stop and child_id not in visited:
                                visited.add(child_id)
                                stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            logical, local = _file_sizes(entry.stat(follow_symlinks=False))
                            total_logical += logical
                            total_local += local
                            file_count += 1
                    except OSError:
                        continue
        except OSError:
            continue

    return total_logical, total_local, file_count


def disk_usage(path: Optional[Path] = None) -> DiskUsage:
    """Query filesystem usage for ``path`` (default ``settings.DATA``) or fallback to home."""
    target = Path(path) if path is not None else Path(settings.DATA)
    while not target.exists() and target != target.parent:
        target = target.parent
    if not target.exists():
        target = Path.home()
    du = shutil.disk_usage(target)
    return DiskUsage(path=str(target), total_bytes=du.total, used_bytes=du.used, free_bytes=du.free)


def device_disk_stats() -> dict:
    """Lightweight disk and blob cache summary for ``local_state`` and ``/worker/health``."""
    try:
        du = disk_usage(settings.DATA)
        disk_free: Optional[int] = du.free_bytes
        disk_total: Optional[int] = du.total_bytes
    except OSError:
        disk_free, disk_total = None, None
    try:
        cache_bytes: Optional[int] = scan_dir(settings.BLOB_DIR)[0] if settings.BLOB_DIR.is_dir() else 0
    except OSError:
        cache_bytes = None
    return dict(disk_free_bytes=disk_free, disk_total_bytes=disk_total, cache_bytes=cache_bytes)


def _scan_library(lib) -> LibraryStorage:
    if lib is None:
        return LibraryStorage(selected=False, kind=None, path=None, total_bytes=0, local_bytes=0, categories=[])

    buckets = {k: [0, 0, 0] for k, _ in LIBRARY_CATEGORIES}
    try:
        root_real = os.path.realpath(lib.root)
    except OSError:
        root_real = ""

    if root_real and os.path.isdir(root_real):
        try:
            with os.scandir(root_real) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            key = entry.name if entry.name in _LIBRARY_NAMED else "other"
                            b, lb, f = scan_dir(Path(entry.path))
                            acc = buckets[key]
                            acc[0] += b
                            acc[1] += lb
                            acc[2] += f
                        elif entry.is_file(follow_symlinks=False):
                            b, lb = _file_sizes(entry.stat(follow_symlinks=False))
                            acc = buckets["other"]
                            acc[0] += b
                            acc[1] += lb
                            acc[2] += 1
                    except OSError:
                        continue
        except OSError:
            pass

    order = {k: i for i, (k, _) in enumerate(LIBRARY_CATEGORIES)}
    categories = [
        StorageCategory(key=k, label=label, bytes=buckets[k][0], local_bytes=buckets[k][1], files=buckets[k][2])
        for k, label in LIBRARY_CATEGORIES
    ]
    categories.sort(key=lambda c: (-c.bytes, order[c.key]))
    return LibraryStorage(
        selected=True,
        kind=lib.kind,
        path=str(lib.root),
        total_bytes=sum(c.bytes for c in categories),
        local_bytes=sum(c.local_bytes for c in categories),
        categories=categories,
    )


def _local_roots() -> dict[str, list[Path]]:
    return {
        "engine": [settings.OLD_REPO, settings.V5_REPO],
        "dataset_cache": [settings.DATASET_ROOT],
        "audio_history": [settings.DATA],
        "training_tmp": [settings.TRAIN_DIR, settings.WEIGHTS_DIR],
        "blobs": [settings.BLOB_DIR],
    }


def _scan_local(lib) -> LocalStorage:
    roots = _local_roots()
    excluded_lib: set = set()
    if lib is not None:
        lib_id = _dir_id(lib.root)
        if lib_id is not None:
            excluded_lib.add(lib_id)

    owner: dict[object, tuple[str, str]] = {}
    for key in _LOCAL_SPECIFICITY:
        for p in roots[key]:
            if p is None:
                continue
            did = _dir_id(p)
            if did is None or did in excluded_lib or did in owner:
                continue
            try:
                real = os.path.realpath(p)
            except OSError:
                continue
            owner[did] = (real, key)

    claimed = set(owner.keys())
    buckets = {k: [0, 0, 0] for k, _ in LOCAL_CATEGORIES}
    for did, (real, key) in owner.items():
        b, lb, f = scan_dir(Path(real), stop_inodes=(claimed - {did}) | excluded_lib)
        acc = buckets[key]
        acc[0] += b
        acc[1] += lb
        acc[2] += f

    order = {k: i for i, (k, _) in enumerate(LOCAL_CATEGORIES)}
    categories = [
        StorageCategory(key=k, label=label, bytes=buckets[k][0], local_bytes=buckets[k][1], files=buckets[k][2])
        for k, label in LOCAL_CATEGORIES
    ]
    categories.sort(key=lambda c: (-c.bytes, order[c.key]))
    return LocalStorage(
        machine=settings.MACHINE_NAME,
        total_bytes=sum(c.bytes for c in categories),
        categories=categories,
    )


def _cache_key(lib) -> tuple:
    return (
        str(lib.root) if lib is not None else None,
        lib.info.get("id") if lib is not None else None,
        lib.kind if lib is not None else None,
        settings.MACHINE_NAME,
        str(settings.DATA),
        str(settings.OLD_REPO),
        str(settings.V5_REPO),
        str(settings.DATASET_ROOT),
        str(settings.TRAIN_DIR),
        str(settings.WEIGHTS_DIR),
        str(settings.BLOB_DIR),
    )


def overview(refresh: bool = False) -> StorageOverview:
    """Compute (or return cached) storage usage overview for disk, library, and local dirs."""
    global _cache
    with _cache_lock:
        gen = _cache_gen
        try:
            lib = library.current()
        except library.NotSelected:
            lib = None
        key = _cache_key(lib)
        now = time.monotonic()
        if not refresh and _cache is not None and _cache[0] == key and now - _cache[1] < CACHE_TTL:
            return _cache[2]

    result = StorageOverview(
        measured_at=now_iso(),
        disk=disk_usage(settings.DATA),
        library=_scan_library(lib),
        local=_scan_local(lib),
    )
    with _cache_lock:
        if _cache_gen == gen:
            _cache = (key, time.monotonic(), result)
    return result
