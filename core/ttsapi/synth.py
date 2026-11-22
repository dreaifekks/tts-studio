"""合成、打分、候选，以及生成音频的存储（audio 表 + data/audio 下的文件）。"""
import asyncio
import json
import random
import secrets
import time
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Optional

from ttsapi import db, library, settings
from ttsapi.compute import remote, speeds
from ttsapi.models import AudioItem, CandidatesResult, Review, TTSParams

catalog = None  # gateway 启动时注入 Catalog 实例
refs = None  # 同上，RefLibrary 实例
# 合成、打分在哪台设备上跑（remote.LOCAL 或局域网设备的 remote.Remote）。任务开始时设，任务里的合成都走它
EXEC: ContextVar = ContextVar("compute", default=remote.LOCAL)


def resolve(engine: str, slug: str, gpt: str = None, sovits: str = None, version: str = None):
    """按当前执行设备解析权重：在局域网设备上跑时底模用那台自己的（本机没装也行），资料库里的权重照常由本机按哈希传。
    version：权重来自这个版本，只认它绑定的记录（见 Catalog.resolve）。"""
    return catalog.resolve(engine, slug, gpt, sovits, remote=EXEC.get() is not remote.LOCAL, version=version)


def iso(ts: float):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def new_audio_id():
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def pick_seed(seed: int):
    return seed if seed >= 0 else random.randrange(2**31 - 1000)


def total_score(s: dict):
    cer = s["cer_kana"] if s.get("cer_kana") is not None else s["cer"]
    return round(100 * (0.7 * (1 - min(cer, 1)) + 0.3 * max(s["spk_sim"], 0)), 2)


# ---------------------------------------------------------------- 存储

_AUDIO_COLS = (
    "id, created, text, character, engine, ref_id, gpt, sovits, temperature, "
    "top_k, top_p, score, work_id, keep, meta"
)


def _audio_values(item: dict, created: float) -> tuple:
    return (
        item["id"], created, item["text"], item["character"], item["engine"], item["ref_id"], item["gpt"],
        item["sovits"], item.get("temperature"), item.get("top_k"), item.get("top_p"), item.get("score"),
        item.get("work_id"), 0, json.dumps(item, ensure_ascii=False),
    )


def save_audio(item: dict, created: float):
    db.run(f"INSERT OR REPLACE INTO audio({_AUDIO_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           *_audio_values(item, created))


def insert_audio(item: dict, created: float):
    """Insert a new audio row without ever overwriting an existing record (raises IntegrityError on collision)."""
    db.run(f"INSERT INTO audio({_AUDIO_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           *_audio_values(item, created))


def review_of(audio_id: str) -> Optional[Review]:
    r = db.one("SELECT * FROM reviews WHERE audio_id=?", audio_id)
    if r is None:
        return None
    return Review(audio_id=audio_id, rating=r["rating"], verdict=r["verdict"], tags=json.loads(r["tags"]),
                  note=r["note"], updated=iso(r["updated"]))


def audio_item(row) -> AudioItem:
    return AudioItem(**json.loads(row["meta"]), review=review_of(row["id"]))


def get_audio(audio_id: str) -> AudioItem:
    """本机 tts.db 里的生成音频；本机没有这行时（换了控制端）用资料库里作品的快照（snapshot_of）。"""
    row = db.one("SELECT id, meta FROM audio WHERE id=?", audio_id)
    if row is not None:
        return audio_item(row)
    _, meta = snapshot_of(audio_id)
    return AudioItem(**meta, review=review_of(audio_id))


def snapshot_of(audio_id: str) -> tuple:
    """当前资料库的作品里这条音频的快照：(作品 id, 元数据)。挂到作品上的音频，元数据记在作品记录的 audio 里，
    文件复制在 works/<work_id>/audio/<audio_id>.wav（works.snapshot）。哪个作品都没有时 KeyError。"""
    lib = library.selected()
    for w in lib.docs("works") if lib else []:
        if audio_id in w["audio"]:
            return w["id"], w["audio"][audio_id]
    raise KeyError(audio_id)


class AudioElsewhere(Exception):
    """盲听记着这条音频，但这台主控的生成目录和资料库里都没有：在出题的主控（machine）上，资料库还没同步过来，
    或者是音频进资料库之前出的题。接口返回 404 {code: "audio_elsewhere", machine}。"""

    def __init__(self, audio_id: str, machine: Optional[str]):
        super().__init__(f"音频 {audio_id} 在{machine or '出题的主控'}上，这台主控上没有")
        self.machine = machine


def blind_audio(doc: dict, audio_id: str) -> Path:
    """盲听 doc 里的一条音频：资料库里这场的 blind/<id>/<音频 id>.wav；没有时本机生成目录里的（这台出的题，还没复制进
    资料库）。先读这场的：换了主控、本机恰好有同名的音频时不会播成别的。"""
    for f in (library.current().path(f"blind/{doc['id']}/{audio_id}.wav"), settings.AUDIO_DIR / f"{audio_id}.wav"):
        if f.is_file():
            return f
    raise AudioElsewhere(audio_id, doc.get("machine"))


def audio_file(audio_id: str) -> Path:
    """音频文件：本机生成目录里的；没有时找资料库里的每份副本（记着它的盲听目录、挂着它的作品的快照）。哪份都没有：
    有盲听记着它时 AudioElsewhere（在出题的主控上），否则 KeyError。"""
    wav = settings.AUDIO_DIR / f"{audio_id}.wav"
    if wav.is_file():
        return wav
    lib = library.selected()
    if lib is None:
        raise KeyError(audio_id)
    blinds = [b for b in lib.docs("blind") if any(audio_id in q["audio"].values() for q in b["questions"])]
    copies = [lib.path(f"blind/{b['id']}/{audio_id}.wav") for b in blinds] + \
        [lib.path(f"works/{w['id']}/audio/{audio_id}.wav") for w in lib.docs("works") if audio_id in w["audio"]]
    f = next((f for f in copies if f.is_file()), None)
    if f is not None:
        return f
    if blinds:
        raise AudioElsewhere(audio_id, blinds[0].get("machine"))
    raise KeyError(audio_id)


def attach_audio(audio_ids: list, work_id: str):
    """音频挂到作品的行上：本机 tts.db 有这条的，记下 work_id 并标记保留，不参与 7 天清理（只在资料库快照里的没有这行）。"""
    for row in db.query(f"SELECT id, meta FROM audio WHERE id IN ({','.join('?' * len(audio_ids))})", *audio_ids):
        meta = json.loads(row["meta"])
        meta["work_id"] = work_id
        db.run("UPDATE audio SET keep=1, work_id=?, meta=? WHERE id=?", work_id, json.dumps(meta, ensure_ascii=False),
               row["id"])


def keep_audio(audio_ids: list):
    """作品用到的、打过 review 的音频不参与自动清理。生成时只记 work_id，挂到作品的行上之后才标记保留。"""
    for a in audio_ids:
        db.run("UPDATE audio SET keep=1 WHERE id=?", a)


def held_audio() -> set:
    """资料库里的作品（每行的候选）和盲听（每题的两段）用到的音频。"""
    lib = library.selected()
    if lib is None:
        return set()
    held = {a for w in lib.docs("works") for line in w["lines"] for a in line["candidates"]}
    return held | {a for b in lib.docs("blind") for q in b["questions"] for a in q["audio"].values() if a}


def release_audio(audio_ids: list):
    """去掉音频的保留标记（之后按 7 天规则清理），仍被 review、作品或盲听引用的除外。"""
    held = held_audio()
    for a in audio_ids:
        if a not in held:
            db.run("UPDATE audio SET keep=0 WHERE id=? AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.audio_id=audio.id)",
                   a)


def import_legacy_meta():
    """第一版把元数据存成 data/audio/<id>.json，迁移进数据库。"""
    for f in settings.AUDIO_DIR.glob("*.json"):
        if db.one("SELECT 1 FROM audio WHERE id=?", f.stem) is None:
            item = json.loads(f.read_text(encoding="utf-8"))
            item["audio_url"] = f"/audio/{item['id']}"
            if "mel_url" in item:
                item["mel_url"] = f"/audio/{item['id']}/mel.png"
            for s in item.get("segments", []):
                for st in s.get("steps", []):
                    st["top5"] = [dict(token=t, p=p) for t, p in st["top5"]]
            item.setdefault("created", iso(f.stat().st_mtime))
            save_audio(item, f.stat().st_mtime)
        f.unlink()


def cleanup_audio(log):
    """删掉超过 7 天（或超出数量上限）、没标记保留的生成音频。资料库里作品、盲听引用的照样保留：
    挂候选时先写作品记录再标记保留，两步之间退出时 keep 还是 0，不能靠它。"""
    held = held_audio()
    rows = [r for r in db.query("SELECT id, created FROM audio WHERE keep=0 ORDER BY created") if r["id"] not in held]
    cutoff = time.time() - settings.AUDIO_KEEP_DAYS * 86400
    stale = [r["id"] for i, r in enumerate(rows) if r["created"] < cutoff or i < len(rows) - settings.AUDIO_KEEP_MAX]
    for audio_id in stale:
        try:
            for f in settings.AUDIO_DIR.glob(audio_id + ".*"):
                f.unlink()
        except PermissionError:  # 正在被下载的文件删不掉，保留这条记录，下一轮再删
            continue
        db.run("DELETE FROM audio WHERE id=? AND keep=0", audio_id)
    # 任务被取消时，worker 已经写出但还没入库的音频没有记录，一天后按文件清掉
    known = {r["id"] for r in db.query("SELECT id FROM audio")} | held
    for f in settings.AUDIO_DIR.iterdir():
        if f.name.split(".")[0] not in known and f.stat().st_mtime < time.time() - 86400:
            f.unlink()
    if stale:
        log.info("cleaned %d old audio", len(stale))


# ---------------------------------------------------------------- 合成

class AuxRefsUnsupported(ValueError):
    """Raised when non-empty aux_ref_ids are requested for an engine that does not support multi-ref fusion."""


class AuxRefUnavailable(Exception):
    """Raised when an auxiliary reference exists in the index but its audio file is missing locally."""


def resolve_aux_refs(aux_ref_ids: list[str], main_ref_id: str) -> tuple[list[str], list[str]]:
    """Validate and deduplicate aux_ref_ids, skipping empty entries and main_ref_id; return (ids, paths)."""
    ids, paths, seen = [], [], {main_ref_id}
    for rid in aux_ref_ids or []:
        if not rid:
            continue
        if rid not in refs.index:
            raise KeyError(f"参考 {rid}")
        if rid in seen:
            continue
        path = refs.index[rid]["path"]
        if not Path(path).is_file():
            raise AuxRefUnavailable(f"辅助参考 {rid} 的音频文件尚未同步到本机，同步完再试")
        seen.add(rid)
        ids.append(rid)
        paths.append(path)
    return ids, paths


def validate_aux_refs(engines: list[str], aux_ref_ids: list[str], main_ref_id: str = "") -> tuple[list[str], list[str]]:
    """Resolve aux_ref_ids and reject non-empty auxiliary refs on engines without multi-ref support."""
    ids, paths = resolve_aux_refs(aux_ref_ids, main_ref_id)
    if ids:
        unsupported = [e for e in dict.fromkeys(engines) if not settings.ENGINES[e]["supports_aux_refs"]]
        if unsupported:
            raise AuxRefsUnsupported(f"引擎 {', '.join(unsupported)} 不支持辅助参考音频融合")
    return ids, paths


async def synth(p: TTSParams, engine: str, ref: dict, weights: tuple, seed: int, score: bool,
                work_id: Optional[str] = None):
    """在 engine worker 上合成一条，返回一个协程函数：await 它会等打分/频谱图做完、入库并返回 AudioItem。
    这样候选接口可以让下一条合成和这一条的打分并行。"""
    gpt, gpt_path, sovits, sovits_path = weights
    aux_ids, aux_paths = validate_aux_refs([engine], p.aux_ref_ids, ref["id"])
    ex = EXEC.get()
    audio_id = new_audio_id()
    created = time.time()
    wav = settings.AUDIO_DIR / f"{audio_id}.wav"
    t = time.perf_counter()
    files = dict(ref_audio=ref["path"], gpt=gpt_path, sovits=sovits_path)
    if aux_paths:
        files["aux_ref_audio_paths"] = aux_paths
    res = await ex.call(engine, "/synth", dict(
        text=p.text, text_lang=p.text_lang, prompt_text=ref["text"], prompt_lang=ref["lang"], top_k=p.top_k,
        top_p=p.top_p, temperature=p.temperature, seed=seed, speed=p.speed, fragment_interval=p.fragment_interval,
        repetition_penalty=p.repetition_penalty, text_split_method=p.text_split_method, sample_steps=p.sample_steps,
        aux_ref_audio_paths=[], debug=p.debug,
    ), files=files, outputs=dict(out_path=wav),
        pretrained={k: key for k, path in (("gpt", gpt_path), ("sovits", sovits_path))
                    if (key := remote.pretrained_key(engine, path))})
    speeds.observe(ex.id, engine, speeds.per_hard(res["infer_sec"], p.text))
    item = dict(
        id=audio_id, created=iso(created), audio_url=f"/audio/{audio_id}", text=p.text, text_lang=p.text_lang,
        character=p.character, engine=engine, gpt=gpt, sovits=sovits, ref_id=ref["id"], seed=seed,
        top_k=p.top_k, top_p=p.top_p, temperature=p.temperature, repetition_penalty=p.repetition_penalty,
        speed=p.speed, fragment_interval=p.fragment_interval, text_split_method=p.text_split_method,
        sample_steps=p.sample_steps, aux_ref_ids=aux_ids,
        duration=res["duration"], infer_sec=res["infer_sec"], synth_wall_sec=round(time.perf_counter() - t, 2),
        semantic_tokens=res["semantic_tokens"], vram_peak_mb=res["vram_peak_mb"],
        ratio_vs_ref=[s["ratio_vs_ref"] for s in res["segments"]], work_id=work_id, compute=ex.id,
    )
    jobs = []
    if score:
        jobs.append(_score(ex, p, wav, ref))
    if p.debug:
        item["mel_url"] = f"/audio/{audio_id}/mel.png"
        item["segments"] = res["segments"]
        jobs.append(ex.call("scorer", "/mel", dict(title=f"{audio_id} {engine} seed={seed}"), files=dict(audio=wav),
                            outputs=dict(png=wav.with_suffix(".png"))))

    async def finish():
        done = await asyncio.gather(*jobs)
        if score:
            item.update(done[0])
            item["score"] = total_score(item)
        save_audio(item, created)
        return AudioItem(**item)

    return finish


async def _score(ex, p: TTSParams, wav, ref: dict):
    t = time.perf_counter()
    res = await ex.call("scorer", "/score", dict(text=p.text, text_lang=p.text_lang),
                        files=dict(audio=wav, ref_audio=ref["path"]))
    speeds.observe(ex.id, "scorer", time.perf_counter() - t)
    return res


async def _reported(finish, on_item):
    item = await finish()
    if on_item:
        on_item(item)
    return item


async def run_candidates(p: TTSParams, engine: str, n: int, ref: dict, base_seed: int,
                         work_id: Optional[str] = None, on_item=None) -> CandidatesResult:
    """on_item(AudioItem)：每条候选打完分就调用一次，任务用它逐条推进度。"""
    weights = resolve(engine, p.character, p.gpt, p.sovits, version=p.version)
    t = time.perf_counter()
    pending = []
    # 合成在 engine worker 上串行，打分交给 scorer worker 和下一条合成并行
    try:
        for i in range(n):
            finish = await synth(p, engine, ref, weights, base_seed + i, score=True, work_id=work_id)
            pending.append(asyncio.create_task(_reported(finish, on_item)))
        items = await asyncio.gather(*pending)
    finally:
        for task in pending:
            task.cancel()
    items = sorted(items, key=lambda c: c.score, reverse=True)
    return CandidatesResult(engine=engine, ref_id=ref["id"], ref_text=ref["text"],
                            elapsed_sec=round(time.perf_counter() - t, 2), candidates=items)
