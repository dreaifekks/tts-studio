"""Unit tests for Feature A (manual cloud GPU synthesis/compare) and Feature B (V5 engine support)."""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import write_wav
from test_colab_jobs import FakePool, FakeVM
from test_colab_jobs import fake_code as _fake_code_fixture
from test_colab_jobs import carol as _carol_fixture
from test_compute import AUTO, M6, cand, l4, mini
from test_dispatch import snapshot_of
from ttsapi import db, gateway, importer, jobs, osutil, reviews, settings, synth, train_runner, works
from ttsapi.compute import colab_jobs, dispatch, registry, scheduler, speeds
from ttsapi.models import (AudioItem, CandidatesParams, CandidatesResult, CompareParams, CompareResult,
                           ComputeChoice, JobItemDone, ReviewIn, WorkCreate, WorkLineIn)

carol = _carol_fixture
fake_code = _fake_code_fixture


def _client():
    async def local(scope, receive, send):
        await gateway.app(dict(scope, client=("127.0.0.1", 50000)) if scope["type"] == "http" else scope, receive, send)
    return TestClient(local)


# ---------------------------------------------------------------- Feature A: Cloud synthesis & compare

def test_plan_allows_cloud_gen_and_estimates_upload_mb(carol, monkeypatch, tmp_path):
    """POST /plan (kind='gen') keeps recommending local for small batches, exposes cloud GPUs as available,
    and estimates upload_mb accurately when character/weights/ref_id/aux_ref_ids are passed."""
    lib, cat, refs = carol
    monkeypatch.setattr(gateway, "catalog", cat)
    monkeypatch.setattr(gateway, "refs", refs)
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()]))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)
    api = _client()

    # Without character/weights: fallback PREP_CLOUD (360s), local still recommended.
    p_plain = api.post("/plan", json=dict(kind="gen", engine="v2pro", items=6)).json()
    assert (p_plain["recommended"], p_plain["reason_code"]) == ("local", "local_fast_enough")
    c_plain = cand(p_plain, "colab-L4")
    assert c_plain["available"] is True and c_plain["reason_code"] is None and c_plain["prep_sec"] == 360

    # With character/ref/aux_ref: computes upload_mb (~few KB in test) -> prep_sec drops to ~CLOUD_BOOT (210s).
    p_sized = api.post("/plan", json=dict(
        kind="gen", engine="v2pro", items=6, character="carol", ref_id="carol-14000140",
        aux_ref_ids=["carol-14200080"], compute="colab-L4",
    )).json()
    c_sized = cand(p_sized, "colab-L4")
    assert p_sized["chosen"] == "colab-L4"
    assert c_sized["available"] is True and c_sized["prep_sec"] < 220

    # Compare (multi-engine) with v5turbo also exposes cloud as available.
    p_cmp = api.post("/plan", json=dict(
        kind="gen", engines=["v2pro", "v5turbo"], items=3, character="carol", ref_id="carol-14000140",
    )).json()
    assert cand(p_cmp, "colab-L4")["available"] is True

    # Missing local weight file surfaces clear cloud_inputs_missing reason on cloud candidate.
    lib.path("weights/carol/v2pro/carol-e15/gpt.ckpt").unlink()
    p_missing = api.post("/plan", json=dict(
        kind="gen", engine="v2pro", items=2, character="carol", ref_id="carol-14000140",
    )).json()
    c_miss = cand(p_missing, "colab-L4")
    assert c_miss["available"] is False and c_miss["reason_code"] == "cloud_inputs_missing"


def test_plan_cloud_capable_false_disables_cloud_candidates(carol, monkeypatch):
    """POST /plan with cloud_capable=False marks cloud GPUs kind_unsupported (works / blind / sync parity)."""
    lib, cat, refs = carol
    monkeypatch.setattr(gateway, "catalog", cat)
    monkeypatch.setattr(gateway, "refs", refs)
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()]))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)
    api = _client()

    for payload in (
        dict(kind="gen", engine="v2pro", items=4, cloud_capable=False),
        dict(kind="gen", engines=["v2pro", "v4"], items=2, cloud_capable=False),
    ):
        p = api.post("/plan", json=payload).json()
        c = cand(p, "colab-L4")
        assert c["available"] is False
        assert c["reason_code"] == "kind_unsupported"
        assert c["reason"] == "云端 GPU 暂支持合成、评测和训练"


def test_plan_surfaces_version_unavailable_and_missing_inputs_as_cloud_error(carol, monkeypatch):
    """POST /plan does not swallow VersionUnavailable or missing ref/weight errors; marks cloud cloud_inputs_missing."""
    lib, cat, refs = carol
    monkeypatch.setattr(gateway, "catalog", cat)
    monkeypatch.setattr(gateway, "refs", refs)
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()]))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)
    api = _client()

    # 1. Version pointing to deleted/unknown weight -> VersionUnavailable -> cloud_inputs_missing.
    p_del = api.post("/plan", json=dict(
        kind="gen", engine="v2pro", items=2, character="carol",
        version="v2pro/deleted-e99.ckpt/carol_e8_s144.pth",
    )).json()
    c_del = cand(p_del, "colab-L4")
    assert c_del["available"] is False and c_del["reason_code"] == "cloud_inputs_missing"
    assert "deleted-e99.ckpt" in c_del["reason"]

    # 2. Missing ref_id -> cloud_inputs_missing.
    p_ref = api.post("/plan", json=dict(
        kind="gen", engine="v2pro", items=2, character="carol", ref_id="carol-does-not-exist",
    )).json()
    c_ref = cand(p_ref, "colab-L4")
    assert c_ref["available"] is False and c_ref["reason_code"] == "cloud_inputs_missing"
    assert "carol-does-not-exist" in c_ref["reason"]

    # 3. Version whose backing weight file is missing on disk -> VersionUnavailable -> cloud_inputs_missing.
    lib.path("weights/carol/v2pro/carol-e15/gpt.ckpt").unlink()
    p_unsynced = api.post("/plan", json=dict(
        kind="gen", engine="v2pro", items=2, character="carol",
        version="v2pro/carol-e15.ckpt/carol_e8_s144.pth",
    )).json()
    c_uns = cand(p_unsynced, "colab-L4")
    assert c_uns["available"] is False and c_uns["reason_code"] == "cloud_inputs_missing"
    assert "还没同步" in c_uns["reason"]


def test_gen_auto_recommend_never_auto_picks_cloud(monkeypatch):
    """Auto mode must never silently pick cloud GPUs for kind='gen' (neither only_cloud nor cloud_halves)."""
    # 1. Local lacks v5turbo, only cloud has it -> auto returns no_device (suggests manual cloud), never colab-L4.
    devs_no_v5 = [mini(engines=["v2pro", "v2proplus", "v4"]), l4()]
    p_auto = scheduler.plan(devs_no_v5, scheduler.Task("gen", [("v5turbo", 2)]), AUTO)
    assert (p_auto["recommended"], p_auto["reason_code"], p_auto["chosen"]) == (None, "no_device", None)
    assert "云端" in p_auto["reason"]
    c_l4 = cand(p_auto, "colab-L4")
    assert c_l4["available"] is True and c_l4["units"] is not None

    # Explicit manual pick selects colab-L4 cleanly.
    p_pick = scheduler.plan(devs_no_v5, scheduler.Task("gen", [("v5turbo", 2)]), AUTO, pick="colab-L4")
    assert p_pick["chosen"] == "colab-L4" and cand(p_pick, "colab-L4")["available"] is True

    # dispatch.choose without explicit compute rejects instead of silently booting cloud.
    monkeypatch.setattr(registry, "snapshot", snapshot_of(devs_no_v5))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)
    with pytest.raises(dispatch.PlanError) as exc:
        asyncio.run(dispatch.choose(scheduler.Task("gen", [("v5turbo", 2)])))
    assert exc.value.code == "compute_unavailable"
    chosen = asyncio.run(dispatch.choose(scheduler.Task("gen", [("v5turbo", 2)]), ComputeChoice(compute="colab-L4")))
    assert chosen["device"] == "colab-L4"

    # 2. Large gen batch where cloud would halve time (>15 min local): still stays local (never cloud_halves).
    p_big_gen = scheduler.plan([mini(), l4()], scheduler.Task("gen", [("v4", 60)]), AUTO)
    assert cand(p_big_gen, "local")["seconds"] >= scheduler.BIG
    assert cand(p_big_gen, "colab-L4")["seconds"] <= cand(p_big_gen, "local")["seconds"] * scheduler.CLOUD_FACTOR
    assert (p_big_gen["recommended"], p_big_gen["reason_code"]) == ("local", "fastest")


def test_cloud_audio_import_never_overwrites_existing_and_is_idempotent(tmp_path):
    """import_gen_audio must never overwrite existing WAVs or audio rows (preserving keep/work_id),
    allocates a unique local ID on collision, rewrites results/completed IDs+URLs, and is idempotent per job_id."""
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    # Seed an existing local audio record with keep=1 and work_id='w-keep'.
    existing_id = "20261122-120000-aaaaaa"
    existing_wav = settings.AUDIO_DIR / f"{existing_id}.wav"
    existing_wav.write_bytes(b"LOCAL-ORIGINAL-WAV")
    existing_meta = AudioItem(
        id=existing_id, created="2026-11-22T12:00:00", audio_url=f"/audio/{existing_id}", text="local original",
        character="carol", engine="v2pro", gpt="g.ckpt", sovits="s.pth", ref_id="r1", seed=1,
        duration=1.0, infer_sec=1.0, synth_wall_sec=1.1, semantic_tokens=10, vram_peak_mb=100,
        ratio_vs_ref=[1.0], work_id="w-keep", compute="local",
    ).model_dump(exclude={"review"})
    db.run(
        f"INSERT OR REPLACE INTO audio({synth._AUDIO_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        existing_id, 1700000000.0, "local original", "carol", "v2pro", "r1", "g.ckpt", "s.pth",
        1.0, 15, 1.0, 90.0, "w-keep", 1, json.dumps(existing_meta, ensure_ascii=False),
    )

    # Seed an orphan WAV on disk (not in DB) that must also never be overwritten.
    orphan_id = "20261122-120000-bbbbbb"
    orphan_wav = settings.AUDIO_DIR / f"{orphan_id}.wav"
    orphan_wav.write_bytes(b"ORPHAN-DISK-WAV")

    # Build a synthetic VM extracted source dir containing colliding VM IDs.
    src = tmp_path / "vm-extract"
    (src / "data" / "audio").mkdir(parents=True)
    (src / "data" / "audio" / f"{existing_id}.wav").write_bytes(b"CLOUD-NEW-WAV-1")
    (src / "data" / "audio" / f"{orphan_id}.wav").write_bytes(b"CLOUD-NEW-WAV-2")

    def mk_item(aid, text):
        return AudioItem(
            id=aid, created="2026-11-22T12:05:00", audio_url=f"/audio/{aid}", text=text,
            character="carol", engine="v5turbo", gpt="g.ckpt", sovits="s.pth", ref_id="r1", seed=7,
            duration=2.0, infer_sec=2.0, synth_wall_sec=2.1, semantic_tokens=20, vram_peak_mb=500,
            ratio_vs_ref=[1.0], score=96.0, compute="local",
        ).model_dump()

    record = dict(
        id="cand-collide-job-1",
        kind="candidates",
        status="succeeded",
        completed=[
            JobItemDone(audio_id=existing_id, audio_url=f"/audio/{existing_id}", score=96.0, seed=7, done=1, total=2).model_dump(),
            JobItemDone(audio_id=orphan_id, audio_url=f"/audio/{orphan_id}", score=95.0, seed=8, done=2, total=2).model_dump(),
        ],
        latest=JobItemDone(audio_id=orphan_id, audio_url=f"/audio/{orphan_id}", score=95.0, seed=8, done=2, total=2).model_dump(),
        candidates_result=CandidatesResult(
            engine="v5turbo", ref_id="r1", ref_text="t", elapsed_sec=4.0,
            candidates=[AudioItem(**mk_item(existing_id, "cloud-1")), AudioItem(**mk_item(orphan_id, "cloud-2"))],
        ).model_dump(),
    )

    id_map = importer.import_gen_audio(src, record=record, device_id="colab-L4", job_id="cand-collide-job-1")
    assert set(id_map) == {existing_id, orphan_id}
    new_1, new_2 = id_map[existing_id], id_map[orphan_id]
    assert new_1 != existing_id and new_2 != orphan_id and new_1 != new_2

    # Original local record and files are completely untouched.
    assert existing_wav.read_bytes() == b"LOCAL-ORIGINAL-WAV"
    row_orig = db.one("SELECT text, work_id, keep FROM audio WHERE id=?", existing_id)
    assert (row_orig["text"], row_orig["work_id"], row_orig["keep"]) == ("local original", "w-keep", 1)
    assert orphan_wav.read_bytes() == b"ORPHAN-DISK-WAV"

    # Imported cloud audio lives at the newly allocated IDs with rewritten metadata.
    assert (settings.AUDIO_DIR / f"{new_1}.wav").read_bytes() == b"CLOUD-NEW-WAV-1"
    assert (settings.AUDIO_DIR / f"{new_2}.wav").read_bytes() == b"CLOUD-NEW-WAV-2"
    got_1 = synth.get_audio(new_1)
    assert (got_1.id, got_1.audio_url, got_1.text, got_1.compute) == (new_1, f"/audio/{new_1}", "cloud-1", "colab-L4")

    # Rewriting candidates_result and completed updates IDs and /audio/<local_id> URLs.
    cr = importer.rewrite_gen_result("candidates_result", record["candidates_result"], id_map, "colab-L4")
    assert [(c["id"], c["audio_url"], c["compute"]) for c in cr["candidates"]] == [
        (new_1, f"/audio/{new_1}", "colab-L4"),
        (new_2, f"/audio/{new_2}", "colab-L4"),
    ]
    done_rw = [importer.rewrite_item_done(d, id_map) for d in record["completed"]]
    assert [(d["audio_id"], d["audio_url"]) for d in done_rw] == [
        (new_1, f"/audio/{new_1}"),
        (new_2, f"/audio/{new_2}"),
    ]

    # Idempotent retry for the SAME job_id returns the exact same mapping without creating new rows.
    before_cnt = db.one("SELECT COUNT(*) FROM audio")[0]
    id_map_retry = importer.import_gen_audio(src, record=record, device_id="colab-L4", job_id="cand-collide-job-1")
    assert id_map_retry == id_map
    assert db.one("SELECT COUNT(*) FROM audio")[0] == before_cnt

    # A DIFFERENT job_id with the same VM audio ID allocates fresh IDs without overwriting anything.
    id_map_other = importer.import_gen_audio(src, record=record, device_id="colab-L4", job_id="cand-collide-job-2")
    assert id_map_other[existing_id] not in (existing_id, new_1)


def test_failed_cloud_gen_job_preserves_completed_audio_and_failed_status(carol, tmp_path, fake_code, monkeypatch):
    """When a cloud candidates job fails mid-way after completing 1 item, run_colab imports the completed WAV + DB
    row, populates job.completed with remapped IDs/URLs, and keeps the job failed with its error."""
    import sqlite3

    lib, cat, refs = carol
    monkeypatch.setattr(gateway, "catalog", cat)
    monkeypatch.setattr(gateway, "refs", refs)
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()]))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)

    # Pre-seed a colliding local ID to also test collision remapping on failed jobs.
    collide_id = "20261122-130000-cccccc"
    settings.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    (settings.AUDIO_DIR / f"{collide_id}.wav").write_bytes(b"KEEP-LOCAL-FILE")

    def fake_fail_vm(task, _cat, root):
        out_audio = root / "out" / "data" / "audio"
        out_audio.mkdir(parents=True, exist_ok=True)
        (out_audio / f"{collide_id}.wav").write_bytes(b"PARTIAL-CLOUD-WAV")
        item = AudioItem(
            id=collide_id, created="2026-11-22T13:00:00", audio_url=f"/audio/{collide_id}", text=task["text"],
            text_lang="ja", character=task["character"], engine=task["engine"], gpt="g.ckpt", sovits="s.pth",
            ref_id=task["ref_id"], seed=10, duration=2.0, infer_sec=1.5, synth_wall_sec=1.6,
            semantic_tokens=30, vram_peak_mb=800, ratio_vs_ref=[1.0], score=94.0, compute="local",
        )
        # Write the completed item into VM's tts.db audio table (just like real gateway does before item_done).
        vdb = sqlite3.connect(root / "out" / "data" / "tts.db")
        vdb.executescript(db.SCHEMA)
        vdb.execute(
            f"INSERT INTO audio({synth._AUDIO_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            synth._audio_values(item.model_dump(exclude={"review"}), 1700000100.0),
        )
        vdb.commit()
        vdb.close()
        done = JobItemDone(audio_id=collide_id, audio_url=f"/audio/{collide_id}", score=94.0, seed=10, done=1, total=2)
        return dict(
            id=task["job_id"], kind="candidates", status="failed", error="CUDA out of memory",
            completed=[done.model_dump()], latest=done.model_dump(),
        )

    vm = FakeVM(tmp_path / "vm-partial", fake_fail_vm)
    monkeypatch.setattr(registry, "pool", FakePool(vm))

    async def run_fail():
        job = await gateway.candidates_job(CandidatesParams(
            text="途中失敗テスト", character="carol", engine="v5turbo", n=2, compute="colab-L4",
        ))
        await jobs.runs[job.id].task
        return jobs.get(job.id)

    j = asyncio.run(run_fail())
    assert j.status == "failed" and j.error and "显存不足" in j.error
    assert j.candidates_result is None
    assert len(j.completed) == 1
    saved_id = j.completed[0].audio_id
    assert saved_id != collide_id
    assert j.completed[0].audio_url == f"/audio/{saved_id}"
    assert j.latest is not None and j.latest.audio_id == saved_id
    assert (settings.AUDIO_DIR / f"{collide_id}.wav").read_bytes() == b"KEEP-LOCAL-FILE"
    assert (settings.AUDIO_DIR / f"{saved_id}.wav").read_bytes() == b"PARTIAL-CLOUD-WAV"
    assert synth.get_audio(saved_id).compute == "colab-L4"


def test_non_cloud_capable_gen_endpoints_still_reject_cloud(carol, monkeypatch):
    """Sync /tts, /tts/candidates, /compare, works generate/regenerate, and /blind reject cloud GPUs."""
    lib, cat, refs = carol
    monkeypatch.setattr(gateway, "catalog", cat)
    monkeypatch.setattr(gateway, "refs", refs)
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()]))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)
    api = _client()

    # Sync endpoints
    for path, body in (
        ("/tts", dict(text="こんにちは", character="carol", engine="v2pro", compute="colab-L4")),
        ("/tts/candidates", dict(text="こんにちは", character="carol", engine="v2pro", n=2, compute="colab-L4")),
        ("/compare", dict(text="こんにちは", character="carol", engines=["v2pro", "v4"], n=1, compute="colab-L4")),
    ):
        r = api.post(path, json=body)
        assert r.status_code == 409 and r.json()["code"] == "compute_unavailable", path
        c = cand(r.json()["plan"], "colab-L4")
        assert c["reason_code"] == "kind_unsupported" and c["reason"] == "云端 GPU 暂支持合成、评测和训练"

    # Works generate & regenerate
    w = works.create(WorkCreate(title="w", lines=[WorkLineIn(character="carol", text="テスト", engine="v2pro")]))
    for path, body in (
        (f"/works/{w.id}/generate", dict(n=1, compute="colab-L4")),
        (f"/works/{w.id}/lines/{w.lines[0].id}/regenerate", dict(n=1, compute="colab-L4")),
    ):
        r = api.post(path, json=body)
        assert r.status_code == 409 and r.json()["code"] == "compute_unavailable", path
        assert cand(r.json()["plan"], "colab-L4")["reason_code"] == "kind_unsupported"

    # Blind create
    r = api.post("/blind", json=dict(
        character="carol", a=dict(engine="v2pro"), b=dict(engine="v4"), eval_set="mini", compute="colab-L4",
    ))
    assert r.status_code == 409 and r.json()["code"] == "compute_unavailable"
    assert cand(r.json()["plan"], "colab-L4")["reason_code"] == "kind_unsupported"


def test_export_candidates_and_compare_inputs_cross_char_and_derived_refs(carol, tmp_path):
    """export_inputs for candidates/compare packs character json, registered & dataset-derived refs (including
    cross-character aux ref), resolved non-pretrained weights + run.json, and NO dataset tar."""
    lib, cat, refs = carol
    # Add an uploaded cross-character aux ref under 'custom'.
    aux_wav = tmp_path / "aux.wav"
    write_wav(aux_wav, 4.0)
    up = refs.add_upload(aux_wav.read_bytes(), ".wav", "別キャラ参照", "ja", "custom")

    # Add a trained run weight for v4 so compare has both july (v2pro) and user run (v4) weights.
    run_name = "carol-v4-20261122-010203"
    g4 = tmp_path / f"{run_name}-e5.ckpt"
    s4 = tmp_path / f"{run_name}_e2_s10_l32.pth"
    g4.write_bytes(b"g4" * 20)
    s4.write_bytes(b"s4" * 20)
    cat.register_run("carol", "SAMPLE_DS.sp0001", dict(run_name=run_name, engine="v4"), dict(gpt=[g4], sovits=[s4]))

    # Candidates with dataset-derived primary ref (carol-14200080) + cross-char uploaded aux ref.
    req = CandidatesParams(text="テスト", character="carol", engine="v2pro", ref_id="carol-14200080", aux_ref_ids=[up["id"]])
    out_dir = tmp_path / "exp-cand"
    out_dir.mkdir()
    inputs = colab_jobs.export_inputs("candidates", [req], cat, refs, out_dir, character="carol",
                                      ref_id="carol-14200080", aux_ref_ids=[up["id"]])
    assert not any(k.endswith(".tar") for k in inputs)
    assert {
        "library/characters/carol.json",
        "library/refs/carol/carol-14200080.json",
        "library/refs/carol/carol-14200080.wav",
        f"library/refs/custom/{up['id']}.json",
        f"library/refs/custom/{up['id']}.wav",
        "library/weights/carol/v2pro/carol-e15/gpt.ckpt",
        "library/weights/carol/v2pro/carol-e15/meta.json",
        "library/weights/carol/v2pro/carol_e8_s144/sovits.pth",
        "library/weights/carol/v2pro/carol_e8_s144/meta.json",
    } <= set(inputs)

    # Compare across v2pro + v4 + v5turbo (v5turbo uses pretrained -> no weight upload for v5turbo).
    cmp_req = CompareParams(text="テスト", character="carol", engines=["v2pro", "v4", "v5turbo"], ref_id="carol-14000140")
    out_cmp = tmp_path / "exp-cmp"
    out_cmp.mkdir()
    cmp_inputs = colab_jobs.export_inputs("compare", [cmp_req], cat, refs, out_cmp, character="carol",
                                          ref_id="carol-14000140", aux_ref_ids=[])
    assert not any(k.endswith(".tar") for k in cmp_inputs)
    assert f"library/runs/{run_name}/run.json" in cmp_inputs
    assert f"library/weights/carol/v4/{run_name}-e5/gpt.ckpt" in cmp_inputs
    assert "library/weights/carol/v2pro/carol-e15/gpt.ckpt" in cmp_inputs

    spec = colab_jobs.job_spec("cmp-1", "compare", cmp_req, {}, character="carol", ref_id="carol-14000140", aux_ref_ids=[])
    assert spec["engines"] == ["v2pro", "v4", "v5turbo"]
    assert spec["tasks"][0]["kind"] == "compare" and spec["tasks"][0]["compute"] == "local"
    assert spec["tasks"][0]["ref_id"] == "carol-14000140"

    # Missing local weight file fails clearly.
    lib.path("weights/carol/v2pro/carol-e15/gpt.ckpt").unlink()
    out_bad = tmp_path / "exp-bad"
    out_bad.mkdir()
    with pytest.raises(FileNotFoundError):
        colab_jobs.export_inputs("candidates", [req], cat, refs, out_bad, character="carol",
                                 ref_id="carol-14200080", aux_ref_ids=[])


def test_candidates_and_compare_jobs_route_cloud_and_update_speeds(carol, tmp_path, fake_code, monkeypatch):
    """POST /tts/candidates/jobs and /compare/jobs pick primary ref once, route cloud to colab_jobs.run_colab
    (never dispatch.executor), import audio + db rows, and update cloud speeds.json."""
    lib, cat, refs = carol
    monkeypatch.setattr(gateway, "catalog", cat)
    monkeypatch.setattr(gateway, "refs", refs)
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()]))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)
    monkeypatch.setattr(settings, "SPEEDS_FILE", tmp_path / "speeds.json")
    monkeypatch.setattr(dispatch, "executor", lambda chosen: (_ for _ in ()).throw(AssertionError("should not use local/LAN executor")))

    def fake_gen_vm(task, _cat, root):
        out_audio = root / "out" / "data" / "audio"
        out_audio.mkdir(parents=True, exist_ok=True)

        def mk(engine, idx):
            aid = f"cloud-{engine}-{idx}"
            (out_audio / f"{aid}.wav").write_bytes(b"RIFF-cloud-" + engine.encode())
            return AudioItem(
                id=aid, created="2026-11-22T12:00:00", audio_url=f"/audio/{aid}", text=task["text"],
                text_lang="ja", character=task["character"], engine=engine, gpt="g.ckpt", sovits="s.pth",
                ref_id=task["ref_id"], seed=10 + idx, duration=2.0, infer_sec=2.5, synth_wall_sec=2.7,
                semantic_tokens=40, vram_peak_mb=1024, ratio_vs_ref=[1.0], score=95.0, compute="local",
            )

        if task["kind"] == "candidates":
            items = [mk(task["engine"], i) for i in range(task["n"])]
            cr = CandidatesResult(engine=task["engine"], ref_id=task["ref_id"], ref_text="t", elapsed_sec=5.0, candidates=items)
            return dict(id=task["job_id"], kind="candidates", status="succeeded", candidates_result=cr.model_dump())
        results = [
            CandidatesResult(engine=e, ref_id=task["ref_id"], ref_text="t", elapsed_sec=3.0,
                             candidates=[mk(e, i) for i in range(task["n"])])
            for e in task["engines"]
        ]
        cmp = CompareResult(text=task["text"], ref_id=task["ref_id"], ref_text="t", base_seed=10,
                            elapsed_sec=6.0, results=results)
        return dict(id=task["job_id"], kind="compare", status="succeeded", compare_result=cmp.model_dump())

    class AudioFakeVM(FakeVM):
        def download(self, remote, local, timeout=180):
            super().download(remote, local, timeout=timeout)
            # Also inject data/audio/<id>.wav into the packed tar produced by FakeVM.
            audio_dir = self.root / "out" / "data" / "audio"
            if audio_dir.is_dir():
                with tarfile.open(local, "a") as tf:
                    for f in sorted(audio_dir.iterdir()):
                        tf.add(f, f"data/audio/{f.name}")

    # Test candidates_job on cloud (with emotion tag -> picked once on control side).
    refs.set_tags("carol-14000140", ["calm"])
    vm1 = AudioFakeVM(tmp_path / "vm1", fake_gen_vm)
    monkeypatch.setattr(registry, "pool", FakePool(vm1))

    async def run_cand():
        job = await gateway.candidates_job(CandidatesParams(
            text="今日はいい天気だね。", character="carol", engine="v5turbo", emotion="calm", n=2, compute="colab-L4",
        ))
        await jobs.runs[job.id].task
        return jobs.get(job.id)

    j1 = asyncio.run(run_cand())
    assert j1.status == "succeeded" and j1.compute.device == "colab-L4"
    assert j1.candidates_result is not None and len(j1.candidates_result.candidates) == 2
    assert j1.candidates_result.ref_id == "carol-14000140"
    for item in j1.candidates_result.candidates:
        assert item.compute == "colab-L4"
        assert (settings.AUDIO_DIR / f"{item.id}.wav").is_file()
        assert synth.get_audio(item.id).compute == "colab-L4"
        # Review & work attach work identically to local audio.
        rev = reviews.upsert(item.id, ReviewIn(rating=5, verdict="good"))
        assert rev.rating == 5
    sp_data = json.loads(settings.SPEEDS_FILE.read_text(encoding="utf-8"))
    assert "v5turbo" in sp_data["colab-L4"] and sp_data["colab-L4"]["v5turbo"]["n"] == 2

    # Test compare_job on cloud even on a read-only (unowned) library!
    lib.info["owner"] = dict(machine_id="other-host", name="other")
    vm2 = AudioFakeVM(tmp_path / "vm2", fake_gen_vm)
    monkeypatch.setattr(registry, "pool", FakePool(vm2))

    async def run_cmp():
        job = await gateway.compare_job(CompareParams(
            text="比較テストです。", character="carol", engines=["v2pro", "v5dev"], n=1, compute="colab-L4",
        ))
        await jobs.runs[job.id].task
        return jobs.get(job.id)

    j2 = asyncio.run(run_cmp())
    assert j2.status == "succeeded" and j2.compare_result is not None
    assert [r.engine for r in j2.compare_result.results] == ["v2pro", "v5dev"]
    for part in j2.compare_result.results:
        item = part.candidates[0]
        assert item.compute == "colab-L4" and (settings.AUDIO_DIR / f"{item.id}.wav").is_file()
    sp_data = json.loads(settings.SPEEDS_FILE.read_text(encoding="utf-8"))
    assert "v2pro" in sp_data["colab-L4"] and "v5dev" in sp_data["colab-L4"]


# ---------------------------------------------------------------- Feature B: V5 support

def test_v5_reference_speeds_mem_and_compute_list(tmp_path, monkeypatch):
    """CLOUD_ENGINES, reference-speeds.json, engine_mem_gb, and GET /compute cover v5turbo and v5dev."""
    assert settings.CLOUD_ENGINES == ["v2pro", "v2proplus", "v4", "v5dev", "v5turbo"]
    assert speeds.engine_mem_gb("v5turbo") == 14
    assert speeds.engine_mem_gb("v5dev") == 14

    monkeypatch.setattr(settings, "SPEEDS_FILE", tmp_path / "speeds.json")
    expected = {
        "L4": (10.0, 38.0),
        "T4": (16.0, 60.0),
        "A100": (6.0, 23.0),
        "G4": (5.0, 19.0),
    }
    for gpu, (vt, vd) in expected.items():
        dev = dict(id=f"colab-{gpu}", type="cloud", gpu=gpu)
        assert speeds.lookup(dev, "v5turbo") == (vt, "reference")
        assert speeds.lookup(dev, "v5dev") == (vd, "reference")
    assert speeds.lookup(M6, "v5turbo") == (42, "reference")
    assert speeds.lookup(M6, "v5dev") == (None, "untested")

    monkeypatch.setattr(registry, "pool", None)
    cloud_devs = {d["id"]: d for d in registry.cloud_states([])}
    for did in ("colab-L4", "colab-T4", "colab-A100", "colab-G4"):
        sp = cloud_devs[did]["speeds"]
        assert {"v5turbo", "v5dev", "train", "scorer"} <= set(sp)
        assert sp["v5turbo"][0] is not None and sp["v5dev"][0] is not None


def test_train_runner_v5_config_and_scripts(tmp_path, monkeypatch):
    """Isolated train_runner.train() check for v5turbo: runs preprocess & s2_train_v3_lora.py in V5_REPO with
    V5_PYTHON and lora_rank, and runs s1_train.py in OLD_REPO (v2pro) using V5's pretrained_gpt."""
    old_repo = tmp_path / "GPTSoVits"
    v5_repo = tmp_path / "GPTSoVits-v5"
    for r in (old_repo, v5_repo):
        (r / "GPT_SoVITS" / "configs").mkdir(parents=True)
        (r / "tools").mkdir(parents=True)
    (v5_repo / "GPT_SoVITS" / "configs" / "s2.json").write_text(
        json.dumps(dict(train=dict(), model=dict(), data=dict())), encoding="utf-8"
    )
    (old_repo / "GPT_SoVITS" / "configs" / "s1longer-v2.yaml").write_text(
        "data:\n  num_workers: 4\ntrain:\n  batch_size: 8\n", encoding="utf-8"
    )
    v5_py = v5_repo / ".venv" / "bin" / "python"
    old_py = old_repo / ".venv" / "bin" / "python"

    monkeypatch.setattr(settings, "TRAIN_DIR", tmp_path / "training")
    monkeypatch.setattr(settings, "WEIGHTS_DIR", tmp_path / "weights")
    monkeypatch.setitem(settings.ENGINES["v2pro"], "repo", old_repo)
    monkeypatch.setitem(settings.ENGINES["v2pro"], "python", old_py)
    monkeypatch.setitem(settings.ENGINES["v5turbo"], "repo", v5_repo)
    monkeypatch.setitem(settings.ENGINES["v5turbo"], "python", v5_py)
    monkeypatch.setitem(settings.ENGINES["v5turbo"], "pretrained_gpt",
                        v5_repo / "GPT_SoVITS" / "pretrained_models" / "s1v3.ckpt")
    monkeypatch.setitem(settings.ENGINES["v5turbo"], "pretrained_sovits",
                        v5_repo / "GPT_SoVITS" / "pretrained_models" / "gsv-v5-pretrained" / "s2Gv5turbo.pth")

    run_name = "carol-v5-20261122-000000"
    run_dir = settings.TRAIN_DIR / run_name
    exp = run_dir / "exp"
    exp.mkdir(parents=True)
    log_path = tmp_path / "job.log"
    spec = dict(
        run_name=run_name,
        train_list=str(run_dir / "train.list"),
        log_path=str(log_path),
        req=dict(engine="v5turbo", sovits_epochs=2, sovits_save_every=1, sovits_batch=4,
                 gpt_epochs=5, gpt_save_every=5, gpt_batch=4, lora_rank=64),
    )
    (run_dir / "runner_spec.json").write_text(json.dumps(spec), encoding="utf-8")

    calls = []
    runner = train_runner.Runner(run_dir)

    def fake_run_script(eng, ws, args, env_extra, on_line=None):
        calls.append((eng["repo"], eng["python"], list(map(str, args)), dict(env_extra)))
        if str(args[0]).endswith("1-get-text.py"):
            (exp / "2-name2text-0.txt").write_text("a|b\n", encoding="utf-8")
        elif str(args[0]).endswith("3-get-semantic.py"):
            (exp / "6-name2semantic-0.tsv").write_text("a\t1 2\n", encoding="utf-8")

    monkeypatch.setattr(runner, "run_script", fake_run_script)
    train_runner.train(runner)

    # 1-get-text, 2-get-hubert, 3-get-semantic, s2_train_v3_lora (in V5_REPO) + s1_train (in OLD_REPO).
    assert [Path(c[2][0]).name for c in calls] == [
        "1-get-text.py", "2-get-hubert-wav32k.py", "3-get-semantic.py", "s2_train_v3_lora.py", "s1_train.py",
    ]
    assert all(c[0] == v5_repo and c[1] == v5_py for c in calls[:4])
    assert calls[4][0] == old_repo and calls[4][1] == old_py
    assert calls[3][3]["version"] == "v5turbo" and calls[4][3]["version"] == "v5turbo"

    s2_cfg = json.loads((run_dir / "s2_config.json").read_text(encoding="utf-8"))
    assert s2_cfg["model"]["version"] == "v5turbo"
    assert s2_cfg["train"]["lora_rank"] == "64"
    assert s2_cfg["train"]["pretrained_s2G"].endswith("gsv-v5-pretrained/s2Gv5turbo.pth")


def test_cuda_lib_dirs_follows_pth_for_lightweight_v5_venv(tmp_path, monkeypatch):
    """osutil.cuda_lib_dirs discovers nvidia/*/lib inside main venv via .pth in lightweight V5 venv."""
    monkeypatch.setattr(osutil, "IS_WINDOWS", False)
    main_site = tmp_path / "main-venv" / "lib" / "python3.10" / "site-packages"
    cublas = main_site / "nvidia" / "cublas" / "lib"
    cublas.mkdir(parents=True)
    v5_venv = tmp_path / "v5-venv"
    v5_site = v5_venv / "lib" / "python3.10" / "site-packages"
    v5_site.mkdir(parents=True)
    (v5_site / "zz_main_venv.pth").write_text(f"{main_site}\n", encoding="utf-8")
    v5_py = v5_venv / "bin" / "python"
    assert osutil.cuda_lib_dirs(v5_py) == [str(cublas)]


def test_vm_setup_v5_code_and_env_isolated(tmp_path):
    """Isolated test of colab/vm_setup.py: checks out GSV_V5_COMMIT when engines includes v5turbo,
    passes GSV_V5_ROOT to install_linux.sh, and skips GSV_V5 when engines omits V5."""
    def git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    # Build minimal fake upstream repo and bundles.
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "-q", "-b", "main", cwd=upstream)
    (upstream / "v5.txt").write_text("v5", encoding="utf-8")
    git("add", "v5.txt", cwd=upstream)
    git("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "v5", cwd=upstream)
    v5_sha = git("rev-parse", "HEAD", cwd=upstream)

    (upstream / "f.txt").write_text("base\n", encoding="utf-8")
    git("add", "f.txt", cwd=upstream)
    git("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "base", cwd=upstream)
    gsv_sha = git("rev-parse", "HEAD", cwd=upstream)
    (upstream / "f.txt").write_text("patched\n", encoding="utf-8")
    patch = git("diff", cwd=upstream) + "\n"
    patch_sha = hashlib.sha256(patch.encode()).hexdigest()

    api_src = tmp_path / "api-src"
    (api_src / "scripts").mkdir(parents=True)
    env_dump = tmp_path / "install_env.json"
    (api_src / "scripts" / "install_linux.sh").write_text(
        f"#!/usr/bin/env bash\n{sys.executable} -c 'import json,os; "
        f"open({json.dumps(str(env_dump))},\"w\").write(json.dumps({{k:os.environ.get(k) for k in "
        f"(\"GSV_ROOT\",\"GSV_V5_ROOT\",\"ENGINES\",\"GSV_V5_COMMIT\")}}))'\n",
        encoding="utf-8",
    )
    git("init", "-q", "-b", "core", cwd=api_src)
    git("add", ".", cwd=api_src)
    git("-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "api", cwd=api_src)
    api_sha = git("rev-parse", "HEAD", cwd=api_src)

    root = tmp_path / "vm"
    job = root / "content" / "job"
    inputs = root / "content" / "inputs"
    (inputs / "code").mkdir(parents=True)
    job.mkdir(parents=True)
    (inputs / "code" / "gsv.patch").write_text(patch, encoding="utf-8")
    git("bundle", "create", str(inputs / "code" / "api.bundle"), "HEAD", cwd=api_src)

    # Copy vm_lib.py & vm_setup.py rewritten to root/content and local upstream path.
    for name in ("vm_lib.py", "vm_setup.py"):
        text = (settings.ROOT / "colab" / name).read_text(encoding="utf-8")
        text = text.replace("/content", f"{root}/content")
        text = text.replace("https://github.com/RVC-Boss/GPT-SoVITS.git", str(upstream))
        (job / name).write_text(text, encoding="utf-8")
    # Fake uv in PATH so `uv pip freeze` succeeds without touching network.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uv").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    (fake_bin / "uv").chmod(0o755)
    spec = dict(
        inputs=str(inputs), tz="<+0900>-09:00", engines=["v2pro", "v5turbo"],
        gsv_patch="code/gsv.patch", gsv_patch_sha256=patch_sha, gsv_branch="tts-studio", gsv_base=gsv_sha,
        ttsapi_bundle="code/api.bundle", ttsapi_commit=api_sha, gsv_v5_commit=v5_sha,
    )
    (job / "job.json").write_text(json.dumps(spec), encoding="utf-8")
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}", STEPS="code,env")
    res = subprocess.run([sys.executable, str(job / "vm_setup.py")], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr + res.stdout
    assert (root / "content" / "GPTSoVits-v5" / "v5.txt").read_text(encoding="utf-8") == "v5"
    dumped = json.loads(env_dump.read_text(encoding="utf-8"))
    assert dumped["GSV_V5_ROOT"] == str(root / "content" / "GPTSoVits-v5")
    assert dumped["ENGINES"] == "v2pro,v5turbo"
    assert dumped["GSV_V5_COMMIT"] == v5_sha

    gsv = root / "content" / "GPTSoVits"
    assert git("rev-parse", "HEAD^", cwd=gsv) == gsv_sha
    first_head = git("rev-parse", "HEAD", cwd=gsv)
    assert git("branch", "--show-current", cwd=gsv) == "tts-studio"
    assert (gsv / "f.txt").read_text() == "patched\n"
    assert (gsv / ".tts-studio-patch.sha256").read_text().strip() == patch_sha

    # Simulate reassignment of the same VM. Removing the upstream proves objects are reused.
    shutil.rmtree(upstream)
    shutil.rmtree(root / "content" / "tts-api")
    shutil.rmtree(root / "content" / "GPTSoVits-v5")
    spec["engines"] = ["v2pro"]
    (job / "job.json").write_text(json.dumps(spec), encoding="utf-8")
    warm_env = dict(env, GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")
    warm = subprocess.run([sys.executable, str(job / "vm_setup.py")], env=warm_env, capture_output=True, text=True)
    assert warm.returncode == 0, warm.stderr + warm.stdout
    assert git("rev-parse", "HEAD", cwd=gsv) == first_head
    assert not (root / "content" / "GPTSoVits-v5").exists()

    # A matching marker alone is insufficient if setup was interrupted after resetting the branch.
    git("reset", "--hard", gsv_sha, cwd=gsv)
    shutil.rmtree(root / "content" / "tts-api")
    retry = subprocess.run([sys.executable, str(job / "vm_setup.py")], env=warm_env, capture_output=True, text=True)
    assert retry.returncode == 0, retry.stderr + retry.stdout
    assert git("rev-parse", "HEAD^", cwd=gsv) == gsv_sha
    assert (gsv / "f.txt").read_text() == "patched\n"

    (inputs / "code" / "gsv.patch").write_text("bad patch")
    bad = subprocess.run([sys.executable, str(job / "vm_setup.py")], env=env, capture_output=True, text=True)
    assert bad.returncode != 0 and "patch SHA-256 mismatch" in bad.stderr



def test_install_scripts_route_v5_pretrained_and_link_base(tmp_path):
    """install_macos.sh / install_linux.sh (isolated with local PRETRAINED dir and synthetic manifest):
    - ENGINES=v2pro creates nothing in GSV_V5_ROOT
    - ENGINES=v2pro,v5turbo places gsv-v5-pretrained/* in GSV_V5_ROOT (not GSV_ROOT) and links base models."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_name = "install_macos.sh" if sys.platform == "darwin" else "install_linux.sh"
    shutil.copy2(settings.ROOT / "scripts" / script_name, scripts_dir / script_name)
    shutil.copy2(settings.ROOT / "scripts" / "gptsovits.sh", scripts_dir / "gptsovits.sh")

    pre_src = tmp_path / "pre-src"
    files = {
        "base": [("GPT_SoVITS/pretrained_models/s1v3.ckpt", b"s1v3-bytes")],
        "v2pro": [("GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth", b"v2pro-bytes")],
        "v5turbo": [
            ("GPT_SoVITS/pretrained_models/gsv-v5-pretrained/s2Gv5turbo.pth", b"v5turbo-bytes"),
            ("GPT_SoVITS/pretrained_models/gsv-v5-pretrained/vocoder.pth", b"vocoder-bytes"),
        ],
    }
    manifest_lines = []
    for eng, items in files.items():
        for rel, data in items:
            p = pre_src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            manifest_lines.append(f"{eng} {hashlib.sha256(data).hexdigest()} {rel} https://example.invalid/{rel}")
    (scripts_dir / "pretrained.sha256").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    # 1. Without V5: GSV_V5_ROOT is untouched / not created.
    gsv1 = tmp_path / "run1" / "GPT-SoVITS"
    v5_1 = tmp_path / "run1" / "GPT-SoVITS-v5"
    (gsv1 / ".venv" / "bin").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(gsv1)], check=True)
    os.symlink(sys.executable, gsv1 / ".venv" / "bin" / "python")
    r1 = subprocess.run(
        ["bash", str(scripts_dir / script_name)],
        env=dict(os.environ, GSV_ROOT=str(gsv1), GSV_V5_ROOT=str(v5_1), ENGINES="v2pro",
                 PRETRAINED=str(pre_src), STEPS="pretrained", SKIP_CHECK="1"),
        capture_output=True, text=True,
    )
    assert r1.returncode == 0, r1.stderr + r1.stdout
    assert (gsv1 / "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth").is_file()
    assert not v5_1.exists()

    # 2. With v5turbo: V5 weights land in GSV_V5_ROOT (not GSV_ROOT), and base s1v3.ckpt is linked into GSV_V5_ROOT.
    gsv2 = tmp_path / "run2" / "GPT-SoVITS"
    v5_2 = tmp_path / "run2" / "GPT-SoVITS-v5"
    (gsv2 / ".venv" / "bin").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(gsv2)], check=True)
    os.symlink(sys.executable, gsv2 / ".venv" / "bin" / "python")
    r2 = subprocess.run(
        ["bash", str(scripts_dir / script_name)],
        env=dict(os.environ, GSV_ROOT=str(gsv2), GSV_V5_ROOT=str(v5_2), ENGINES="v2pro,v5turbo",
                 PRETRAINED=str(pre_src), STEPS="pretrained", GSV_V5_SKIP_CLONE="1", SKIP_CHECK="1"),
        capture_output=True, text=True,
    )
    assert r2.returncode == 0, r2.stderr + r2.stdout
    assert (v5_2 / "GPT_SoVITS/pretrained_models/gsv-v5-pretrained/s2Gv5turbo.pth").read_bytes() == b"v5turbo-bytes"
    assert (v5_2 / "GPT_SoVITS/pretrained_models/gsv-v5-pretrained/vocoder.pth").read_bytes() == b"vocoder-bytes"
    assert not (gsv2 / "GPT_SoVITS/pretrained_models/gsv-v5-pretrained/s2Gv5turbo.pth").exists()
    assert (v5_2 / "GPT_SoVITS/pretrained_models/s1v3.ckpt").read_bytes() == b"s1v3-bytes"


def test_device_install_script_v5_paths_unified():
    """device/install.sh passes POSIX sh syntax check and unifies GSV_V5 under $REL/GPT-SoVITS-v5."""
    script = settings.ROOT / "scripts" / "device" / "install.sh"
    res = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    text = script.read_text(encoding="utf-8")
    assert "GSV_V5=$REL/GPT-SoVITS-v5" in text
    assert "PY_V5=$GSV_V5/.venv/bin/python" in text
    assert "TTSAPI_GSV_V5_ROOT=$GSV_V5" in text
    assert "TTSAPI_V5_PYTHON=$PY_V5" in text
    assert "TTSAPI_GSV_V5_ROOT=$ROOT/GPT-SoVITS-v5" not in text
    assert "GSV_V5_ROOT=$GSV_V5" in text
    assert "$OLD/GPT-SoVITS-v5" in text
