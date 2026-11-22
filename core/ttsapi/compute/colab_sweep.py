"""扫参（POST /train/sweep）：角色 × 引擎 × 训练参数，在同一台云端 VM 上依次训练、训后评测，每组训完就取回、写进资料库。

- 展开（groups）：每组一个 TrainRequest，slug = <角色>-<引擎>-s<SoVITS 轮数>g<GPT 轮数>-<其余参数的短哈希>。资料库里已有同样参数（KEY）的 run
  的组标成 skipped，不再训练：重复提交同一批，或上次被回收、失败后重新提交，只跑还没进资料库的组。
- 估时（task）：要跑的组每组一个训练任务；云端按账号余额分段（scheduler.segments：分几台 VM，每台各算一次开机准备和花费），
  POST /train/sweep/plan、提交时的提醒线和余额判断、执行时每台 VM 跑哪些组都用同一个分段算法。
- 跑（run_sweep）：一台 VM 上传一次输入（这些组用到的资料库子集和代码）、装一次环境，然后每组：
  vm_run.py <序号> 训练 + 训后评测 + 样本 → vm_pack.py <序号> 只打这一组的 run 和它前 keep 个组合的权重 → 取回 →
  importer 写进资料库（这一组的训练任务记录 <任务 id>-<组序号>、权重、评测、样本）。
- 续跑：VM 中途没了（被回收）时，已经写进资料库的组不重跑，剩下的组开一台新 VM 接着跑，最多续 VMS - 1 次。
  一组训练失败不影响后面的组；最后有没跑完或失败的组时任务失败，重新提交同一批只跑这些组。
- 取消：等手头这一步（上传一个文件、下载、写库）做完，释放 VM。
"""
import asyncio
import hashlib
import json
import tempfile
import threading
import time
import uuid
from pathlib import Path

from ttsapi import library, settings, training
from ttsapi.compute import colab_jobs, registry, scheduler
from ttsapi.compute.colab import RunState
from ttsapi.compute.colab.cli import CliError
from ttsapi.compute.colab.job import SCRIPTS, VM_SCRIPTS, reset_reused
from ttsapi.importer import job_record
from ttsapi.models import ComboEval, SweepGroup, TrainRequest

VMS = 3          # 一个扫参任务里 VM 中途没了最多几次（再没了就停，剩下的组重新提交时接着跑）
POLL_SEC = 30    # 等 VM 上的命令时多久查一次
# 样本（盲听素材）：hard 集里的 5 句（长句、多音读法、愤怒、哭腔、口语缩约）
SAMPLE_TEXTS = [settings.EVAL_SETS["hard"]["sentences"][i]["text"] for i in (0, 3, 6, 7, 10)]
# 判断「资料库里已有这一组」：run.json 的 params（VM 上的 TrainRequest）里这些字段都一样
KEY = ("slug", "dataset_id", "engine", "sovits_epochs", "sovits_save_every", "gpt_epochs", "gpt_save_every",
       "sovits_batch", "gpt_batch", "min_sec", "max_sec", "exclude_interjections", "max_lines")


def _norm_lora_rank(engine: str, lora_rank):
    """Normalize lora_rank: LoRA engines default None -> 32; full-param engines always None."""
    td = settings.TRAIN_DEFAULTS.get(engine)
    if not td or not td["lora"]:
        return None
    return 32 if lora_rank is None else int(lora_rank)


def _key(params: dict) -> tuple:
    return (
        *(params.get(k) for k in KEY),
        _norm_lora_rank(params.get("engine") or "", params.get("lora_rank")),
    )


def key(tr: TrainRequest) -> tuple:
    """一组的完整参数键：判断资料库里有没有、同样的组是不是正在别的扫参任务里跑，都按它。"""
    return _key(tr.model_dump())


def groups(req, catalog) -> list:
    """展开成 [(SweepGroup, TrainRequest)]，参数一样的只留一组；资料库里已有同样参数的 run 的组 status=skipped。"""
    chars = catalog.characters()
    done = {_key(r.get("params") or {}): r for r in catalog.runs()}
    out, seen = [], set()
    for c in req.characters:
        if c not in chars:
            raise KeyError(f"角色 {c}")
        for e in req.engines:
            for t in req.trains:
                if t.engine not in (None, e):
                    continue
                norm_rank = _norm_lora_rank(e, t.lora_rank)
                params = {**t.model_dump(exclude={"engine"}), "lora_rank": norm_rank}
                fields = dict(dataset_id=chars[c]["dataset_id"], engine=e, min_sec=req.min_sec, max_sec=req.max_sec,
                              exclude_interjections=req.exclude_interjections, max_lines=req.max_lines,
                              ref_id=req.ref_ids.get(c), **params)
                # Preserve legacy slug hashes for default LoRA rank (32) and full-param engines;
                # append non-default LoRA rank so different ranks get distinct slugs.
                hash_vals = [fields.get(x) for x in KEY[1:]]
                if norm_rank not in (None, 32):
                    hash_vals.append(norm_rank)
                rest = hashlib.sha256(json.dumps(hash_vals).encode()).hexdigest()[:6]
                tr = TrainRequest(slug=f"{c}-{e}-s{t.sovits_epochs}g{t.gpt_epochs}-{rest}", **fields)
                k = key(tr)
                if k in seen:
                    continue
                seen.add(k)
                g = SweepGroup(slug=tr.slug, character=c, engine=e, status="pending", **params)
                if r := done.get(k):
                    g.status, g.run_name, g.job_id, g.kept = "skipped", r["run_name"], r["job_id"], r["gpt"] + r["sovits"]
                out.append((g, tr))
    return out


def task(pending: list, lines: dict, samples: int) -> scheduler.Task:
    """要跑的组的估时任务：合起来的训练任务（引擎、非云端设备的估时），groups 是每组一个（训练、训后评测、样本），
    云端按它分段（scheduler.segments）：每台 VM 各算一次开机准备和花费。lines：数据集 id → 过滤、抽取后的训练句数。"""
    groups = []
    for _, tr in pending:
        t = training.train_task(tr, lines[tr.dataset_id])
        groups.append(scheduler.Task("train", t.parts + ([(tr.engine, samples * len(SAMPLE_TEXTS))] if samples else []),
                                     lines=t.lines, epochs=t.epochs))
    return scheduler.Task("train", [x for g in groups for x in g.parts], lines=sum(g.lines * g.epochs for g in groups),
                          epochs=1.0, cloud_only=True, groups=groups)


# ---------------------------------------------------------------- VM 上的步骤（任务线程里跑）

class Cancelled(Exception):
    """任务被取消：线程里的等待提前结束。"""


async def _step(stop: threading.Event, fn, vm, *args):
    """阻塞的一步 fn(vm, *args) 放进线程；任务被取消时通知它尽早结束（stop）、控制协程马上释放 VM（线程里正在传的
    文件随之失败），等线程返回再把取消抛出去。"""
    fut = asyncio.ensure_future(asyncio.to_thread(fn, vm, *args))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        stop.set()
        await asyncio.to_thread(vm.stop)
        await asyncio.wait({fut})
        if not fut.cancelled():
            fut.exception()  # 线程里的 Cancelled：取走，不当成没处理的异常报
        raise


def _background(vm, stop, cmd: str, marker: str) -> RunState:
    vm.run_background(f"cd /content/job && {cmd}", marker, heartbeat="/content/job/job.json")
    while (state := vm.poll()) == RunState.RUNNING:
        if stop.wait(POLL_SEC):
            raise Cancelled
    return state


def _upload(vm, stop, spec: dict, inputs: dict):
    """同 ttsapi.compute.colab.run_job 的上传：输入放 /content/tts-inputs-<随机 id>/，脚本和 job.json 放 /content/job/。"""
    mirror = f"/content/tts-inputs-{uuid.uuid4().hex}"
    reset_reused(vm)
    for rel, local in inputs.items():
        if stop.is_set():
            raise Cancelled
        vm.upload(local, f"{mirror}/{rel}")
    for name in SCRIPTS:
        vm.upload(VM_SCRIPTS / name, f"/content/job/{name}")
    with tempfile.TemporaryDirectory() as folder:
        f = Path(folder) / "job.json"
        f.write_text(json.dumps(dict(spec, inputs=mirror)))
        vm.upload(f, "/content/job/job.json")


def _fetch(vm, stop, token: str, tag: str, index, local: Path) -> RunState:
    """vm_pack.py 打包（index：只打这个任务训出的 run；None：全部）并取回。下载失败时 VM 还开着，隔 RESULT_RETRY_SEC 秒
    重试，最多 RESULT_RETRIES 次（colab_jobs 的）。返回打包的状态（gone 时没取回）。"""
    remote = f"/content/results-{token}-{tag}.tar"
    state = _background(vm, stop, f"rm -f /content/results-{token}-*.tar && "
                                  f"python3 vm_pack.py {remote}{'' if index is None else f' {index}'}",
                        f"/content/job/{token}-pack-{tag}.exit")
    if state != RunState.DONE:
        return state
    for i in range(colab_jobs.RESULT_RETRIES + 1):
        try:
            vm.download(remote, local, timeout=1800)
            return state
        except CliError:
            if i == colab_jobs.RESULT_RETRIES:
                raise
        if stop.wait(colab_jobs.RESULT_RETRY_SEC):
            raise Cancelled


# ---------------------------------------------------------------- 任务

async def run_sweep(run, groups: list, chosen: dict, *, approved: bool, pool, catalog, refs, keep: int, samples: int,
                    into_builtin: bool, max_minutes: int = None, keep_minutes: int = 0):
    """跑 groups（groups() 的结果；run.job.sweep_result.groups 是同一批 SweepGroup，边跑边更新）里 pending 的组。
    chosen：提交时选中的云端卡（scheduler.estimate 的结果：group_sec 是每组的工作秒数，cold_prep_sec 是每台新开卡的开机准备，
    warm_prep_sec 是接手留机的；budget 是确认过的整批花费，dispatch.choose 给）。
    每台 VM 开之前按当时的账号余额重新分段（scheduler.segments，和 /train/sweep/plan 同一个算法），这台只跑第一段；
    正常跑完的 VM 按实际花的（_spent）从预算里扣。剩下的组按现在的分段要花的超过预算还没花的部分时停下：已完成的组
    留在资料库里，任务失败、sweep_result.confirm_required，重新提交同一批再确认（已完成的组跳过）。
    VM 中途没了：这台没跑完的组下一台接着跑（这台花的不扣，自动续跑不再问），这样最多续 VMS - 1 次。
    max_minutes、keep_minutes：设置里的「最长用多久」「做完先别关」（每台 VM 正常跑完时留着，下一段或下一个任务接手）。"""
    result, gpu = run.job.sweep_result, chosen["gpu"]
    did = chosen.get("device") or f"colab-{gpu}"
    registry.assigned.setdefault(run.job.id, (did, time.time() + (chosen.get("seconds") or 0)))
    try:
        pending = [i for i, (g, _) in enumerate(groups) if g.status == "pending"]
        sec = dict(zip(pending, chosen["group_sec"], strict=True))
        budget, gone = chosen["budget"], 0
        warm_prep = chosen.get("warm_prep_sec", chosen["cold_prep_sec"])
        cold_only = False  # 想接手的留机没接上：这一台按新开卡重新算
        with library.pinned(library.current()):  # 结果都写进任务开始时的资料库
            while todo := [(i, g, tr) for i, (g, tr) in enumerate(groups) if g.status == "pending"]:
                # 有做完先别关的同型号 VM 时，第一台按接手它（热启动）算，其余按新开卡
                warm = 0 if cold_only else await asyncio.to_thread(lambda: pool.idle_vms().get(gpu, 0))
                segs = await asyncio.to_thread(_segments, pool, gpu, [sec[i] for i, _, _ in todo], chosen["cold_prep_sec"],
                                               warm_prep, warm)
                if segs is None:
                    raise RuntimeError(f"能开 {gpu} 的账号余额付不起剩下的 {len(todo)} 组（最前面的 {todo[0][1].slug}）")
                cost = sum(u for _, _, u in segs)
                if cost > budget + 0.01:
                    result.confirm_required = True
                    raise RuntimeError(f"剩下 {len(todo)} 组按现在的账号余额要分 {len(segs)} 台、约 {cost:.2f} 单元，"
                                       f"超过确认过的还没花的约 {budget:.2f} 单元，先停下（已完成的组留在资料库里）。"
                                       f"重新提交同一批确认（已完成的组跳过）")
                ix, seconds, units = segs[0]
                vm = None
                if warm:
                    vm = await asyncio.to_thread(pool.adopt, gpu, task_id=run.job.id, estimated_minutes=seconds / 60,
                                                 max_minutes=max_minutes, approved=approved)
                    if vm is None:  # 刚被别的任务接走、余额或剩余时长不够：按新开卡重查预算和时限
                        cold_only = True
                        continue
                cold_only = False
                batch = [todo[j] for j in ix]
                if len(segs) > 1:
                    run.log(f"按账号余额分 {len(segs)} 台：这台跑 {len(batch)} 组，剩下 {len(todo) - len(batch)} 组下一台接着跑")
                result.vms += 1
                state, spent = await _on_vm(run, batch, result.vms, gpu, seconds / 60, approved, pool, catalog, refs, keep,
                                            samples, into_builtin, max_minutes, keep_minutes, vm)
                if state == RunState.GONE:
                    gone += 1
                    if gone >= VMS:
                        break
                    run.log(f"云端 VM 没了：剩下的组开第 {result.vms + 1} 台接着跑（已写进资料库的组不重跑）")
                else:
                    budget -= spent
                    run.log(f"这台 VM 约花 {spent:.2f} 单元（估 {units:.2f}），确认过的还剩约 {budget:.2f} 单元")
        left = [g for g in result.groups if g.status in ("pending", "failed")]
        if left:
            raise RuntimeError(f"扫参有 {len(left)} 组没完成："
                               f"{'；'.join(f'{g.slug}（{g.error or g.status}）' for g in left)}。重新提交同一批只跑这些组")
    finally:
        registry.assigned.pop(run.job.id, None)


def _segments(pool, gpu: str, secs: list, prep: float, warm_prep: float = None, warm: int = 0):
    """按账号池现在的余额（能开这种卡的账号）分段，见 scheduler.segments。"""
    balances = [a.balance for a in pool.accounts(refresh=False) if gpu in a.gpus and a.balance is not None]
    return scheduler.segments(secs, balances, pool.rates()[gpu], prep, warm_prep, warm)


def _spent(pool, name: str) -> float:
    """VM name 花了多少单元（账号池账本 usage 里这台的记录）：已结算的（actual_units），账单还没入账（晚扣）时
    按已运行时长 × 费率计提，取大的。接手别的任务留下的 VM（做完先别关）时只算接手以后的（已结算的按时长比例分）。"""
    with pool.db() as db:
        r = db.execute("SELECT * FROM usage WHERE name=?", (name,)).fetchone()
    end = r["stopped"] or pool.clock()
    adopted = r["adopted"] if "adopted" in r.keys() else None
    start = adopted or r["started"]
    share = (end - adopted) / max(1.0, end - r["started"]) if adopted else 1.0
    accrued = (end - start) / 3600 * pool.rates()[r["gpu"]]
    return max((r["actual_units"] or 0.0) * share, accrued)


async def _on_vm(run, todo, n, gpu, minutes, approved, pool, catalog, refs, keep, samples, into_builtin,
                 max_minutes=None, keep_minutes=0, vm=None) -> tuple:
    """在一台 VM 上跑 todo（[(组序号, SweepGroup, TrainRequest)]）。返回 (状态, 这台花的单元)：状态是 gone（VM 中途没了、
    到了时限、传输重试后还失败：剩下的组换一台）或 done。退出时释放 VM（keep_minutes 时正常跑完的留着，花费先算好
    再开放接手）。vm：已经接手的留机（不给就开一台）。"""
    job_id, work_dir = run.job.id, settings.DATA / "colab" / run.job.id
    work_dir.mkdir(parents=True, exist_ok=True)
    stop, token = threading.Event(), uuid.uuid4().hex[:8]
    progress = lambda: 0.05 + 0.9 * sum(g.status != "pending" for g in run.job.sweep_result.groups) / \
        len(run.job.sweep_result.groups)
    with tempfile.TemporaryDirectory(prefix="ttscore-colab-") as tmp:
        tmp = Path(tmp)
        run.stage(f"准备输入：资料库子集、代码（{len(todo)} 组）", progress())

        def prepare():
            inputs = colab_jobs.export_inputs("train", [tr for _, _, tr in todo], catalog, refs, tmp)
            code_inputs, code, note = colab_jobs.bundle_code(tmp)
            tasks = [dict(kind="train", job_id=f"{job_id}-{i:02d}", **tr.model_dump(exclude={"compute", "approved"}),
                          compute="local") for i, _, tr in todo]
            spec = dict(run_id=f"{job_id}-v{n}", engines=sorted({tr.engine for _, _, tr in todo}), tasks=tasks,
                        keep=keep, samples=samples, sample_texts=SAMPLE_TEXTS, tz=colab_jobs.control_tz(), **code)
            return {**inputs, **code_inputs}, spec, note

        inputs, spec, note = await asyncio.to_thread(prepare)
        if note:
            run.log(note)
        if vm is None:
            run.stage(f"开云端 GPU（{gpu}）", progress())
            vm = await colab_jobs.start_vm(run, pool, gpu, minutes, approved, max_minutes)
        run.job.status = "running"
        run.log(f"VM {vm.name}（账号 {vm.account_id}）：{len(todo)} 组")
        state, spent = None, None
        try:
            state = await _groups_on_vm(run, vm, todo, stop, token, work_dir, progress, spec, inputs, catalog, refs,
                                        into_builtin)
        except (TimeoutError, CliError) as e:  # 到了 VM 时限、传输重试后还失败：当成 VM 没了，剩下的组换一台
            run.log(f"云端 VM 不能用了（{type(e).__name__}: {e}）")
            state = RunState.GONE
        finally:
            try:
                if keep_minutes and state == RunState.DONE:
                    # 先按这一段的占用定下花费，再标空闲：标了之后它随时可能被下一个任务接手（adopted 改成新的）
                    spent = await asyncio.to_thread(_spent, pool, vm.name)
                    if not await asyncio.to_thread(vm.keep_idle, keep_minutes):
                        spent = None
                if spent is None:
                    await asyncio.to_thread(vm.stop)
            except Exception as e:  # 释放失败由账号池的 reap 回收，不影响已写进资料库的结果和续跑
                run.log(f"释放 VM {vm.name} 失败（{type(e).__name__}: {e}），账号池稍后回收")
    return state, spent if spent is not None else await asyncio.to_thread(_spent, pool, vm.name)


async def _groups_on_vm(run, vm, todo, stop, token, work_dir, progress, spec, inputs, catalog, refs,
                        into_builtin) -> RunState:
    """上传、装环境，逐组训练、取回、写库。返回 gone（VM 中途没了）或 done。"""
    job_id = run.job.id
    run.stage("上传输入（数据集、代码）", progress())
    await _step(stop, _upload, vm, stop, spec, inputs)
    run.stage("云端装环境、下底模（约 6 分钟）", progress())
    state = await _step(stop, _background, vm, stop, "python3 vm_setup.py", f"/content/job/{token}-setup.exit")
    if state == RunState.FAILED:  # 取回日志再报错
        tar = work_dir / "results-setup.tar"
        if await _step(stop, _fetch, vm, stop, token, "setup", None, tar) == RunState.DONE:
            await asyncio.to_thread(colab_jobs.collect, tar, work_dir, "", catalog, refs)
            tar.unlink()
        raise RuntimeError(f"云端装环境失败，日志在 {work_dir / 'job'}")
    if state == RunState.GONE:
        return state
    for k, (i, g, _) in enumerate(todo):
        run.stage(f"云端训练、评测第 {i + 1}/{len(run.job.sweep_result.groups)} 组：{g.slug}", progress())
        state = await _step(stop, _background, vm, stop, f"python3 vm_run.py {k}",
                            f"/content/job/{token}-run-{k}.exit")
        if state == RunState.GONE:
            return state
        run.stage(f"取回第 {i + 1} 组：{g.slug}", progress())
        tar = work_dir / f"results-{i:02d}.tar"
        state = await _step(stop, _fetch, vm, stop, token, str(k), k, tar)
        if state == RunState.GONE:
            return state
        if state != RunState.DONE:  # 这一组记失败，接着跑下一组
            g.status, g.error = "failed", f"云端结果打包失败，日志在 VM 的 /content/job/{token}-pack-{k}.exit.log"
            run.save()
            continue
        await _collect(run, g, f"{job_id}-{i:02d}", tar, work_dir, catalog, refs, into_builtin)
    return RunState.DONE


async def _collect(run, g: SweepGroup, job_id: str, tar: Path, work_dir: Path, catalog, refs, into_builtin: bool):
    """一组的结果 tar 写进资料库，更新 g。写库时任务被取消：等写完再结束。写库失败时这一组记失败、tar 留着，接着跑下一组。"""
    write = asyncio.ensure_future(asyncio.to_thread(colab_jobs.collect, tar, work_dir, job_id, catalog, refs,
                                                    into_builtin))
    retry = f"结果留在 {tar}，可以用 POST /runs/import 重试"
    try:
        imported, summary, record, vm_log = await asyncio.shield(write)
    except asyncio.CancelledError:
        run.log(f"取消：等 {g.slug} 的结果写完资料库再结束；{retry}")
        await asyncio.wait({write})
        raise
    except Exception as e:
        g.status, g.error = "failed", f"结果没能写进资料库（{type(e).__name__}: {e}）；{retry}"
        run.save()
        return
    if vm_log:
        offset = run.log_path.stat().st_size
        with open(run.log_path, "a", encoding="utf-8") as f:
            f.write(f"---- 云端任务日志：{g.slug} ----\n{vm_log}")
        run.follow(offset)
    row = next((r for r in summary or [] if r["job"] == job_id), None)
    tr = record and record.get("train_result")
    if row and row["status"] == "succeeded" and tr and catalog.has_run(tr["run_name"]):
        rec = job_record(record)["train_result"]
        r = catalog.run(tr["run_name"])
        g.status, g.run_name, g.job_id, g.kept = "succeeded", tr["run_name"], job_id, r["gpt"] + r["sovits"]
        g.best = rec["eval"] and ComboEval(**dict(rec["eval"]["best"], per_sentence=None))
        if row.get("samples") and library.current().path(f"{row['samples']}/items.json").exists():
            g.samples = row["samples"]
        if row.get("samples_error"):
            g.error = f"样本没生成出来：{row['samples_error']}"
        tar.unlink()
    elif row is None or row["status"] != "succeeded" or not tr:
        g.status, g.error = "failed", (row or {}).get("error") or "云端没有这一组的结果"
        if tr and catalog.has_run(tr["run_name"]):  # 训完了、训后评测失败：权重已带回
            r = catalog.run(tr["run_name"])
            g.run_name, g.job_id, g.kept = tr["run_name"], job_id, r["gpt"] + r["sovits"]
            g.error += "（训练完成，权重已全部写进资料库；重新提交会跳过这一组，可以用 POST /weights/eval 补评测）"
        tar.unlink()
    else:  # 训练成功但 run 没写进资料库（缺权重、角色数据集不一致）
        g.status, g.error = "failed", f"run {tr['run_name']} 没写进资料库：{imported and imported['skipped']}；{retry}"
    run.save()
