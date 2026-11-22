"""在 GPT 自回归采样处挂钩，不改 GPT-SoVITS 源码。

engine worker 先 import TTS，再 import 本模块并调用 install()。
每次合成前把 REC 设成 Recorder，合成后读出每段的 token 数，detail=True 时还有逐步的采样信息。
"""
import torch

import AR.models.t2s_model as t2s_mod
from text import symbols2

REC = None


class Recorder:
    def __init__(self, detail: bool):
        self.detail = detail
        self.eos = None
        self.segments = []

    def begin(self, x, x_lens, prompts, early_stop_num):
        self.segments.append(dict(
            phone_ids=x[0].tolist(),
            x_len=int(x_lens[0]),
            prompt_tokens=int(prompts.shape[1]),
            early_stop_num=int(early_stop_num),
            steps=[],
            eos_step=None,
            eos_by=None,
            n_tokens=None,
        ))


def _sample(logits, previous_tokens=None, **kw):
    if REC is None or not REC.detail:
        return _orig_sample(logits, previous_tokens, **kw)
    raw = logits[0].float().clone()
    idx_next, probs = _orig_sample(logits, previous_tokens, **kw)
    seg = REC.segments[-1]
    p_raw = torch.softmax(raw, -1)
    entropy = -(p_raw * torch.log2(p_raw.clamp_min(1e-12))).sum()
    top_p, top_i = p_raw.topk(5)
    tok = int(idx_next[0, 0])
    argmax_after_penalty = int(logits[0].argmax())
    step = len(seg["steps"])
    seg["steps"].append(dict(
        step=step,
        token=tok,
        p=round(float(probs[0, tok]), 4),
        p_raw=round(float(p_raw[tok]), 4),
        entropy=round(float(entropy), 3),
        top5=[dict(token=int(i), p=round(float(p), 4)) for i, p in zip(top_i, top_p)],
    ))
    if seg["eos_step"] is None and REC.eos in (tok, argmax_after_penalty):
        seg["eos_step"] = step
        seg["eos_by"] = "sampled" if tok == REC.eos else "argmax"
    return idx_next, probs


def _batch_infer(self, x, x_lens, prompts, bert_feature, top_k=-100, top_p=100, early_stop_num=-1, **kw):
    if REC is not None:
        REC.eos = self.EOS
        REC.begin(x, x_lens, prompts, early_stop_num)
    y_list, idx_list = _orig_batch_infer(
        self, x, x_lens, prompts, bert_feature, top_k=top_k, top_p=top_p, early_stop_num=early_stop_num, **kw
    )
    if REC is not None:
        REC.segments[-1]["n_tokens"] = int(idx_list[0])
    return y_list, idx_list


_orig_sample = t2s_mod.sample
_orig_batch_infer = t2s_mod.Text2SemanticDecoder.infer_panel_batch_infer


def install():
    t2s_mod.sample = _sample
    t2s_mod.Text2SemanticDecoder.infer_panel_batch_infer = _batch_infer


def summarize(rec: Recorder, prompt_phones: int, prompt_pad_tokens: int, detail: bool):
    """把 Recorder 整理成对外的 debug 结构。prompt_phones 是参考文本的音素数，prompt_pad_tokens 是参考末尾补零对应的 token 数。"""
    out = []
    for seg in rec.segments:
        text_phones = seg["x_len"] - prompt_phones
        ref_tpp = (seg["prompt_tokens"] - prompt_pad_tokens) / prompt_phones
        tpp = seg["n_tokens"] / text_phones
        item = dict(
            n_tokens=seg["n_tokens"],
            text_phones=text_phones,
            phones=[symbols2.symbols[i] for i in seg["phone_ids"][prompt_phones:]],
            tokens_per_phone=round(tpp, 3),
            ref_tokens_per_phone=round(ref_tpp, 3),
            ratio_vs_ref=round(tpp / ref_tpp, 3),
            hit_max_len=seg["n_tokens"] >= min(seg["early_stop_num"], 1500) - 1,
        )
        if detail:
            item.update(eos_step=seg["eos_step"], eos_by=seg["eos_by"], steps=seg["steps"])
        out.append(item)
    return out
