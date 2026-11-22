"""本机模拟 Colab：上传到临时目录，从本地上游仓库取基点、应用补丁，
运行假 gateway 执行作业，再将结果 tar 导入控制端资料库。跳过模型和环境安装。"""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from ttsapi import db, library, settings, synth
from ttsapi.catalog import Catalog
from ttsapi.compute import colab_jobs, colab_sweep
from ttsapi.compute.colab import Account, RunState
from ttsapi.compute.colab.pool import RATES
from ttsapi.jobs import JobRun
from ttsapi.library import Library, sha256_of
from ttsapi.models import CandidatesParams, CompareParams, EvalRequest, Job, SweepRequest, SweepResult, TrainRequest
from ttsapi.refs import RefLibrary

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FOLDER = "sp0001_alice"
DATASET = "SAMPLE_DS.sp0001"
WINDOWS_AUDIO = r"C:\datasets\SAMPLE_DS\characters\sp0001_alice\audio"
CLIPS = (("14000140", 6.1, "明日の朝は、公園で散歩しようと思います。"), ("14200770", 4.0, "みなさん、おはよう"))


class LocalVM:
    """ColabRun 的替身：VM 的 / 是 root；上传到 /content/job/ 的脚本和 job.json 里的 /content 换成 root/content。"""
    name, account_id = "local-vm", "sim"

    def __init__(self, root: Path, upstream: Path):
        self.upstream = upstream.as_uri()
        self.root, self.rc, self.stopped, self.pending_result, self.downloads = root, None, False, None, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    def local(self, remote: str) -> Path:
        return self.root / remote.lstrip("/")

    def upload(self, local, remote):
        dst = self.local(remote)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if remote.startswith("/content/job/"):
            source = Path(local).read_text(encoding="utf-8")
            source = source.replace("/content", f"{self.root}/content")
            source = source.replace("https://github.com/RVC-Boss/GPT-SoVITS.git", self.upstream)
            dst.write_text(source, encoding="utf-8")
        else:
            shutil.copyfile(local, dst)

    def run_background(self, cmd, marker, heartbeat=None):
        # VM 上没有控制端的 TTSAPI_* 环境变量，且不安装运行环境。
        env = dict(PATH=os.environ["PATH"], HOME=os.environ["HOME"], STEPS="code,library",
                   GIT_TERMINAL_PROMPT="0", PYTHONDONTWRITEBYTECODE="1")
        with open(self.local(marker + ".log"), "ab") as log:
            self.rc = subprocess.run(["bash", "-c", cmd.replace("/content", f"{self.root}/content")], env=env,
                                     stdout=log, stderr=subprocess.STDOUT, timeout=120).returncode

    def wait(self):
        return RunState.DONE if self.rc == 0 else RunState.FAILED

    poll = wait

    def download(self, remote, local, timeout=180):
        self.downloads.append(remote)
        shutil.copyfile(self.local(remote), local)

    def stop(self):
        self.stopped = True


class Pool:
    """假账号池：开出的总是 vm；账本 usage 里记一行（扫参按它算这台花了多少）。"""

    def __init__(self, vm):
        self.vm = vm
        self.path = Path(tempfile.mkdtemp(prefix="ttscore-pool-")) / "usage.sqlite"
        with self.db() as db:
            db.execute("CREATE TABLE usage(name TEXT, gpu TEXT, started REAL, stopped REAL, actual_units REAL)")

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn

    def clock(self):
        return time.time()

    def accounts(self, refresh=True, include_disabled=False):
        return [Account(id="sim", label="sim", balance=100.0, month_used=None, gpus=("T4",), recorded_month_used=0.0)]

    def rates(self):
        return dict(RATES)

    def idle_vms(self):
        return {}

    def start(self, gpu, max_minutes=None, *, task_id, estimated_minutes, approved):
        with self.db() as db:
            db.execute("INSERT INTO usage VALUES (?,?,?,?,?)", (self.vm.name, gpu, self.clock(), None, None))
        return self.vm


def git(repo: Path, *args: str):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, message: str):
    git(repo, "add", ".")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def control(tmp_path, monkeypatch):
    """控制端资料库和运行代码步骤所需的本地 Git 仓库。"""
    folder = settings.DATASET_ROOT / "SAMPLE_DS" / "characters" / FOLDER
    for name, sec, _ in CLIPS:
        (folder / "audio").mkdir(parents=True, exist_ok=True)
        sf.write(folder / "audio" / f"{name}.wav", np.zeros(int(16000 * sec), dtype="float32"), 16000)
    (folder / f"{FOLDER}.ja.list").write_text("".join(f"{WINDOWS_AUDIO}\\{n}.wav|carol|ja|{t}\n" for n, _, t in CLIPS),
                                              encoding="utf-8")
    lib = library.open_or_create(tmp_path / "library")
    library.use(lib)
    lib.path("datasets").mkdir()
    with tarfile.open(lib.path("datasets/SAMPLE_DS.tar"), "w") as tf:
        tf.add(folder, arcname=f"SAMPLE_DS/characters/{FOLDER}")
    tar = lib.path("datasets/SAMPLE_DS.tar")  # 登记带 sha256、大小（真实情况）：VM、控制端都走 tar 的解压缓存
    lib.put("datasets/SAMPLE_DS.json", dict(id="SAMPLE_DS", name="SAMPLE_DS", tar="SAMPLE_DS.tar",
                                             sha256=sha256_of(tar), size=tar.stat().st_size,
                                             created="2026-11-20T00:00:00+09:00",
                                             characters={FOLDER: dict(id=DATASET, list=f"{FOLDER}.ja.list")}))
    cat = Catalog()
    cat.add_character("carol", "builtin", DATASET)
    refs = RefLibrary(cat)
    refs.register("carol-14000140")
    lib.put("characters/carol.json", dict(cat.character("carol"), default_ref="carol-14000140"))
    for kind, name in (("gpt", "carol-e15.ckpt"), ("sovits", "carol_e8_s144.pth")):
        (tmp_path / name).write_bytes(name.encode() * 10)
        cat.add_weight("carol", "v2pro", kind, tmp_path / name, "july")
    refs.reload()
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q")
    engine_source = upstream / "engine.py"
    engine_source.write_text("PATCHED = False\n")
    base_sha = commit(upstream, "engine base")
    engine_source.write_text("PATCHED = True\n")
    patch_text = git(upstream, "diff") + "\n"
    git(upstream, "restore", "engine.py")
    (upstream / "v5.py").write_text("VERSION = 5\n")
    v5_sha = commit(upstream, "v5")

    core_src = tmp_path / "core-source"
    core_src.mkdir()
    git(core_src, "init", "-q")
    (core_src / "README.md").write_text("Simulated Core")
    core_sha = commit(core_src, "core")

    vm = LocalVM(tmp_path / "vm", upstream)
    gateway = vm.local("/content/GPTSoVits/.venv/bin/python")
    gateway.parent.mkdir(parents=True)
    gateway.write_text(f"#!/bin/sh\nPYTHONPATH={ROOT} exec {sys.executable} {HERE / 'fake_gateway.py'}\n")
    gateway.chmod(0o755)

    def bundle_code(folder):
        bundle = folder / "tts-core-test.bundle"
        patch = folder / "gsv-test.patch"
        git(core_src, "bundle", "create", str(bundle), "HEAD")
        patch.write_text(patch_text)
        return ({f"code/{p.name}": p for p in (bundle, patch)},
                dict(ttsapi_bundle=f"code/{bundle.name}", ttsapi_commit=core_sha,
                     gsv_patch=f"code/{patch.name}", gsv_patch_sha256=sha256_of(patch),
                     gsv_base=base_sha, gsv_branch="tts-studio", gsv_v5_commit=v5_sha), None)
    monkeypatch.setattr(colab_jobs, "bundle_code", bundle_code)
    yield lib, cat, refs, vm
    library.use(None)


def colab(kind, target, req, cat, refs, vm, sweep=None, **kw):
    settings.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job = Job(id=f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{target[:4]}", kind=kind, target=target, status="running",
              progress=0, stage="", created="2026-11-20T00:00:00")
    run = JobRun(job, time.time())
    if sweep:
        sreq, groups = sweep
        run.job.sweep_result = SweepResult(groups=[g for g, _ in groups])
        pending = [g for g, _ in groups if g.status == "pending"]
        chosen = dict(gpu="T4", group_sec=[900] * len(pending), cold_prep_sec=360, units=10.0, budget=10.0)  # 提交时选中的 T4：每组估 15 分钟
        return run, asyncio.run(colab_sweep.run_sweep(
            run, groups, chosen, approved=False, pool=Pool(vm), catalog=cat, refs=refs,
            keep=sreq.keep, samples=sreq.samples, into_builtin=sreq.into_builtin))
    return run, asyncio.run(colab_jobs.run_colab(run, kind, req, gpu="T4", estimated_minutes=30, approved=False,
                                                 pool=Pool(vm), catalog=cat, refs=refs, **kw))


def vm_library(vm) -> dict:
    return json.loads(vm.local("/content/library/library.json").read_text(encoding="utf-8"))


def test_train_new_character(control):
    """新角色在内置角色的数据集上训练：VM 上数据集解开、默认参考沿用 carol 的；run、权重、训后评测记在控制端任务名下。"""
    lib, cat, refs, vm = control
    run, out = colab("train", "carol-colab", TrainRequest(slug="carol-colab", dataset_id=DATASET, engine="v2proplus"),
                     cat, refs, vm)
    (imp,) = out["imported"]
    name = imp["run"]
    assert imp["character"] == "carol-colab" and out["evals"] == 2 and out["jobs"] == []
    assert cat.run(name)["job_id"] == run.job.id and cat.builtin_of("carol-colab") == "carol"
    assert lib.path(f"weights/carol-colab/v2proplus/{name}-e10/gpt.ckpt").exists()
    (ev,) = lib.docs("evals")
    assert ev["job_id"] == run.job.id and ev["ref_id"] == "carol-14000140"
    tr = run.job.train_result
    assert tr.run_name == name and tr.eval.job_id == run.job.id and tr.eval.ref_id == "carol-14000140"
    assert cat.defaults("v2proplus", "carol-colab") == (tr.eval.best.gpt, tr.eval.best.sovits)
    # train.list 跟着 run 过来，参考库能从它派生（模拟里 VM 的路径在本机也存在，所以不换算成控制端的数据集路径）
    line = lib.path(f"runs/{name}/train.list").read_text(encoding="utf-8").splitlines()[0]
    assert line.split("|")[0].endswith(f"SAMPLE_DS/characters/{FOLDER}/audio/14000140.wav")
    refs.reload()
    assert "carol-colab-14000140" in refs.index
    # VM：资料库归 VM（认领过），新 id；数据集按正式逻辑从 tar 解到 VM 的解压缓存，结果里不带 tar
    info = vm_library(vm)
    assert info["owner"]["name"] == f"colab-{run.job.id}" and info["id"] != lib.info["id"]
    assert list(vm.local("/content/GPTSoVits/datasets/.unpacked").glob(f"{info['id']}-*/SAMPLE_DS/characters"))
    assert line.split("|")[0].startswith(str(vm.local("/content/GPTSoVits/datasets/.unpacked")))
    work = settings.DATA / "colab" / run.job.id
    assert vm.stopped and not (work / "results.tar").exists()
    with tarfile.open(vm.local(next(d for d in vm.downloads if "results" in d))) as tf:
        assert not [n for n in tf.getnames() if n.endswith(".tar")] and "library/datasets/SAMPLE_DS.json" in tf.getnames()
    log = run.log_path.read_text(encoding="utf-8")  # VM 的任务日志接在后面
    assert log.index("云端任务日志") < log.index('"slug": "carol-colab"') and '"compute": "local"' in log


def test_eval_library_weights(control):
    """评测控制端资料库里的 7 月权重：权重和参考传到 VM，评测导回来，eval_result 填好；输入的权重不再下载回来。"""
    lib, cat, refs, vm = control
    req = EvalRequest(engine="v2pro", gpt="carol-e15.ckpt", sovits="carol_e8_s144.pth")
    run, out = colab("eval", "carol", req, cat, refs, vm, character="carol", ref_id="carol-14200770")
    assert out["imported"] == [] and out["evals"] == 1
    (ev,) = lib.docs("evals")
    assert ev["job_id"] == run.job.id and ev["character"] == "carol" and ev["ref_id"] == "carol-14200770"
    assert run.job.eval_result.best.score_median == 93.5 and run.job.eval_result.job_id == run.job.id
    assert not vm.local("/content/library/weights/carol/v2pro/carol-e15/gpt.ckpt").exists()
    assert vm.local("/content/library/weights/carol/v2pro/carol-e15/meta.json").exists()


def test_failed_task_keeps_logs(control):
    lib, cat, refs, vm = control
    with pytest.raises(RuntimeError, match="boom"):
        colab("train", "bad-x", TrainRequest(slug="bad-x", dataset_id=DATASET), cat, refs, vm)
    (work,) = (settings.DATA / "colab").glob("train-*-bad-*")
    (row,) = json.loads((work / "job" / "summary.json").read_text(encoding="utf-8"))
    assert row["status"] == "failed" and (work / "job" / "timeline.jsonl").exists()
    assert not (work / "results.tar").exists() and lib.docs("runs") == []
    assert Library(vm.local("/content/library"), vm.root / "idx", dict(id="x", name="x")).docs("runs") == []


def test_sweep_two_groups_keep_and_samples(control):
    """扫参两组：VM 上逐组跑 vm_run.py <k>、vm_pack.py <k>，每组只带回训后评测最好的组合（keep=1）的权重和样本，
    并到内置角色 carol；VM 上 summary.json 两组都在。"""
    lib, cat, refs, vm = control
    req = SweepRequest(characters=["carol"], engines=["v2proplus"], keep=1, samples=1,
                       trains=[dict(sovits_epochs=4, gpt_epochs=5), dict(sovits_epochs=8, gpt_epochs=10)])
    groups = colab_sweep.groups(req, cat)
    run, _ = colab("sweep", "carol", None, cat, refs, vm, sweep=(req, groups))
    assert [g.status for g in run.job.sweep_result.groups] == ["succeeded", "succeeded"]
    for g in run.job.sweep_result.groups:
        r = cat.run(g.run_name)
        assert r["character"] == "carol" and len(r["gpt"]) == 1 and len(r["sovits"]) == 1   # 2 个 GPT 只带回 1 个
        assert r["gpt"][0].endswith("-e10.ckpt") and sorted(g.kept) == sorted(r["gpt"] + r["sovits"])
        items = json.loads(lib.path(f"{g.samples}/items.json").read_text(encoding="utf-8"))
        assert len(items["items"]) == 5 and items["ref_id"] == "carol-14000140"
        assert len(list(lib.path(g.samples).rglob("*.wav"))) == 5
    rows = json.loads(vm.local("/content/job/summary.json").read_text(encoding="utf-8"))
    assert [(r["index"], r["status"]) for r in rows] == [(0, "succeeded"), (1, "succeeded")]


def test_candidates_cloud_sim_imports_audio_and_db(control):
    """End-to-end cloud candidates synthesis: exports refs (including dataset-derived aux ref) + weights without
    dataset tar, runs vm_run -> fake_gateway -> vm_pack, imports wavs into AUDIO_DIR and rows into control tts.db."""
    lib, cat, refs, vm = control
    req = CandidatesParams(
        text="おはよう、今日もいい天気だね。", character="carol", engine="v2pro",
        ref_id="carol-14000140", aux_ref_ids=["carol-14200770"], n=2, seed=42,
    )
    run, out = colab("candidates", "carol", req, cat, refs, vm, character="carol", ref_id="carol-14000140",
                     aux_ref_ids=["carol-14200770"], device_id="colab-T4")
    assert vm.local("/content/GPTSoVits/engine.py").read_text() == "PATCHED = True\n"
    assert vm.local("/content/tts-api/README.md").read_text() == "Simulated Core"
    assert out["audio"] == 2
    cr = run.job.candidates_result
    assert cr is not None and cr.engine == "v2pro" and cr.ref_id == "carol-14000140" and len(cr.candidates) == 2
    for item in cr.candidates:
        assert item.compute == "colab-T4"
        assert item.aux_ref_ids == ["carol-14200770"]
        wav = settings.AUDIO_DIR / f"{item.id}.wav"
        assert wav.is_file() and wav.read_bytes().startswith(b"RIFF")
        got = synth.get_audio(item.id)
        assert got.id == item.id and got.compute == "colab-T4" and got.text == req.text
        row = db.one("SELECT id, character, engine FROM audio WHERE id=?", item.id)
        assert row is not None and row["character"] == "carol" and row["engine"] == "v2pro"
    # Verify tar packed generated audio and excluded dataset tar and input weight binaries.
    with tarfile.open(vm.local(next(d for d in vm.downloads if "results" in d))) as tf:
        names = set(tf.getnames())
    assert not any(n.endswith(".tar") for n in names)
    assert all(f"data/audio/{c.id}.wav" in names for c in cr.candidates)
    assert "library/weights/carol/v2pro/carol-e15/gpt.ckpt" not in names


def test_compare_cloud_sim_with_v5turbo_passes_v5_env(control):
    """End-to-end cloud compare across v2pro + v5turbo: vm_run passes TTSAPI_GSV_V5_ROOT & TTSAPI_V5_PYTHON,
    all audio items for both engines are packed and imported into AUDIO_DIR and control tts.db."""
    lib, cat, refs, vm = control
    req = CompareParams(
        text="テスト比較のセリフです。", character="carol", engines=["v2pro", "v5turbo"],
        ref_id="carol-14200770", n=2, seed=77,
    )
    run, out = colab("compare", "carol", req, cat, refs, vm, character="carol", ref_id="carol-14200770",
                     aux_ref_ids=[], device_id="colab-L4")
    assert out["audio"] == 4
    cmp = run.job.compare_result
    assert cmp is not None and cmp.ref_id == "carol-14200770" and [r.engine for r in cmp.results] == ["v2pro", "v5turbo"]
    for part in cmp.results:
        assert len(part.candidates) == 2
        for item in part.candidates:
            assert item.compute == "colab-L4"
            assert (settings.AUDIO_DIR / f"{item.id}.wav").is_file()
            assert synth.get_audio(item.id).engine == part.engine
    log_text = run.log_path.read_text(encoding="utf-8")
    assert "GPTSoVits-v5" in log_text and "v5_python" in log_text


def test_candidates_cloud_sim_failed_midway_packs_and_imports_completed(control):
    """End-to-end colab_sim: when a cloud candidates job fails after completing 1 item, vm_pack packs the
    completed WAV, run_colab imports it into AUDIO_DIR + tts.db and populates job.completed, then raises the error."""
    lib, cat, refs, vm = control
    req = CandidatesParams(
        text="FAIL_AFTER_ONE", character="carol", engine="v2pro", ref_id="carol-14000140", n=2, seed=55,
    )
    settings.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job = Job(id=f"cand-fail-{time.strftime('%Y%m%d-%H%M%S')}", kind="candidates", target="carol", status="running",
              progress=0, stage="", created="2026-11-20T00:00:00")
    run = JobRun(job, time.time())
    with pytest.raises(RuntimeError, match="midway boom"):
        asyncio.run(colab_jobs.run_colab(
            run, "candidates", req, gpu="T4", estimated_minutes=15, approved=False,
            pool=Pool(vm), catalog=cat, refs=refs, character="carol", ref_id="carol-14000140",
            aux_ref_ids=[], device_id="colab-T4",
        ))
    assert run.job.candidates_result is None
    assert len(run.job.completed) == 1
    aid = run.job.completed[0].audio_id
    assert run.job.completed[0].audio_url == f"/audio/{aid}"
    assert (settings.AUDIO_DIR / f"{aid}.wav").is_file()
    assert synth.get_audio(aid).compute == "colab-T4"
    with tarfile.open(vm.local(next(d for d in vm.downloads if "results" in d))) as tf:
        assert f"data/audio/{aid}.wav" in tf.getnames()
