"""作品渲染：去掉每条首尾静音，统一采样率，按 ITU-R BS.1770 响度把每行归一到同一 LUFS，
按行后停顿拼接，整轨峰值压到 -1 dBFS，导出 wav/mp3 和 SRT/LRC 字幕。
这里只读选中音频的快照（资料库 works/<work_id>/audio/）、在内存里出产物，不写资料库；
发布进资料库 works/<work_id>/ 由渲染任务做（works.render_job）。"""
import io
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import lfilter, resample_poly

from ttsapi import settings
from ttsapi.models import RenderResult, RenderTiming


def _biquads(sr: int):
    """BS.1770 的 K 计权（高架 + 高通），按采样率重新算系数，公式同 libebur128。"""
    k = math.tan(math.pi * 1681.974450955533 / sr)
    q = 0.7071752369554196
    vh = 10 ** (3.999843853973347 / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    shelf = ([(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0],
             [1, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    k = math.tan(math.pi * 38.13547087602444 / sr)
    q = 0.5003270373238773
    a0 = 1 + k / q + k * k
    highpass = ([1, -2, 1], [1, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    return [shelf, highpass]


def loudness(y: np.ndarray, sr: int) -> float:
    for b, a in _biquads(sr):
        y = lfilter(b, a, y)
    block, step = int(0.4 * sr), int(0.1 * sr)
    z = np.array([np.mean(y[i:i + block] ** 2) for i in range(0, len(y) - block + 1, step)]) if len(y) >= block \
        else np.array([np.mean(y ** 2)])
    lk = -0.691 + 10 * np.log10(z + 1e-12)
    z = z[lk > -70]  # 绝对门限
    if len(z) == 0:
        return -70.0
    rel = -0.691 + 10 * np.log10(z.mean()) - 10  # 相对门限
    z = z[-0.691 + 10 * np.log10(z) > rel]
    return float(-0.691 + 10 * np.log10(z.mean()))


def trim(y: np.ndarray, sr: int, db: float = -45, margin: float = 0.05):
    loud = np.nonzero(np.abs(y) > np.abs(y).max() * 10 ** (db / 20))[0]
    if len(loud) == 0:  # 全静音
        return y
    pad = int(margin * sr)
    return y[max(loud[0] - pad, 0):loud[-1] + pad]


def ts_srt(t: float):
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def ts_lrc(t: float):
    cs = round(t * 100)
    return f"[{cs // 6000:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}]"


def render(doc: dict, audio_dir: Path):
    """audio_dir 里是每行选中的音频（<audio_id>.wav）。返回 (RenderResult, {文件名: 内容})。"""
    sr = settings.RENDER_SR
    pieces, timings, t = [], [], 0.0
    for line in doc["lines"]:
        y, src_sr = sf.read(str(audio_dir / f"{line['picked']}.wav"), dtype="float32", always_2d=True)
        y = y.mean(axis=1)
        if src_sr != sr:
            g = math.gcd(sr, src_sr)
            y = resample_poly(y, sr // g, src_sr // g)
        y = trim(y, sr)
        y = y * 10 ** ((settings.RENDER_LUFS - loudness(y, sr)) / 20)
        timings.append(RenderTiming(line_id=line["id"], character=line["character"], text=line["text"],
                                    start=round(t, 3), end=round(t + len(y) / sr, 3)))
        pieces += [y, np.zeros(int(line["pause_after"] * sr), dtype=np.float32)]
        t += len(y) / sr + line["pause_after"]
    track = np.concatenate(pieces)
    peak = np.abs(track).max()
    if peak > 10 ** (-1 / 20):
        track *= 10 ** (-1 / 20) / peak

    stem = f"{doc['id']}-{datetime.now():%Y%m%d-%H%M%S}"
    files = {}
    for fmt, kw in (("wav", dict(subtype="PCM_16", format="WAV")), ("mp3", dict(format="MP3"))):
        buf = io.BytesIO()
        sf.write(buf, track, sr, **kw)
        files[f"{stem}.{fmt}"] = buf.getvalue()
    srt = "".join(f"{i}\n{ts_srt(x.start)} --> {ts_srt(x.end)}\n{x.text}\n\n" for i, x in enumerate(timings, 1))
    files[f"{stem}.srt"] = srt.encode("utf-8")
    lrc = f"[ti:{doc['title']}]\n" + "".join(f"{ts_lrc(x.start)}{x.text}\n" for x in timings)
    files[f"{stem}.lrc"] = lrc.encode("utf-8")
    url = f"/works/{doc['id']}/files/{stem}"
    return RenderResult(work_id=doc["id"], rendered_at=datetime.now().isoformat(timespec="seconds"),
                        duration=round(len(track) / sr, 3), wav_url=f"{url}.wav", mp3_url=f"{url}.mp3",
                        srt_url=f"{url}.srt", lrc_url=f"{url}.lrc", lines=timings), files
