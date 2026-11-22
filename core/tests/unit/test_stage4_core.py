"""Stage 4 core tests: character prefs & default_ref, aux_ref_ids / fragment_interval / FLAC,
per-engine train_defaults & lora_rank, and LAN worker aux-ref transfer."""
import asyncio
import hashlib
import io
import json
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile as sf
from conftest import seed_carol, write_wav
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ttsapi import gateway, settings, synth, train_runner, works
from ttsapi.compute import colab_jobs, registry, remote, worker_api
from ttsapi.models import Engine, GenParams, TrainRequest, TTSParams, WorkCreate, WorkLineIn


@pytest.fixture
def api(lib, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "COMPUTE_FILE", tmp_path / "compute.json")
    monkeypatch.setattr(settings, "SPEEDS_FILE", tmp_path / "speeds.json")
    monkeypatch.setattr(settings, "installed_engines", lambda: ["v2pro", "v2proplus", "v4"])
    monkeypatch.setattr(registry, "_hw", dict(cpu="Apple M6", cores=12, ram_gb=32, accel="mps", gpu=None, os="darwin",
                                              torch="2.11.0"))

    async def local(scope, receive, send):
        await gateway.app(dict(scope, client=("127.0.0.1", 50000)) if scope["type"] == "http" else scope, receive, send)
    return TestClient(local)


@pytest.fixture
def carol_api(api, lib):
    seed_carol(lib, clips=(
        ("14000140", 6.1, "明日の朝は、公園で散歩しようと思います。"),
        ("14200080", 5.9, "手伝ってくれて、ありがとう"),
        ("14300010", 4.2, "おはよう、今日もいい天気だね"),
        ("14000010", 1.0, "おはよう"),
    ))
    gateway.refs.reload()
    return api, lib


def wav_bytes(seconds: float) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(16000 * seconds), dtype="float32"), 16000, format="WAV")
    return buf.getvalue()


# ---------------------------------------------------------------- 1. Character prefs & default_ref

def test_character_prefs_legacy_and_404(carol_api):
    api, lib = carol_api
    # Legacy character file without prefs key
    raw = lib.get("characters/carol.json")
    assert "prefs" not in raw
    r = api.get("/characters/carol/prefs")
    assert r.status_code == 200
    assert r.json() == dict(
        slug="carol",
        default_ref=None,
        synth_params=dict(
            speed=None, fragment_interval=None, top_k=None, top_p=None, temperature=None,
            repetition_penalty=None, text_split_method=None, sample_steps=None, text_lang=None,
        ),
        last_ref_id=None,
        aux_ref_ids=[],
    )
    assert api.get("/characters/nobody/prefs").status_code == 404
    assert api.patch("/characters/nobody/prefs", json=dict(last_ref_id=None)).status_code == 404


def test_character_prefs_default_ref_register_clear_and_validate(carol_api):
    api, lib = carol_api
    assert gateway.refs.default_for("carol") == "carol-14000140"
    assert not lib.has("refs/carol/carol-14200080.json")

    # Setting default_ref registers the candidate (including audio) in library
    r = api.patch("/characters/carol/prefs", json=dict(default_ref="carol-14200080"))
    assert r.status_code == 200, r.text
    assert r.json()["default_ref"] == "carol-14200080"
    assert lib.has("refs/carol/carol-14200080.json")
    assert lib.path("refs/carol/carol-14200080.wav").is_file()
    assert lib.get("characters/carol.json")["default_ref"] == "carol-14200080"
    assert gateway.refs.default_for("carol") == "carol-14200080"

    # GET /models exposes both effective default_ref and prefs
    m = api.get("/models").json()
    carol_info = next(c for c in m["characters"] if c["slug"] == "carol")
    assert carol_info["default_ref"] == "carol-14200080"
    assert carol_info["prefs"]["default_ref"] == "carol-14200080"

    # Clear with null restores automatic default
    r = api.patch("/characters/carol/prefs", json=dict(default_ref=None))
    assert r.status_code == 200 and r.json()["default_ref"] is None
    assert gateway.refs.default_for("carol") == "carol-14000140"

    # Set again then clear with empty string
    assert api.patch("/characters/carol/prefs", json=dict(default_ref="carol-14300010")).status_code == 200
    r = api.patch("/characters/carol/prefs", json=dict(default_ref=""))
    assert r.status_code == 200 and r.json()["default_ref"] is None
    assert gateway.refs.default_for("carol") == "carol-14000140"

    # Missing ref -> 404
    assert api.patch("/characters/carol/prefs", json=dict(default_ref="carol-nope")).status_code == 404

    # Ref belonging to another character -> 422
    other = gateway.refs.add_upload(wav_bytes(5.0), ".wav", "hello", "ja", "custom")
    r = api.patch("/characters/carol/prefs", json=dict(default_ref=other["id"]))
    assert r.status_code == 422

    # Ref with duration outside [3, 10]s -> 422
    short_ref = gateway.refs.add_upload(wav_bytes(2.0), ".wav", "short", "ja", "carol")
    long_ref = gateway.refs.add_upload(wav_bytes(11.0), ".wav", "long", "ja", "carol")
    assert api.patch("/characters/carol/prefs", json=dict(default_ref=short_ref["id"])).status_code == 422
    assert api.patch("/characters/carol/prefs", json=dict(default_ref=long_ref["id"])).status_code == 422
    assert lib.get("characters/carol.json")["default_ref"] is None


def test_character_prefs_synth_params_last_ref_and_aux_refs(carol_api):
    api, lib = carol_api

    # Partial update merges synth_params and saves last_ref_id / aux_ref_ids (deduped, empty skipped)
    r = api.patch("/characters/carol/prefs", json=dict(
        synth_params=dict(speed=1.15, fragment_interval=0.5, top_k=20, text_lang="ja"),
        last_ref_id="carol-14000140",
        aux_ref_ids=["carol-14200080", "", "carol-14200080", "carol-14300010"],
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synth_params"]["speed"] == 1.15
    assert body["synth_params"]["fragment_interval"] == 0.5
    assert body["synth_params"]["top_k"] == 20
    assert body["synth_params"]["text_lang"] == "ja"
    assert body["last_ref_id"] == "carol-14000140"
    assert body["aux_ref_ids"] == ["carol-14200080", "carol-14300010"]

    # Subsequent partial merge preserves speed/fragment_interval/text_lang and unsets top_k via null
    r = api.patch("/characters/carol/prefs", json=dict(
        synth_params=dict(top_k=None, temperature=0.8, text_split_method="cut3", sample_steps=16),
    ))
    assert r.status_code == 200, r.text
    sp = r.json()["synth_params"]
    assert (sp["speed"], sp["fragment_interval"], sp["top_k"], sp["temperature"]) == (1.15, 0.5, None, 0.8)
    assert (sp["text_split_method"], sp["sample_steps"], sp["text_lang"]) == ("cut3", 16, "ja")

    # reset_synth_params clears all synth_params
    r = api.patch("/characters/carol/prefs", json=dict(reset_synth_params=True))
    assert r.status_code == 200
    assert all(v is None for v in r.json()["synth_params"].values())
    assert r.json()["last_ref_id"] == "carol-14000140"

    # Unknown synth_params key or out-of-range value -> 422
    assert api.patch("/characters/carol/prefs", json=dict(synth_params=dict(unknown_field=1))).status_code == 422
    assert api.patch("/characters/carol/prefs", json=dict(synth_params=dict(speed=5.0))).status_code == 422
    assert api.patch("/characters/carol/prefs", json=dict(synth_params=dict(fragment_interval=0.0))).status_code == 422
    assert api.patch("/characters/carol/prefs", json=dict(synth_params=dict(top_k=0))).status_code == 422
    assert api.patch("/characters/carol/prefs", json=dict(synth_params=dict(text_lang="invalid"))).status_code == 422

    # Validation for last_ref_id and aux_ref_ids: missing -> 404, wrong char -> 422
    other = gateway.refs.add_upload(wav_bytes(4.0), ".wav", "other", "ja", "custom")
    assert api.patch("/characters/carol/prefs", json=dict(last_ref_id="nope")).status_code == 404
    assert api.patch("/characters/carol/prefs", json=dict(last_ref_id=other["id"])).status_code == 422
    assert api.patch("/characters/carol/prefs", json=dict(aux_ref_ids=["nope"])).status_code == 404
    assert api.patch("/characters/carol/prefs", json=dict(aux_ref_ids=[other["id"]])).status_code == 422

    # Clearing last_ref_id and aux_ref_ids
    r = api.patch("/characters/carol/prefs", json=dict(last_ref_id=None, aux_ref_ids=[]))
    assert r.status_code == 200 and r.json()["last_ref_id"] is None and r.json()["aux_ref_ids"] == []

    # Legacy/hand-edited JSON with unknown key in prefs.synth_params is ignored on read
    doc = lib.get("characters/carol.json")
    doc["prefs"]["synth_params"] = dict(speed=1.1, legacy_ignored=999)
    lib.put("characters/carol.json", doc)
    assert api.get("/characters/carol/prefs").json()["synth_params"]["speed"] == 1.1


def test_character_prefs_patch_requires_owned_library(carol_api):
    api, lib = carol_api
    lib.info["owner"] = dict(machine_id="other-machine", name="other")
    r = api.patch("/characters/carol/prefs", json=dict(last_ref_id="carol-14000140"))
    assert r.status_code == 409 and r.json()["code"] == "library_not_owned"


# ---------------------------------------------------------------- 2. aux_ref_ids, fragment_interval, FLAC, LAN worker

def test_gen_params_validation():
    p = GenParams()
    assert p.fragment_interval == 0.3 and p.aux_ref_ids == []
    assert TTSParams(text="hi", format="flac").format == "flac"
    for bad in (0.0, 0.005, 2.5):
        with pytest.raises(ValidationError):
            GenParams(fragment_interval=bad)


def test_tts_local_aux_refs_fragment_interval_meta_and_flac(carol_api, monkeypatch, tmp_path):
    api, lib = carol_api
    pre_g = tmp_path / "pre_g.ckpt"
    pre_s = tmp_path / "pre_s.pth"
    pre_g.write_bytes(b"g")
    pre_s.write_bytes(b"s")
    monkeypatch.setitem(settings.ENGINES["v2pro"], "pretrained_gpt", pre_g)
    monkeypatch.setitem(settings.ENGINES["v2pro"], "pretrained_sovits", pre_s)
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    seen_payloads = []

    async def fake_worker_call(path, payload):
        seen_payloads.append((path, payload))
        write_wav(Path(payload["out_path"]), 0.25)
        return dict(duration=0.25, infer_sec=0.1, semantic_tokens=8, vram_peak_mb=128,
                    segments=[dict(ratio_vs_ref=1.0)])

    monkeypatch.setattr(remote.ENGINES["v2pro"], "call", fake_worker_call)

    # Return JSON: verify aux_ref_ids dedup & primary exclusion, fragment_interval, and persistence in meta
    r = api.post("/tts", json=dict(
        text="おはよう", character="carol", engine="v2pro", ref_id="carol-14000140",
        aux_ref_ids=["carol-14000140", "carol-14200080", "carol-14200080", "", "carol-14300010"],
        fragment_interval=0.55, return_json=True,
    ))
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["fragment_interval"] == 0.55
    assert item["aux_ref_ids"] == ["carol-14200080", "carol-14300010"]
    _, sent = seen_payloads[-1]
    assert sent["fragment_interval"] == 0.55
    assert sent["aux_ref_audio_paths"] == [
        gateway.refs.index["carol-14200080"]["path"],
        gateway.refs.index["carol-14300010"]["path"],
    ]

    meta = api.get(f"/audio/{item['id']}/meta").json()
    assert meta["fragment_interval"] == 0.55 and meta["aux_ref_ids"] == ["carol-14200080", "carol-14300010"]

    # GET /audio/{id}?format=flac
    r_flac = api.get(f"/audio/{item['id']}", params=dict(format="flac"))
    assert r_flac.status_code == 200 and r_flac.headers["content-type"] == "audio/flac"
    assert r_flac.content[:4] == b"fLaC"
    data, sr = sf.read(io.BytesIO(r_flac.content))
    assert sr == 16000 and len(data) > 0

    # POST /tts with format="flac"
    r_tts_flac = api.post("/tts", json=dict(text="おはよう", character="carol", engine="v2pro", format="flac"))
    assert r_tts_flac.status_code == 200 and r_tts_flac.headers["content-type"] == "audio/flac"
    assert r_tts_flac.content[:4] == b"fLaC"

    # GET /refs/{ref_id}/audio?format=flac
    r_ref_flac = api.get("/refs/carol-14000140/audio", params=dict(format="flac"))
    assert r_ref_flac.status_code == 200 and r_ref_flac.headers["content-type"] == "audio/flac"
    assert r_ref_flac.content[:4] == b"fLaC"

    # Missing aux_ref_id -> 404
    r_miss = api.post("/tts", json=dict(text="おはよう", character="carol", engine="v2pro", aux_ref_ids=["carol-404"]))
    assert r_miss.status_code == 404
    r_cand = api.post("/tts/candidates/jobs", json=dict(
        text="おはよう", character="carol", engine="v2pro", aux_ref_ids=["carol-404"]))
    assert r_cand.status_code == 404
    r_cmp = api.post("/compare/jobs", json=dict(
        text="おはよう", character="carol", engines=["v2pro"], aux_ref_ids=["carol-404"]))
    assert r_cmp.status_code == 404


def test_remote_worker_transfers_aux_ref_blobs_and_caches(monkeypatch, tmp_path):
    """LAN Remote transfers aux_ref_audio_paths via blob cache and worker resolves them to local blob paths."""
    monkeypatch.setattr(settings, "WORKER_TOKEN", "tok-aux")
    monkeypatch.setattr(settings, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(settings, "installed_engines", lambda: ["v2pro"])
    calls = []

    async def fake_call(path, payload):
        calls.append((path, payload))
        out = b"WAV:" + b"+".join(Path(p).read_bytes() for p in [payload["ref_audio"], *payload["aux_ref_audio_paths"]])
        Path(payload["out_path"]).write_bytes(out)
        return dict(duration=1.0, infer_sec=0.4, semantic_tokens=10, vram_peak_mb=0, segments=[])

    monkeypatch.setattr(remote.ENGINES["v2pro"], "call", fake_call)

    with TestClient(worker_api.app):
        transport = httpx.ASGITransport(app=worker_api.app)
        real_client = httpx.AsyncClient
        puts = []

        def client_factory(**kw):
            c = real_client(transport=transport, base_url="http://w", **kw)
            orig = c.put

            async def put(url, **k):
                puts.append(url)
                return await orig(url, **k)
            c.put = put
            return c

        monkeypatch.setattr(remote.httpx, "AsyncClient", client_factory)
        ref = tmp_path / "main.wav"
        aux1 = tmp_path / "aux1.wav"
        aux2 = tmp_path / "aux2.wav"
        ref.write_bytes(b"MAIN")
        aux1.write_bytes(b"AUX1")
        aux2.write_bytes(b"AUX2")
        dev = remote.Remote(dict(id="lan-1", name="gpu-worker", address="http://w", token="tok-aux"))

        async def go(out_file):
            return await dev.call(
                "v2pro", "/synth",
                dict(text="hello", fragment_interval=0.45, aux_ref_audio_paths=[]),
                files=dict(
                    ref_audio=ref,
                    aux_ref_audio_paths=[aux1, aux2],
                    gpt=settings.ENGINES["v2pro"]["pretrained_gpt"],
                    sovits=settings.ENGINES["v2pro"]["pretrained_sovits"],
                ),
                outputs=dict(out_path=out_file),
                pretrained=dict(gpt="pretrained_gpt", sovits="pretrained_sovits"),
            )

        out1 = tmp_path / "out1.wav"
        asyncio.run(go(out1))
        assert out1.read_bytes() == b"WAV:MAIN+AUX1+AUX2"
        _, payload = calls[-1]
        assert payload["fragment_interval"] == 0.45
        expected_shas = [hashlib.sha256(b).hexdigest() for b in (b"MAIN", b"AUX1", b"AUX2")]
        assert payload["aux_ref_audio_paths"] == [str(settings.BLOB_DIR / s) for s in expected_shas[1:]]
        assert sorted(u.rsplit("/", 1)[1] for u in puts) == sorted(expected_shas)

        # Second call hits blob cache for primary and all aux refs
        out2 = tmp_path / "out2.wav"
        asyncio.run(go(out2))
        assert out2.read_bytes() == b"WAV:MAIN+AUX1+AUX2"
        assert len(puts) == 3


def test_works_preserves_fragment_interval_and_aux_refs_and_handles_legacy_lines(carol_api, monkeypatch, tmp_path):
    api, lib = carol_api
    # Save a generated audio item with fragment_interval and aux_ref_ids
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    aid = "20261121-120000-abcdef"
    write_wav(settings.AUDIO_DIR / f"{aid}.wav", 0.2)
    synth.save_audio(dict(
        id=aid, created="2026-11-21T12:00:00", audio_url=f"/audio/{aid}", text="テスト", text_lang="ja",
        character="carol", engine="v2pro", gpt="g.ckpt", sovits="s.pth", ref_id="carol-14000140", seed=42,
        top_k=15, top_p=1.0, temperature=1.0, repetition_penalty=1.35, speed=1.0, fragment_interval=0.7,
        text_split_method="cut5", sample_steps=None, aux_ref_ids=["carol-14200080"],
        duration=0.2, infer_sec=0.1, synth_wall_sec=0.1, semantic_tokens=5, vram_peak_mb=0,
        ratio_vs_ref=[1.0], work_id=None, compute="local",
    ), 1000.0)

    w = works.create(WorkCreate(title="stage4", lines=[]))
    from ttsapi.models import AddLineRequest
    w2 = works.add_line(w.id, AddLineRequest(audio_id=aid))
    assert w2.lines[0].fragment_interval == 0.7 and w2.lines[0].aux_ref_ids == ["carol-14200080"]

    # Legacy work line missing fragment_interval & aux_ref_ids still generates cleanly
    w_legacy = works.create(WorkCreate(title="legacy", lines=[WorkLineIn(character="carol", text="hi")]))
    doc = works.load(w_legacy.id)
    doc["lines"][0].pop("fragment_interval", None)
    doc["lines"][0].pop("aux_ref_ids", None)
    works.save(doc)

    seen = []

    async def fake_run_candidates(p, engine, n, ref, seed, work_id=None, on_item=None):
        seen.append((p.fragment_interval, p.aux_ref_ids))
        from types import SimpleNamespace
        return SimpleNamespace(candidates=[SimpleNamespace(id=aid, score=90.0)])

    monkeypatch.setattr(synth, "run_candidates", fake_run_candidates)
    from types import SimpleNamespace
    run = SimpleNamespace(job=SimpleNamespace(), stage=lambda *a: None, log=lambda *a: None, item_done=lambda *a: None)
    from ttsapi.models import GenerateRequest
    asyncio.run(works.generate_job(run, w_legacy.id, GenerateRequest(n=1)))
    assert seen == [(0.3, [])]


# ---------------------------------------------------------------- 3. Engine train_defaults & lora_rank

def test_models_engine_train_defaults(carol_api):
    api, _ = carol_api
    engines = {e["id"]: e for e in api.get("/models").json()["engines"]}
    assert set(engines) == set(Engine.__args__)

    full_expected = dict(
        sovits_epochs=8, sovits_save_every=4, sovits_batch=8,
        gpt_epochs=15, gpt_save_every=5, gpt_batch=8,
        lora=False, lora_rank=None, lora_ranks=[],
    )
    lora_expected = dict(
        sovits_epochs=2, sovits_save_every=1, sovits_batch=4,
        gpt_epochs=15, gpt_save_every=5, gpt_batch=8,
        lora=True, lora_rank=32, lora_ranks=[16, 32, 64, 128],
    )
    for eid in ("v1", "v2", "v2pro", "v2proplus"):
        assert engines[eid]["train_defaults"] == full_expected
    for eid in ("v3", "v4", "v5dev", "v5turbo"):
        assert engines[eid]["train_defaults"] == lora_expected


def test_train_request_lora_rank_validation_and_runner_config(monkeypatch, tmp_path):
    assert TrainRequest(slug="alice", dataset_id="d", engine="v4").lora_rank is None
    for rank in (16, 32, 64, 128):
        req = TrainRequest(slug="alice", dataset_id="d", engine="v4", lora_rank=rank)
        assert req.lora_rank == rank
        spec = colab_jobs.job_spec("j1", "train", req, {})
        assert spec["tasks"][0]["lora_rank"] == rank
    for bad in (0, 8, 24, 256):
        with pytest.raises(ValidationError):
            TrainRequest(slug="alice", dataset_id="d", engine="v4", lora_rank=bad)

    # Verify train_runner writes specified lora_rank (and default 32) into s2_config.json
    repo = tmp_path / "repo"
    (repo / "GPT_SoVITS" / "configs").mkdir(parents=True)
    (repo / "GPT_SoVITS" / "configs" / "s2.json").write_text(
        json.dumps(dict(train={}, model={}, data={})), encoding="utf-8"
    )
    (repo / "GPT_SoVITS" / "configs" / "s1longer-v2.yaml").write_text(
        "data:\n  num_workers: 4\ntrain: {}\n", encoding="utf-8"
    )
    monkeypatch.setattr(train_runner, "workspace", lambda r: tmp_path / "ws")
    monkeypatch.setattr(settings, "TRAIN_DIR", tmp_path / "training")
    monkeypatch.setattr(settings, "WEIGHTS_DIR", tmp_path / "weights")
    monkeypatch.setitem(settings.ENGINES["v4"], "repo", repo)
    monkeypatch.setitem(settings.ENGINES["v2pro"], "repo", repo)

    for idx, (lora_rank, expected) in enumerate(((64, "64"), (None, "32"))):
        run_name = f"carol-lora-{idx}"
        run_dir = settings.TRAIN_DIR / run_name
        exp = run_dir / "exp"
        exp.mkdir(parents=True)
        req = TrainRequest(slug="carol", dataset_id="d", engine="v4", sovits_epochs=2, sovits_save_every=1,
                           sovits_batch=4, lora_rank=lora_rank)
        (run_dir / "runner_spec.json").write_text(json.dumps(dict(
            job_id=f"j-{idx}", run_name=run_name, req=req.model_dump(),
            train_list=str(run_dir / "train.list"), log_path=str(run_dir / "job.log"),
        )), encoding="utf-8")

        runner = train_runner.Runner(run_dir)
        scripts_called = []

        def fake_run_script(eng, ws, args, env_extra, on_line=None, _exp=exp):
            scripts_called.append(args)
            if args[0].endswith("1-get-text.py"):
                (_exp / "2-name2text-0.txt").write_text("a\n", encoding="utf-8")
            elif args[0].endswith("3-get-semantic.py"):
                (_exp / "6-name2semantic-0.tsv").write_text("x\ty\n", encoding="utf-8")

        monkeypatch.setattr(runner, "run_script", fake_run_script)
        train_runner.train(runner)

        s2_cfg = json.loads((run_dir / "s2_config.json").read_text(encoding="utf-8"))
        assert s2_cfg["train"]["lora_rank"] == expected
        assert any(a[0] == "GPT_SoVITS/s2_train_v3_lora.py" for a in scripts_called)


# ---------------------------------------------------------------- 4. Review regressions (sol round 1)

def test_reset_sovits_prompt_cache_clears_stale_aux_specs():
    """Switching SoVITS weights clears refer_spec and aux_ref_audio_paths so a subsequent call
    without aux refs never reuses stale aux specs and a call with the same aux ref recomputes it."""
    from types import SimpleNamespace
    from ttsapi.worker_common import reset_sovits_prompt_cache

    model_tag = ["sovits-A"]
    tts = SimpleNamespace(prompt_cache={
        "ref_audio_path": None, "prompt_semantic": None, "refer_spec": [],
        "aux_ref_audio_paths": [], "raw_audio": None, "raw_sr": None,
    })

    def run_like_upstream(ref_audio_path, aux_ref_audio_paths):
        # Mirrors upstream GPT-SoVITS TTS.run + _set_ref_spec cache logic
        if ref_audio_path != tts.prompt_cache["ref_audio_path"]:
            spec_main = (model_tag[0], ref_audio_path)
            if tts.prompt_cache["refer_spec"] in [[], None]:
                tts.prompt_cache["refer_spec"] = [spec_main]
            else:
                tts.prompt_cache["refer_spec"][0] = spec_main
            tts.prompt_cache["ref_audio_path"] = ref_audio_path
        aux = aux_ref_audio_paths or []
        common = set(aux) & set(tts.prompt_cache["aux_ref_audio_paths"])
        if not (len(common) == len(aux) == len(tts.prompt_cache["aux_ref_audio_paths"])):
            tts.prompt_cache["aux_ref_audio_paths"] = list(aux)
            tts.prompt_cache["refer_spec"] = [tts.prompt_cache["refer_spec"][0]]
            for p in aux:
                tts.prompt_cache["refer_spec"].append((model_tag[0], p))
        return list(tts.prompt_cache["refer_spec"])

    assert run_like_upstream("/main.wav", ["/aux1.wav"]) == [("sovits-A", "/main.wav"), ("sovits-A", "/aux1.wav")]

    # Switch SoVITS weight -> reset cache -> call WITHOUT aux refs must not retain stale aux1 spec
    model_tag[0] = "sovits-B"
    reset_sovits_prompt_cache(tts)
    assert run_like_upstream("/main.wav", []) == [("sovits-B", "/main.wav")]

    # Switch again -> call WITH the same aux1 must recompute aux1 under sovits-C
    model_tag[0] = "sovits-C"
    reset_sovits_prompt_cache(tts)
    assert run_like_upstream("/main.wav", ["/aux1.wav"]) == [("sovits-C", "/main.wav"), ("sovits-C", "/aux1.wav")]


def test_aux_refs_unsupported_engines_rejected_and_worker_defensive(carol_api):
    api, _ = carol_api
    from types import SimpleNamespace
    from ttsapi.worker_common import build_synth_inputs

    engines = {e["id"]: e for e in api.get("/models").json()["engines"]}
    for eid in ("v1", "v2", "v2pro", "v2proplus"):
        assert engines[eid]["supports_aux_refs"] is True
    for eid in ("v3", "v4", "v5dev", "v5turbo"):
        assert engines[eid]["supports_aux_refs"] is False

    # Reject non-empty aux_ref_ids on v4 / v5turbo across tts, candidates, compare
    r = api.post("/tts", json=dict(text="hi", character="carol", engine="v4", aux_ref_ids=["carol-14200080"]))
    assert r.status_code == 422 and "v4" in r.json()["detail"]
    r = api.post("/tts/candidates", json=dict(text="hi", character="carol", engine="v4", aux_ref_ids=["carol-14200080"]))
    assert r.status_code == 422
    r = api.post("/tts/candidates/jobs", json=dict(text="hi", character="carol", engine="v4", aux_ref_ids=["carol-14200080"]))
    assert r.status_code == 422
    r = api.post("/compare", json=dict(text="hi", character="carol", engines=["v2pro", "v4"], aux_ref_ids=["carol-14200080"]))
    assert r.status_code == 422 and "v4" in r.json()["detail"]
    r = api.post("/compare/jobs", json=dict(text="hi", character="carol", engines=["v2pro", "v5turbo"], aux_ref_ids=["carol-14200080"]))
    assert r.status_code == 422 and "v5turbo" in r.json()["detail"]

    # Defensive guard in worker input builder drops aux paths for unsupported engines
    fake_tts = SimpleNamespace(configs=SimpleNamespace(use_vocoder=False, sampling_rate=32000), vocoder_configs={})
    req = dict(
        text="hi", text_lang="ja", ref_audio="/m.wav", prompt_text="p", prompt_lang="ja",
        aux_ref_audio_paths=["/a.wav"], top_k=15, top_p=1.0, temperature=1.0,
        text_split_method="cut5", speed=1.0, fragment_interval=0.3, seed=1, repetition_penalty=1.35,
    )
    assert build_synth_inputs("v2pro", fake_tts, req)["aux_ref_audio_paths"] == ["/a.wav"]
    assert build_synth_inputs("v4", fake_tts, req)["aux_ref_audio_paths"] == []
    assert build_synth_inputs("v5turbo", fake_tts, req)["aux_ref_audio_paths"] == []


def test_fragment_interval_sampling_rate_compensation():
    """Vocoder engines whose upstream audio_postprocess sizes zero_wav by configs.sampling_rate
    get compensated by vocoder_sr / configs.sampling_rate so output silence equals requested seconds."""
    from types import SimpleNamespace
    from ttsapi.worker_common import compensate_fragment_interval

    def buggy_postprocess(self, fragment_interval=0.3):
        return int(self.configs.sampling_rate * fragment_interval)

    def fixed_postprocess(self, sr, fragment_interval=0.3):
        return int(sr * fragment_interval)

    v2pro = SimpleNamespace(configs=SimpleNamespace(use_vocoder=False, sampling_rate=32000), vocoder_configs={"sr": None})
    v4_buggy = SimpleNamespace(configs=SimpleNamespace(use_vocoder=True, sampling_rate=32000),
                               vocoder_configs={"sr": 48000}, audio_postprocess=buggy_postprocess)
    v3_buggy = SimpleNamespace(configs=SimpleNamespace(use_vocoder=True, sampling_rate=32000),
                               vocoder_configs={"sr": 24000}, audio_postprocess=buggy_postprocess)
    v5_fixed = SimpleNamespace(configs=SimpleNamespace(use_vocoder=True, sampling_rate=32000),
                               vocoder_configs={"sr": 48000}, audio_postprocess=fixed_postprocess)

    assert compensate_fragment_interval(v2pro, 0.6) == pytest.approx(0.6)
    assert compensate_fragment_interval(v4_buggy, 0.6) == pytest.approx(0.9)   # 32000 * 0.9 / 48000 == 0.6s
    assert compensate_fragment_interval(v3_buggy, 0.6) == pytest.approx(0.45)  # 32000 * 0.45 / 24000 == 0.6s
    assert compensate_fragment_interval(v5_fixed, 0.6) == pytest.approx(0.6)   # no double compensation


def test_default_ref_rejects_rounded_boundary_and_missing_file(carol_api, tmp_path):
    """2.997s (rounds to 3.00s), 10.004s (rounds to 10.00s), and missing audio files are rejected with 422."""
    from ttsapi.refs import _index_list
    api, lib = carol_api

    def exact_wav(n_samples: int, sr: int = 16000) -> bytes:
        buf = io.BytesIO()
        sf.write(buf, np.zeros(n_samples, dtype="float32"), sr, format="WAV")
        return buf.getvalue()

    near_short = gateway.refs.add_upload(exact_wav(47952), ".wav", "2.997s", "ja", "carol")
    near_long = gateway.refs.add_upload(exact_wav(160064), ".wav", "10.004s", "ja", "carol")
    exact_min = gateway.refs.add_upload(exact_wav(48000), ".wav", "3.000s", "ja", "carol")
    exact_max = gateway.refs.add_upload(exact_wav(160000), ".wav", "10.000s", "ja", "carol")
    assert near_short["duration"] == 3.0 and near_long["duration"] == 10.0

    assert api.patch("/characters/carol/prefs", json=dict(default_ref=near_short["id"])).status_code == 422
    assert api.patch("/characters/carol/prefs", json=dict(default_ref=near_long["id"])).status_code == 422
    assert api.patch("/characters/carol/prefs", json=dict(default_ref=exact_min["id"])).status_code == 200
    assert api.patch("/characters/carol/prefs", json=dict(default_ref=exact_max["id"])).status_code == 200

    # _index_list also enforces unrounded sample-count bounds (excludes 2.997s and 10.004s)
    d = tmp_path / "ds"
    (d / "audio").mkdir(parents=True)
    for name, n in (("short", 47952), ("ok", 48000), ("long", 160064)):
        (d / "audio" / f"{name}.wav").write_bytes(exact_wav(n))
    lst = d / "a.list"
    lst.write_text("\n".join(f"{d / 'audio' / f'{n}.wav'}|carol|ja|text" for n in ("short", "ok", "long")), encoding="utf-8")
    indexed = _index_list("carol", lst)
    assert list(indexed) == ["carol-ok"]

    # Missing backing audio file (JSON present, audio deleted) -> 422
    vanished = gateway.refs.add_upload(exact_wav(80000), ".wav", "5.0s", "ja", "carol")
    Path(gateway.refs.index[vanished["id"]]["path"]).unlink()
    assert api.patch("/characters/carol/prefs", json=dict(default_ref=vanished["id"])).status_code == 422


def test_bench_works_after_clearing_carol_default_ref(carol_api, monkeypatch):
    """Clearing carol's explicit default_ref does not break bench_ref() (CLI) or POST /bench."""
    from ttsapi.bench import bench_ref
    api, lib = carol_api

    assert api.patch("/characters/carol/prefs", json=dict(default_ref="carol-14200080")).status_code == 200
    assert api.patch("/characters/carol/prefs", json=dict(default_ref=None)).status_code == 200
    assert lib.get("characters/carol.json")["default_ref"] is None

    # CLI path (refs=None) and gateway path (refs=gateway.refs) both resolve automatic default
    r_cli = bench_ref()
    r_gw = bench_ref(gateway.refs)
    assert r_cli["id"] == r_gw["id"] == "carol-14000140"
    assert Path(r_cli["path"]).is_file()

    seen = []

    async def fake_run_bench(ref, engines, quick, progress):
        seen.append(ref)
        return dict(machine="mini", at="2026-11-21T00:00:00+09:00", engines={}, scorer=None)

    monkeypatch.setattr(gateway, "run_bench", fake_run_bench)
    r = api.post("/bench", json=dict(device="local", quick=True))
    assert r.status_code == 200, r.text
    assert seen and seen[0]["id"] == "carol-14000140"


def test_sweep_lora_rank_dedup_and_legacy_compat(carol_api, tmp_path):
    """SweepTrain.lora_rank distinguishes LoRA ranks, normalizes None/32 to match legacy runs,
    and ignores lora_rank for full-param engines."""
    from ttsapi.catalog import Catalog
    from ttsapi.compute import colab_sweep
    from ttsapi.models import SweepRequest

    _, lib = carol_api
    cat = Catalog()

    # Register a legacy v4 run without lora_rank in params (implicit rank 32)
    req_default = SweepRequest(characters=["carol"], engines=["v4"], trains=[dict(sovits_epochs=2, sovits_save_every=1)])
    (g_def, tr_def), = colab_sweep.groups(req_default, cat)
    assert g_def.lora_rank == 32 and tr_def.lora_rank == 32

    legacy_params = tr_def.model_dump()
    legacy_params.pop("lora_rank", None)
    w_dir = tmp_path / "sw"
    w_dir.mkdir()
    g_file, s_file = w_dir / f"{tr_def.slug}-e5.ckpt", w_dir / f"{tr_def.slug}_e2_s8.pth"
    g_file.write_bytes(b"g" * 10)
    s_file.write_bytes(b"s" * 10)
    cat.register_run(
        tr_def.slug, tr_def.dataset_id,
        dict(run_name=f"{tr_def.slug}-20261120-000000", engine="v4", job_id="j-old", best=None,
             filter=None, lines_used=3, minutes_used=0.1, params=legacy_params),
        dict(gpt=[g_file], sovits=[s_file]),
    )

    # Sweep with None, 32, and 64 on v4 + shared train on v2proplus
    req = SweepRequest(
        characters=["carol"],
        engines=["v4", "v2proplus"],
        trains=[
            dict(engine="v4", sovits_epochs=2, sovits_save_every=1),
            dict(engine="v4", sovits_epochs=2, sovits_save_every=1, lora_rank=32),
            dict(engine="v4", sovits_epochs=2, sovits_save_every=1, lora_rank=64),
            dict(sovits_epochs=8, sovits_save_every=4, lora_rank=64),
        ],
    )
    expanded = colab_sweep.groups(req, cat)
    v4_groups = [(g, tr) for g, tr in expanded if g.engine == "v4"]
    v2_groups = [(g, tr) for g, tr in expanded if g.engine == "v2proplus"]

    # v4 s2g15: None and 32 deduplicate into ONE skipped group matching legacy run; 64 is distinct & pending
    s2 = [(g, tr) for g, tr in v4_groups if g.sovits_epochs == 2]
    assert len(s2) == 2
    assert (s2[0][0].lora_rank, s2[0][0].status, s2[0][0].slug) == (32, "skipped", g_def.slug)
    assert (s2[1][0].lora_rank, s2[1][0].status) == (64, "pending")
    assert s2[1][0].slug != g_def.slug and s2[1][1].lora_rank == 64

    # Full-param engine normalizes lora_rank to None
    assert len(v2_groups) == 1 and v2_groups[0][0].lora_rank is None and v2_groups[0][1].lora_rank is None


def test_old_lan_worker_capability_gating_and_blob_compat(carol_api, monkeypatch, tmp_path):
    """Old LAN workers without caps are skipped for aux_refs / non-default fragment_interval,
    while default tasks still work and Remote.call never sends empty lists in blobs."""
    from test_compute import AUTO, cand, lan, mini
    from ttsapi.compute import scheduler

    api, _ = carol_api

    # 1. Worker health advertises caps
    monkeypatch.setattr(settings, "WORKER_TOKEN", "tok-cap")
    monkeypatch.setattr(settings, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setattr(settings, "installed_engines", lambda: ["v2pro"])
    with TestClient(worker_api.app) as wclient:
        h = wclient.get("/worker/health", headers={"Authorization": "Bearer tok-cap"}).json()
        assert set(h["caps"]) >= {"aux_refs", "fragment_interval"}

    # 2. Remote.call omits empty list entries from blobs (old worker schema compatibility)
    seen_bodies = []

    async def fake_post(self, path, body, files, timeout):
        seen_bodies.append(body)
        return dict(result=dict(ok=True), outputs={})

    monkeypatch.setattr(remote.Remote, "_post", fake_post)
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"REF")
    dev = remote.Remote(dict(id="lan-1", name="old", address="http://w", token="t"))
    asyncio.run(dev.call("v2pro", "/synth", dict(text="x"), files=dict(ref_audio=ref, aux_ref_audio_paths=[])))
    assert "aux_ref_audio_paths" not in seen_bodies[-1]["blobs"]
    assert all(isinstance(v, str) for v in seen_bodies[-1]["blobs"].values())

    # 3. Scheduler gates old LAN worker (no caps) on aux_refs and fragment_interval != 0.3
    old_lan = lan(speeds={"v2pro": (1.0, "measured"), "scorer": (1.0, "measured")})
    new_lan = lan(id="lan-new", name="newNuc", caps=["aux_refs", "fragment_interval"],
                  speeds={"v2pro": (1.0, "measured"), "scorer": (1.0, "measured")})
    loc = mini(speeds={"v2pro": (10.0, "measured"), "scorer": (5.0, "measured")})

    # Without special caps, old LAN is available
    p_plain = scheduler.plan([loc, old_lan], scheduler.Task("gen", [("v2pro", 30)]), AUTO)
    assert cand(p_plain, "lan-aaaaaa")["available"] is True

    # With aux_refs / fragment_interval, old LAN is worker_outdated and new LAN is available
    for req_caps in (("aux_refs",), ("fragment_interval",), ("aux_refs", "fragment_interval")):
        p = scheduler.plan([loc, old_lan, new_lan],
                           scheduler.Task("gen", [("v2pro", 30)], required_caps=req_caps), AUTO)
        c_old = cand(p, "lan-aaaaaa")
        assert (c_old["available"], c_old["reason_code"]) == (False, "worker_outdated")
        assert "更新" in c_old["reason"]
        assert cand(p, "lan-new")["available"] is True and p["recommended"] == "lan-new"

    # POST /plan reflects capability gating
    async def snap():
        return [loc, old_lan], AUTO

    monkeypatch.setattr(registry, "snapshot", snap)
    r = api.post("/plan", json=dict(kind="gen", engine="v2pro", items=30, fragment_interval=0.5))
    assert r.status_code == 200
    assert cand(r.json(), "lan-aaaaaa")["reason_code"] == "worker_outdated"


def test_flac_export_preserves_24bit_and_16bit(carol_api, tmp_path):
    """FLAC transcode uses PCM_24 for 24-bit/32-bit/float sources and PCM_16 for 16-bit sources."""
    _, _ = carol_api
    samples = np.zeros(1600, dtype="float32")
    wav16 = tmp_path / "s16.wav"
    wav24 = tmp_path / "s24.wav"
    wavf = tmp_path / "sf32.wav"
    sf.write(wav16, samples, 16000, subtype="PCM_16")
    sf.write(wav24, samples, 16000, subtype="PCM_24")
    sf.write(wavf, samples, 16000, subtype="FLOAT")

    for src, expected in ((wav16, "PCM_16"), (wav24, "PCM_24"), (wavf, "PCM_24")):
        raw = gateway.audio_bytes(src, "flac")
        assert raw[:4] == b"fLaC"
        assert sf.info(io.BytesIO(raw)).subtype == expected


def test_work_generate_and_regenerate_prevalidate_aux_refs(carol_api):
    """POST /works/{id}/generate and regenerate fail fast with 422/404 before enqueueing jobs."""
    from ttsapi import jobs
    api, _ = carol_api
    before = dict(jobs.runs)

    # Unsupported engine (v4) with aux_ref_ids -> 422 before creating a job
    w_bad_eng = api.post("/works", json=dict(
        title="bad-eng",
        lines=[dict(character="carol", text="hi", engine="v4", aux_ref_ids=["carol-14200080"])],
    )).json()
    lid = w_bad_eng["lines"][0]["id"]
    assert api.post(f"/works/{w_bad_eng['id']}/generate", json=dict(n=1)).status_code == 422
    assert api.post(f"/works/{w_bad_eng['id']}/lines/{lid}/regenerate", json=dict(n=1)).status_code == 422

    # Missing aux_ref_id on supported engine (v2pro) -> 404 before creating a job
    w_miss = api.post("/works", json=dict(
        title="miss-aux",
        lines=[dict(character="carol", text="hi", engine="v2pro", aux_ref_ids=["carol-404"])],
    )).json()
    lid2 = w_miss["lines"][0]["id"]
    assert api.post(f"/works/{w_miss['id']}/generate", json=dict(n=1)).status_code == 404
    assert api.post(f"/works/{w_miss['id']}/lines/{lid2}/regenerate", json=dict(n=1)).status_code == 404
    assert jobs.runs == before


# ---------------------------------------------------------------- 5. Review regressions (sol round 2)

def test_sync_candidates_and_compare_pick_primary_ref_once(carol_api, monkeypatch):
    """Sync /tts/candidates and /compare with emotion pick the primary ref once and reuse it during execution."""
    from types import SimpleNamespace
    from ttsapi.models import CandidatesResult

    api, _ = carol_api
    gateway.refs.set_tags("carol-14000140", ["happy"])
    gateway.refs.set_tags("carol-14200080", ["happy"])

    picks = []
    real_pick = gateway.refs.pick

    def counting_pick(character, ref_id=None, emotion=None):
        chosen = real_pick(character, ref_id, emotion)
        picks.append(chosen["id"])
        return chosen

    monkeypatch.setattr(gateway.refs, "pick", counting_pick)
    seen_refs = []

    async def fake_run_candidates(p, engine, n, ref, seed, work_id=None, on_item=None):
        seen_refs.append(ref["id"])
        return CandidatesResult(engine=engine, ref_id=ref["id"], ref_text=ref["text"], elapsed_sec=0.1, candidates=[])

    monkeypatch.setattr(synth, "run_candidates", fake_run_candidates)

    async def fake_ready(task, choice):
        return dict(device="local", type="local")

    monkeypatch.setattr(gateway, "ready", fake_ready)
    monkeypatch.setattr(gateway.dispatch, "executor", lambda chosen: SimpleNamespace(id="local"))

    r1 = api.post("/tts/candidates", json=dict(text="hi", character="carol", engine="v2pro", emotion="happy", n=1))
    assert r1.status_code == 200, r1.text
    assert len(picks) == 1 and seen_refs == picks

    picks.clear()
    seen_refs.clear()
    r2 = api.post("/compare", json=dict(text="hi", character="carol", engines=["v2pro", "v2proplus"], emotion="happy", n=1))
    assert r2.status_code == 200, r2.text
    assert len(picks) == 1 and seen_refs == [picks[0], picks[0]]


def test_auto_default_ref_skips_missing_and_unrounded_boundary_files(carol_api):
    """Automatic default_for/default_or_none skips missing files and 2.997s/10.004s boundary files."""
    api, lib = carol_api

    def exact_wav(n_samples: int, sr: int = 16000) -> bytes:
        buf = io.BytesIO()
        sf.write(buf, np.zeros(n_samples, dtype="float32"), sr, format="WAV")
        return buf.getvalue()

    # Delete the closest-to-6s ref's audio file and add a 2.997s upload claiming 6.0s display duration
    Path(gateway.refs.index["carol-14000140"]["path"]).unlink()
    near_short = gateway.refs.add_upload(exact_wav(47952), ".wav", "これは短いテスト文です", "ja", "carol")
    gateway.refs.index[near_short["id"]]["duration"] = 6.0  # closer to 6.0 than carol-14200080 (5.9s)

    # Should skip both vanished carol-14000140 and 2.997s near_short, picking valid carol-14200080
    assert gateway.refs.default_for("carol") == "carol-14200080"

    # If all remaining carol refs vanish, default_or_none returns None
    for rid in ("carol-14200080", "carol-14300010"):
        Path(gateway.refs.index[rid]["path"]).unlink()
    assert gateway.refs.default_or_none("carol") is None


def test_resolve_aux_refs_rejects_unsynced_missing_file(carol_api):
    """Auxiliary ref whose metadata exists in index but audio file is missing locally returns 409."""
    api, _ = carol_api
    Path(gateway.refs.index["carol-14200080"]["path"]).unlink()

    with pytest.raises(synth.AuxRefUnavailable):
        synth.resolve_aux_refs(["carol-14200080"], "carol-14000140")

    r = api.post("/tts", json=dict(text="hi", character="carol", engine="v2pro", aux_ref_ids=["carol-14200080"]))
    assert r.status_code == 409 and r.json()["code"] == "aux_ref_unavailable"


# ---------------------------------------------------------------- 6. Review regressions (cross-client round)

def test_emotion_pick_filters_out_short_and_unreadable_refs(carol_api):
    """RefLibrary.pick(..., emotion=...) only picks readable 3-10s primary refs and raises cleanly if none."""
    api, _ = carol_api

    def exact_wav(n_samples: int, sr: int = 16000) -> bytes:
        buf = io.BytesIO()
        sf.write(buf, np.zeros(n_samples, dtype="float32"), sr, format="WAV")
        return buf.getvalue()

    short_aux = gateway.refs.add_upload(exact_wav(int(1.4 * 16000)), ".wav", "short", "ja", "carol")
    near_min = gateway.refs.add_upload(exact_wav(47952), ".wav", "2.997s", "ja", "carol")
    vanished = gateway.refs.add_upload(exact_wav(80000), ".wav", "gone", "ja", "carol")
    valid = gateway.refs.add_upload(exact_wav(81600), ".wav", "valid 5.1s", "ja", "carol")

    for r in (short_aux, near_min, vanished, valid):
        gateway.refs.set_tags(r["id"], ["sad"])
    Path(gateway.refs.index[vanished["id"]]["path"]).unlink()

    for _ in range(20):
        assert gateway.refs.pick("carol", emotion="sad")["id"] == valid["id"]

    gateway.refs.set_tags(short_aux["id"], ["aux_only"])
    with pytest.raises(KeyError, match="可用的主参考"):
        gateway.refs.pick("carol", emotion="aux_only")
    res = api.post("/tts", json=dict(text="hi", character="carol", engine="v2pro", emotion="aux_only"))
    assert res.status_code == 404 and "可用的主参考" in res.json()["detail"]


def test_cross_engine_default_remembers_latest_engine_and_falls_back(carol_api, tmp_path):
    """Setting manual default on a later engine (v4 after v2pro) persists default_engine so versions()
    recommends v4, and clearing/deleting v4's manual default falls back cleanly."""
    from ttsapi.catalog import Catalog, versions

    api, lib = carol_api
    cat = Catalog()

    def make_w(name: str) -> Path:
        p = tmp_path / "w" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(name.encode() * 10)
        return p

    cat.register_run("carol", "SAMPLE_DS.sp0001", dict(run_name="carol-v2p", engine="v2pro",
                                                        created="2026-11-01T00:00:00+09:00"),
                     dict(gpt=[make_w("v2p-e5.ckpt")], sovits=[make_w("v2p_e4.pth")]))
    cat.register_run("carol", "SAMPLE_DS.sp0001", dict(run_name="carol-v4", engine="v4",
                                                        created="2026-11-02T00:00:00+09:00"),
                     dict(gpt=[make_w("v4-e5.ckpt")], sovits=[make_w("v4_e2.pth")]))

    # Set manual default on v2pro first, then v4 -> v4 must become recommended
    cat.set_default("v2pro", "carol", "gpt", "v2p-e5.ckpt")
    assert lib.get("characters/carol.json")["default_engine"] == "v2pro"
    assert next(v for v in versions(cat, "carol") if v["recommended"])["engine"] == "v2pro"

    cat.set_default("v4", "carol", "gpt", "v4-e5.ckpt")
    assert lib.get("characters/carol.json")["default_engine"] == "v4"
    vs = api.get("/characters/carol/versions").json()
    assert next(v for v in vs if v["recommended"])["id"] == "v4/v4-e5.ckpt/v4_e2.pth"

    # Clearing v4 manual default clears default_engine and falls back to remaining manual default (v2pro)
    cat.set_default("v4", "carol", "gpt", None)
    assert lib.get("characters/carol.json")["default_engine"] is None
    assert next(v for v in versions(cat, "carol") if v["recommended"])["engine"] == "v2pro"

    # Deleting a manual default weight on default_engine also falls back cleanly
    cat.set_default("v4", "carol", "gpt", "v4-e5.ckpt")
    assert next(v for v in versions(cat, "carol") if v["recommended"])["engine"] == "v4"
    cat.delete_weight(cat.find("v4:v4-e5.ckpt"))
    assert lib.get("characters/carol.json")["default_engine"] is None
    assert next(v for v in versions(cat, "carol") if v["recommended"])["engine"] == "v2pro"

    # Legacy character JSON without default_engine key remains compatible
    doc = dict(lib.get("characters/carol.json"))
    doc.pop("default_engine", None)
    lib.put("characters/carol.json", doc)
    assert next(v for v in versions(cat, "carol") if v["recommended"])["engine"] == "v2pro"



