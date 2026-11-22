"""review、历史记录查询和按设置汇总的统计。"""
import json
import math
import time
from collections import defaultdict
from typing import Optional

from ttsapi import db, synth
from ttsapi.models import AudioPage, Review, ReviewIn, ReviewStats, StatsDimension, StatsGroup


def upsert(audio_id: str, r: ReviewIn) -> Review:
    synth.get_audio(audio_id)
    db.run("INSERT OR REPLACE INTO reviews(audio_id, rating, verdict, tags, note, updated) VALUES(?,?,?,?,?,?)",
           audio_id, r.rating, r.verdict, json.dumps(r.tags, ensure_ascii=False), r.note, time.time())
    synth.keep_audio([audio_id])
    return synth.review_of(audio_id)


def delete(audio_id: str):
    if db.one("SELECT 1 FROM reviews WHERE audio_id=?", audio_id) is None:
        raise KeyError(f"{audio_id} 的 review")
    db.run("DELETE FROM reviews WHERE audio_id=?", audio_id)
    # 不在任何作品、盲听里的音频重新参与 7 天清理
    synth.release_audio([audio_id])


def _where(character=None, engine=None, ref_id=None, gpt=None, sovits=None, work_id=None, q=None,
           temperature_min=None, temperature_max=None, reviewed=None, tag=None, verdict=None, min_rating=None):
    conds, args = [], []
    for col, val in (("a.character", character), ("a.engine", engine), ("a.ref_id", ref_id), ("a.gpt", gpt),
                     ("a.sovits", sovits), ("a.work_id", work_id), ("r.verdict", verdict)):
        if val is not None:
            conds.append(f"{col}=?")
            args.append(val)
    for cond, val in (("a.text LIKE ?", q and f"%{q}%"), ("a.temperature>=?", temperature_min),
                      ("a.temperature<=?", temperature_max), ("r.tags LIKE ?", tag and f'%"{tag}"%'),
                      ("r.rating>=?", min_rating)):
        if val is not None:
            conds.append(cond)
            args.append(val)
    if reviewed is not None:
        conds.append("r.audio_id IS NOT NULL" if reviewed else "r.audio_id IS NULL")
    return (" WHERE " + " AND ".join(conds) if conds else ""), args


SORT_COLUMNS = dict(created="a.created", score="a.score", duration="json_extract(a.meta, '$.duration')",
                    character="a.character", engine="a.engine", rating="r.rating", reviewed_at="r.updated")


def page(limit: int, offset: int, sort: str = "created", order: str = "desc", **filters) -> AudioPage:
    where, args = _where(**filters)
    base = f"FROM audio a LEFT JOIN reviews r ON r.audio_id=a.id{where}"
    total = db.one(f"SELECT COUNT(*) AS n {base}", *args)["n"]
    # 空值（没打分、没 review）不管升序降序都排最后；同值按生成时间新的在前
    col = SORT_COLUMNS[sort]
    rows = db.query(f"SELECT a.id, a.meta {base} ORDER BY {col} IS NULL, {col} {order.upper()}, a.created DESC "
                    "LIMIT ? OFFSET ?", *args, limit, offset)
    return AudioPage(total=total, items=[synth.audio_item(r) for r in rows])


def _group(key: str, items: list) -> StatsGroup:
    n = len(items)
    ratings = [r["rating"] for r, _ in items if r["rating"] is not None]
    good = sum(1 for r, _ in items if r["verdict"] == "good" or (r["rating"] or 0) >= 4)
    bad = sum(1 for r, _ in items if r["verdict"] == "bad" or (r["rating"] is not None and r["rating"] <= 2))
    scores = [m["score"] for _, m in items if m.get("score") is not None]
    tags = defaultdict(int)
    for r, _ in items:
        for t in set(json.loads(r["tags"])):
            tags[t] += 1
    return StatsGroup(key=key, n=n, avg_rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
                      good_ratio=round(good / n, 3), bad_ratio=round(bad / n, 3),
                      avg_score=round(sum(scores) / len(scores), 2) if scores else None,
                      tag_ratios={t: round(c / n, 3) for t, c in sorted(tags.items(), key=lambda x: -x[1])})


def _temperature_bucket(t: Optional[float]):
    if t is None:
        return "unknown"
    lo = math.floor(t * 5 + 1e-9) / 5
    return f"{lo:.1f}-{lo + 0.2:.1f}"


DIMENSIONS = {
    "engine": lambda m: m["engine"],
    "character": lambda m: m["character"],
    "ref_id": lambda m: m["ref_id"],
    "weights": lambda m: f"{m['gpt']} + {m['sovits']}",
    "temperature": lambda m: _temperature_bucket(m.get("temperature")),
    "top_k": lambda m: str(m.get("top_k")),
}


def stats(**filters) -> ReviewStats:
    where, args = _where(reviewed=True, **filters)
    rows = db.query(f"SELECT r.*, a.meta FROM audio a LEFT JOIN reviews r ON r.audio_id=a.id{where}", *args)
    items = [(r, json.loads(r["meta"])) for r in rows]
    dims = []
    for name, keyfn in DIMENSIONS.items():
        groups = defaultdict(list)
        for r, m in items:
            groups[keyfn(m)].append((r, m))
        dims.append(StatsDimension(dimension=name, groups=sorted(
            (_group(k, v) for k, v in groups.items()), key=lambda g: -g.n)))
    overall = _group("all", items) if items else StatsGroup(key="all", n=0, good_ratio=0, bad_ratio=0, tag_ratios={})
    return ReviewStats(overall=overall, dimensions=dims)
