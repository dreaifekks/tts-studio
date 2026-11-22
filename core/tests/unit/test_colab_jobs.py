"""ttsapi/compute/colab_jobs.py：导出输入资料库、代码 bundle、等待云端 GPU、用假 VM 跑通 run_colab 并写进资料库。不联网。"""
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from conftest import CAROL_FOLDER, CAROL_LIST, seed_carol
from test_compute import cand, l4, mini
from test_dispatch import snapshot_of
from ttsapi import datasets, db, gateway, importer, jobs, library, power, settings, training, version
from ttsapi.catalog import Catalog
from ttsapi.compute import colab_jobs, dispatch, registry
from ttsapi.compute.colab import RunState, WaitingForGPU
from ttsapi.compute.colab.cli import CliError
from ttsapi.jobs import JobRun
from ttsapi.library import Library, sha256_of
from ttsapi.models import ComboEval, EvalRequest, Job, TrainEval, TrainRequest, TrainResult
from ttsapi.refs import RefLibrary

DATASET = "SAMPLE_DS.sp0001"
VM_AUDIO = "/content/GPTSoVits/datasets/SAMPLE_DS/characters/sp0001_alice/audio"


@pytest.fixture
def carol(lib, tmp_path):
    """控制端资料库：内置角色 carol（数据集 + tar，默认参考登记在资料库里）、7 月 v2pro 权重。"""
    folder = seed_carol(lib)
    tar = lib.path("datasets/SAMPLE_DS.tar")
    with tarfile.open(tar, "w") as tf:
        tf.add(folder, arcname=f"SAMPLE_DS/characters/{folder.name}")
    lib.put("datasets/SAMPLE_DS.json", dict(lib.get("datasets/SAMPLE_DS.json"), sha256=sha256_of(tar),
                                             size=tar.stat().st_size))
    cat = Catalog()
    refs = RefLibrary(cat)
    refs.register("carol-14000140")
    lib.put("characters/carol.json", dict(cat.character("carol"), default_ref="carol-14000140"))
    for kind, name in (("gpt", "carol-e15.ckpt"), ("sovits", "carol_e8_s144.pth")):
        (tmp_path / name).write_bytes(name.encode() * 10)
        cat.add_weight("carol", "v2pro", kind, tmp_path / name, "july")
    refs.reload()
    return lib, cat, refs


def materialize(inputs: dict, root: Path) -> Library:
    for rel, src in inputs.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, root / rel)
    return Library(root / "library", root / "index", dict(id="vm", name="vm"))


def test_export_train_inputs(carol, tmp_path):
    lib, cat, refs = carol
    req = TrainRequest(slug="carol-colab", dataset_id=DATASET, engine="v2proplus")
    inputs = colab_jobs.export_inputs("train", [req], cat, refs, tmp_path)
    assert sorted(inputs) == ["library/characters/carol.json", "library/datasets/SAMPLE_DS.json",
                              "library/datasets/SAMPLE_DS.tar", "library/library.json",
                              "library/refs/carol/carol-14000140.json", "library/refs/carol/carol-14000140.wav"]
    assert inputs["library/datasets/SAMPLE_DS.tar"] == lib.path("datasets/SAMPLE_DS.tar")  # 原文件，不复制
    vm = materialize(inputs, tmp_path / "vm")
    assert vm.info["id"] != lib.info["id"] and vm.info["exported_from"] == lib.info["id"] and not vm.owned
    assert vm.counts() == dict(characters=1, refs=1, datasets=1, weights=0, evals=0, blind=0, works=0, runs=0,
                               bench=0)


def test_export_uploads_only_used_characters(carol, tmp_path):
    """数据集里还有别的角色：上传的 tar 只含用到的角色，记录跟着换（角色、sha256、大小），VM 上按它核对、解压；
    资料库里的原 tar 不动。估上传量也只算用到的角色（本机解压目录里它的文件夹）。"""
    lib, cat, refs = carol
    other = tmp_path / "sp0002_bob"
    (other / "audio").mkdir(parents=True)
    (other / "audio" / "1.wav").write_bytes(b"x" * 300_000)
    (other / "sp0002.ja.list").write_text("audio/1.wav|alice|ja|あ\n", encoding="utf-8")
    tar = lib.path("datasets/SAMPLE_DS.tar")
    with tarfile.open(tar, "a") as tf:
        tf.add(other, arcname=f"SAMPLE_DS/characters/{other.name}")
    doc = lib.get("datasets/SAMPLE_DS.json")
    doc["characters"][other.name] = dict(id="SAMPLE_DS.sp0002", list="sp0002.ja.list")
    lib.put("datasets/SAMPLE_DS.json", dict(doc, sha256=sha256_of(tar), size=tar.stat().st_size))
    before = sha256_of(tar)
    req = TrainRequest(slug="carol-colab", dataset_id=DATASET, engine="v2proplus")
    (tmp_path / "out").mkdir()
    inputs = colab_jobs.export_inputs("train", [req], cat, refs, tmp_path / "out")
    sub = inputs["library/datasets/SAMPLE_DS.tar"]
    assert sub != tar and sha256_of(tar) == before
    with tarfile.open(sub) as tf:
        names = tf.getnames()
    assert f"SAMPLE_DS/characters/{CAROL_FOLDER}/{CAROL_LIST}" in names
    assert all(n.startswith(f"SAMPLE_DS/characters/{CAROL_FOLDER}") for n in names)
    vm = materialize(inputs, tmp_path / "vm")
    vdoc = vm.get("datasets/SAMPLE_DS.json")
    assert list(vdoc["characters"]) == [CAROL_FOLDER] and vdoc["size"] == sub.stat().st_size
    root = datasets.local_root(vdoc, vm)  # VM 上按新记录核对大小和 sha256、解压
    assert (root / "characters" / CAROL_FOLDER / CAROL_LIST).exists() and not (root / "characters" / other.name).exists()
    carol_bytes = sum(f.stat().st_size for f in (settings.DATASET_ROOT / "SAMPLE_DS" / "characters" / CAROL_FOLDER).rglob("*") if f.is_file())
    other_files = sum(f.stat().st_size for k, f in inputs.items() if k.startswith(("library/characters/", "library/refs/")))
    assert colab_jobs.upload_mb("train", [req], cat, refs) * 2**20 == carol_bytes + other_files  # 不算另一个角色的 300KB
    # 用到全部角色时直接用原 tar（不另打）
    lib.put("datasets/SAMPLE_DS.json", dict(lib.get("datasets/SAMPLE_DS.json"),
                                             characters={CAROL_FOLDER: doc["characters"][CAROL_FOLDER]}))
    (tmp_path / "out2").mkdir()
    assert colab_jobs.export_inputs("train", [req], cat, refs, tmp_path / "out2")["library/datasets/SAMPLE_DS.tar"] == tar


def test_export_eval_inputs(carol, tmp_path):
    """评测：要评的权重（7 月权重在资料库里）、角色、参考（数据集派生的候选：带角色的数据集）都在。"""
    lib, cat, refs = carol
    req = EvalRequest(engine="v2pro", gpt="carol-e15.ckpt", sovits="carol_e8_s144.pth")
    inputs = colab_jobs.export_inputs("eval", [req], cat, refs, tmp_path, character="carol", ref_id="carol-14200080")
    assert {"library/weights/carol/v2pro/carol-e15/gpt.ckpt", "library/weights/carol/v2pro/carol-e15/meta.json",
            "library/weights/carol/v2pro/carol_e8_s144/sovits.pth", "library/datasets/SAMPLE_DS.tar",
            "library/characters/carol.json", "library/refs/carol/carol-14000140.json"} <= set(inputs)
    vm = materialize(inputs, tmp_path / "vm")
    assert vm.counts()["weights"] == 2 and vm.get("weights/carol/v2pro/carol-e15/meta.json")["source"] == "july"


def test_vm_scripts_use_control_timezone(tmp_path):
    """job.json 的 tz 是主控现在的时区；VM 上的脚本（vm_lib）一导入就按它记时间，起的 gateway 继承它（VM 本身是 UTC）。"""
    tz = colab_jobs.control_tz()
    spec = colab_jobs.job_spec("train-x", "train", TrainRequest(slug="carol-colab", dataset_id=DATASET), {})
    assert spec["tz"] == tz
    job = tmp_path / "content" / "job"
    job.mkdir(parents=True)
    src = settings.ROOT / "colab" / "vm_lib.py"
    (job / "vm_lib.py").write_text(src.read_text(encoding="utf-8").replace("/content", f"{tmp_path}/content"),
                                   encoding="utf-8")
    for want in (tz, "<+0530>-05:30"):
        (job / "job.json").write_text(json.dumps(dict(inputs="/x", tz=want)), encoding="utf-8")
        code = "import os, time, vm_lib; print(time.localtime().tm_gmtoff, os.environ['TZ'])"
        out = subprocess.run([sys.executable, "-c", code], cwd=job, env=dict(os.environ, TZ="UTC"), capture_output=True,
                             text=True, check=True).stdout.split()
        offset = datetime.now().astimezone().utcoffset().total_seconds() if want == tz else 5.5 * 3600
        assert (int(out[0]), out[1]) == (offset, want)


def test_bundle_code(tmp_path, monkeypatch):
    """tts-core 能 clone 出来：只有 core/ 的内容（放在根上），版本号和主控一样；GPT-SoVITS 传补丁和校验值，无需本机引擎仓库。"""
    monkeypatch.setattr(settings, "OLD_REPO", tmp_path / "does-not-exist")

    def git(*args, cwd=tmp_path):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    inputs, code, _ = colab_jobs.bundle_code(tmp_path)
    patch = colab_jobs.GSV_PATCH.read_bytes()
    digest = hashlib.sha256(patch).hexdigest()
    assert code == {
        "ttsapi_bundle": code["ttsapi_bundle"], "ttsapi_commit": code["ttsapi_commit"],
        "gsv_patch": f"code/gsv-{digest[:12]}.patch", "gsv_patch_sha256": digest,
        "gsv_base": colab_jobs.GSV_BASE, "gsv_branch": "tts-studio",
    }
    assert set(inputs) == {code["ttsapi_bundle"], code["gsv_patch"]}
    assert inputs[code["gsv_patch"]].read_bytes() == patch
    git("clone", "-q", str(inputs[code["ttsapi_bundle"]]), str(tmp_path / "api"))
    api = tmp_path / "api"
    assert git("rev-parse", "HEAD", cwd=api) == code["ttsapi_commit"]
    core_tree = git("ls-tree", "--full-tree", "--name-only", "HEAD:./", cwd=settings.ROOT).split()
    assert git("ls-tree", "--name-only", "HEAD", cwd=api).split() == core_tree and "ttsapi" in core_tree
    assert (api / "ttsapi" / "version.py").is_file() and not (api / "core").exists()
    out = subprocess.run([sys.executable, "-c", "from ttsapi import version; print(version.VERSION)"], cwd=api,
                         env=dict(os.environ, PYTHONPATH=str(api)), capture_output=True, text=True, check=True).stdout
    assert out.strip() == version.describe()[0]
    assert not (tmp_path / "tts-core.git").exists()


# ---------------------------------------------------------------- 假 VM 跑通 run_colab

class FakeVM:
    """假 ColabRun：上传的文件放进 root（VM 的 /），download 结果 tar 时调用 produce(task) 在输入资料库上「跑」这个任务
    （像 VM 上的 gateway：认领资料库、写 run / 权重 / 评测），再按 colab/vm_pack.py 的布局打包。"""
    name, account_id = "vm-1", "acct"

    def __init__(self, root: Path, produce, state=RunState.DONE, fail_downloads=0):
        self.root, self.produce, self.state = root, produce, state
        self.uploads, self.commands, self.stopped = [], [], False
        self.fail_downloads, self.pending_result, self.kept = fail_downloads, None, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if not self.kept:
            self.stop()

    def keep_for_retry(self):
        self.kept += 1

    def upload(self, local, remote):
        self.uploads.append(remote)
        dst = self.root / remote.lstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, dst)

    def run_background(self, cmd, marker, heartbeat=None):
        self.commands.append(cmd)

    def wait(self):
        return self.state

    def download(self, remote, local, timeout=180):
        self.pending_result, self.kept = (remote, local), 0
        if self.fail_downloads:
            self.fail_downloads -= 1
            raise CliError("download timed out")
        self.pending_result = None
        if (self.root / "out").exists():
            shutil.rmtree(self.root / "out")
        spec = json.loads((self.root / "content/job/job.json").read_text(encoding="utf-8"))
        (task,) = spec["tasks"]
        vmlib = Library(self.root / spec["inputs"].lstrip("/") / "library", self.root / "index",
                        dict(id="vm", name="colab-vm"))
        vmlib.claim()
        mine = library.selected()
        library.use(vmlib)
        try:
            record = self.produce(task, Catalog(), self.root)
        finally:
            library.use(mine)
        out = self.root / "out"
        (out / "data" / "jobs").mkdir(parents=True)
        conn = sqlite3.connect(out / "data" / "tts.db")
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO jobs VALUES(?,?,?)", (task["job_id"], time.time(), json.dumps(record)))
        conn.commit()
        conn.close()
        (out / "data" / "jobs" / f"{task['job_id']}.log").write_text("[12:00:01] vm step 1\n[12:00:02] vm step 2\n", encoding="utf-8")
        (out / "job").mkdir()
        (out / "job" / "summary.json").write_text(json.dumps([dict(
            task="task00", job=task["job_id"], status=record["status"], error=record.get("error"))]))
        with tarfile.open(local, "w") as tf:
            tf.add(vmlib.root, "library")
            for name in ("data", "job"):
                tf.add(out / name, name)

    def stop(self):
        self.stopped = True


class FakePool:
    def __init__(self, vm, busy=0):
        self.vm, self.busy, self.calls = vm, busy, []

    def start(self, gpu, max_minutes=None, *, task_id, estimated_minutes, approved):
        self.calls.append((gpu, task_id, estimated_minutes, approved))
        if self.busy:
            self.busy -= 1
            e = WaitingForGPU(gpu)
            e.retry_after = 0
            raise e
        return self.vm


class RecordingRun(JobRun):
    def stage(self, stage, progress=None):
        self.history.append((self.job.status, stage))
        super().stage(stage, progress)


def job_run(kind, target):
    settings.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job = Job(id=f"{kind}-20261120-000000-{target[-4:]}", kind=kind, target=target, status="running", progress=0,
              stage="", created="2026-11-20T00:00:00")
    run = RecordingRun(job, time.time())
    run.history = []
    return run


def combo(gpt, sovits, score):
    return ComboEval(gpt=gpt, sovits=sovits, n=12, cer_median=0.0, spk_sim_median=0.9, score_median=score,
                     score_mean=score)


@pytest.fixture
def fake_code(monkeypatch):
    def bundle_code(folder):
        for n in ("a.bundle", "gsv-b.patch"):
            (folder / n).write_bytes(b"bundle")
        return ({"code/a.bundle": folder / "a.bundle", "code/gsv-b.patch": folder / "gsv-b.patch"},
                dict(ttsapi_bundle="code/a.bundle", ttsapi_commit="c" * 40, gsv_patch="code/gsv-b.patch",
                     gsv_patch_sha256="d" * 64, gsv_base="e" * 40, gsv_branch="tts-studio"), None)
    monkeypatch.setattr(colab_jobs, "bundle_code", bundle_code)


def train_on_vm(task, cat, root):
    """VM 上训练 + 训后评测：新角色 carol-colab 的 run（2 个 GPT、1 个 SoVITS），评测记在控制端任务 id 下。"""
    run_name = f"{task['slug']}-20261120-010203"
    files = dict(gpt=[], sovits=[])
    (root / "w").mkdir()
    for kind, n in (("gpt", f"{run_name}-e5.ckpt"), ("gpt", f"{run_name}-e10.ckpt"), ("sovits", f"{run_name}_e4_s8.pth")):
        (root / "w" / n).write_bytes(n.encode() * 50)
        files[kind].append(root / "w" / n)
    train = root / "training" / run_name
    train.mkdir(parents=True)
    (train / "train.list").write_text(f"{VM_AUDIO}/14000140.wav|{task['slug']}|ja|明日の朝は\n", encoding="utf-8")
    cat.register_run(task["slug"], task["dataset_id"], dict(
        run_name=run_name, engine=task["engine"], job_id=task["job_id"], best=None, filter=None, lines_used=1,
        minutes_used=0.1, params={}), files, train_dir=train)
    combos = [combo(g.name, files["sovits"][0].name, 90 + i) for i, g in enumerate(files["gpt"])]
    for c in combos:
        cat.record_eval(task["engine"], task["slug"], "carol-14000140", task["job_id"], "hard", c)
    cat.set_best(run_name, combos[1].gpt, combos[1].sovits)
    ev = TrainEval(job_id=task["job_id"], engine=task["engine"], character=task["slug"], ref_id="carol-14000140",
                   eval_set="hard", sentences=["s"], candidates_per_sentence=1, combos=combos[::-1], best=combos[1])
    result = TrainResult(slug=task["slug"], engine=task["engine"], run_name=run_name, lines_used=1, minutes_used=0.1,
                         gpt_weights=[f.name for f in files["gpt"]], sovits_weights=[files["sovits"][0].name], eval=ev)
    return dict(id=task["job_id"], kind="train", status="succeeded", train_result=result.model_dump())


def test_run_colab_train_waits_for_gpu_then_imports(carol, tmp_path, fake_code):
    lib, cat, refs = carol
    vm = FakeVM(tmp_path / "vm", train_on_vm)
    pool = FakePool(vm, busy=1)
    run = job_run("train", "carol-colab")
    req = TrainRequest(slug="carol-colab", dataset_id=DATASET, engine="v2proplus")
    out = asyncio.run(colab_jobs.run_colab(run, "train", req, gpu="T4", estimated_minutes=40, approved=False,
                                           pool=pool, catalog=cat, refs=refs))

    # 第一次开不出来：任务转为排队、阶段是等待云端 GPU；重试开出来后回到 running
    assert pool.calls == [("T4", run.job.id, 40, False)] * 2
    assert ("queued", "等待云端 GPU（T4 暂时开不出来）") in run.history
    assert run.history[-1][0] == "running" and run.job.status == "running" and vm.stopped
    # 上传：输入资料库、代码、VM 脚本、job.json；job.json 带控制端任务 id
    assert {"/content/job/vm_pack.py", "/content/job/job.json"} <= set(vm.uploads)
    assert any(u.endswith("/library/datasets/SAMPLE_DS.tar") for u in vm.uploads)
    assert [c.split()[-1] for c in vm.commands][:2] == ["vm_setup.py", "vm_run.py"]
    spec = json.loads((vm.root / "content/job/job.json").read_text())
    assert spec["tasks"][0]["job_id"] == run.job.id and spec["engines"] == ["v2proplus"]

    # 写进控制端资料库：新角色、run、权重、评测（记在这个任务名下），任务记录不重复导入
    run_name = "carol-colab-20261120-010203"
    assert [r["run"] for r in out["imported"]] == [run_name] and out["evals"] == 2 and out["jobs"] == []
    assert cat.run(run_name)["job_id"] == run.job.id and cat.character("carol-colab")["source"] == "user"
    assert lib.path(f"weights/carol-colab/v2proplus/{run_name}-e10/gpt.ckpt").exists()
    (ev,) = lib.docs("evals")
    assert ev["job_id"] == run.job.id and len(ev["combos"]) == 2
    assert cat.defaults("v2proplus", "carol-colab") == (f"{run_name}-e10.ckpt", f"{run_name}_e4_s8.pth")
    tr = run.job.train_result
    assert tr.run_name == run_name and tr.eval.best.gpt == f"{run_name}-e10.ckpt" and tr.eval.job_id == run.job.id
    # 作业目录：结果 tar 删掉，VM 的 job/ 留着；VM 任务日志接在任务日志后面
    work = settings.DATA / "colab" / run.job.id
    assert not (work / "results.tar").exists() and (work / "job" / "summary.json").exists()
    assert "vm step 2" in run.log_path.read_text(encoding="utf-8") and "vm step 2" in run.tail[-1]


def test_run_colab_eval_of_library_weights(carol, tmp_path, fake_code):
    """评测控制端资料库里已有的权重：VM 库里的评测记录不属于任何新 run，也导进来；eval_result 填好。"""
    lib, cat, refs = carol

    def eval_on_vm(task, cat, root):
        assert (task["character"], task["ref_id"]) == ("carol", "carol-14200080")
        c = combo(task["gpt"], task["sovits"], 93.5)
        cat.record_eval(task["engine"], task["character"], task["ref_id"], task["job_id"], task["eval_set"], c)
        ev = TrainEval(job_id=task["job_id"], engine=task["engine"], character="carol", ref_id=task["ref_id"],
                       eval_set=task["eval_set"], sentences=["s"], candidates_per_sentence=3, combos=[c], best=c)
        return dict(id=task["job_id"], kind="eval", status="succeeded", eval_result=ev.model_dump())

    vm = FakeVM(tmp_path / "vm", eval_on_vm)
    run = job_run("eval", "carol-eval")
    req = EvalRequest(engine="v2pro", gpt="carol-e15.ckpt", sovits="carol_e8_s144.pth")
    out = asyncio.run(colab_jobs.run_colab(run, "eval", req, gpu="L4", estimated_minutes=20, approved=False,
                                           pool=FakePool(vm), catalog=cat, refs=refs, character="carol",
                                           ref_id="carol-14200080"))
    assert out["imported"] == [] and out["evals"] == 1
    assert any(u.endswith("/library/weights/carol/v2pro/carol-e15/gpt.ckpt") for u in vm.uploads)
    (ev,) = lib.docs("evals")
    assert ev["job_id"] == run.job.id and ev["character"] == "carol" and ev["ref_id"] == "carol-14200080"
    assert cat.latest_evals("v2pro")["v2pro", "carol", "carol-e15.ckpt", "carol_e8_s144.pth", "hard"].score_median == 93.5
    assert run.job.eval_result.best.score_median == 93.5 and run.job.eval_result.job_id == run.job.id
    assert ("running", "云端评测") in run.history


def test_run_colab_max_and_keep_minutes(carol, tmp_path, fake_code):
    """设置里的「最长用多久」带进开卡；「做完先别关」：做完、VM 还在时留着（keep_idle），不释放；VM 没了照常释放。"""
    lib, cat, refs = carol

    def evaluated(task, cat, root):
        return dict(id=task["job_id"], kind="eval", status="succeeded")

    class Pool(FakePool):
        def start(self, gpu, max_minutes=None, **kw):
            self.max_minutes = max_minutes
            return super().start(gpu, **kw)

    req = EvalRequest(engine="v2pro", gpt="carol-e15.ckpt", sovits="carol_e8_s144.pth")
    kw = dict(gpu="L4", estimated_minutes=20, approved=False, catalog=cat, refs=refs, character="carol",
              ref_id="carol-14000140", max_minutes=90, keep_minutes=10)
    vm = FakeVM(tmp_path / "a", evaluated)
    vm.keep_idle = lambda minutes: setattr(vm, "kept_idle", minutes) or setattr(vm, "kept", 1) or True
    pool = Pool(vm)
    asyncio.run(colab_jobs.run_colab(job_run("eval", "keep"), "eval", req, pool=pool, **kw))
    assert pool.max_minutes == 90 and vm.kept_idle == 10 and not vm.stopped
    vm = FakeVM(tmp_path / "f", evaluated, state=RunState.FAILED)  # 失败的不留（VM 可能坏了）
    vm.keep_idle = lambda minutes: pytest.fail("failed VM kept")
    with pytest.raises(RuntimeError):
        asyncio.run(colab_jobs.run_colab(job_run("eval", "keepfail"), "eval", req, pool=Pool(vm), **kw))
    assert vm.stopped
    vm = FakeVM(tmp_path / "b", evaluated, state=RunState.GONE)
    vm.keep_idle = lambda minutes: pytest.fail("gone VM kept")
    with pytest.raises(RuntimeError, match="VM 中途没了"):
        asyncio.run(colab_jobs.run_colab(job_run("eval", "keepgone"), "eval", req, pool=Pool(vm), **kw))
    assert vm.stopped


def test_run_colab_failed_task_and_gone_vm(carol, tmp_path, fake_code):
    """VM 上的任务失败：取回的部分照样导入，然后按 VM 的错误报错；VM 中途没了：报错、VM 照样释放。"""
    lib, cat, refs = carol

    def failed(task, cat, root):
        return dict(id=task["job_id"], kind="eval", status="failed", error="CUDA out of memory")

    req = EvalRequest(engine="v2pro", gpt="carol-e15.ckpt", sovits="carol_e8_s144.pth")
    kw = dict(gpu="T4", estimated_minutes=20, approved=False, catalog=cat, refs=refs, character="carol",
              ref_id="carol-14000140")
    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        asyncio.run(colab_jobs.run_colab(job_run("eval", "fail"), "eval", req,
                                         pool=FakePool(FakeVM(tmp_path / "a", failed)), **kw))
    vm = FakeVM(tmp_path / "b", failed, state=RunState.GONE)
    with pytest.raises(RuntimeError, match="VM 中途没了"):
        asyncio.run(colab_jobs.run_colab(job_run("eval", "gone"), "eval", req, pool=FakePool(vm), **kw))
    assert vm.stopped and lib.docs("evals") == []
    with pytest.raises(ValueError, match="v3"):
        asyncio.run(colab_jobs.run_colab(job_run("eval", "eng3"), "eval", req.model_copy(update=dict(engine="v3")),
                                         pool=FakePool(vm), **kw))


def test_run_colab_retries_result_download(carol, tmp_path, fake_code, monkeypatch):
    """结果下载失败：VM 留着（keep_for_retry），过一会儿再取，取到了照常导入，最后释放 VM。"""
    lib, cat, refs = carol
    monkeypatch.setattr(colab_jobs, "RESULT_RETRY_SEC", 0)
    vm = FakeVM(tmp_path / "vm", train_on_vm, fail_downloads=2)
    run = job_run("train", "carol-retry")
    req = TrainRequest(slug="carol-colab", dataset_id=DATASET, engine="v2proplus")
    out = asyncio.run(colab_jobs.run_colab(run, "train", req, gpu="L4", estimated_minutes=40, approved=False,
                                           pool=FakePool(vm), catalog=cat, refs=refs))
    assert [s for _, s in run.history if s.startswith("取回结果失败")] == \
        ["取回结果失败，0 秒后重试（1/5）", "取回结果失败，0 秒后重试（2/5）"]
    assert out["imported"] and vm.stopped and vm.pending_result is None


def test_run_colab_gives_up_after_retries(carol, tmp_path, fake_code, monkeypatch):
    lib, cat, refs = carol
    monkeypatch.setattr(colab_jobs, "RESULT_RETRY_SEC", 0)
    vm = FakeVM(tmp_path / "vm", train_on_vm, fail_downloads=99)
    run = job_run("train", "carol-gives")
    req = TrainRequest(slug="carol-colab", dataset_id=DATASET, engine="v2proplus")
    with pytest.raises(CliError):
        asyncio.run(colab_jobs.run_colab(run, "train", req, gpu="L4", estimated_minutes=40, approved=False,
                                         pool=FakePool(vm), catalog=cat, refs=refs))
    assert vm.stopped and lib.docs("runs") == []


def test_cancel_while_starting_vm_stops_late_vm(lib, tmp_path):
    """开卡时取消：开卡线程停不下来，等它返回，迟到的 VM 接过来释放掉，任务才结束。"""
    vm = FakeVM(tmp_path / "vm", train_on_vm)
    entered, release = threading.Event(), threading.Event()

    class SlowPool(FakePool):
        def start(self, gpu, max_minutes=None, **kw):
            entered.set()
            assert release.wait(5)
            return super().start(gpu, **kw)

    async def go():
        task = asyncio.create_task(colab_jobs.start_vm(job_run("train", "slow"), SlowPool(vm), "T4", 40, False))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()  # 还在等开卡线程
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert vm.stopped


def test_cancel_while_writing_keeps_job_and_target_library(carol, tmp_path, fake_code, monkeypatch):
    """写库时取消：等写库线程做完任务才结束（之前一直在 jobs.runs 里，换库会被挡住）；写进的是任务开始时的资料库，
    即使主控中途换了库；结果 tar 留着，POST /runs/import 重试时按原任务 id 补上 train_result。"""
    lib, cat, refs = carol
    monkeypatch.setattr(power, "keep_awake", lambda on: None)
    entered, release = threading.Event(), threading.Event()
    real_import, written = importer.import_runs, []

    def slow_import(*a, **kw):
        entered.set()
        assert release.wait(5)
        written.append(real_import(*a, **kw))
        return written[-1]
    monkeypatch.setattr(importer, "import_runs", slow_import)
    other = library.open_or_create(tmp_path / "other")
    req = TrainRequest(slug="carol-colab", dataset_id=DATASET, engine="v2proplus")
    pool = FakePool(FakeVM(tmp_path / "vm", train_on_vm))

    async def go():
        job = jobs.start("train", "carol-colab", lambda run: colab_jobs.run_colab(
            run, "train", req, gpu="T4", estimated_minutes=40, approved=False, pool=pool, catalog=cat, refs=refs))
        await asyncio.to_thread(entered.wait, 5)
        cancel = asyncio.create_task(jobs.cancel(job.id))
        await asyncio.sleep(0.1)
        assert job.id in jobs.runs and job.status == "running"  # 写库线程还在跑：任务没结束
        library.use(other)  # 主控换了库（gateway 这时会 409，这里直接换）
        release.set()
        await cancel
        return job
    job = asyncio.run(go())
    run_name = "carol-colab-20261120-010203"
    assert job.status == "cancelled" and job.id not in jobs.runs and job.train_result is None
    assert [r["run"] for r in written[0]["imported"]] == [run_name]  # 写库线程做完了
    assert lib.has(f"runs/{run_name}/run.json") and other.docs("runs") == [] and other.docs("weights") == []
    tar = settings.DATA / "colab" / job.id / "results.tar"
    assert tar.exists()

    library.use(lib)
    out = importer.import_runs(tar, cat, refs)
    assert out["skipped"] == [dict(run=run_name, reason="资料库里已有")] and out["filled"] == [job.id]
    got = jobs.get(job.id)
    assert got.status == "cancelled" and got.train_result.run_name == run_name
    assert got.train_result.eval.best.gpt == f"{run_name}-e10.ckpt"


def test_run_colab_skipped_import_keeps_tar_and_retry_fills_job(carol, tmp_path, fake_code):
    """训练完了但导入被跳过（资料库里同名角色用的是别的数据集）：结果 tar 留着、任务不填 train_result 并报错；
    处理好后用 tar 重试导入，按原任务 id 补上 train_result。"""
    lib, cat, refs = carol
    cat.add_character("carol-colab", "user", "OTHER.x")
    run = job_run("train", "carol-skip")
    req = TrainRequest(slug="carol-colab", dataset_id=DATASET, engine="v2proplus")
    with pytest.raises(RuntimeError, match="没写进资料库.*数据集.*POST /runs/import"):
        asyncio.run(colab_jobs.run_colab(run, "train", req, gpu="T4", estimated_minutes=40, approved=False,
                                         pool=FakePool(FakeVM(tmp_path / "vm", train_on_vm)), catalog=cat, refs=refs))
    tar = settings.DATA / "colab" / run.job.id / "results.tar"
    assert tar.exists() and run.job.train_result is None
    run.job.status = "failed"  # jobs._run 收尾时存的状态
    run.save()

    lib.delete("characters/carol-colab.json")
    out = importer.import_runs(tar, cat, refs)
    run_name = "carol-colab-20261120-010203"
    assert [r["run"] for r in out["imported"]] == [run_name] and out["filled"] == [run.job.id]
    got = jobs.get(run.job.id)
    assert got.train_result.run_name == run_name and got.train_result.slug == "carol-colab"
    assert got.train_result.eval.best.gpt == f"{run_name}-e10.ckpt"


def test_export_eval_of_trained_weights_includes_run(carol, tmp_path):
    """评测训练出来的权重：目录只认已提交 run 的权重，所以 run.json 也要导出，VM 上才找得到。"""
    lib, cat, refs = carol
    run_name = "carol-v2pro-20261120-000000"
    files = {}
    for kind, name in (("gpt", f"{run_name}-e5.ckpt"), ("sovits", f"{run_name}_e4_s8.pth")):
        (tmp_path / name).write_bytes(name.encode() * 10)
        files[kind] = [tmp_path / name]
    cat.register_run("carol", DATASET, dict(run_name=run_name, engine="v2pro"), files)
    req = EvalRequest(engine="v2pro", gpt=f"{run_name}-e5.ckpt", sovits=f"{run_name}_e4_s8.pth")
    (tmp_path / "out").mkdir()
    inputs = colab_jobs.export_inputs("eval", [req], cat, refs, tmp_path / "out", character="carol",
                                      ref_id="carol-14000140")
    assert f"library/runs/{run_name}/run.json" in inputs
    vm = materialize(inputs, tmp_path / "vm")
    vm.claim()
    library.use(vm)
    try:
        assert Catalog().resolve("v2pro", "carol", req.gpt, req.sovits)[0] == req.gpt
    finally:
        library.use(lib)


def test_cancel_while_running_stops_vm_now(carol, tmp_path, fake_code, monkeypatch):
    """作业在 VM 上跑着时取消：控制协程马上释放 VM（不等作业线程跑完），线程随之退出，任务变成 cancelled。"""
    lib, cat, refs = carol
    monkeypatch.setattr(power, "keep_awake", lambda on: None)
    running, released = threading.Event(), threading.Event()

    class BusyVM(FakeVM):
        timed_out = False

        def wait(self):  # 作业一直在跑，直到 VM 被释放
            running.set()
            if not released.wait(5):
                self.timed_out = True
            raise RuntimeError("run is closed")

        def stop(self):
            super().stop()
            released.set()

    vm = BusyVM(tmp_path / "vm", train_on_vm)
    req = TrainRequest(slug="carol-colab", dataset_id=DATASET, engine="v2proplus")

    async def go():
        job = jobs.start("train", "carol-colab", lambda run: colab_jobs.run_colab(
            run, "train", req, gpu="T4", estimated_minutes=40, approved=False, pool=FakePool(vm), catalog=cat,
            refs=refs))
        await asyncio.to_thread(running.wait, 5)
        await jobs.cancel(job.id)
        return job
    job = asyncio.run(go())
    assert job.status == "cancelled" and vm.stopped and not vm.timed_out


def test_export_refuses_tar_not_matching_record(carol, tmp_path):
    """数据集 tar 被换成同样大小、内容不同的（资料库记录没变，本机已有旧内容的解压缓存）：开卡前导出输入时就挡下，
    不从它打子集上传，也不上传整个 tar。"""
    lib, cat, refs = carol
    tar = lib.path("datasets/SAMPLE_DS.tar")
    other = tmp_path / "sp0002_bob"
    (other / "audio").mkdir(parents=True)
    (other / "audio" / "1.wav").write_bytes(b"x" * 3000)
    (other / "sp0002.ja.list").write_text("audio/1.wav|alice|ja|あ\n", encoding="utf-8")
    with tarfile.open(tar, "a") as tf:
        tf.add(other, arcname=f"SAMPLE_DS/characters/{other.name}")
    doc = lib.get("datasets/SAMPLE_DS.json")
    doc["characters"][other.name] = dict(id="SAMPLE_DS.sp0002", list="sp0002.ja.list")
    lib.put("datasets/SAMPLE_DS.json", dict(doc, sha256=sha256_of(tar), size=tar.stat().st_size))
    datasets.local_root(lib.get("datasets/SAMPLE_DS.json"), lib)  # 本机解压缓存（旧内容）
    req = TrainRequest(slug="carol-colab", dataset_id=DATASET, engine="v2proplus")
    swapped = bytearray(tar.read_bytes())
    swapped[-1] = 1  # 大小不变，内容变了（tar 照样能读）
    tar.write_bytes(bytes(swapped))
    for kind, reqs, kw in (("train", [req], {}),                                       # 只用 carol：打子集
                           ("eval", [EvalRequest(engine="v2pro", gpt="carol-e15.ckpt", sovits="carol_e8_s144.pth")],
                            dict(character="carol", ref_id="carol-14200080"))):
        out = tmp_path / f"out-{kind}"
        out.mkdir()
        with pytest.raises(datasets.DatasetUnavailable):
            colab_jobs.export_inputs(kind, reqs, cat, refs, out, **kw)
        assert not (out / "datasets" / "SAMPLE_DS.tar").exists()
    # 只有 carol 一个角色的数据集（整个 tar 上传）也一样
    lib.put("datasets/SAMPLE_DS.json", dict(doc, characters={CAROL_FOLDER: doc["characters"][CAROL_FOLDER]}))
    (tmp_path / "out-all").mkdir()
    with pytest.raises(datasets.DatasetUnavailable):
        colab_jobs.export_inputs("train", [req], cat, refs, tmp_path / "out-all")


@pytest.fixture
def control(carol, monkeypatch):
    """gateway 用这个测试的资料库；算力是 mini 本机和云端 L4。"""
    lib, cat, refs = carol
    monkeypatch.setattr(gateway, "catalog", cat)
    monkeypatch.setattr(gateway, "refs", refs)
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()]))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)
    return carol


def test_local_train_not_blocked_by_missing_cloud_inputs(control, monkeypatch):
    """云端要上传的输入本机缺（角色的 train.list 还没同步过来）：显式 compute=local 照样训；云端候选不可用。"""
    lib, cat, refs = control
    lib.put("characters/carol.json", dict(cat.character("carol"), train_list="runs/carol-old/train.list"))
    started = []
    monkeypatch.setattr(gateway, "start_on", lambda chosen, *a, **kw: started.append(chosen["device"]))
    asyncio.run(gateway.train(TrainRequest(slug="carol", dataset_id=DATASET, compute="local")))
    assert started == ["local"]
    with pytest.raises(dispatch.PlanError) as e:
        asyncio.run(gateway.train(TrainRequest(slug="carol", dataset_id=DATASET, compute="colab-L4")))
    c = cand(e.value.plan, "colab-L4")
    assert e.value.code == "compute_unavailable" and c["reason_code"] == "cloud_inputs_missing"
    assert "train.list" in c["reason"]


def test_cloud_train_speed_from_vm_training_time(control, monkeypatch):
    """云端训练做完：训练速度按 VM 上训练进程实际跑的秒数（run 的 runner_events.jsonl）算，不含开卡、上传、
    训后评测、取回：训练 100 句 × 1 轮跑了 100 秒 → 1 秒/句。"""
    lib, cat, refs = control
    req = TrainRequest(slug="carol-colab", dataset_id=DATASET, engine="v2proplus")
    task = training.train_task(req, 100)
    events = [dict(t="start", pid=1, ts=1000.0), dict(t="stage", stage="训练 SoVITS", progress=0.5, ts=1040.0),
              dict(t="end", state="succeeded", ts=1000.0 + 100 * task.epochs)]

    async def fake_run_colab(run, kind, req, **kw):
        run.job.train_result = TrainResult(slug=req.slug, engine=req.engine, run_name="carol-colab-1", lines_used=100,
                                           minutes_used=1.0, gpt_weights=[], sovits_weights=[])
        lib.put_bytes("runs/carol-colab-1/runner_events.jsonl",
                      "".join(json.dumps(e) + "\n" for e in events).encode("utf-8"))
    monkeypatch.setattr(colab_jobs, "run_colab", fake_run_colab)
    seen = []
    monkeypatch.setattr(gateway.speeds, "observe", lambda device, key, sec: seen.append((device, key, sec)))
    chosen = dict(device="colab-L4", gpu="L4", seconds=3000)
    asyncio.run(gateway.on_cloud(chosen, "train", req, task)(job_run("train", "carol-colab")))
    assert seen == [("colab-L4", "train", pytest.approx(1.0))]


def test_run_colab_version_eval_pretrained_half_not_installed(carol, tmp_path, fake_code, monkeypatch):
    """按版本在云端评测「底模 GPT + carol 的 SoVITS」，控制端没装 GPT 底模：成绩照样导进来（底模按引擎配置认，
    键里写 pretrained），eval_result 填好；成绩对不上资料库里的权重记录（归属是 carol，但这个文件名只有 carol-b 有）时
    任务失败，结果包留着可以重试。"""
    lib, cat, refs = carol
    pre = settings.ENGINES["v2pro"]["pretrained_gpt"].name
    monkeypatch.setitem(settings.ENGINES["v2pro"], "pretrained_gpt", tmp_path / "not-installed" / pre)
    Catalog.add_character("carol-b", "user", "SAMPLE_DS.sp0001")
    (tmp_path / "b_e1.pth").write_bytes(b"b" * 10)
    cat.add_weight("carol-b", "v2pro", "sovits", tmp_path / "b_e1.pth", "july")

    def eval_on_vm(task, cat, root, sovits=None):
        c = combo(task["gpt"], sovits or task["sovits"], 94.0)
        cat.record_eval(task["engine"], task["character"], task["ref_id"], task["job_id"], task["eval_set"], c,
                        None if sovits else task["version"])
        if sovits:  # VM 上记的归属是 carol，文件名却是控制端只有 carol-b 才有的
            rel = cat.eval_rel(task["job_id"])
            doc = library.current().get(rel)
            library.current().put(rel, dict(doc, combos=[dict(x, sovits_owner="carol") for x in doc["combos"]]))
        ev = TrainEval(job_id=task["job_id"], engine=task["engine"], character="carol", ref_id=task["ref_id"],
                       eval_set=task["eval_set"], sentences=["s"], candidates_per_sentence=3, combos=[c], best=c)
        return dict(id=task["job_id"], kind="eval", status="succeeded", eval_result=ev.model_dump())

    req = EvalRequest(version="v2pro/pretrained/carol_e8_s144.pth", character="carol")
    run = job_run("eval", "carol-eval")
    out = asyncio.run(colab_jobs.run_colab(run, "eval", req, gpu="L4", estimated_minutes=20, approved=False,
                                           pool=FakePool(FakeVM(tmp_path / "vm", eval_on_vm)), catalog=cat,
                                           refs=refs, character="carol", ref_id="carol-14200080"))
    assert out["evals"] == 1 and run.job.eval_result.best.score_median == 94.0
    assert cat.latest_evals()["v2pro", "carol", "pretrained", "carol_e8_s144.pth", "hard"].score_median == 94.0
    assert not (settings.DATA / "colab" / run.job.id / "results.tar").exists()

    run = job_run("eval", "carol-ev2")
    vm = FakeVM(tmp_path / "vm2", lambda task, c, root: eval_on_vm(task, c, root, sovits="b_e1.pth"))
    with pytest.raises(RuntimeError, match="没写进资料库"):
        asyncio.run(colab_jobs.run_colab(run, "eval", req, gpu="L4", estimated_minutes=20, approved=False,
                                         pool=FakePool(vm), catalog=cat, refs=refs, character="carol",
                                         ref_id="carol-14200080"))
    assert (settings.DATA / "colab" / run.job.id / "results.tar").exists()


def test_run_colab_version_eval_weight_file_not_synced(carol, fake_code, tmp_path):
    """版本绑定的训练权重只同步到了 meta.json（文件还没到）：开卡前就 version_unavailable，不开 VM。"""
    from ttsapi.catalog import VersionUnavailable, weight_dir
    lib, cat, refs = carol
    lib.path(f"{weight_dir('carol', 'v2pro', 'carol_e8_s144.pth')}/sovits.pth").unlink()
    pool = FakePool(FakeVM(tmp_path / "vm", None))
    req = EvalRequest(version="v2pro/carol-e15.ckpt/carol_e8_s144.pth", character="carol")
    with pytest.raises(VersionUnavailable):
        asyncio.run(colab_jobs.run_colab(job_run("eval", "carol-ev3"), "eval", req, gpu="L4", estimated_minutes=20,
                                         approved=False, pool=pool, catalog=cat, refs=refs, character="carol",
                                         ref_id="carol-14200080"))
    assert pool.calls == []
