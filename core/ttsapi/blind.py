"""盲听 A/B：同一角色、同一参考，两个 GPT+SoVITS 组合（可以是不同引擎）把评测集的每句各念一遍（或几遍），
每道题左右顺序随机。题目接口只给 /blind/{id}/questions/{qid}/left|right 这样的地址，不给音频 id 和模型信息；
揭晓前盲听本身的 a / b 也是 null，自动标题不含模型。全部投完（或主动揭晓）后才给出两边的组合，
/result 给出每题哪边是哪个组合和胜负统计。

一场盲听是资料库里的一个文件 blind/<test_id>.json（题目、投票、揭晓）；题目的音频跟着进资料库 blind/<test_id>/<音频 id>.wav，
换主控、换数据目录照样能听。出题的主控本机也留一份（tts.db 的 audio，被盲听引用的不参与 7 天清理）；记录里的 machine 是
出题的主控在设备列表里的名字（registry.local_name），资料库里的音频还没同步过来时接口说「音频在 X 上」（synth.AudioElsewhere）。"""
import asyncio
import logging
import random
import secrets
import time
from datetime import datetime

from fastapi import HTTPException

from ttsapi import jobs, library, settings, synth
from ttsapi.compute import registry
from ttsapi.models import (BlindCombo, BlindComboResult, BlindCreate, BlindQuestion, BlindResult, BlindRevealQuestion,
                           BlindSummary, BlindTest, TTSParams)
from ttsapi.training import gpu_lock

log = logging.getLogger("tts-api")
SIDES = ("A", "B")


def rel(test_id: str):
    return f"blind/{test_id}.json"


def load(test_id: str) -> dict:
    return library.current().get(rel(test_id))


def save(doc: dict):
    library.current().put(rel(doc["id"]), doc)


def create(req: BlindCreate, catalog, refs, remote: bool = False) -> dict:
    """remote：生成派到了局域网设备（底模用那台的，见 Catalog.entries）。"""
    ref_id = req.ref_id or refs.default_for(req.character)
    refs.index[ref_id]
    combos = {}
    for label, side in zip(SIDES, (req.a, req.b), strict=True):
        gpt, _, sovits, _ = catalog.resolve(side.engine, req.character, side.gpt, side.sovits, remote,
                                            version=side.version)
        combos[label] = dict(engine=side.engine, gpt=gpt, sovits=sovits, version=side.version)
    sentences = settings.EVAL_SETS[req.eval_set]["sentences"]
    questions = [dict(id=f"q{i + 1:02d}", sentence=k, repeat=r, seed=random.randrange(2**31 - 1000),
                      left=random.choice(SIDES), audio=dict(A=None, B=None), score=dict(A=None, B=None),
                      vote=None, note="", voted_at=None)
                 for i, (k, r) in enumerate((k, r) for k in range(len(sentences)) for r in range(req.repeats))]
    now = time.time()
    doc = dict(id=f"b-{secrets.token_hex(4)}", created=synth.iso(now), created_ts=now,
               title=req.title or f"{req.character} · {req.eval_set} · {datetime.fromtimestamp(now):%Y-%m-%d %H:%M}",
               character=req.character, eval_set=req.eval_set, repeats=req.repeats, ref_id=ref_id, combos=combos,
               questions=questions, revealed_at=None, job_id=None, machine=registry.local_name())
    save(doc)
    return doc


def involves(doc: dict, catalog) -> set:
    """生成任务用到的角色：盲听的角色、两边权重所属的角色、参考音频所属的角色。"""
    owners = {catalog.owner(c["engine"], c[k]) for c in doc["combos"].values() for k in ("gpt", "sovits")}
    return ({doc["character"], synth.refs.index[doc["ref_id"]]["character"]} | owners) - {None}


async def generate_job(run, test_id: str):
    """生成还没有音频的题。和训练、评测用同一把锁：训练期间排队，生成期间新的训练也会等它做完。
    每道题 A、B 用同一个 seed，先后顺序随机；每题生成完就存一次，生成中途也能开始听和投票。"""
    run.job.status = "queued"
    run.stage("等待 GPU（训练、评测结束）", 0.0)
    async with gpu_lock():
        run.job.status = "running"
        try:
            await _generate(run, test_id)
        except Exception as e:
            # worker 和权重查找的报错带引擎名、权重文件名，任务的 error / 日志揭晓前客户端也看得到：原文只写服务端日志
            log.warning("blind %s generation failed: %s", test_id, e, exc_info=True)
            raise RuntimeError(f"盲听 {test_id} 生成失败，原因见服务端日志") from None


async def _generate(run, test_id: str):
    doc = load(test_id)
    ref = synth.refs.index[doc["ref_id"]]
    weights = {s: synth.resolve(c["engine"], doc["character"], c["gpt"], c["sovits"], version=c.get("version"))
               for s, c in doc["combos"].items()}
    todo = [q["id"] for q in doc["questions"] if not (q["audio"]["A"] and q["audio"]["B"])]
    for i, qid in enumerate(todo):
        q = next(q for q in doc["questions"] if q["id"] == qid)
        run.stage(f"第 {i + 1}/{len(todo)} 题", i / len(todo))
        text = settings.EVAL_SETS[doc["eval_set"]]["sentences"][q["sentence"]]["text"]
        finishes = {}
        for s in random.sample(SIDES, 2):
            c = doc["combos"][s]
            p = TTSParams(text=text, character=doc["character"], engine=c["engine"], gpt=c["gpt"], sovits=c["sovits"])
            finishes[s] = await synth.synth(p, c["engine"], ref, weights[s], q["seed"], score=True)
        items = dict(zip(finishes, await asyncio.gather(*(f() for f in finishes.values())), strict=True))
        # 生成期间用户可能投了票，重新读一遍再写回这一题的音频；盲听被删了这里就 404，音频不标记保留、不进资料库
        doc = load(test_id)
        for it in items.values():
            library.current().put_file(f"blind/{test_id}/{it.id}.wav", settings.AUDIO_DIR / f"{it.id}.wav")
        synth.keep_audio([it.id for it in items.values()])
        q = next(q for q in doc["questions"] if q["id"] == qid)
        q["audio"] = {s: items[s].id for s in SIDES}
        q["score"] = {s: items[s].score for s in SIDES}
        doc["machine"] = registry.local_name()
        save(doc)
    run.log(f"生成完 {len(todo)} 题")


def _question_view(doc: dict, q: dict, index: int) -> BlindQuestion:
    s = settings.EVAL_SETS[doc["eval_set"]]["sentences"][q["sentence"]]
    ready = bool(q["audio"]["A"] and q["audio"]["B"])
    base = f"/blind/{doc['id']}/questions/{q['id']}"
    return BlindQuestion(id=q["id"], index=index, text=s["text"], tests=s["tests"], ready=ready,
                         left_url=f"{base}/left" if ready else None, right_url=f"{base}/right" if ready else None,
                         vote=q["vote"], note=q["note"], voted_at=q["voted_at"])


def _status(doc: dict):
    """没生成完的：生成任务在本机跑着（排队、运行中的任务都在 jobs.runs 里）是 generating，否则 failed。
    不查本机的任务记录：换了控制端时盲听同步过来了，生成它的任务行不在这台机器上。"""
    if all(q["audio"]["A"] and q["audio"]["B"] for q in doc["questions"]):
        return "ready"
    return "generating" if doc["job_id"] in jobs.runs else "failed"


def summary(doc: dict) -> BlindSummary:
    qs = doc["questions"]
    n_voted = sum(1 for q in qs if q["vote"])
    shown = revealed(doc)
    a, b = (BlindCombo(**doc["combos"][s]) if shown else None for s in SIDES)
    return BlindSummary(id=doc["id"], title=doc["title"], created=doc["created"], character=doc["character"],
                        eval_set=doc["eval_set"], repeats=doc["repeats"], ref_id=doc["ref_id"], a=a, b=b,
                        status=_status(doc), job_id=doc["job_id"], n_questions=len(qs),
                        n_ready=sum(1 for q in qs if q["audio"]["A"] and q["audio"]["B"]), n_voted=n_voted,
                        revealed=shown)


def to_model(doc: dict) -> BlindTest:
    return BlindTest(**summary(doc).model_dump(),
                     questions=[_question_view(doc, q, i) for i, q in enumerate(doc["questions"])])


def revealed(doc: dict):
    return doc["revealed_at"] is not None or all(q["vote"] for q in doc["questions"])


def list_tests(character: str = None, limit: int = 50, offset: int = 0):
    docs = sorted((d for d in library.current().docs("blind") if character is None or d["character"] == character),
                  key=lambda d: d["created_ts"], reverse=True)
    return len(docs), [summary(d) for d in docs[offset:offset + limit]]


def find_question(doc: dict, qid: str) -> dict:
    for q in doc["questions"]:
        if q["id"] == qid:
            return q
    raise KeyError(f"题目 {qid}")


def audio_path(test_id: str, qid: str, side: str):
    doc = load(test_id)
    q = find_question(doc, qid)
    s = q["left"] if side == "left" else ({"A": "B", "B": "A"}[q["left"]])
    if not q["audio"][s]:
        raise HTTPException(409, f"题目 {qid} 还没生成好")
    return synth.blind_audio(doc, q["audio"][s])


def vote(test_id: str, qid: str, choice: str, note: str) -> BlindQuestion:
    doc = load(test_id)
    q = find_question(doc, qid)
    if not (q["audio"]["A"] and q["audio"]["B"]):
        raise HTTPException(409, f"题目 {qid} 还没生成好，不能投票")
    q.update(vote=choice, note=note, voted_at=synth.iso(time.time()))
    save(doc)
    return _question_view(doc, q, doc["questions"].index(q))


def reveal(test_id: str) -> BlindResult:
    doc = load(test_id)
    if doc["revealed_at"] is None:
        doc["revealed_at"] = synth.iso(time.time())
        save(doc)
    return result(test_id)


def result(test_id: str) -> BlindResult:
    doc = load(test_id)
    if not revealed(doc):
        n = sum(1 for q in doc["questions"] if not q["vote"])
        raise HTTPException(409, f"还有 {n} 题没投，投完或先 POST /blind/{test_id}/reveal 揭晓")
    counts = {s: dict(wins=0, losses=0, ties=0) for s in SIDES}
    reveal_qs = []
    for i, q in enumerate(doc["questions"]):
        winner = None
        if q["vote"] == "tie":
            winner = "tie"
            for s in SIDES:
                counts[s]["ties"] += 1
        elif q["vote"]:
            winner = q["left"] if q["vote"] == "left" else ({"A": "B", "B": "A"}[q["left"]])
            counts[winner]["wins"] += 1
            counts[{"A": "B", "B": "A"}[winner]]["losses"] += 1
        s = settings.EVAL_SETS[doc["eval_set"]]["sentences"][q["sentence"]]
        ready = bool(q["audio"]["A"] and q["audio"]["B"])
        reveal_qs.append(BlindRevealQuestion(
            id=q["id"], index=i, text=s["text"], tests=s["tests"], vote=q["vote"], note=q["note"],
            left=q["left"] if ready else None, winner=winner,
            a_audio_url=f"/audio/{q['audio']['A']}" if q["audio"]["A"] else None,
            b_audio_url=f"/audio/{q['audio']['B']}" if q["audio"]["B"] else None,
            a_score=q["score"]["A"], b_score=q["score"]["B"]))
    n_voted = sum(1 for q in doc["questions"] if q["vote"])

    def side(s):
        c = counts[s]
        scores = [q["score"][s] for q in doc["questions"] if q["score"][s] is not None]
        return BlindComboResult(label=s, combo=BlindCombo(**doc["combos"][s]), **c,
                                win_rate=round((c["wins"] + 0.5 * c["ties"]) / n_voted, 4) if n_voted else None,
                                auto_score_mean=round(sum(scores) / len(scores), 2) if scores else None)

    return BlindResult(id=doc["id"], title=doc["title"], character=doc["character"], eval_set=doc["eval_set"],
                       n_questions=len(doc["questions"]), n_voted=n_voted,
                       revealed_at=doc["revealed_at"] or max(q["voted_at"] for q in doc["questions"]),
                       a=side("A"), b=side("B"), questions=reveal_qs)


async def delete(test_id: str):
    """删除盲听：先删记录（之后的补生成请求会 404，正在跑的生成任务写回时也找不到它），生成任务还在跑就取消；
    资料库里它的音频一起删，本机的那份不在作品里、没打 review 的，重新参与 7 天自动清理。"""
    doc = load(test_id)
    lib = library.current()
    lib.delete(rel(test_id))
    if lib.path(f"blind/{test_id}").is_dir():
        lib.delete_tree(f"blind/{test_id}")
    if doc["job_id"] in jobs.runs:
        await jobs.cancel(doc["job_id"])
    synth.release_audio([a for q in doc["questions"] for a in q["audio"].values() if a])
