"""资料库里的数据集 tar：Drive 上 json 先到、tar 还没到时不挡启动和扫描，训练时报原因；本机解压缓存按库 id + tar 内容区分，
两个资料库里同名不同内容的数据集不串；训练列表里别的资料库（Colab VM）解压缓存的路径换到本库的解压缓存。"""
import tarfile

import pytest

from conftest import write_wav
from ttsapi import datasets, library, settings
from ttsapi.catalog import Catalog
from ttsapi.library import sha256_of
from ttsapi.refs import RefLibrary

NAME, FOLDER = "SAMENAME", "c01_x"
DID = datasets.dataset_id(NAME, FOLDER)


def put_dataset(lb, tmp_path, text: str, with_tar=True):
    """在资料库 lb 里登记数据集 SAMENAME（一句 5 秒，.list 用 win-gpu 上的绝对路径），打好 tar；内置角色 x 用它。"""
    src = tmp_path / f"src-{lb.info['id']}" / NAME / "characters" / FOLDER
    write_wav(src / "audio" / "1.wav", 5.0)
    (src / f"{FOLDER}.list").write_text(
        rf"C:\datasets\{NAME}\characters\{FOLDER}\audio\1.wav|x|ja|{text}" "\n", encoding="utf-8")
    tar = lb.path(f"datasets/{NAME}.tar")
    tar.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar, "w") as tf:
        tf.add(src, arcname=f"{NAME}/characters/{FOLDER}")
    doc = dict(id=NAME, name=NAME, tar=f"{NAME}.tar", sha256=sha256_of(tar), size=tar.stat().st_size,
               created="2026-11-20T00:00:00+09:00", characters={FOLDER: dict(id=DID, list=f"{FOLDER}.list")})
    if not with_tar:
        tar.unlink()
    lb.put(f"datasets/{NAME}.json", doc)
    Catalog.add_character("x", "builtin", DID)
    return doc


def test_json_before_tar_does_not_block(lib, tmp_path):
    doc = put_dataset(lib, tmp_path, "こんにちは、元気ですか", with_tar=False)
    refs = RefLibrary(Catalog())  # 启动、重扫都走 reload
    assert refs.index == {} and datasets.scan() == {}
    with pytest.raises(datasets.DatasetUnavailable, match="还没同步"):
        datasets.get(DID)
    with pytest.raises(datasets.DatasetUnavailable):
        datasets.preview(DID, 1.0, 12.0, True)
    with pytest.raises(KeyError):
        datasets.get("NOPE.c01")
    # 同步了一半：大小对不上
    lib.path(f"datasets/{NAME}.tar").write_bytes(b"x" * (doc["size"] // 2))
    refs.reload()
    assert datasets.scan() == {}
    assert not (settings.DATASET_ROOT / datasets.UNPACKED / f"{lib.info['id']}-{doc['sha256'][:16]}").exists()


def test_same_name_in_two_libraries_does_not_mix(lib, tmp_path):
    a_doc = put_dataset(lib, tmp_path, "こちらはＡの資料庫です")
    b = library.open_or_create(tmp_path / "b")
    library.use(b)
    b_doc = put_dataset(b, tmp_path, "こちらはＢの資料庫です")
    assert a_doc["sha256"] != b_doc["sha256"]
    refs = RefLibrary(Catalog())
    for lb, text in ((lib, "こちらはＡの資料庫です"), (b, "こちらはＢの資料庫です"), (lib, "こちらはＡの資料庫です")):
        library.use(lb)
        refs.reload()
        list_path, slug = datasets.get(DID)
        cache = settings.DATASET_ROOT / datasets.UNPACKED / f"{lb.info['id']}-{lb.get(f'datasets/{NAME}.json')['sha256'][:16]}"
        assert slug == "x" and list_path == cache / NAME / "characters" / FOLDER / f"{FOLDER}.list"
        rs = datasets.rows(list_path)
        assert [r.text for r in rs] == [text]
        assert rs[0].path == str(cache / NAME / "characters" / FOLDER / "audio" / "1.wav") and rs[0].duration == 5.0
        assert [r["text"] for r in refs.index.values()] == [text]
        assert refs.index["x-1"]["path"] == rs[0].path
    assert not [p for p in (settings.DATASET_ROOT / datasets.UNPACKED).iterdir() if p.name.endswith(".tmp")]


def test_train_list_from_other_library_cache(lib, tmp_path):
    """Colab VM（另一个资料库 id）训出的角色：训练列表里是 VM 解压缓存的路径，本机同一个 DATASET_ROOT 下也没有那个目录。
    导入后、重建参考库时按数据集名换到本库的解压缓存，参考能读。"""
    doc = put_dataset(lib, tmp_path, "こんにちは、元気ですか")
    foreign = settings.DATASET_ROOT / datasets.UNPACKED / f"vmlibrary-{doc['sha256'][:16]}" / NAME / "characters" / \
        FOLDER / "audio" / "1.wav"
    cache = settings.DATASET_ROOT / datasets.UNPACKED / f"{lib.info['id']}-{doc['sha256'][:16]}"
    expect = str(cache / NAME / "characters" / FOLDER / "audio" / "1.wav")
    assert datasets.resolve_audio(str(foreign)) == expect
    assert datasets.resolve_audio(f"/content/GPTSoVits/datasets/.unpacked/vm-1/{NAME}/characters/{FOLDER}/audio/1.wav") \
        == expect
    # 训出的角色：训练列表（资料库里的 train.list）是 VM 的路径
    run_dir = tmp_path / "run"
    (run_dir).mkdir()
    (run_dir / "train.list").write_text(f"/content/GPTSoVits/datasets/.unpacked/vm-1/{NAME}/characters/{FOLDER}/"
                                        "audio/1.wav|x-colab|ja|こんにちは、元気ですか\n", encoding="utf-8")
    lib.put_file("runs/r1/train.list", run_dir / "train.list")
    Catalog.add_character("x-colab", "user", DID, train_list="runs/r1/train.list")
    refs = RefLibrary(Catalog())
    assert refs.index["x-colab-1"]["path"] == expect
