"""Core 单元测试和 Colab 模拟共用隔离的数据目录及资料库夹具。"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

TMP = Path(tempfile.mkdtemp(prefix="ttsapi-test-"))
os.environ.update(
    TTSAPI_DATA_DIR=str(TMP / "data"),
    TTSAPI_WEB_DIR=str(TMP / "no-web"),
    TTSAPI_TRAIN_DIR=str(TMP / "training"),
    TTSAPI_WEIGHTS_DIR=str(TMP / "weights"),
    TTSAPI_GSV_ROOT=str(TMP / "gsv"),
    TTSAPI_GSV_V5_ROOT=str(TMP / "gsv-v5"),
    TTSAPI_DATASET_ROOT=str(TMP / "gsv" / "datasets"),
)
(TMP / "data").mkdir()
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def write_wav(path: Path, seconds: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros(int(16000 * seconds), dtype="float32"), 16000)
    return path


@pytest.fixture
def clean_data():
    """每个测试前清掉参考索引等派生文件（数据库不清，测试各用自己的 run 名、任务 id）。"""
    from ttsapi import settings
    derived = (settings.REFS_INDEX, settings.DATASET_CACHE)
    for p in derived:
        p.unlink(missing_ok=True)
    yield settings
    for p in derived:
        p.unlink(missing_ok=True)


@pytest.fixture
def lib(tmp_path, clean_data):
    """每个测试一个新的资料库（本机管理），设为当前；结束后回到未选。"""
    from ttsapi import library
    lb = library.open_or_create(tmp_path / "library")
    library.use(lb)
    yield lb
    library.use(None)


CAROL_FOLDER = "sp0001_alice"
CAROL_LIST = f"{CAROL_FOLDER}.ja.list"


def seed_carol(lb, clips=(("14000140", 6.1, "明日の朝は、公園で散歩しようと思います。"),
                        ("14200080", 5.9, "……っ"), ("14000010", 1.0, "おはよう"))):
    """资料库里登记数据集 SAMPLE_DS（只有 carol 的文件夹，已经解压在本机 DATASET_ROOT，不打 tar）和内置角色 carol。
    .list 用别的机器（win-gpu）上的绝对路径。返回本机的角色文件夹。"""
    from ttsapi import settings
    from ttsapi.catalog import Catalog
    folder = settings.DATASET_ROOT / "SAMPLE_DS" / "characters" / CAROL_FOLDER
    src = r"C:\datasets\SAMPLE_DS\characters\sp0001_alice\audio"
    for name, sec, _ in clips:
        write_wav(folder / "audio" / f"{name}.wav", sec)
    (folder / CAROL_LIST).write_text("".join(f"{src}\\{n}.wav|carol|ja|{t}\n" for n, _, t in clips), encoding="utf-8")
    lb.put("datasets/SAMPLE_DS.json", dict(id="SAMPLE_DS", name="SAMPLE_DS", tar="SAMPLE_DS.tar", sha256=None,
                                            size=None, created="2026-11-20T00:00:00+09:00",
                                            characters={CAROL_FOLDER: dict(id="SAMPLE_DS.sp0001", list=CAROL_LIST)}))
    Catalog.add_character("carol", "builtin", "SAMPLE_DS.sp0001")
    return folder
