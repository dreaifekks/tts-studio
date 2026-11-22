"""「在哪跑」：估时公式、推荐规则 0–5、提醒线（设计第 3 节）。纯函数，设备状态由 registry.snapshot() 给。

估时 = 排队 + 准备 + 句数 × 每句秒数（评测再加每条打分秒数）；
训练 = 排队 + 准备 + 训练句数 × 轮数折算 × 训练每句秒数 + 训后逐组评测条数 ×（每句秒数 + 每条打分秒数）。
准备：本机或局域网，模型已加载 0，没加载 15 秒；云端新开一台 = 开卡、装环境 CLOUD_BOOT 秒 + 上传输入（Task.upload_mb ÷
参考表的上传速度），不知道上传多少时按 360 秒。
云端单元 = max(0.25, 预计小时数 × 每小时单元)；账号池合计余额不够时这张卡不可用（insufficient_balance）。
云端有做完先别关的同型号 VM（warm）时不算开卡、装环境；预计超过设置里的「最长用多久」时这张卡不可用（over_max_minutes）。
睡着、配了唤醒的局域网设备（wakeable）照样是候选，准备时间加 WAKE_SEC，派任务前先唤醒（候选的 wake=true）。
"""
import math
from dataclasses import dataclass, field
from typing import Optional

from ttsapi.compute.speeds import REFERENCE, engine_mem_gb

PREP_COLD, PREP_CLOUD = 15, 360
CLOUD_BOOT = 210        # 云端开卡 + 装环境、下底模（607a8ebf 实测装环境约 3.2 分钟）
WARM_PREP = 60          # 接手做完先别关的 VM：清上次的目录、核对环境
WAKE_SEC = 30           # 唤醒一台睡着的局域网设备
UPLOAD_MBPS = REFERENCE["cloud_upload_mb_per_sec"]
LOCAL_ENOUGH = 120     # 规则 2：本机预计不超过 2 分钟
LAN_FACTOR = 1.5       # 规则 3：局域网比本机至少快一半……
LAN_SAVE = 60          # ……并且能省 1 分钟以上
BIG = 900              # 规则 4：本机和局域网最快的也要 15 分钟以上……
CLOUD_FACTOR = 0.5     # ……云端能把时间砍掉一半以上
TRAIN_GPU = "L4"       # 规则 1 和 4 用的卡
CLOUD_KINDS = ("gen", "eval", "train")  # Cloud-eligible task kinds
LAN_KINDS = ("gen", "eval")      # 能派到局域网设备的任务
SOURCES = ("untested", "reference", "measured")
CAP_LABELS = {"aux_refs": "辅助参考", "fragment_interval": "分段间隔"}


@dataclass
class Task:
    kind: str                                   # gen（生成、候选、对比、作品、盲听出题）/ eval / train
    parts: list = field(default_factory=list)   # [(引擎, 句数)]；对比是多个引擎
    lines: int = 0                              # 训练：训练数据句数
    epochs: float = 1.0                         # 训练：轮数相对默认参数（训练每句秒数是按默认轮数测的）的倍数
    cloud_only: bool = False                    # 只能在云端 GPU 上跑（扫参）
    cloud_capable: bool = True                  # Whether this task endpoint supports cloud execution
    groups: Optional[list] = None               # 扫参：每组一个训练任务；云端按分段计划（segments）估时、算单元
    upload_mb: Optional[float] = None           # 云端：要上传的输入（数据集、权重）MB，开机准备按它算
    cloud_error: Optional[str] = None           # 云端要上传的输入本机缺（说明）：只有云端候选不可用
    required_caps: tuple = ()                   # Optional LAN worker capabilities required by this task

    @property
    def engines(self):
        return [e for e, _ in self.parts]


def units(seconds: float, rate: float) -> float:
    return round(max(0.25, seconds / 3600 * rate), 2)


def _work(dev: dict, task: Task):
    """这台设备做这个任务的工作秒数（不含排队、准备）。"""
    speeds, scored = dev["speeds"], task.kind in ("eval", "train")  # 训练的 parts 是训后评测的条数
    work = sum(n * (speeds[e][0] + (speeds["scorer"][0] if scored else 0)) for e, n in task.parts)
    return work + (task.lines * task.epochs * speeds["train"][0] if task.kind == "train" else 0)


def prep_cloud(task: Task, warm: bool = False) -> float:
    """云端新开一台的准备秒数：开卡、装环境，加上传输入；不知道上传多少时 PREP_CLOUD。
    warm：接手做完先别关的 VM，开卡、装环境换成 WARM_PREP。"""
    boot = WARM_PREP if warm else CLOUD_BOOT
    return PREP_CLOUD - CLOUD_BOOT + boot if task.upload_mb is None else boot + task.upload_mb / UPLOAD_MBPS


def segments(secs: list, balances: Optional[list], rate: float, prep: float = PREP_CLOUD, warm_prep: float = None,
             warm: int = 0):
    """扫参在云端分几台 VM 跑（和执行时 colab_sweep 的做法、账号池的扣费一样）：每台给余额最多的账号，从排在前面的组起
    放它付得起的（开机准备 + 这些组的花费 max(0.25, 小时 × 每小时单元)），付完从它的余额里扣掉，剩下的下一台。
    secs：每组的工作秒数；balances：能开这种卡的各账号余额（None：不知道，当作不限）；prep：每台新开卡的开机准备秒数；
    warm 台（有做完先别关的 VM 可以接手）的准备秒数是 warm_prep，其余都按新开卡算。
    返回 [(组下标列表, 这台秒数, 这台单元)]；有一组谁都付不起时 None。"""
    bal = list(balances) if balances is not None else [math.inf]

    def cost(sec):
        return max(0.25, sec / 3600 * rate)
    out, i = [], 0
    while i < len(secs):
        best = max(bal, default=0)
        j, sec = i, warm_prep if len(out) < warm else prep
        while j < len(secs) and cost(sec + secs[j]) <= best:
            sec += secs[j]
            j += 1
        if j == i:
            return None
        out.append((list(range(i, j)), sec, cost(sec)))
        bal[bal.index(best)] -= cost(sec)
        i = j
    return out


def estimate(dev: dict, task: Task) -> dict:
    """一台设备做这个任务：seconds、units、speed_source，云端还有 prep_sec（开始干活前准备多久，含在 seconds 里），
    以及不能用时的 reason_code / reason（规则 0）。能算出时间时即使不能用也给出时间（界面上灰掉但仍显示多久、多少单元）。
    扫参（task.groups）在云端：按 segments 分台，每台各算一次开机准备和花费（vms 台；prep_sec 是第一台的；
    segments、group_sec、cold_prep_sec、warm_prep_sec 给执行时用）。"""
    asleep = bool(dev.get("wakeable")) or dev["status"] == "waking"
    out = dict(device=dev["id"], name=dev["name"], type=dev["type"], gpu=dev.get("gpu"), seconds=None, units=None,
               speed_source=None, available=False, reason_code=None, reason=None, wake=asleep)
    speeds = dev["speeds"]
    scored = task.kind in ("eval", "train")
    keys = (["train"] if task.kind == "train" else []) + task.engines + (["scorer"] if scored else [])
    plan = ()  # 扫参的分段计划；() 是不用分段，None 是付不起
    if all(speeds.get(k, (None,))[0] is not None for k in keys):
        prep = prep_cloud(task, bool(dev.get("warm"))) if dev["type"] == "cloud" else \
            (0 if set(task.engines) <= set(dev["loaded"]) else PREP_COLD) + (WAKE_SEC if asleep else 0)
        out["seconds"] = round(dev.get("queue_sec", 0) + prep + _work(dev, task))
        out["speed_source"] = min((speeds[k][1] for k in keys), key=SOURCES.index)
        if dev["type"] == "cloud":
            out["prep_sec"] = round(prep)
        if dev["type"] == "cloud" and dev["rate"] is not None:
            out["units"] = units(out["seconds"], dev["rate"])
            if task.groups is not None:
                secs = [_work(dev, g) for g in task.groups]
                # 有留着的 VM 时只有能接手它的那几台按热启动算，其余新开卡
                cold, warm_prep = prep_cloud(task), prep_cloud(task, True)
                plan = segments(secs, dev.get("balances"), dev["rate"], cold, warm_prep, int(dev.get("warm") or 0))
                out.update(group_sec=secs, cold_prep_sec=cold, warm_prep_sec=warm_prep)
                if plan is not None:
                    out.update(segments=[ix for ix, _, _ in plan], vms=len(plan),
                               seconds=round(dev.get("queue_sec", 0) + sum(sec for _, sec, _ in plan)),
                               units=round(sum(u for _, _, u in plan), 2))
    else:
        out["speed_source"] = "untested"
    code, reason = unavailable(dev, task, keys)
    if code is None and dev["type"] == "cloud" and dev.get("max_minutes") and out["seconds"] is not None:
        longest = max((sec for _, sec, _ in plan), default=0) if plan else out["seconds"]
        if longest > dev["max_minutes"] * 60:
            code, reason = "over_max_minutes", \
                f"预计 {_minutes(longest)}，超过了「最长用多久」{dev['max_minutes']} 分钟"
    # 云端：账号池里能开这种卡的账号余额不够这次的花费，开不出来（扫参按分段计划，别的任务按合计余额）
    if code is None and plan is None:
        code, reason = "insufficient_balance", "账号池里能开这种卡的账号余额按每台 VM 分段也付不起整批"
    elif code is None and task.groups is None and out["units"] is not None and dev.get("hours") is not None \
            and out["units"] > dev["hours"] * dev["rate"]:
        code, reason = "insufficient_balance", \
            f"账号池合计余额约 {dev['hours'] * dev['rate']:.1f} 单元，不够这次约 {out['units']:g} 单元"
    out.update(available=code is None, reason_code=code, reason=reason)
    return out


def unavailable(dev: dict, task: Task, keys: list):
    """规则 0：(理由代码, 说明)；能用时 (None, None)。"""
    status = dev["status"]
    if dev.get("wakeable") or status == "waking":
        status = "idle"  # 睡着、配了唤醒：派任务前先唤醒
    if dev["type"] == "cloud":
        if dev.get("paused") or status == "paused":
            return "paused", dev.get("status_reason") or "已暂停"
        if status in ("offline", "unavailable") or dev.get("status_reason"):
            code = "offline" if status == "offline" else "unavailable"
            return code, dev.get("status_reason") or {"offline": "离线", "unavailable": "暂时不能用"}[code]
    elif status in ("offline", "paused", "training", "unavailable"):
        return status, dev.get("status_reason") or {"offline": "离线", "paused": "已暂停", "training": "正在训练",
                                                     "unavailable": "暂时不能用"}[status]
    if task.cloud_only and dev["type"] != "cloud":
        return "cloud_only", "只在云端 GPU 上跑"
    if dev["type"] == "cloud" and (task.kind not in CLOUD_KINDS or not task.cloud_capable):
        return "kind_unsupported", "云端 GPU 暂支持合成、评测和训练"
    if dev["type"] == "lan" and task.kind not in LAN_KINDS:
        return "kind_unsupported", "局域网设备只跑生成和评测"
    if dev["type"] == "lan" and task.required_caps:
        have = set(dev.get("caps") or ())
        missing_caps = [c for c in task.required_caps if c not in have]
        if missing_caps:
            labels = "/".join(CAP_LABELS.get(c, c) for c in missing_caps)
            return "worker_outdated", f"局域网设备版本较旧，请更新后再用{labels}"
    if dev["type"] == "cloud" and dev["rate"] is None:
        return "no_rate", "这种卡没有报价，估不出花费"
    if dev["type"] == "cloud" and task.cloud_error:
        return "cloud_inputs_missing", task.cloud_error
    # 能跑哪些引擎（dev["engines"]：本机装好的、局域网设备报的、云端 VM 能装的）：展示、推荐、派发都按它
    if missing := [e for e in task.engines if e not in dev["engines"]]:
        return "engine_missing", f"{'云端不支持' if dev['type'] == 'cloud' else '没装'} {', '.join(missing)}"
    if dev["type"] != "cloud" and task.kind != "train":
        # 统一内存或纯 CPU 才按内存挡；CUDA 设备（ram_gb 为 None）看的是显存，这里不挡
        if dev["ram_gb"] is not None and \
                (need := max((engine_mem_gb(e) for e in task.engines if engine_mem_gb(e) > dev["ram_gb"]), default=None)):
            return "not_enough_memory", f"内存不够（约要 {need:g}GB）"
    # 本机没测过、参考表里也没有：照样能用，只是估不出时间（新装的机器测速前也能生成）
    if dev["type"] != "local" and any(dev["speeds"].get(k, (None,))[0] is None for k in keys):
        return "untested", "没测过，也没有参考值"
    return None, None


def _minutes(sec):
    return f"约 {max(1, round(sec / 60))} 分钟" if sec >= 90 else f"约 {round(sec)} 秒"


def recommend(cands: list, task: Task, gpu: str = TRAIN_GPU):
    """推荐规则 1–5，返回 (设备 id, 理由代码, 理由)；没有能用的设备时 (None, "no_device", ...)。
    gpu：规则 1 和 4 用的卡（设置里的「开哪张卡」，自动时是 L4）。"""
    ok = [c for c in cands if c["available"]]
    local = next((c for c in ok if c["type"] == "local"), None)
    near = [c for c in ok if c["type"] != "cloud" and c["seconds"] is not None]  # 本机没测过时估不出时间，不比
    cloud = [c for c in ok if c["type"] == "cloud" and c["gpu"] == gpu]
    allow_auto_cloud = task.kind != "gen"
    if task.kind == "train" and cloud:
        return cloud[0]["device"], "train_needs_gpu", f"训练要 GPU，用{cloud[0]['name']}"
    if local and local["seconds"] is None:
        return local["device"], "local_untested", f"{local['name']}没测过这个引擎的速度，估不出时间，先在{local['name']}上跑"
    if local and local["seconds"] <= LOCAL_ENOUGH:
        return local["device"], "local_fast_enough", f"{local['name']}{_minutes(local['seconds'])}出完，用不着别的机器"
    if local:
        lan = [c for c in near if c["type"] == "lan" and c["idle"] and c["seconds"] * LAN_FACTOR <= local["seconds"]
               and local["seconds"] - c["seconds"] >= LAN_SAVE]
        if lan:
            best = min(lan, key=lambda c: c["seconds"])
            return best["device"], "lan_faster", \
                f"{best['name']}空闲，比{local['name']}快 {local['seconds'] / best['seconds']:.1f} 倍"
    # 规则 4：本机和局域网设备里最快的也要 15 分钟以上，云端 L4 能把它砍掉一半以上（生成类不自动上云，需手动选择）
    quickest = min(near, key=lambda c: c["seconds"], default=None)
    if allow_auto_cloud and cloud and quickest and quickest["seconds"] >= BIG \
            and cloud[0]["seconds"] <= quickest["seconds"] * CLOUD_FACTOR:
        c = cloud[0]
        return c["device"], "cloud_halves", \
            f"{quickest['name']}{_minutes(quickest['seconds'])}，{c['name']} {_minutes(c['seconds'])} · {c['units']:g} 单元"
    # 规则 5：本机能用就是本机（换到局域网设备只按规则 3）；本机不能用时是局域网里最快的
    fastest = local or quickest
    if fastest:
        return fastest["device"], "fastest", f"{fastest['name']}最快"
    if allow_auto_cloud and ok:
        best = min(ok, key=lambda c: c["seconds"])
        return best["device"], "only_cloud", f"只有{best['name']}能跑"
    if not allow_auto_cloud and any(c["type"] == "cloud" for c in ok):
        return None, "no_device", "没有可用的本地或局域网设备（可点「更改」手动选择云端 GPU）"
    return None, "no_device", "没有能跑这个任务的设备"


def confirm_reasons(cand: dict, auto: dict, month_used: float) -> list:
    """用云端时要不要先问：一次超过 ask_over 单元、本月合计超过 ask_month（设成 0 = 每次都问）、高费率卡。
    只有能派发的云端卡（有报价、估得出时间）才算花费；不能用的卡提交时按 compute_unavailable 挡。"""
    if cand is None or cand["type"] != "cloud" or not cand["available"]:
        return []
    out = []
    if auto["ask_over"] == 0 or cand["units"] > auto["ask_over"]:
        out.append("over_task")
    if auto["ask_month"] == 0 or month_used + cand["units"] > auto["ask_month"]:
        out.append("over_month")
    if cand.get("premium"):
        out.append("premium_gpu")
    return out


def plan(devices: list, task: Task, auto: dict, month_used: float = 0.0, pick: str = None) -> dict:
    """推荐项和候选列表。auto：{"mode": "auto"|"fixed", "device", "ask_over", "ask_month"}。
    pick：提交时指定的设备（只影响 chosen 和是否要先问）。"""
    cands = []
    for dev in devices:
        c = estimate(dev, task)
        c.update(idle=dev["status"] == "idle" or c["wake"], premium=dev.get("premium", False))
        cands.append(c)
    if auto["mode"] == "fixed":
        pinned = next((c for c in cands if c["device"] == auto["device"]), None)
        cands = [pinned] if pinned else []
        rec, code, reason = (pinned["device"] if pinned and pinned["available"] else None), "pinned", "已固定"
    else:
        pref = auto.get("gpu_pref", "auto")
        rec, code, reason = recommend(cands, task, TRAIN_GPU if pref == "auto" else pref)
    chosen_id = pick or rec
    chosen = next((c for c in cands if c["device"] == chosen_id), None)
    return dict(recommended=rec, reason_code=code, reason=reason, chosen=chosen_id, candidates=cands,
                confirm=confirm_reasons(chosen, auto, month_used), month_used=round(month_used, 2),
                ask_over=auto["ask_over"], ask_month=auto["ask_month"])
