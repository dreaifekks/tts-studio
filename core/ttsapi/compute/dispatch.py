"""提交任务时选设备：按 scheduler 的推荐（或请求里的 compute 指定的设备），检查提醒线，记下任务派到了哪台。

生成和评测在本机或局域网设备上跑：synth.EXEC 设成那台设备的执行器，synth 里调 worker 都经它走。
评测和训练在云端跑：交给 ttsapi.compute.colab_jobs.run_colab。
"""
import time

from ttsapi import jobs
from ttsapi.compute import registry, scheduler
from ttsapi.compute.remote import LOCAL, Remote
from ttsapi.models import ComputeChoice, JobCompute


class PlanError(Exception):
    """提交时不能按这个设备开始：status 409，code 是 confirm_required（花费超过提醒线，要带 approved=true 和确认的设备、单元再提交）
    或 compute_unavailable（指定的设备现在不能用），plan 是 POST /plan 的结果，给界面弹窗用。"""

    def __init__(self, code: str, detail: str, plan: dict):
        super().__init__(detail)
        self.status, self.code, self.detail, self.plan = 409, code, detail, plan


async def plan(task: scheduler.Task, pick: str = None) -> dict:
    devices, auto = await registry.snapshot()
    return scheduler.plan(devices, task, auto, registry.month_used(), pick)


async def choose(task: scheduler.Task, choice: ComputeChoice = None) -> dict:
    """返回选中的候选（device、type、gpu、seconds、units……；budget 是确认过的单元：要先问时是确认时弹窗里的
    approved_units，不用问时是这次的估算）；不能开始时抛 PlanError。choice：请求里的 compute、approved……
    要先问时，确认的设备和这次选中的不同、或这次的单元超过确认的（比如等确认时余额变了要多分一台），照样 confirm_required。"""
    choice = choice or ComputeChoice()
    compute = choice.compute
    p = await plan(task, compute)
    chosen = next((c for c in p["candidates"] if c["device"] == p["chosen"]), None)
    # 本机没测过本来就可用（估不出时间）；指定了局域网设备时「没测过」也不挡；云端卡估不出时间就估不出花费，照样挡
    if chosen is None or not (chosen["available"] or (compute and chosen["reason_code"] == "untested"
                                                      and chosen["type"] != "cloud")):
        why = chosen["reason"] if chosen else (p["reason"] if p["chosen"] is None else f"没有设备 {compute}")
        raise PlanError("compute_unavailable", f"不能在{chosen['name'] if chosen else '这台设备'}上跑：{why}", p)
    chosen["budget"] = chosen["units"]
    if p["confirm"]:
        if not (choice.approved and choice.approved_device == chosen["device"] and choice.approved_units is not None
                and chosen["units"] <= choice.approved_units):
            seen = f"（确认的是 {choice.approved_device} {choice.approved_units:g} 单元）" \
                if choice.approved and choice.approved_units is not None else ""
            raise PlanError("confirm_required", f"这次在{chosen['name']}上约花 {chosen['units']:g} 单元{seen}，"
                                                f"超过了提醒线（{', '.join(p['confirm'])}），确认后带 approved=true、"
                                                f"approved_device、approved_units 再提交", p)
        chosen["budget"] = choice.approved_units
    chosen["plan_reason"] = p["reason"] if p["chosen"] == p["recommended"] else "手动选择"
    chosen["plan_reason_code"] = p["reason_code"] if p["chosen"] == p["recommended"] else "picked"
    ensure_not_paused(chosen, p)
    return chosen


def ensure_not_paused(chosen: dict, plan_doc: dict = None):
    """Synchronously re-check paused state (no await) right before starting or assigning a job."""
    if chosen["device"] in registry.load()["paused"]:
        raise PlanError("compute_unavailable", f"不能在{chosen['name']}上跑：已暂停", plan_doc)


async def prepare(chosen: dict, stage=None):
    """派任务前：选中的是睡着、配了唤醒的局域网设备（候选的 wake）时先发唤醒包、等它上线（这期间设备状态 waking）。
    stage：任务里显示阶段用。超时算离线：抛 PlanError（compute_unavailable，plan 为 null），之后一段时间不再推荐它。"""
    if not chosen.get("wake"):
        return
    if stage:
        stage(f"唤醒{chosen['name']}中")
    try:
        await registry.wake(chosen["device"])
    except registry.WakeTimeout as e:
        raise PlanError("compute_unavailable", str(e), None) from None


def executor(chosen: dict):
    """本机或局域网设备的执行器（云端不走这里）。"""
    return LOCAL if chosen["type"] == "local" else Remote(registry.lan_device(chosen["device"]))


def assign(job, chosen: dict):
    """任务派到了这台：记进 job.compute（界面上标在哪台跑）和排队估算。登记前同步复核暂停配置。"""
    if chosen["device"] in registry.load()["paused"]:
        if (run := jobs.runs.get(job.id)) is not None and run.task is not None and not run.task.done():
            run.cancel_requested = True
            run.task.cancel()
        raise PlanError("compute_unavailable", f"不能在{chosen['name']}上跑：已暂停", None)
    job.compute = JobCompute(device=chosen["device"], name=chosen["name"], type=chosen["type"],
                             estimated_sec=chosen["seconds"], units=chosen["units"],
                             reason_code=chosen["plan_reason_code"], reason=chosen["plan_reason"])
    for done in [j for j in registry.assigned if j not in jobs.runs]:
        del registry.assigned[done]
    registry.assigned[job.id] = (chosen["device"], time.time() + (chosen["seconds"] or 0))
