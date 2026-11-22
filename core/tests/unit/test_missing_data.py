"""本机只有部分数据时（Colab 上只带 carol、数据集没解压、底模没装）：目录和参考库跳过缺的，不报错；
内置角色的默认组合来自角色记录里手动设的默认（开发建库命令设成 7 月的 e15 + e8）。"""
from pathlib import Path

from conftest import seed_carol, write_wav
from ttsapi import settings
from ttsapi.catalog import Catalog
from ttsapi.refs import RefLibrary


def test_refs_only_characters_with_local_lists(lib):
    folder = seed_carol(lib)
    Catalog.add_character("dana", "builtin", "SAMPLE_DS.sp0003")  # 数据集里没有它的文件夹
    Catalog.add_character("ghost", "user", "SAMPLE_DS.sp0001", train_list="runs/ghost-1/train.list")  # 列表不在
    refs = RefLibrary(Catalog())
    assert {r["character"] for r in refs.index.values()} == {"carol"}
    assert sorted(refs.index) == ["carol-14000140", "carol-14200080"]  # 1 秒的太短，不进
    assert refs.index["carol-14000140"]["path"] == str(folder / "audio" / "14000140.wav")
    assert refs.default_for("carol") == "carol-14000140"  # 「〜〜っ」没有实际词句
    assert refs.default_or_none("dana") is None and refs.default_or_none("ghost") == "carol-14000140"  # 沿用内置角色的


def test_catalog_without_pretrained(lib):
    seed_carol(lib)
    cat = Catalog()
    assert cat.slugs() == ["carol"]
    assert not [x for x in cat.entries() if x["source"] == "pretrained"]  # 底模文件都不在
    cat.weight_entries()


def test_july_defaults_and_pretrained_when_present(lib, tmp_path):
    seed_carol(lib)
    cfg = settings.ENGINES["v2pro"]
    for p in (cfg["pretrained_gpt"], cfg["pretrained_sovits"]):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    cat = Catalog()
    for kind, name in (("gpt", "carol-e15.ckpt"), ("gpt", "carol-e10.ckpt"), ("sovits", "carol_e8_s144.pth")):
        (tmp_path / name).write_bytes(name.encode())
        cat.add_weight("carol", "v2pro", kind, tmp_path / name, "july")
    try:
        # 没手动设默认、也没评测：用最后一个 checkpoint（按轮数排，e15 在 e10 后面；文件名排的话 e10 在后）
        assert cat.defaults("v2pro", "carol") == ("carol-e15.ckpt", "carol_e8_s144.pth")
        (tmp_path / "carol_e12_s200.pth").write_bytes(b"e12")
        cat.add_weight("carol", "v2pro", "sovits", tmp_path / "carol_e12_s200.pth", "july")
        assert cat.defaults("v2pro", "carol")[1] == "carol_e12_s200.pth"
        cat.set_default("v2pro", "carol", "gpt", "carol-e10.ckpt")
        assert cat.defaults("v2pro", "carol") == ("carol-e10.ckpt", "carol_e12_s200.pth")
        names = {x["name"] for x in cat.entries("v2pro") if x["source"] == "pretrained"}
        assert names == {cfg["pretrained_gpt"].name, cfg["pretrained_sovits"].name}
        ws = {w.name: w for w in cat.weight_entries()}
        assert ws["carol-e10.ckpt"].default_for == ["carol"] and not ws["carol-e10.ckpt"].deletable
        assert ws["carol_e12_s200.pth"].default_for == ["carol"]
        assert ws["carol-e10.ckpt"].dataset_id == "SAMPLE_DS.sp0001" and ws["carol-e10.ckpt"].sha256
        assert ws[cfg["pretrained_gpt"].name].sha256 is None
        # 其他引擎没有这个角色的权重：底模
        assert cat.defaults("v2proplus", "carol")[0] == settings.ENGINES["v2proplus"]["pretrained_gpt"].name
    finally:
        for p in (cfg["pretrained_gpt"], cfg["pretrained_sovits"]):
            Path(p).unlink()


def test_unregistered_character_zero_shot(lib, tmp_path):
    """上传参考时用的角色名（custom）没登记成角色：默认组合是底模，默认参考自动选。"""
    seed_carol(lib)
    cat = Catalog()
    refs = RefLibrary(cat)
    wav = write_wav(tmp_path / "up-1.wav", 5.0)
    refs.index["up-1"] = dict(id="up-1", character="custom", text="こんにちは、元気？", lang="ja", duration=5.0, path=str(wav))
    assert refs.default_for("custom") == "up-1"
    assert cat.defaults("v2pro", "custom") == (settings.ENGINES["v2pro"]["pretrained_gpt"].name,
                                               settings.ENGINES["v2pro"]["pretrained_sovits"].name)
