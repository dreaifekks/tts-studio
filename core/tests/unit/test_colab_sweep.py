"""扫参（ttsapi/compute/colab_sweep.py、POST /train/sweep）：整批估时和提醒线、每组只带回前 keep 个组合的权重和样本、
VM 被回收后在新 VM 上接着跑、重复提交跳过资料库里已有的组、一组失败不挡后面的组、取消时释放 VM。不联网。

假 VM（SweepVM）在本机目录里「跑」：vm_setup 把输入资料库移到 /content/library，vm_run.py <k> 训练第 k 个任务（写 run、
权重、评测、VM 的任务记录和 summary.json），vm_pack.py 用仓库里真的脚本打包（脚本里的 /content 换到本机目录）。"""
import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException
from test_colab_jobs import DATASET, combo, fake_code, job_run, carol  # noqa: F401 (fixtures)
from test_compute import AUTO, cand, l4, mini
from test_dispatch import snapshot_of

from ttsapi import db, gateway, jobs, library, power, settings
from ttsapi.catalog import Catalog
from ttsapi.compute import colab_sweep, dispatch, registry, scheduler
from ttsapi.compute.colab import Account, RunState
from ttsapi.compute.colab.pool import RATES
from ttsapi.library import Library
from ttsapi.models import SweepRequest, SweepResult, TrainEval, TrainRequest, TrainResult


class SweepVM:
    name, account_id = "vm", "acct"

    def __init__(self, root: Path, fail=(), gone_at=None, hang=False, eval_fail=(), timeout_at=None, pack_fail=None,
                 lost=()):
        self.root, self.fail, self.gone_at, self.hang = root, set(fail), gone_at, hang
        self.eval_fail, self.timeout_at, self.pack_fail, self.lost = set(eval_fail), timeout_at, pack_fail, set(lost)
        self.uploads, self.commands, self.packed, self.packed_jobs, self.stopped = [], [], {}, {}, False
        self.state, self.pending_result = None, None
        self.ran, self.billed = 0.0, None  # 账号池账本里这台：开机后已运行的秒数、已结算的单元（None：还没入账）

    def local(self, remote: str) -> Path:
        return self.root / remote.lstrip("/")

    def upload(self, local, remote):
        self.uploads.append(remote)
        dst = self.local(remote)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if remote.endswith(".py"):
            dst.write_text(Path(local).read_text(encoding="utf-8").replace("/content", f"{self.root}/content"),
                           encoding="utf-8")
        else:
            shutil.copyfile(local, dst)

    def spec(self):
        return json.loads(self.local("/content/job/job.json").read_text(encoding="utf-8"))

    def run_background(self, cmd, marker, heartbeat=None):
        self.commands.append(cmd)
        script, *args = cmd.split("&& ")[-1].split()[1:]
        if script == "vm_setup.py":
            self.local(self.spec()["inputs"] + "/library").rename(self.local("/content/library"))
            self.vmlib().claim()
            self.state = RunState.DONE
        elif script == "vm_run.py":
            k = int(args[0])
            if k == self.timeout_at:
                raise TimeoutError("Colab VM time limit reached")
            self.state = RunState.RUNNING if self.hang else RunState.GONE if k == self.gone_at else self.train(k)
        elif args[1:] == [str(self.pack_fail)]:
            self.state = RunState.FAILED
        else:
            subprocess.run([sys.executable, str(self.local("/content/job/vm_pack.py")), str(self.local(args[0])),
                            *args[1:]], check=True)
            with tarfile.open(self.local(args[0])) as tf:
                self.packed[args[0]] = [m.name for m in tf.getmembers() if m.isfile()]
                tf.extract("data/tts.db", self.root / "packed" / args[0].strip("/"))
            conn = sqlite3.connect(self.root / "packed" / args[0].strip("/") / "data/tts.db")
            self.packed_jobs[args[0]] = {r[0] for r in conn.execute("SELECT id FROM jobs")}
            conn.close()
            self.state = RunState.DONE

    def poll(self):
        return self.state

    def download(self, remote, local, timeout=180):
        shutil.copyfile(self.local(remote), local)

    def stop(self):
        self.stopped = True

    def vmlib(self):
        return Library(self.local("/content/library"), self.root / "index", dict(id="vm", name="colab-vm"))

    def train(self, k):
        """训练第 k 个任务：3 个 GPT × 2 个 SoVITS（6 个组合，分数各不同），按 keep 记要带回的权重、按 samples 写样本。"""
        spec = self.spec()
        task = spec["tasks"][k]
        req = TrainRequest(**{x: v for x, v in task.items() if x not in ("kind", "job_id")})
        data = self.local("/content/tts-api/data")
        (data / "jobs").mkdir(parents=True, exist_ok=True)
        row = dict(index=k, task=f"task{k:02d}", job=task["job_id"], status="succeeded", error=None)
        base = dict(id=task["job_id"], kind="train", target=req.slug, progress=1.0, stage="完成",
                    created="2026-11-20T01:02:03")
        if unhashed(req.slug) in self.fail:
            row.update(status="failed", error="CUDA out of memory")
            record = dict(base, status="failed", error=row["error"])
        else:
            with library.pinned(self.vmlib()):  # 任务线程里控制端的资料库是 pinned 的
                record, row = self.produce(req, task["job_id"], k, spec, row, base)
            if unhashed(req.slug) in self.lost:  # 训练、评测都完成了，vm_run 等任务结果时出错：summary 里没有 run、keep
                row = dict(row, status="failed", error="gateway 没响应")
                del row["run"], row["keep"]
        conn = sqlite3.connect(data / "tts.db")
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO jobs VALUES(?,?,?)", (task["job_id"], time.time(), json.dumps(record)))
        conn.commit()
        conn.close()
        (data / "jobs" / f"{task['job_id']}.log").write_text(f"vm train {req.slug}\n", encoding="utf-8")
        summary = self.local("/content/job/summary.json")
        rows = [r for r in json.loads(summary.read_text()) if r["index"] != k] if summary.exists() else []
        summary.write_text(json.dumps(sorted([*rows, row], key=lambda r: r["index"])))
        return RunState.DONE if row["status"] == "succeeded" else RunState.FAILED

    def produce(self, req, job_id, k, spec, row, base):
        cat = Catalog()
        run_name = f"{req.slug}-20261120-0102{k:02d}"
        w = self.root / "w" / run_name
        w.mkdir(parents=True)
        files = dict(gpt=[w / f"{run_name}-e{e}.ckpt" for e in (5, 10, 15)],
                     sovits=[w / f"{run_name}_e{e}_s8.pth" for e in (4, 8)])
        for f in (*files["gpt"], *files["sovits"]):
            f.write_bytes(f.name.encode() * 50)
        cat.register_run(req.slug, req.dataset_id, dict(
            run_name=run_name, engine=req.engine, job_id=job_id, best=None, filter=None, lines_used=3,
            minutes_used=0.1, params=req.model_dump()), files, train_dir=None)
        combos = sorted((combo(g.name, s.name, 90 + i) for i, (g, s) in enumerate(
            (g, s) for g in files["gpt"] for s in files["sovits"])), key=lambda c: -c.score_median)
        for c in combos:
            cat.record_eval(req.engine, req.slug, "carol-14000140", job_id, "hard", c)
        cat.set_best(run_name, combos[0].gpt, combos[0].sovits)
        ev = TrainEval(job_id=job_id, engine=req.engine, character=req.slug, ref_id="carol-14000140", eval_set="hard",
                       sentences=["s"], candidates_per_sentence=1, combos=combos, best=combos[0])
        tr = TrainResult(slug=req.slug, engine=req.engine, run_name=run_name, lines_used=3, minutes_used=0.1,
                         gpt_weights=[f.name for f in files["gpt"]], sovits_weights=[f.name for f in files["sovits"]],
                         eval=ev)
        if unhashed(req.slug) in self.eval_fail:  # 训完登记了，训后评测失败：vm_run 记 run、不记 keep
            row.update(run=run_name, status="failed", error="scorer crashed")
            return dict(base, status="failed", error="scorer crashed",
                        train_result=dict(tr.model_dump(), eval=None)), row
        row.update(run=run_name, keep=sorted({c.gpt for c in combos[:spec["keep"]]} |
                                             {c.sovits for c in combos[:spec["keep"]]}))
        if spec["samples"]:
            out = library.current().path(f"runs/{run_name}/samples")
            for c in combos[:spec["samples"]]:
                (out / f"{c.gpt}+{c.sovits}").mkdir(parents=True)
                (out / f"{c.gpt}+{c.sovits}" / "00.wav").write_bytes(b"RIFF")
            (out / "items.json").write_text(json.dumps(dict(texts=spec["sample_texts"], items=[])))
            row["samples"] = f"runs/{run_name}/samples"
        return dict(base, status="succeeded", train_result=tr.model_dump()), row


def unhashed(slug: str) -> str:
    """组的 slug 去掉参数哈希：carol-v2proplus-s8g15-1a2b3c → carol-v2proplus-s8g15。"""
    return slug.rsplit("-", 1)[0]


class Pool:
    """假账号池：start 依次给出 vms，在账本 usage 里记一行（开机时间按 vm.ran 往前推、已结算的是 vm.billed）；
    balances 是能开 L4 的各账号余额。"""

    def __init__(self, *vms, balances=(1000.0,)):
        self.vms, self.calls, self.balances = list(vms), [], balances
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
        return [Account(id=f"acct-{i}", label=f"a{i}", balance=b, month_used=None, gpus=("T4", "L4"),
                        recorded_month_used=0.0) for i, b in enumerate(self.balances)]

    def rates(self):
        return dict(RATES)

    def idle_vms(self):
        return {}

    def start(self, gpu, max_minutes=None, *, task_id, estimated_minutes, approved):
        self.calls.append(estimated_minutes)
        vm = self.vms.pop(0)
        vm.name = f"vm-{len(self.calls)}"
        with self.db() as db:
            db.execute("INSERT INTO usage(name,gpu,started,stopped,actual_units) VALUES (?,?,?,?,?)", (vm.name, gpu, self.clock() - vm.ran, None, vm.billed))
        return vm


def request(**kw):
    return SweepRequest(characters=["carol"], engines=["v2proplus", "v4"],
                        trains=[dict(), dict(sovits_epochs=16, gpt_epochs=20, engine="v2proplus")], **kw)


def chosen(req, cat, balances=(1000.0,)):
    """提交时选中的云端 L4（scheduler.estimate：按这些余额分段的 group_sec、segments、units；budget 同 units，
    即不用问时 dispatch.choose 给的）。"""
    c = scheduler.estimate(l4(balances=list(balances)), asyncio.run(gateway.sweep_groups(req))[1])
    return dict(c, budget=c["units"])


def start(run, groups, pick, pool, cat, refs, req):
    return colab_sweep.run_sweep(run, groups, pick, approved=False, pool=pool, catalog=cat, refs=refs, keep=req.keep,
                                 samples=req.samples, into_builtin=req.into_builtin)


def sweep(req, cat, *vms, balances=(1000.0,), budget=None):
    """跑一个扫参任务（不经 jobs.start）：返回 (run, pool, 抛出的错误或 None)。budget：确认过的单元（默认按计划）。"""
    pick = chosen(req, cat, balances)
    if budget is not None:
        pick["budget"] = budget
    groups = colab_sweep.groups(req, cat)
    run = job_run("sweep", f"carol-{len(list(settings.DATA.glob('colab/*')))}")
    run.job.sweep_result = SweepResult(groups=[g for g, _ in groups])
    pool = Pool(*vms, balances=balances)
    try:
        asyncio.run(start(run, groups, pick, pool, cat, gateway.refs, req))
        return run, pool, None
    except Exception as e:
        return run, pool, e


@pytest.fixture
def ctl(carol, fake_code, monkeypatch):  # noqa: F811
    """gateway 的目录、参考库用这个测试的资料库；云端只有 L4。"""
    lib, cat, refs = carol
    monkeypatch.setattr(gateway, "catalog", cat)
    monkeypatch.setattr(gateway, "refs", refs)
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()]))
    monkeypatch.setattr(registry, "month_used", lambda: 0.0)
    return lib, cat, refs


# ---------------------------------------------------------------- 估时和提醒线

def test_plan_counts_whole_batch_and_asks_once(ctl, monkeypatch):
    lib, cat, refs = ctl
    req = request(samples=1)
    p = asyncio.run(gateway.train_sweep_plan(req))
    assert [(unhashed(g.slug), g.status) for g in p.groups] == [
        ("carol-v2proplus-s8g15", "pending"), ("carol-v2proplus-s16g20", "pending"), ("carol-v4-s8g15", "pending")]
    whole = cand(p.plan.model_dump(), "colab-L4")
    # 整批（一台 VM）= 开机准备一次 + 每组（训练 + 训后评测 + 样本）相加
    secs = chosen(req, cat)["group_sec"]
    assert whole["vms"] == 1 and whole["seconds"] == round(prep() + sum(secs))
    assert whole["units"] == round(max(0.25, whole["seconds"] / 3600 * l4()["rate"]), 2)
    assert p.plan.recommended == "colab-L4"
    each = [round(max(0.25, (prep() + s) / 3600 * l4()["rate"]), 2) for s in secs]
    # 提醒线按整批算：每组单独都不超，整批超了，提交时先问
    ask = max(each) + 0.01
    assert whole["units"] > ask
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()], dict(AUTO, ask_over=ask)))
    assert asyncio.run(gateway.train_sweep_plan(req)).plan.confirm == ["over_task"]
    with pytest.raises(dispatch.PlanError) as e:
        asyncio.run(gateway.train_sweep(req))
    assert e.value.code == "confirm_required" and e.value.plan["candidates"]


def test_sweep_runs_only_on_cloud(ctl, monkeypatch):
    """本机、局域网设备对扫参不可用（cloud_only）：云端开不了时不推荐本机；指定本机 409。"""
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4(status="unavailable")]))
    p = asyncio.run(gateway.train_sweep_plan(request())).plan
    assert p.recommended is None and cand(p.model_dump(), "local")["reason_code"] == "cloud_only"
    with pytest.raises(dispatch.PlanError) as e:
        asyncio.run(gateway.train_sweep(request(compute="local")))
    assert e.value.code == "compute_unavailable" and "云端" in e.value.detail


# ---------------------------------------------------------------- 跑

def test_each_group_brings_back_top_n_and_samples(ctl, tmp_path):
    lib, cat, refs = ctl
    vm = SweepVM(tmp_path / "vm")
    run, pool, err = sweep(request(keep=2, samples=1), cat, vm)
    assert err is None and vm.stopped and run.job.sweep_result.vms == 1
    # 一台 VM：上传一次、装一次环境，每组跑一次、打包一次（只打这一组）
    cmds = [c.split("&& ")[-1] for c in vm.commands]
    assert cmds[0] == "python3 vm_setup.py" and sum("vm_setup" in c for c in cmds) == 1
    assert [c.split()[1:] for c in cmds if "vm_run" in c] == [["vm_run.py", "0"], ["vm_run.py", "1"], ["vm_run.py", "2"]]
    spec = vm.spec()
    assert spec["keep"] == 2 and spec["samples"] == 1 and len(spec["sample_texts"]) == 5
    assert [t["job_id"] for t in spec["tasks"]] == [f"{run.job.id}-{i:02d}" for i in range(3)]
    groups = run.job.sweep_result.groups
    assert [g.status for g in groups] == ["succeeded"] * 3
    for i, g in enumerate(groups):
        r = cat.run(g.run_name)
        kept = r["gpt"] + r["sovits"]
        assert len(kept) < 5 and sorted(kept) == sorted(g.kept)       # 3 GPT + 2 SoVITS 里只带回前 2 组用到的
        assert r["character"] == "carol" and r["job_id"] == g.job_id == f"{run.job.id}-{i:02d}"  # 并到内置角色
        assert g.best.score_median == 95 and g.best.per_sentence is None
        assert jobs.get(g.job_id).train_result.run_name == g.run_name  # 每组的训练任务记录
        ev = lib.get(Catalog.eval_rel(g.job_id))
        assert {(c["gpt"], c["sovits"]) for c in ev["combos"]} <= {(a, b) for a in r["gpt"] for b in r["sovits"]}
        assert g.samples == f"runs/{g.run_name}/samples" and lib.path(f"{g.samples}/items.json").exists()
        assert len(list(lib.path(g.samples).rglob("*.wav"))) == 1
    # 第 2 组的包里没有第 1 组的权重文件（每组只带自己的）
    tar = next(k for k in vm.packed if k.endswith("-1.tar"))
    second = vm.packed[tar]
    assert not any(groups[0].run_name in m and not m.endswith("meta.json") for m in second if "/weights/" in m)
    # 也没有第 1 组的 run 目录（含样本）、评测、任务日志、任务记录
    assert any(f"library/runs/{groups[1].run_name}/samples/" in m for m in second)
    assert not any(f"library/runs/{groups[0].run_name}/" in m for m in second)
    assert not any(groups[0].job_id in m for m in second)
    assert [m for m in second if m.startswith("data/jobs/")] == [f"data/jobs/{groups[1].job_id}.log"]
    assert any(m.startswith("library/evals/") and groups[1].job_id in m for m in second)
    assert vm.packed_jobs[tar] == {groups[1].job_id}
    work = settings.DATA / "colab" / run.job.id
    assert not list(work.glob("results-*.tar"))
    assert "vm train carol-v4-s8g15-" in run.log_path.read_text(encoding="utf-8")


def test_group_without_run_in_summary_keeps_all_weights(ctl, tmp_path):
    """vm_run 没在 summary 里记下 run（等任务结果时出错）：vm_pack 按任务 id 从 VM 资料库找到这组的 run，权重全带回。"""
    lib, cat, refs = ctl
    vm = SweepVM(tmp_path / "vm", lost={"carol-v2proplus-s16g20"})
    run, pool, err = sweep(request(keep=1, samples=0), cat, vm)
    groups = run.job.sweep_result.groups
    assert isinstance(err, RuntimeError) and [g.status for g in groups] == ["succeeded", "failed", "succeeded"]
    assert len(groups[0].kept) == 2 and len(groups[2].kept) == 2  # 有排名的组只带前 1 个组合
    r = cat.run(groups[1].run_name)
    assert len(r["gpt"]) == 3 and len(r["sovits"]) == 2 and sorted(groups[1].kept) == sorted(r["gpt"] + r["sovits"])
    assert "权重已全部写进资料库" in groups[1].error


def test_gone_vm_resumes_on_new_vm(ctl, tmp_path):
    """第 1 台在第 2 组时被回收：第 1 组已写进资料库，第 2 台只跑第 2、3 组；预计时长只算剩下的。"""
    lib, cat, refs = ctl
    vm1, vm2 = SweepVM(tmp_path / "a", gone_at=1), SweepVM(tmp_path / "b")
    run, pool, err = sweep(request(samples=0), cat, vm1, vm2)
    assert err is None and vm1.stopped and vm2.stopped and run.job.sweep_result.vms == 2
    assert [unhashed(t["slug"]) for t in vm2.spec()["tasks"]] == ["carol-v2proplus-s16g20", "carol-v4-s8g15"]
    assert [t["job_id"][-2:] for t in vm2.spec()["tasks"]] == ["01", "02"]  # 组序号不变
    assert pool.calls[1] < pool.calls[0]
    assert [g.status for g in run.job.sweep_result.groups] == ["succeeded"] * 3
    assert len(cat.runs("carol")) == 3 and "剩下的组开第 2 台" in run.log_path.read_text(encoding="utf-8")


def test_failed_group_does_not_stop_the_rest_and_resubmit_runs_only_it(ctl, tmp_path, monkeypatch):
    lib, cat, refs = ctl
    run, _, err = sweep(request(samples=0), cat, SweepVM(tmp_path / "a", fail={"carol-v2proplus-s8g15"}))
    slug = run.job.sweep_result.groups[0].slug
    assert isinstance(err, RuntimeError) and f"{slug}（CUDA out of memory）" in str(err)
    assert [g.status for g in run.job.sweep_result.groups] == ["failed", "succeeded", "succeeded"]
    # 重新提交同一批：资料库里已有的两组跳过，只跑失败的那组
    req = request(samples=0)
    assert [g.status for g, _ in colab_sweep.groups(req, cat)] == ["pending", "skipped", "skipped"]
    vm = SweepVM(tmp_path / "b")
    run, _, err = sweep(req, cat, vm)
    assert err is None and [t["slug"] for t in vm.spec()["tasks"]] == [slug]
    assert [g.status for g in run.job.sweep_result.groups] == ["succeeded", "skipped", "skipped"]
    skipped = run.job.sweep_result.groups[1]
    assert skipped.run_name and skipped.kept
    # 已跳过的组不查数据集和参考（本机没了它的数据集也能续跑）；都跳过时不估花费（plan 为 null）
    def gone(name):
        raise KeyError(f"数据集 {name}")
    monkeypatch.setattr(gateway.datasets, "get", gone)
    req = request(samples=0, ref_ids={"carol": "gone-00000000"})
    assert asyncio.run(gateway.train_sweep_plan(req)).plan is None
    with pytest.raises(KeyError):  # 要跑的组照样查
        asyncio.run(gateway.train_sweep_plan(SweepRequest(characters=["carol"], engines=["v2proplus"],
                                                          trains=[dict(sovits_epochs=4)])))
    # 全都在资料库里：不选设备、不开 VM，任务直接完成
    monkeypatch.setattr(power, "keep_awake", lambda on: None)

    async def no_choose(*a, **kw):
        raise AssertionError("不该选设备")
    monkeypatch.setattr(dispatch, "choose", no_choose)

    async def submit():
        job = await gateway.train_sweep(req)
        await asyncio.wait({jobs.runs[job.id].task})
        return job
    job = asyncio.run(submit())
    assert job.status == "succeeded" and {g.status for g in job.sweep_result.groups} == {"skipped"}


def test_cancel_releases_vm(ctl, tmp_path, monkeypatch):
    lib, cat, refs = ctl
    monkeypatch.setattr(colab_sweep, "POLL_SEC", 0.01)
    monkeypatch.setattr(power, "keep_awake", lambda on: None)
    vm = SweepVM(tmp_path / "vm", hang=True)
    req = request(samples=0)
    groups, pick = colab_sweep.groups(req, cat), chosen(req, cat)

    async def go():
        job = jobs.start("sweep", "carol", lambda run: start(run, groups, pick, Pool(vm), cat, refs, req))
        job.sweep_result = SweepResult(groups=[g for g, _ in groups])
        while not any("vm_run" in c for c in vm.commands):
            await asyncio.sleep(0.01)
        await jobs.cancel(job.id)
        return job
    job = asyncio.run(go())
    assert job.status == "cancelled" and vm.stopped and lib.docs("runs") == []


def test_eval_failure_keeps_weights_and_vm_trouble_moves_on(ctl, tmp_path):
    """训后评测失败：这一组记失败，但权重全部写进资料库；打包失败：只这一组失败；到了 VM 时限：剩下的组换一台。"""
    lib, cat, refs = ctl
    vm1 = SweepVM(tmp_path / "a", eval_fail={"carol-v2proplus-s8g15"}, pack_fail=1, timeout_at=2)
    vm2 = SweepVM(tmp_path / "b")
    run, _, err = sweep(request(samples=0), cat, vm1, vm2)
    g0, g1, g2 = run.job.sweep_result.groups
    assert (g0.status, g1.status, g2.status) == ("failed", "failed", "succeeded")
    assert g0.run_name and len(g0.kept) == 5 and "权重已全部写进资料库" in g0.error   # 没排名：全带回
    assert len(cat.run(g0.run_name)["gpt"]) == 3
    assert "打包失败" in g1.error and g1.run_name is None
    assert vm1.stopped and vm2.stopped and run.job.sweep_result.vms == 2
    assert [unhashed(t["slug"]) for t in vm2.spec()["tasks"]] == ["carol-v4-s8g15"]
    assert "TimeoutError" in run.log_path.read_text(encoding="utf-8")
    assert isinstance(err, RuntimeError) and "2 组没完成" in str(err)


def test_same_groups_running_is_409(ctl, monkeypatch):
    lib, cat, refs = ctl
    monkeypatch.setattr(power, "keep_awake", lambda on: None)
    started = []

    async def fake_run(run, *a, **kw):
        started.append(run.job.id)
        await asyncio.sleep(3600)
    monkeypatch.setattr(colab_sweep, "run_sweep", fake_run)

    async def go():
        job = await gateway.train_sweep(request(approved=True))
        with pytest.raises(HTTPException) as e:
            await gateway.train_sweep(request(approved=True))
        assert e.value.status_code == 409 and job.id in e.value.detail
        run = jobs.runs[job.id]
        run.cancel_requested = True
        run.task.cancel()
        await asyncio.wait({run.task})
    asyncio.run(go())


def test_concurrent_submit_registers_once(ctl, monkeypatch):
    """同一批同时提交两次、两个请求都等在选设备上：放行后只登记一个任务，另一个 409。"""
    monkeypatch.setattr(power, "keep_awake", lambda on: None)
    choose, waiting, gate = dispatch.choose, [], asyncio.Event()

    async def slow_choose(*a, **kw):
        waiting.append(1)
        await gate.wait()
        return await choose(*a, **kw)
    monkeypatch.setattr(dispatch, "choose", slow_choose)

    async def fake_run(run, *a, **kw):
        await asyncio.sleep(3600)
    monkeypatch.setattr(colab_sweep, "run_sweep", fake_run)

    async def go():
        both = [asyncio.ensure_future(gateway.train_sweep(request(samples=0))) for _ in range(2)]
        while len(waiting) < 2:
            await asyncio.sleep(0.01)
        gate.set()
        done = await asyncio.gather(*both, return_exceptions=True)
        ok = [d for d in done if not isinstance(d, Exception)]
        err = [d for d in done if isinstance(d, HTTPException)]
        assert len(ok) == 1 and len(err) == 1 and err[0].status_code == 409 and ok[0].id in err[0].detail
        assert [r.job.id for r in jobs.runs.values() if r.job.kind == "sweep"] == [ok[0].id]
        run = jobs.runs[ok[0].id]
        run.cancel_requested = True
        run.task.cancel()
        await asyncio.wait({run.task})
    asyncio.run(go())


def test_cancel_during_upload_releases_vm_first(ctl, tmp_path, monkeypatch):
    """上传一个大文件时取消：线程看不到取消，控制协程马上释放 VM，上传随之失败，任务变成 cancelled。"""
    lib, cat, refs = ctl
    monkeypatch.setattr(power, "keep_awake", lambda on: None)
    uploading, released = threading.Event(), threading.Event()

    class SlowUploadVM(SweepVM):
        timed_out = False

        def upload(self, local, remote):
            uploading.set()
            if not released.wait(5):
                self.timed_out = True
            raise RuntimeError("run is closed")

        def stop(self):
            super().stop()
            released.set()

    vm = SlowUploadVM(tmp_path / "vm")
    req = request(samples=0)
    groups, pick = colab_sweep.groups(req, cat), chosen(req, cat)

    async def go():
        job = jobs.start("sweep", "carol", lambda run: start(run, groups, pick, Pool(vm), cat, refs, req))
        job.sweep_result = SweepResult(groups=[g for g, _ in groups])
        await asyncio.to_thread(uploading.wait, 5)
        await jobs.cancel(job.id)
        return job
    job = asyncio.run(go())
    assert job.status == "cancelled" and vm.stopped and not vm.timed_out



# ---------------------------------------------------------------- 账号余额、分段

def prep():
    """每台 VM 的开机准备（开卡、装环境 + 上传这一批的输入）。"""
    return chosen(request(samples=0), None)["cold_prep_sec"]


def cost(*secs):
    return max(0.25, (prep() + sum(secs)) / 3600 * RATES["L4"])


def test_plan_counts_each_vm_and_asks_by_segments(ctl, monkeypatch):
    """单个账号付不起整批：计划分几台，每台各算开机准备和花费；提醒线、余额都按这个分段算。"""
    req = request(samples=0)
    secs = chosen(req, Catalog())["group_sec"]
    one = cand(asyncio.run(gateway.train_sweep_plan(req)).plan.model_dump(), "colab-L4")   # 余额不限：一台
    assert one["vms"] == 1 and one["units"] == round(cost(*secs), 2)
    assert one["prep_sec"] == round(prep())  # 预览给出第一台开始前要准备多久（界面「要先准备环境约 N 分钟」）
    # 有留机可接手：第一台按热启动准备，执行时新开卡用的准备时间不变
    task = asyncio.run(gateway.sweep_groups(req))[1]
    warm = scheduler.estimate(l4(warm=1), task)
    assert warm["prep_sec"] == round(warm["warm_prep_sec"]) < one["prep_sec"] and warm["cold_prep_sec"] == prep()
    each = max(cost(s) for s in secs) + 0.01  # 每个账号只付得起一组：三台
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4(balances=[each] * 3)],
                                                          dict(AUTO, ask_over=one["units"] + 0.01)))
    p = asyncio.run(gateway.train_sweep_plan(req)).plan
    three = cand(p.model_dump(), "colab-L4")
    assert three["vms"] == 3 and three["units"] == round(sum(cost(s) for s in secs), 2) > one["units"]
    assert three["seconds"] == round(3 * prep() + sum(secs))
    assert p.confirm == ["over_task"]  # 一台时不用问，分三台超了提醒线
    # 两个账号各只付得起一组（sol 的例子）：第三组没人付得起，整批不可用
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4(balances=[each] * 2)]))
    c = cand(asyncio.run(gateway.train_sweep_plan(req)).plan.model_dump(), "colab-L4")
    assert c["reason_code"] == "insufficient_balance" and not c["available"]
    with pytest.raises(dispatch.PlanError) as e:
        asyncio.run(gateway.train_sweep(request(samples=0, compute="colab-L4", approved=True)))
    assert e.value.code == "compute_unavailable" and "余额" in e.value.detail


def test_vms_follow_segments(ctl, tmp_path):
    """执行时每台 VM 跑的组、开机时长和计划的分段一样。"""
    lib, cat, refs = ctl
    req = request(samples=0)
    secs = chosen(req, cat)["group_sec"]
    balances = (cost(secs[0], secs[1]) + 0.01, cost(secs[2]) + 0.01)  # 第一个账号付前两组，第二个付第三组
    assert chosen(req, cat, balances)["segments"] == [[0, 1], [2]]
    each = prep()  # 跑之前算（跑完组都在资料库里，没有要上传的）
    vms = [SweepVM(tmp_path / f"v{i}") for i in range(2)]
    run, pool, err = sweep(req, cat, *vms, balances=balances)
    assert err is None and run.job.sweep_result.vms == 2 and all(vm.stopped for vm in vms)
    assert [[unhashed(t["slug"]) for t in vm.spec()["tasks"]] for vm in vms] == \
        [["carol-v2proplus-s8g15", "carol-v2proplus-s16g20"], ["carol-v4-s8g15"]]
    assert [round(m * 60) for m in pool.calls] == [round(each + secs[0] + secs[1]), round(each + secs[2])]
    assert [g.status for g in run.job.sweep_result.groups] == ["succeeded"] * 3


def test_costlier_than_confirmed_stops_before_spending(ctl, tmp_path):
    """执行时按当时的余额分段比确认过的贵：不开 VM，停下等重新提交确认。"""
    lib, cat, refs = ctl
    req = request(samples=0)
    secs = chosen(req, cat)["group_sec"]
    vm = SweepVM(tmp_path / "vm")
    run, pool, err = sweep(req, cat, vm, balances=(max(cost(s) for s in secs) + 0.01,) * 3,
                           budget=round(cost(*secs), 2))  # 确认时按一台算的价
    assert isinstance(err, RuntimeError) and "超过确认过的" in str(err) and pool.calls == []
    run, pool, err = sweep(req, cat, vm, balances=(0.2,))
    assert isinstance(err, RuntimeError) and "付不起" in str(err) and pool.calls == []


def test_sweep_confirm_carries_units_and_budget(ctl, monkeypatch):
    """提醒线 0（每次问）：确认带着弹窗里的设备和单元，执行时它就是预算；等确认时余额变了要多分几台（更贵），
    带着旧的单元再提交照样 confirm_required（附新 plan）。"""
    monkeypatch.setattr(power, "keep_awake", lambda on: None)
    ask = dict(AUTO, ask_over=0)
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()], ask))
    with pytest.raises(dispatch.PlanError) as e:
        asyncio.run(gateway.train_sweep(request(samples=0)))
    units = cand(e.value.plan, "colab-L4")["units"]
    approved = dict(approved=True, approved_device="colab-L4", approved_units=units)
    each = max(cost(s) for s in chosen(request(samples=0), None)["group_sec"]) + 0.01  # 每个账号只付得起一组
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4(balances=[each] * 3)], ask))
    with pytest.raises(dispatch.PlanError) as e:
        asyncio.run(gateway.train_sweep(request(samples=0, **approved)))
    assert e.value.code == "confirm_required" and cand(e.value.plan, "colab-L4")["units"] > units
    monkeypatch.setattr(registry, "snapshot", snapshot_of([mini(), l4()], ask))
    seen = {}

    async def fake_run(run, groups, chosen, **kw):
        seen["budget"] = chosen["budget"]
    monkeypatch.setattr(colab_sweep, "run_sweep", fake_run)

    async def go():
        job = await gateway.train_sweep(request(samples=0, **dict(approved, approved_units=units + 1)))
        await asyncio.wait({jobs.runs[job.id].task})
    asyncio.run(go())
    assert seen["budget"] == units + 1


def test_overspent_vm_stops_before_the_next_and_asks_again(ctl, tmp_path):
    """第一台正常跑完但实际花得比估的多（已结算的单元；或账单还没入账，按已运行时长 × 费率计提）：
    不开第二台，已完成的组留在资料库里，任务以「需要重新确认」结束。"""
    lib, cat, refs = ctl
    for n, (billed, ran) in enumerate(((5.0, 0.0), (None, 7200.0))):
        req = request(samples=0, max_lines=10 + n)  # 每轮参数不同：上一轮写进资料库的组不算
        secs = chosen(req, cat)["group_sec"]
        balances = (cost(secs[0], secs[1]) + 0.01, cost(secs[2]) + 0.01)  # 分两台：前两组、第三组
        vm1, vm2 = SweepVM(tmp_path / f"a{n}"), SweepVM(tmp_path / f"b{n}")
        vm1.billed, vm1.ran = billed, ran
        run, pool, err = sweep(req, cat, vm1, vm2, balances=balances)
        assert isinstance(err, RuntimeError) and "超过确认过的" in str(err)
        assert run.job.sweep_result.confirm_required and len(pool.calls) == 1 and vm2.commands == []
        assert [g.status for g in run.job.sweep_result.groups] == ["succeeded", "succeeded", "pending"]
        assert "这台 VM 约花" in run.log_path.read_text(encoding="utf-8")


def register_done(cat, tr, folder: Path):
    """资料库里登记一个参数和 tr 一样的 run（这一组已完成）。"""
    run_name = f"{tr.slug}-20261120-000000"
    files = {}
    for kind, name in (("gpt", f"{run_name}-e15.ckpt"), ("sovits", f"{run_name}_e8_s8.pth")):
        (folder / name).write_bytes(name.encode() * 10)
        files[kind] = [folder / name]
    cat.register_run(tr.slug, tr.dataset_id, dict(run_name=run_name, engine=tr.engine, job_id="x", best=None,
                                                  filter=None, lines_used=3, minutes_used=0.1,
                                                  params=tr.model_dump()), files)


def save_every(*gpt_save_every):
    """轮数相同、只有 GPT 保存间隔不同的几组。"""
    return SweepRequest(characters=["carol"], engines=["v2proplus"], samples=0,
                        trains=[dict(gpt_epochs=15, gpt_save_every=s) for s in gpt_save_every])


def test_groups_differing_only_in_save_every_are_told_apart(ctl, monkeypatch):
    """slug 带参数哈希，两组不同；运行中登记按完整参数键：跑着间隔 5 那组时，间隔 3 那组照样能提交，同样的 5 再提交 409。"""
    lib, cat, refs = ctl
    monkeypatch.setattr(power, "keep_awake", lambda on: None)
    a, b = (tr for _, tr in colab_sweep.groups(save_every(5, 3), cat))
    assert a.slug != b.slug and unhashed(a.slug) == unhashed(b.slug) == "carol-v2proplus-s8g15"

    async def fake_run(run, *a, **kw):
        await asyncio.sleep(3600)
    monkeypatch.setattr(colab_sweep, "run_sweep", fake_run)

    async def go():
        five = await gateway.train_sweep(save_every(5))
        three = await gateway.train_sweep(save_every(3))
        with pytest.raises(HTTPException) as e:
            await gateway.train_sweep(save_every(5))
        assert e.value.status_code == 409 and five.id in e.value.detail and three.id not in e.value.detail
        for job in (five, three):
            run = jobs.runs[job.id]
            run.cancel_requested = True
            run.task.cancel()
            await asyncio.wait({run.task})
    asyncio.run(go())


def test_group_written_while_choosing_is_caught_by_full_key(ctl, tmp_path, monkeypatch):
    """等选设备时，同名（轮数相同）的两组里一组写进了资料库：复查按完整参数键发现，409，不把两组都入队。"""
    lib, cat, refs = ctl
    choose = dispatch.choose
    five = next(tr for _, tr in colab_sweep.groups(save_every(5), cat))

    async def slow_choose(task, choice):
        register_done(cat, five, tmp_path)
        return await choose(task, choice)
    monkeypatch.setattr(dispatch, "choose", slow_choose)
    with pytest.raises(HTTPException) as e:
        asyncio.run(gateway.train_sweep(save_every(5, 3)))
    assert e.value.status_code == 409 and "刚写进资料库" in e.value.detail
    assert [g.status for g, _ in colab_sweep.groups(save_every(5, 3), cat)] == ["skipped", "pending"]


def test_keep_minutes_keeps_last_vm_and_releases_gone_one(ctl, tmp_path):
    """做完先别关：正常跑完的 VM 留着（keep_idle），不释放；中途没了的照常释放。最长用多久带进开卡。"""
    lib, cat, refs = ctl

    class KeptVM(SweepVM):
        idle = None

        def keep_idle(self, minutes):
            self.idle = minutes
            return True
    vm1, vm2 = KeptVM(tmp_path / "a", gone_at=1), KeptVM(tmp_path / "b")
    req = request(samples=0)
    pick, groups = chosen(req, cat), colab_sweep.groups(req, cat)
    run = job_run("sweep", "carol-keep")
    run.job.sweep_result = SweepResult(groups=[g for g, _ in groups])
    starts = []

    class KeepPool(Pool):
        def start(self, gpu, max_minutes=None, **kw):
            starts.append(max_minutes)
            return super().start(gpu, **kw)
    pool = KeepPool(vm1, vm2)
    asyncio.run(colab_sweep.run_sweep(run, groups, pick, approved=False, pool=pool, catalog=cat, refs=refs, keep=req.keep,
                                      samples=0, into_builtin=req.into_builtin, max_minutes=120, keep_minutes=15))
    assert starts == [120, 120]
    assert (vm1.stopped, vm1.idle) == (True, None) and (vm2.stopped, vm2.idle) == (False, 15)


def test_spent_counts_only_after_adoption():
    """接手的 VM：账本同一行，扣预算只算接手以后的（已结算的按时长比例分）。"""
    pool = Pool()
    with pool.db() as db:
        db.execute("ALTER TABLE usage ADD COLUMN adopted REAL")
        now = pool.clock()
        db.execute("INSERT INTO usage VALUES (?,?,?,?,?,?)", ("vm", "L4", now - 3600, now, 2.0, now - 900))
        db.execute("INSERT INTO usage VALUES (?,?,?,?,?,?)", ("own", "L4", now - 3600, now, 2.0, None))
    assert colab_sweep._spent(pool, "vm") == pytest.approx(0.5)
    assert colab_sweep._spent(pool, "own") == pytest.approx(2.0)


def test_spent_fixed_before_vm_is_opened_for_takeover(ctl, tmp_path):
    """做完先别关：这一段的花费先按自己的占用算好再标空闲；标了之后马上被接手（adopted 改成新任务的）也不影响。"""
    lib, cat, refs = ctl

    class KeptVM(SweepVM):
        def keep_idle(self, minutes):
            with pool.db() as db:                                     # 另一个任务立刻接手
                db.execute("UPDATE usage SET adopted=? WHERE name=?", (pool.clock(), self.name))
            return True
    vm = KeptVM(tmp_path / "vm")
    vm.ran = 1800.0
    req = request(samples=0)
    pick, groups = chosen(req, cat), colab_sweep.groups(req, cat)
    run = job_run("sweep", "carol-spent")
    run.job.sweep_result = SweepResult(groups=[g for g, _ in groups])
    pool = Pool(vm)
    with pool.db() as db:
        db.execute("ALTER TABLE usage ADD COLUMN adopted REAL")
    asyncio.run(colab_sweep.run_sweep(run, groups, dict(pick, budget=100), approved=False, pool=pool, catalog=cat,
                                      refs=refs, keep=req.keep, samples=0, into_builtin=req.into_builtin,
                                      keep_minutes=10))
    assert f"这台 VM 约花 {1800 / 3600 * RATES['L4']:.2f} 单元" in run.log_path.read_text(encoding="utf-8")


def test_warm_quote_only_for_the_segment_that_adopts():
    secs = [3600.0, 3600.0]
    rate = RATES["L4"]
    budget_one = max(0.25, (3600 + 600) / 3600 * rate) + 0.01        # 每个账号只付得起一组（带冷启动准备）
    plan = scheduler.segments(secs, [budget_one, budget_one], rate, 600, 60, 1)
    assert [(ix, sec) for ix, sec, _ in plan] == [([0], 3660), ([1], 4200)]
    warm_all = scheduler.segments(secs, [budget_one, budget_one], rate, 600, 60, 2)
    assert [sec for _, sec, _ in warm_all] == [3660, 3660]


def test_sweep_replans_cold_when_kept_vm_cannot_be_adopted(ctl, tmp_path):
    """计划按接手留机（热启动）报价，但留机接不上：按新开卡重新分段、带冷启动的预计时长开卡。接上了就不开新卡。"""
    lib, cat, refs = ctl
    req = request(samples=0)
    pick = chosen(req, cat)
    pick = dict(pick, cold_prep_sec=600.0, warm_prep_sec=60.0, budget=100)
    cold = scheduler.segments(pick["group_sec"], [1000.0], RATES["L4"], 600.0)

    class WarmPool(Pool):
        adopted = None

        def idle_vms(self):
            return {"L4": 1}

        def adopt(self, gpu, *, task_id, estimated_minutes, max_minutes=None, approved=False):
            self.asked = estimated_minutes
            return self.adopted
    for adopted in (False, True):
        groups = colab_sweep.groups(request(samples=0, max_lines=20 + adopted), cat)
        run = job_run("sweep", f"carol-warm{adopted:d}")
        run.job.sweep_result = SweepResult(groups=[g for g, _ in groups])
        vm = SweepVM(tmp_path / f"vm{adopted:d}")
        pool = WarmPool(*([] if adopted else [vm]))
        if adopted:
            vm.name, pool.adopted = "kept", vm
            with pool.db() as db:
                db.execute("INSERT INTO usage VALUES ('kept','L4',?,NULL,NULL)", (pool.clock(),))
        asyncio.run(colab_sweep.run_sweep(run, groups, pick, approved=False, pool=pool, catalog=cat, refs=refs,
                                          keep=req.keep, samples=0, into_builtin=req.into_builtin))
        assert pool.asked == pytest.approx((cold[0][1] - 540) / 60)  # 问接手时按热启动的那一段
        assert pool.calls == ([] if adopted else [pytest.approx(cold[0][1] / 60)])
        assert vm.stopped
