"""打分 worker：whisper 转写算 CER、ERes2NetV2 算说话人相似度、画 mel 频谱图。

python -m ttsapi.scorer_worker <parent_pid>
"""
import os
import sys
import threading
import unicodedata
from collections import OrderedDict

from ttsapi import settings
from ttsapi.worker_common import exit_with_parent, json_errors

os.chdir(settings.OLD_REPO)
sys.path[:0] = [str(settings.OLD_REPO / "GPT_SoVITS")]

import librosa  # noqa: E402
import librosa.display  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
import numpy as np  # noqa: E402
import pyopenjtalk  # noqa: E402
import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import Body, FastAPI  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402
from sv import SV  # noqa: E402

from ttsapi import device  # noqa: E402

DEVICE = device.setup("scorer")
# faster-whisper（ctranslate2）只有 CUDA 和 CPU：设备是 mps 时 whisper 在 CPU 上跑，ERes2NetV2 照样用 MPS
if DEVICE.startswith("cuda"):
    asr = WhisperModel(str(settings.ASR_MODEL), device="cuda", compute_type="float16")
    ASR_DESC = "cuda float16"
else:
    asr = WhisperModel(str(settings.ASR_MODEL), device="cpu", compute_type=settings.ASR_CPU_COMPUTE_TYPE,
                       cpu_threads=settings.ASR_CPU_THREADS)
    ASR_DESC = f"cpu {settings.ASR_CPU_COMPUTE_TYPE} threads={settings.ASR_CPU_THREADS or 'default'}"
sv = SV(DEVICE, device.half(DEVICE))
print(f"scorer: whisper {ASR_DESC}, sv {device.describe(DEVICE)}", flush=True)
ref_emb_cache = OrderedDict()
lock = threading.Lock()
app = FastAPI()
json_errors(app)


def clean(s: str) -> str:
    """NFKC 规范化后去掉标点、符号、空白。"""
    s = unicodedata.normalize("NFKC", s)
    return "".join(c for c in s if unicodedata.category(c)[0] not in "PSZC")


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> float:
    ref, hyp = clean(ref), clean(hyp)
    return round(edit_distance(ref, hyp) / max(len(ref), 1), 4)


def kana(s: str) -> str:
    return pyopenjtalk.g2p(clean(s), kana=True) if clean(s) else ""


def whisper_lang(text_lang: str):
    lang = text_lang.removeprefix("all_")
    return None if lang.startswith("auto") else lang


def embedding(path: str):
    wav, _ = librosa.load(path, sr=16000, mono=True)
    e = sv.compute_embedding3(torch.from_numpy(wav).to(DEVICE).unsqueeze(0)).float()
    return F.normalize(e, dim=-1)[0]


def ref_embedding(path: str):
    if path not in ref_emb_cache:
        ref_emb_cache[path] = embedding(path)
        if len(ref_emb_cache) > 64:
            ref_emb_cache.popitem(last=False)
    return ref_emb_cache[path]


@app.get("/health")
def health():
    return dict(ok=True, asr=ASR_DESC, **device.describe(DEVICE))


@app.post("/score")
def score(r: dict = Body(...)):
    with lock:
        segs, _ = asr.transcribe(r["audio"], language=whisper_lang(r["text_lang"]), beam_size=5,
                                 condition_on_previous_text=False)
        asr_text = "".join(s.text for s in segs).strip()
        sim = float(embedding(r["audio"]) @ ref_embedding(r["ref_audio"]))
        device.release(DEVICE)
    out = dict(asr_text=asr_text, cer=cer(r["text"], asr_text), spk_sim=round(sim, 4))
    if r["text_lang"].removeprefix("all_") == "ja":
        out["cer_kana"] = cer(kana(r["text"]), kana(asr_text))
    return out


@app.post("/mel")
def mel(r: dict = Body(...)):
    y, sr = librosa.load(r["audio"], sr=None, mono=True)
    hop = sr // 100
    S = librosa.feature.melspectrogram(y=y, sr=sr, n_fft=2048, hop_length=hop, n_mels=160, fmax=sr / 2)
    S_db = librosa.power_to_db(S, ref=np.max, top_db=90)
    # 下半张是线性频率轴的 STFT，mel 轴会把 4kHz 以上压扁，电音、嘶声在线性轴上更明显
    L_db = librosa.amplitude_to_db(np.abs(librosa.stft(y, n_fft=2048, hop_length=hop)), ref=np.max, top_db=90)
    # 不用 pyplot：它的全局状态不是线程安全的，候选接口会并发画图
    fig = Figure(figsize=(max(8, len(y) / sr * 1.6), 8), dpi=100)
    axes = fig.subplots(2, 1, sharex=True)
    for ax, data, axis, name in ((axes[0], S_db, "mel", "mel"), (axes[1], L_db, "linear", "linear STFT")):
        img = librosa.display.specshow(data, sr=sr, hop_length=hop, x_axis="time", y_axis=axis, fmax=sr / 2,
                                       ax=ax, cmap="magma")
        fig.colorbar(img, ax=ax, format="%+2.0f dB")
        ax.set_title(f"{r.get('title', '')}  {name}")
    fig.tight_layout()
    fig.savefig(r["png"])
    return dict(png=r["png"])


if __name__ == "__main__":
    exit_with_parent(int(sys.argv[1]))
    uvicorn.run(app, host="127.0.0.1", port=settings.SCORER["port"], log_level="warning")
