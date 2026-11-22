"""参考音频。两部分：
- 资料库 refs/<角色>/<ref_id>.json + 音频：上传的参考、打过情绪标签的参考、角色的默认参考。记录：id、character、text、lang、
  duration、tags、audio（同目录的音频文件名）、source（从数据集来的参考记原路径，上传的为 null）。
- 候选：从各角色的 .list（内置角色是资料库登记的数据集，训练出来的角色是训练时的 train.list）挑 3–10 秒的句子，
  派生出来的，缓存在本机 data/refs_index.json，不进资料库。给候选打标签时把它（连音频）登记进资料库。

默认参考（没指定 ref_id、emotion 时用的那条）按顺序：角色记录的 default_ref → 用内置角色数据集训练的角色沿用内置角色的 →
自动选：有实际词句的参考里时长最接近 6 秒的一条。
「有实际词句」见 has_words()：整句由语气词片段组成的（はぁ、えへへ、〜〜っ）或只有括号里的动作说明的不算，
这种参考当 GPT 的 prompt 时模型几乎立刻输出 EOS，合成出来是静音。"""
import hashlib
import json
import random
import re
import unicodedata
from pathlib import Path

import soundfile as sf

from ttsapi import datasets, library, settings
from ttsapi.datasets import INTERJECTION_CHARS, clean, resolve_audio

# 括号里的动作、旁白说明（「……っ（ぐいぐいと缶を押し付けてくる）」）不是念出来的
_ANNOTATION = re.compile(r"[（(［\[【〔][^）)］\]】〕]*[）)］\]】〕]")
# 语气词片段用到的假名（平假名；片假名先换成平假名）：语气词假名 + 啜泣、咬牙、哼声的「ぐ」「く」「す」「む」
_HEADS = {c for c in INTERJECTION_CHARS if "ぁ" <= c <= "ゖ"} - set("ぁぃぅぇぉゃゅょっ") | set("ぐくすむ")
# 只拉长前一个音、本身不成词的：小写假名、促音、长音（「〜」先换成长音）
_TAILS = set("ぁぃぅぇぉゃゅょゎっー")
# 叠音算语气词的只有这些（うう、ああ、ふふ、へへ、んん）；「いい」「ええ」「よよ」这种叠音可能是词（いい？）
_REPEATABLE = set("あうおはひふへほんぐくむ")
# 拟声片段（亲吻、舔舐、咳嗽、喘气、吃东西）：整段换成分隔符，剩下的部分再按语气词规则看。只收多字片段和「ぷっ」，
# 单个「ぷ」「ちゅ」以外的字太常见（すぷーん 之类）
_SOUND = re.compile(r"(?:ちゅぱ|ちゅ|じゅ|ぷっ|けほ|げほ|こほ|ごほ|かふ|はふ|あふ|ぷふ|うふ|はむ|あむ|ぺろ|れろ)"
                    r"[ぅっーく]*")
_VOWEL = dict(zip("あいうえおはひふへほやゆよわぐくすむぁぃぅぇぉゃゅょゎ", "aiueoaiueoauoauuuuaiueoauoa", strict=True))


def _hiragana(t: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in t)


def _stretches(prev: str, c: str) -> bool:
    """c 是 prev 的拉长音：同元音（ふう、はあ、へえ），或 o 段后的「う」、e 段后的「い」（ほう、おう、えい）。
    prev 本身是元音时同元音算叠音，归 _REPEATABLE 管（「いい」不算拉长）。"""
    v = _VOWEL.get(prev)
    if c not in "あいうえお" or v is None:
        return False
    return (v, c) in (("o", "う"), ("e", "い")) or (v == _VOWEL[c] and prev not in "あいうえお")


def _filler_segment(t: str) -> bool:
    """一段（标点之间）是不是纯语气词：只用 _HEADS、_TAILS 里的假名，并且每个假名都不成词——
    后面跟小写假名 / 促音 / 长音（はぁ、あっ、んー），和相邻的字相同且是 _REPEATABLE 里的（ふふ、うう），
    是前一个字的拉长音（ふう、ほう），是「ん」，或者在段首、「ん」之后（えへへ、うわぁ、んあっ）。"""
    if any(c not in _HEADS and c not in _TAILS for c in t):
        return False
    for i, c in enumerate(t):
        prev, nxt = t[i - 1] if i else "", t[i + 1] if i + 1 < len(t) else ""
        if not (i == 0 or prev == "ん" or c in _TAILS or c == "ん" or nxt in _TAILS
                or (c in _REPEATABLE and c in (prev, nxt)) or _stretches(prev, c)):
            return False
    return True


def has_words(text: str) -> bool:
    """参考文本里有没有实际词句：去掉括号说明、拟声片段（_SOUND）后，按标点、空格分段，每段都是纯语气词
    （_filler_segment）的不算。所以「はぁ、はぁ……っ」「あ……え？」「えへへ」「〜〜っ」「ん……ちゅ」「けほけほっ」是语气词；
    「はい」「いいえ」「いい？」「おはよう」「すき」是词。
    整句都在括号里的是心里话（也是念出来的），不去括号。"""
    t = _ANNOTATION.sub("", text)
    if not clean(t):
        t = text
    t = unicodedata.normalize("NFKC", re.sub("[〜～~]", "ー", t))
    t = _hiragana("".join(" " if unicodedata.category(c)[0] in "PSZC" else c for c in t))
    segments = _SOUND.sub(" ", t).split()
    return not all(_filler_segment(seg) for seg in segments)


REF_MIN_SAMPLES_16K = int(settings.REF_MIN_SEC * 16000)
REF_MAX_SAMPLES_16K = int(settings.REF_MAX_SEC * 16000)


def raw_duration(path: Path) -> float:
    """Read unrounded audio duration in seconds from file header (frames / samplerate)."""
    if not path.is_file():
        raise ValueError(f"参考音频文件不存在：{path.name}")
    try:
        info = sf.info(str(path))
    except Exception as e:
        raise ValueError(f"无法读取参考音频文件：{path.name}（{e}）") from None
    if not info.samplerate:
        raise ValueError(f"参考音频采样率无效：{path.name}")
    return info.frames / info.samplerate


def is_valid_primary_duration(sec: float) -> bool:
    """Strict unrounded check matching upstream TTS.py 16kHz sample bounds [48000, 160000]."""
    samples_16k = int(sec * 16000)
    return (
        settings.REF_MIN_SEC <= sec <= settings.REF_MAX_SEC
        and REF_MIN_SAMPLES_16K <= samples_16k <= REF_MAX_SAMPLES_16K
    )


def _index_list(name: str, list_file: Path, root: Path = None):
    """root：数据集的 .list 是它的解压目录（别的机器上的绝对路径换算到这里），训练列表不给（本机数据根）。"""
    index = {}
    for line in list_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        path, _, lang, text = line.split("|", 3)
        path = resolve_audio(path, list_file, root)
        info = sf.info(path)
        sec = info.frames / info.samplerate
        if is_valid_primary_duration(sec):
            ref_id = f"{name}-{Path(path).stem}"
            index[ref_id] = dict(id=ref_id, character=name, text=text, lang=lang, duration=round(sec, 2), path=path)
    return index


LABEL_CHARS = 10


def label(r: dict, default_id: str) -> str:
    """参考音频的显示名（不给用户看 alice-00000010 这种 id）。"""
    sec = f"{r['duration']:.1f} 秒"
    if r["id"] == default_id:
        return f"默认参考音频 · {sec}"
    text = r["text"].strip()
    if not text:
        return f"参考音频 · {sec}"
    return f"{text if len(text) <= LABEL_CHARS else text[:LABEL_CHARS] + '…'} · {sec}"


def ref_rel(character: str, ref_id: str):
    return f"refs/{character}/{ref_id}.json"


class RefLibrary:
    def __init__(self, catalog):
        self.catalog = catalog
        self.index, self.tags = {}, {}
        self.derived = {}  # 角色 -> {"list": .list 路径, "mtime", "refs": {ref_id: 参考}}
        self.reload()

    def list_file(self, slug: str):
        """这个角色的候选从哪个 .list 来：训练出来的角色是资料库里的 train.list，内置角色是数据集的 .list。本机没有就 None。"""
        c = self.catalog.character(slug)
        if c["train_list"]:
            p = library.current().path(c["train_list"])
        else:
            p = datasets.scan().get(c["dataset_id"], (None,))[0]
        return p if p is not None and p.exists() else None

    def reload(self):
        """按当前资料库重建：候选（本机缓存里 .list 没变的直接用）+ 资料库里的参考。没选资料库时为空。"""
        self.index, self.tags = {}, {}
        lib = library.selected()
        if lib is None:
            return
        cache = json.loads(settings.REFS_INDEX.read_text(encoding="utf-8")) if settings.REFS_INDEX.exists() else {}
        self.derived = {}
        for slug in self.catalog.slugs():
            if (lst := self.list_file(slug)) is None:  # 训练列表、数据集不在本机（别的机器训练的角色）就没有候选
                continue
            hit = cache.get(slug)
            if hit and hit["list"] == str(lst) and hit["mtime"] == lst.stat().st_mtime:
                self.derived[slug] = hit
            else:
                root = None if self.catalog.character(slug)["train_list"] else lst.parents[3]
                self.derived[slug] = dict(list=str(lst), mtime=lst.stat().st_mtime, refs=_index_list(slug, lst, root))
        settings.REFS_INDEX.write_text(json.dumps(self.derived, ensure_ascii=False), encoding="utf-8")
        for d in self.derived.values():
            self.index.update(d["refs"])
        for rel, r in lib.items("refs").items():
            self.index[r["id"]] = dict(id=r["id"], character=r["character"], text=r["text"], lang=r["lang"],
                                       duration=r["duration"], path=str(lib.path(rel).parent / r["audio"]))
            self.tags[r["id"]] = r["tags"]

    def has_character(self, slug: str):
        return any(r["character"] == slug for r in self.index.values())

    def view(self, ref_id: str, default: str = None):
        """default：这条参考所属角色的默认参考 id（列表里按角色算一次传进来；不传时现算）。"""
        r = self.index[ref_id]
        default = default if default is not None else self.default_or_none(r["character"])
        return dict(r, tags=self.tags.get(ref_id, []), audio_url=f"/refs/{ref_id}/audio", label=label(r, default))

    def list(self, character: str = None, emotion: str = None, sort: str = None, order: str = "asc"):
        defaults = {}

        def default(slug):
            if slug not in defaults:
                defaults[slug] = self.default_or_none(slug) or ""
            return defaults[slug]
        out = [self.view(i, default(r["character"])) for i, r in self.index.items()
               if (character is None or r["character"] == character)
               and (emotion is None or emotion in self.tags.get(i, []))]
        if sort:
            out.sort(key=lambda r: r[sort], reverse=order == "desc")
        return out

    def register(self, ref_id: str, tags: list = None):
        """把一条候选（连音频）登记进资料库；已经在资料库里的只改标签（tags 为 None 时不改）。"""
        r = self.index[ref_id]
        lib = library.current()
        rel = ref_rel(r["character"], ref_id)
        if lib.has(rel):
            doc = lib.get(rel)
        else:
            src = Path(r["path"])
            lib.put_file(f"refs/{r['character']}/{ref_id}{src.suffix}", src)
            doc = dict(id=ref_id, character=r["character"], text=r["text"], lang=r["lang"], duration=r["duration"],
                       tags=[], audio=f"{ref_id}{src.suffix}", source=str(src))
        lib.put(rel, dict(doc, tags=doc["tags"] if tags is None else tags))
        self.index[ref_id] = dict(r, path=str(lib.path(rel).parent / doc["audio"]))
        self.tags[ref_id] = lib.get(rel)["tags"]

    def set_tags(self, ref_id: str, tags: list):
        self.register(ref_id, tags)
        return self.view(ref_id)

    def add_upload(self, data: bytes, suffix: str, text: str, lang: str, character: str):
        """上传的参考直接进资料库 refs/<character>/up-<sha1 前 12 位>.<后缀>。同一文件重复上传得到同一个 id。"""
        ref_id = "up-" + hashlib.sha1(data).hexdigest()[:12]
        lib = library.current()
        lib.put_bytes(f"refs/{character}/{ref_id}{suffix}", data)
        path = lib.path(f"refs/{character}/{ref_id}{suffix}")
        rel = ref_rel(character, ref_id)
        tags = lib.get(rel)["tags"] if lib.has(rel) else []
        sec = raw_duration(path)
        lib.put(rel, dict(id=ref_id, character=character, text=text, lang=lang,
                          duration=round(sec, 2), tags=tags, audio=path.name, source=None))
        self.index[ref_id] = dict(id=ref_id, character=character, text=text, lang=lang,
                                  duration=lib.get(rel)["duration"], path=str(path))
        self.tags[ref_id] = tags
        return self.view(ref_id)

    def validate_for_character(self, slug: str, ref_id: str, *, require_primary: bool = False) -> dict:
        """Validate that ref_id exists, belongs to slug, and if require_primary=True,
        that the backing audio file exists, is readable, and satisfies strict 3-10s sample bounds."""
        if ref_id not in self.index:
            raise KeyError(f"参考 {ref_id}")
        r = self.index[ref_id]
        if r["character"] != slug:
            raise ValueError(f"参考 {ref_id} 属于 {r['character']}，不属于 {slug}")
        if require_primary:
            sec = raw_duration(Path(r["path"]))
            if not is_valid_primary_duration(sec):
                raise ValueError(
                    f"默认主参考时长需在 {settings.REF_MIN_SEC:g}–{settings.REF_MAX_SEC:g} 秒之间（当前 {sec:.3f} 秒）"
                )
        return r

    @staticmethod
    def _is_usable_primary(r: dict) -> bool:
        """Verify backing file exists, is readable, and satisfies strict unrounded 3-10s bounds."""
        try:
            return is_valid_primary_duration(raw_duration(Path(r["path"])))
        except ValueError:
            return False

    def default_for(self, character: str):
        c = self.catalog.characters().get(character)  # 没登记的角色（上传参考用的 custom 之类）只有自动选
        if c and (explicit := c.get("default_ref")) in self.index and self._is_usable_primary(self.index[explicit]):
            return explicit
        if builtin := self.catalog.builtin_of(character):
            return self.default_for(builtin)
        candidates = sorted(
            (r for r in self.index.values() if r["character"] == character),
            key=lambda r: (not has_words(r["text"]), abs(r["duration"] - 6.0)),
        )
        for r in candidates:
            if self._is_usable_primary(r):
                return r["id"]
        raise KeyError(f"角色 {character} 没有可用默认参考音频")

    def default_or_none(self, character: str):
        """没有任何参考的角色（训练列表、数据集不在本机）返回 None，/models 里不列它。"""
        try:
            return self.default_for(character)
        except KeyError:
            return None

    def pick(self, character: str, ref_id: str = None, emotion: str = None):
        if ref_id:
            return self.index[ref_id]
        if emotion:
            tagged = self.list(character, emotion)
            if not tagged:
                raise KeyError(f"{character} 没有标签为 {emotion} 的参考")
            usable = [self.index[r["id"]] for r in tagged if self._is_usable_primary(self.index[r["id"]])]
            if not usable:
                raise KeyError(
                    f"{character} 标签为 {emotion} 的参考里没有可用的主参考"
                    f"（需 {settings.REF_MIN_SEC:g}–{settings.REF_MAX_SEC:g} 秒且音频文件可读）"
                )
            return random.choice(usable)
        return self.index[self.default_for(character)]
