import inspect

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ttsapi import settings
from ttsapi.osutil import exit_with_parent  # noqa: F401  worker 入口从这里 import


def json_errors(app: FastAPI):
    """把异常信息原样返回给 gateway，方便它判断是不是 CUDA 坏了需要重启 worker。"""

    @app.exception_handler(Exception)
    async def handler(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content=dict(error=f"{type(exc).__name__}: {exc}"))


def reset_sovits_prompt_cache(tts):
    """Clear all SoVITS-dependent prompt cache entries so switching weights never retains stale aux specs."""
    cache = tts.prompt_cache
    cache["ref_audio_path"] = None
    cache["prompt_semantic"] = None
    cache["refer_spec"] = []
    cache["aux_ref_audio_paths"] = []
    cache.pop("raw_audio", None)
    cache.pop("raw_sr", None)


def compensate_fragment_interval(tts, fragment_interval: float) -> float:
    """Compensate for upstream audio_postprocess sizing zero_wav with configs.sampling_rate
    even when a vocoder engine outputs at vocoder_configs['sr'] (e.g. v3 24kHz, v4 48kHz vs 32kHz)."""
    cfg = tts.configs
    if not getattr(cfg, "use_vocoder", False):
        return fragment_interval
    out_sr = (getattr(tts, "vocoder_configs", None) or {}).get("sr")
    cfg_sr = getattr(cfg, "sampling_rate", None)
    if not (out_sr and cfg_sr and out_sr != cfg_sr):
        return fragment_interval
    fn = getattr(tts, "audio_postprocess", None)
    if fn is not None:
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            src = None
        if src is not None and "self.configs.sampling_rate * fragment_interval" not in src:
            return fragment_interval
    return fragment_interval * (out_sr / cfg_sr)


def build_synth_inputs(engine_id: str, tts, r: dict) -> dict:
    """Build the upstream TTS.run(inputs) payload from a worker /synth request."""
    supports_aux = settings.ENGINES[engine_id]["supports_aux_refs"]
    aux_paths = list(r.get("aux_ref_audio_paths") or []) if supports_aux else []
    fi = compensate_fragment_interval(tts, float(r.get("fragment_interval", 0.3)))
    inputs = dict(
        text=r["text"], text_lang=r["text_lang"],
        ref_audio_path=r["ref_audio"], prompt_text=r["prompt_text"], prompt_lang=r["prompt_lang"],
        aux_ref_audio_paths=aux_paths,
        top_k=r["top_k"], top_p=r["top_p"], temperature=r["temperature"],
        text_split_method=r["text_split_method"], batch_size=1, speed_factor=r["speed"],
        fragment_interval=fi,
        seed=r["seed"], parallel_infer=True, repetition_penalty=r["repetition_penalty"],
        streaming_mode=False, return_fragment=False,
        # Disable bucket reordering when batch_size=1 so debug segment order matches input text
        split_bucket=False,
        # Disable CUDA graph acceleration so debug hooks observe sampling steps
        use_cuda_graph=False, use_flash_attention=False,
    )
    if r.get("sample_steps"):
        inputs["sample_steps"] = r["sample_steps"]
    return inputs
