"""默认参考：排除纯语气词 / 无词汇内容；显式配置优先。"""
import pytest
from conftest import TMP, write_wav

from ttsapi.refs import RefLibrary, has_words


@pytest.mark.parametrize("text", [
    "……っ",              # carol 原来的默认参考，当 GPT prompt 会出静音
    "……っ（ぐいぐいと缶を押し付けてくる）",  # 括号里是动作说明，念出来的只有「っ」
    "う、ううぅ……っ", "はぁ、はぁ……っ", "んー……", "〜〜〜〜〜ッ", "あ、ああ……", "えへへ", "――……", "",
    "……ふう", "ほう", "うふふっ", "ハァ、ハァ", "うわぁっ", "ん？", "ぐすっ", "ふぇぇ……", "あっ",
    "あ……え？", "あ、ぁあ……ぐっ", "うわ〜ん！", "い、っや〜〜〜〜！！",
    "ん……ちゅ", "ちゅ", "――……けほけほっ", "げほげほ……っ", "ん、んく……ん、ちゅく……はふ", "じゅぷっ、ん、んっ",  # 拟声
])
def test_no_words(text):
    assert not has_words(text)


@pytest.mark.parametrize("text", [
    "明日の朝は、公園で散歩しようと思います。",
    "はい。……凄く眩しくて、綺麗な場所ですね、ここは",
    "ありがとう", "そうなの？",
    "おはよう", "おやすみ", "はい", "いいえ", "すき", "ハイ", "おはよー", "いえ……",  # 只用假名、不含汉字的词
    "う、うそ", "んー……そうか", "……おはよ", "うん。いいよ",  # 语气词和词混在一句里
    "いい？", "ちょっと", "ちゅうい", "すぷーん",  # 叠音、像拟声的词
    "（……全ての願いを叶えるとは言ってもとうぜん、無理なことはある）",  # 整句在括号里的是心里话，也是念出来的
    "Hello world",
])
def test_has_words(text):
    assert has_words(text)


class FakeCatalog:
    """RefLibrary 只用到角色记录和 builtin_of。"""

    def __init__(self, chars: dict, builtin_of=lambda s: None):
        self.chars, self.builtin_of = chars, builtin_of

    def characters(self):
        return self.chars

    def slugs(self):
        return list(self.chars)


def make_lib(refs: dict, builtin_of=lambda s: None, default_refs: dict = None):
    chars = {r["character"]: dict(slug=r["character"], default_ref=(default_refs or {}).get(r["character"]))
             for r in refs.values()}
    lib = RefLibrary(FakeCatalog(chars, builtin_of))  # 没选资料库：reload 后是空的，再放进测试的参考
    lib.index = dict(refs)
    return lib


def ref(rid, character, text, duration):
    p = TMP / "refs-default" / f"{rid}-{duration}.wav"
    if not p.is_file():
        write_wav(p, duration)
    return {rid: dict(id=rid, character=character, text=text, lang="ja", duration=duration, path=str(p))}


def test_default_skips_interjection():
    lib = make_lib({**ref("carol-1", "carol", "……っ", 5.94),
                    **ref("carol-2", "carol", "明日の朝は、公園で散歩しようと思います。", 6.11),
                    **ref("carol-3", "carol", "そうなの", 3.0)})
    assert lib.default_for("carol") == "carol-2"


def test_default_falls_back_when_nothing_has_words():
    lib = make_lib({**ref("x-1", "x", "はぁ……", 6.5), **ref("x-2", "x", "んっ", 3.0)})
    assert lib.default_for("x") == "x-1"


def test_explicit_default_in_character():
    lib = make_lib({**ref("carol-2", "carol", "明日の朝は", 6.0), **ref("carol-9", "carol", "あのね、聞いて", 3.5)},
                   default_refs={"carol": "carol-9"})
    assert lib.default_for("carol") == "carol-9"


def test_explicit_default_missing_ref_is_ignored():
    lib = make_lib({**ref("carol-2", "carol", "明日の朝は", 6.0)}, default_refs={"carol": "carol-404"})
    assert lib.default_for("carol") == "carol-2"


def test_trained_character_uses_builtin_default():
    lib = make_lib({**ref("carol-2", "carol", "明日の朝は", 6.0), **ref("carol-colab-2", "carol-colab", "夜", 6.0)},
                   builtin_of=lambda s: "carol" if s == "carol-colab" else None)
    assert lib.default_for("carol-colab") == "carol-2"


def test_explicit_default_beats_builtin_mapping():
    lib = make_lib({**ref("carol-2", "carol", "明日の朝は", 6.0),
                    **ref("carol-colab-7", "carol-colab", "あのね、聞いて", 4.0)},
                   builtin_of=lambda s: "carol" if s == "carol-colab" else None, default_refs={"carol-colab": "carol-colab-7"})
    assert lib.default_for("carol-colab") == "carol-colab-7"


def test_unknown_character():
    lib = make_lib(ref("carol-2", "carol", "明日の朝は", 6.0))
    with pytest.raises(KeyError):
        lib.default_for("nobody")
    assert lib.default_or_none("nobody") is None


def test_train_request_ref_id():
    from ttsapi.models import TrainRequest
    assert TrainRequest(slug="carol-colab", dataset_id="SAMPLE_DS.sp0001").ref_id is None
    assert TrainRequest(slug="carol-colab", dataset_id="d", ref_id="carol-14200770").ref_id == "carol-14200770"


def test_ref_labels():
    """列表、单条都给人能读的 label：默认参考写「默认参考音频」，其余是文本前 10 个字 + 时长，没文本写「参考音频」。"""
    lib = make_lib({**ref("carol-2", "carol", "明日の朝は、公園で散歩しようと思います。", 6.11),
                    **ref("carol-3", "carol", "そうなの", 3.04), **ref("carol-4", "carol", " ", 1.0),
                    **ref("x-1", "x", "はぁ……", 6.5)})
    labels = {r["id"]: r["label"] for r in lib.list()}
    assert labels == {"carol-2": "默认参考音频 · 6.1 秒", "carol-3": "そうなの · 3.0 秒", "carol-4": "参考音频 · 1.0 秒",
                      "x-1": "默认参考音频 · 6.5 秒"}
    assert lib.view("carol-3")["label"] == "そうなの · 3.0 秒"
    lib.catalog.chars["carol"]["default_ref"] = "carol-3"
    assert [r["label"] for r in lib.list("carol")] == ["明日の朝は、公園で散… · 6.1 秒", "默认参考音频 · 3.0 秒",
                                                     "参考音频 · 1.0 秒"]
