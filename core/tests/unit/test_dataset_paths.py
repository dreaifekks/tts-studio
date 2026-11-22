"""数据集 .list 里的音频路径：相对路径、别的机器上的绝对路径换算到本机数据根；本机能打开的不变。"""
from pathlib import Path

from conftest import write_wav
from ttsapi import datasets, settings
from ttsapi.datasets import resolve_audio


def test_relative_to_list(tmp_path):
    lst = tmp_path / "chars" / "a.list"
    assert resolve_audio("audio/1.ogg", lst) == str(tmp_path / "chars" / "audio" / "1.ogg")


def test_windows_relative_to_list(tmp_path):
    lst = tmp_path / "chars" / "a.list"
    assert resolve_audio("audio\\1.ogg", lst) == str(tmp_path / "chars" / "audio" / "1.ogg")
    assert resolve_audio("sub\\audio/1.ogg", lst) == str(tmp_path / "chars" / "sub" / "audio" / "1.ogg")


def test_existing_absolute_unchanged(tmp_path):
    f = write_wav(tmp_path / "x.wav", 1)
    assert resolve_audio(str(f)) == str(f)


def test_under_dataset_root_unchanged():
    p = str(settings.DATASET_ROOT / "SAMPLE_DS" / "characters" / "sp0001_alice" / "audio" / "1.ogg")
    assert resolve_audio(p) == p


def test_windows_path_rebased():
    p = r"C:\datasets\SAMPLE_DS\characters\sp0001_alice\audio\14000010.ogg"
    assert Path(resolve_audio(p)) == settings.DATASET_ROOT / "SAMPLE_DS/characters/sp0001_alice/audio/14000010.ogg"


def test_posix_path_rebased():
    p = "/content/GPTSoVits/datasets/SAMPLE_DS/characters/sp0001_alice/audio/14000010.ogg"
    assert Path(resolve_audio(p)) == settings.DATASET_ROOT / "SAMPLE_DS/characters/sp0001_alice/audio/14000010.ogg"


def test_last_datasets_segment_wins():
    p = "/home/u/datasets/backup/GPTSoVits/datasets/X/characters/c/1.ogg"
    assert Path(resolve_audio(p)) == settings.DATASET_ROOT / "X/characters/c/1.ogg"


def test_unknown_absolute_unchanged():
    assert resolve_audio("/nowhere/1.ogg") == "/nowhere/1.ogg"


def test_rows_with_relative_and_foreign_paths(clean_data):
    folder = settings.DATASET_ROOT / "PATHTEST" / "characters" / "c01_x"
    write_wav(folder / "audio" / "1.wav", 2.0)
    write_wav(folder / "audio" / "2.wav", 3.0)
    lst = folder / "c01_x.list"
    lst.write_text("audio\\1.wav|x|ja|こんにちは\n"
                   r"E:\old\datasets\PATHTEST\characters\c01_x\audio\2.wav|x|ja|さようなら" "\n", encoding="utf-8")
    rs = datasets.rows(lst)
    assert [r.path for r in rs] == [str(folder / "audio" / "1.wav"), str(folder / "audio" / "2.wav")]
    assert [r.duration for r in rs] == [2.0, 3.0]
