"""作品：多行台词，每行出候选、选一条，最后按停顿拼成整轨并出字幕。

作品是资料库里的 works/<work_id>.json；渲染产物在 works/<work_id>/ 下。候选音频是本机的生成音频（tts.db 的 audio），
挂到作品上时在资料库里留一份快照：元数据记在作品记录的 audio（{audio_id: 元数据}），文件复制到
works/<work_id>/audio/<audio_id>.wav。换了控制端、本机 tts.db 没有这些音频时，作品照样能打开、渲染（synth.get_audio）。
挂在作品上的音频不参与 7 天清理。"""
import asyncio
import itertools
import secrets
import time

from fastapi import HTTPException

from ttsapi import db, library, render, synth
from ttsapi.models import (AddLineRequest, GenerateRequest, GenerateResult, GenParams, LineGenResult, TTSParams, Work,
                           WorkCreate, WorkLine, WorkLineIn, WorkPatch, WorkSummary)

# 行上存的生成参数（不含在哪跑：compute、approved 是这次提交的）；version 另外带上（旧的行没有这个字段），
# 用版本建的行生成时同样只认这个角色自己的权重
GEN_FIELDS = set(GenParams.model_fields) - {"version"}
# 从已有音频建行时沿用的采样参数（seed 不沿用：之后再生成这一行时用新的随机 seed）
LINE_PARAMS = ("top_k", "top_p", "temperature", "repetition_penalty", "speed", "fragment_interval",
               "text_split_method", "sample_steps", "aux_ref_ids")


def load(work_id: str, lib=None) -> dict:
    """lib：不给时是当前资料库；渲染任务用提交时的资料库（任务期间可能切了库）。"""
    return (lib or library.current()).get(f"works/{work_id}.json")


def save(doc: dict, lib=None):
    doc["updated"] = synth.iso(time.time())
    (lib or library.current()).put(f"works/{doc['id']}.json", doc)


def snapshot(doc: dict, audio_ids: list):
    """候选音频的快照放进作品（还没保存）：文件复制到 works/<work_id>/audio/，元数据记进 doc["audio"]。
    debug 的逐步采样和频谱图不进快照。"""
    lib = library.current()
    for a in audio_ids:
        if a not in doc["audio"]:
            lib.put_file(f"works/{doc['id']}/audio/{a}.wav", synth.audio_file(a))
            doc["audio"][a] = synth.get_audio(a).model_dump(exclude={"review", "segments", "mel_url"})


def line_doc(line: WorkLineIn, old: dict = None):
    d = line.model_dump()
    keep = old or dict(id=secrets.token_hex(4), candidates=[], picked=None)
    d.update(id=keep["id"], candidates=keep["candidates"], picked=keep["picked"])
    return d


def to_model(doc: dict) -> Work:
    lines = [WorkLine(**{**l, "candidates": [synth.get_audio(a) for a in l["candidates"]]}) for l in doc["lines"]]
    return Work(id=doc["id"], title=doc["title"], created=doc["created"], updated=doc["updated"], lines=lines,
                render=doc.get("render"))


def summary(doc: dict) -> WorkSummary:
    return WorkSummary(id=doc["id"], title=doc["title"], created=doc["created"], updated=doc["updated"],
                       n_lines=len(doc["lines"]), n_picked=sum(1 for l in doc["lines"] if l["picked"]),
                       rendered_at=(doc.get("render") or {}).get("rendered_at"))


def create(req: WorkCreate) -> Work:
    now = time.time()
    doc = dict(id=f"w-{secrets.token_hex(4)}", title=req.title, created=synth.iso(now), created_ts=now,
               lines=[line_doc(l) for l in req.lines], audio={}, render=None)
    save(doc)
    return to_model(doc)


def list_works(sort: str = "created", order: str = "desc"):
    out = [summary(d) for d in sorted(library.current().docs("works"), key=lambda d: d["created_ts"], reverse=True)]
    out.sort(key=lambda w: getattr(w, sort), reverse=order == "desc")
    return out


def patch(work_id: str, req: WorkPatch) -> Work:
    doc = load(work_id)
    if req.title is not None:
        doc["title"] = req.title
    if req.lines is not None:
        old = {l["id"]: l for l in doc["lines"]}
        doc["lines"] = [line_doc(l, old.get(l.id)) for l in req.lines]
    save(doc)
    return to_model(doc)


def delete(work_id: str):
    load(work_id)
    lib = library.current()
    lib.delete(f"works/{work_id}.json")
    if lib.path(f"works/{work_id}").exists():
        lib.delete_tree(f"works/{work_id}")
    # 作品删掉后，没打过 review、也不在盲听里的音频重新参与自动清理
    synth.release_audio([r["id"] for r in db.query("SELECT id FROM audio WHERE work_id=?", work_id)])


def find_line(doc: dict, line_id: str):
    for l in doc["lines"]:
        if l["id"] == line_id:
            return l
    raise KeyError(f"行 {line_id}")


def pick(work_id: str, line_id: str, audio_id: str) -> Work:
    doc = load(work_id)
    line = find_line(doc, line_id)
    if audio_id not in line["candidates"]:
        raise HTTPException(400, f"{audio_id} 不是这一行的候选")
    line["picked"] = audio_id
    save(doc)
    return to_model(doc)


def attach(work_id: str, line_id: str, audio_id: str, pick_it: bool) -> Work:
    """把已有的生成音频挂到某一行上作为候选（可同时选中）。"""
    synth.get_audio(audio_id)
    doc = load(work_id)
    line = find_line(doc, line_id)
    if audio_id not in line["candidates"]:
        line["candidates"].append(audio_id)
    if pick_it:
        line["picked"] = audio_id
    snapshot(doc, [audio_id])
    save(doc)
    synth.attach_audio([audio_id], work_id)
    return to_model(doc)


def add_line(work_id: str, req: AddLineRequest) -> Work:
    """新增一行；带 audio_id 时这条音频成为这一行的候选并被选中，不填 line 就按这条音频的参数建行。"""
    if req.line is None and req.audio_id is None:
        raise HTTPException(400, "line 和 audio_id 至少给一个")
    doc = load(work_id)
    line = req.line
    if line is None:
        a = synth.get_audio(req.audio_id)
        line = WorkLineIn(character=a.character, text=a.text, lang=a.text_lang or "ja", ref_id=a.ref_id,
                          engine=a.engine, gpt=a.gpt, sovits=a.sovits,
                          **{k: getattr(a, k) for k in LINE_PARAMS if getattr(a, k) is not None})
    new = line_doc(line)
    doc["lines"].insert(len(doc["lines"]) if req.position is None else req.position, new)
    save(doc)
    if req.audio_id:
        return attach(work_id, new["id"], req.audio_id, True)
    return to_model(doc)


def check_lines(work_id: str, line_ids):
    doc = load(work_id)
    for lid in line_ids or []:
        find_line(doc, lid)


def targets(doc: dict, line_ids) -> list:
    """要生成的行：指定的行，没指定时是还没有候选的行。"""
    return [l for l in doc["lines"] if (l["id"] in line_ids if line_ids else not l["candidates"])]


async def generate_job(run, work_id: str, req: GenerateRequest):
    targets_ = [l["id"] for l in targets(load(work_id), req.line_ids)]
    result = run.job.generate_result = GenerateResult(work_id=work_id, lines=[])
    done = itertools.count(1)
    total = len(targets_) * req.n
    for i, line_id in enumerate(targets_):
        # 每行开始前重新读：生成期间用户可能改了这一行的文本、参数，或者删了这一行
        line = next((l for l in load(work_id)["lines"] if l["id"] == line_id), None)
        if line is None:
            continue
        run.stage(f"第 {i + 1}/{len(targets_)} 行：{line['text'][:20]}")
        p = TTSParams(text=line["text"], character=line["character"], text_lang=line["lang"],
                      version=line.get("version"), **{k: line[k] for k in GEN_FIELDS if k in line})
        ref = synth.refs.pick(line["character"], line["ref_id"], line["emotion"])
        res = await synth.run_candidates(
            p, line["engine"], req.n, ref, synth.pick_seed(line["seed"]), work_id=work_id,
            on_item=lambda item, line_id=line_id: run.item_done(item, next(done), total, line_id))
        ids = [c.id for c in res.candidates]
        # 生成期间用户可能改过作品，重新读一遍再写回这一行
        doc = load(work_id)
        target = next((l for l in doc["lines"] if l["id"] == line["id"]), None)
        if target is not None:
            target["candidates"] += ids
            target["picked"] = ids[0]
            snapshot(doc, ids)
            save(doc)
            synth.attach_audio(ids, work_id)
        result.lines.append(LineGenResult(line_id=line["id"], picked=ids[0], best_score=res.candidates[0].score,
                                          candidates=ids))
        run.log(f"line {line['id']}: best {ids[0]} score {res.candidates[0].score}")


def render_task(work_id: str):
    """提交渲染任务时调用：固定当前资料库，返回任务函数（jobs.start 的 fn）。"""
    lib = library.current()
    load(work_id, lib)
    return lambda run: render_job(run, work_id, lib)


async def render_job(run, work_id: str, lib):
    """渲染线程只读选中音频的快照、在内存里出产物；回到这里（任务没被取消）并且作品还在时，才发布进提交任务时的资料库 lib。
    任务取消只结束等待的协程，线程照样算完，算完的结果没人发布，直接丢掉。"""
    doc = load(work_id, lib)
    missing = [l["id"] for l in doc["lines"] if not l["picked"]]
    if missing:
        from ttsapi.jobs import UserError
        raise UserError(f"这些行还没有选中的候选：{', '.join(missing)}")
    run.stage("拼接、响度归一、导出", 0.1)
    result, files = await asyncio.to_thread(render.render, doc, lib.path(f"works/{work_id}/audio"))
    if not lib.path(f"works/{work_id}.json").is_file():  # 看文件：切走再切回来时删除可能是另一个 Library 对象做的
        from ttsapi.jobs import UserError
        raise UserError(f"作品 {work_id} 在渲染期间被删除，产物不保存")
    for name, data in files.items():
        lib.put_bytes(f"works/{work_id}/{name}", data)
    doc = load(work_id, lib)
    doc["render"] = result.model_dump()
    save(doc, lib)
    run.job.render_result = result
