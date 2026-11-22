"""单个 TTS 引擎的 worker：只监听 127.0.0.1，由 gateway 启停。

python -m ttsapi.engine_worker <engine> <parent_pid>
"""
import os
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from ttsapi import settings
from ttsapi.worker_common import build_synth_inputs, exit_with_parent, json_errors, reset_sovits_prompt_cache

ENGINE = sys.argv[1]
CFG = settings.ENGINES[ENGINE]
REPO = Path(CFG["repo"])
os.chdir(REPO)
sys.path[:0] = [str(REPO), str(REPO / "GPT_SoVITS")]
if settings.FFMPEG_BIN:  # Windows：老目录自带的 ffmpeg 共享库
    os.add_dll_directory(str(settings.FFMPEG_BIN))
    os.environ["PATH"] = f"{settings.FFMPEG_BIN}{os.pathsep}{os.environ['PATH']}"

import soundfile as sf  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import Body, FastAPI  # noqa: E402

from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config  # noqa: E402
from ttsapi import debug_hooks, device  # noqa: E402

debug_hooks.install()
DEVICE = device.setup(ENGINE)
print(f"engine {ENGINE}: {device.describe(DEVICE)}", flush=True)
VITS_CONFIG_KEYS = ("version", "languages", "use_vocoder", "sampling_rate", "filter_length", "segment_size",
                    "hop_length", "win_length", "n_speakers", "semantic_frame_rate")


class Engine:
    def __init__(self):
        # 先加载底模，请求里指定的权重按需换上
        gpt, sovits = str(CFG["pretrained_gpt"]), str(CFG["pretrained_sovits"])
        cfg = TTS_Config(dict(custom=dict(
            device=DEVICE, is_half=device.half(DEVICE), version=CFG["version"],
            t2s_weights_path=gpt, vits_weights_path=sovits,
            bert_base_path="GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
            cnhuhbert_base_path="GPT_SoVITS/pretrained_models/chinese-hubert-base",
        )))
        # init_*_weights 会调用 save_configs，把配置写到 tts-api 自己的目录，不碰 GPT-SoVITS 里的 yaml
        cfg.configs_path = str(settings.RUNTIME / f"tts_infer_{ENGINE}.yaml")
        self.tts = TTS(cfg)
        self.t2s_cache = OrderedDict({gpt: self._t2s_state()})
        self.vits_cache = OrderedDict({sovits: self._vits_state()})
        self.gpt, self.sovits = gpt, sovits

    def _t2s_state(self):
        c = self.tts.configs
        return self.tts.t2s_model, c.max_sec, c.mute_emb_sim_matrix

    def _vits_state(self):
        # init_vits_weights 会按权重改写这些配置，从缓存换回模型时要一起恢复
        c = self.tts.configs
        return self.tts.vits_model, self.tts.is_v2pro, {k: getattr(c, k) for k in VITS_CONFIG_KEYS}

    def use(self, gpt: str, sovits: str):
        tts = self.tts
        if gpt != self.gpt:
            if getattr(tts, "t2s_accel", None) is not None:
                tts.t2s_accel.close()
                tts.t2s_accel = None
            if gpt in self.t2s_cache:
                tts.t2s_model, tts.configs.max_sec, tts.configs.mute_emb_sim_matrix = self.t2s_cache[gpt]
                tts.configs.t2s_weights_path = gpt
            else:
                tts.init_t2s_weights(gpt)
                self.t2s_cache[gpt] = self._t2s_state()
            self.t2s_cache.move_to_end(gpt)
            self.gpt = gpt
        if sovits != self.sovits:
            if sovits in self.vits_cache:
                tts.vits_model, tts.is_v2pro, conf = self.vits_cache[sovits]
                for k, v in conf.items():
                    setattr(tts.configs, k, v)
                tts.configs.vits_weights_path = sovits
            else:
                tts.init_vits_weights(sovits)
                self.vits_cache[sovits] = self._vits_state()
            self.vits_cache.move_to_end(sovits)
            # prompt_semantic and refer_spec depend on SoVITS weights; clear them cleanly on switch
            reset_sovits_prompt_cache(tts)
            self.sovits = sovits
        for cache in (self.t2s_cache, self.vits_cache):
            while len(cache) > settings.MODEL_CACHE_SIZE:
                cache.popitem(last=False)
        device.empty_cache(DEVICE)

    def synth(self, r: dict):
        # gateway 传来的是权重的绝对路径（7 月的旧权重、底模或 tts-api/weights 下用户训练的）
        self.use(r["gpt"], r["sovits"])
        debug_hooks.REC = debug_hooks.Recorder(detail=r["debug"])
        inputs = build_synth_inputs(ENGINE, self.tts, r)
        device.reset_peak(DEVICE)
        t = time.perf_counter()
        try:
            # run() 出错时先 yield 一段静音，下一次迭代才抛异常，所以要把生成器跑完
            (sr, audio), *_ = list(self.tts.run(inputs))
            elapsed = time.perf_counter() - t
            vram_peak_mb = device.peak_mb(DEVICE)
        finally:
            device.release(DEVICE)
        sf.write(r["out_path"], audio, sr)
        # 参考音频提取语义 token 前，上游在 16kHz 音频后补了 sampling_rate*0.3 个零（每 640 个采样一个 token）
        prompt_pad_tokens = int(self.tts.configs.sampling_rate * 0.3) // 640
        segments = debug_hooks.summarize(debug_hooks.REC, len(self.tts.prompt_cache["phones"]), prompt_pad_tokens,
                                         r["debug"])
        debug_hooks.REC = None
        return dict(
            sr=sr, duration=round(len(audio) / sr, 3), infer_sec=round(elapsed, 3),
            semantic_tokens=sum(s["n_tokens"] for s in segments), segments=segments,
            vram_peak_mb=vram_peak_mb,
        )


engine = Engine()
lock = threading.Lock()
app = FastAPI()
json_errors(app)


@app.get("/health")
def health():
    return dict(engine=ENGINE, gpt=engine.gpt, sovits=engine.sovits,
                cached_gpt=list(engine.t2s_cache), cached_sovits=list(engine.vits_cache), **device.describe(DEVICE))


@app.post("/synth")
def synth(req: dict = Body(...)):
    with lock:
        return engine.synth(req)


if __name__ == "__main__":
    exit_with_parent(int(sys.argv[2]))
    uvicorn.run(app, host="127.0.0.1", port=CFG["port"], log_level="warning")
