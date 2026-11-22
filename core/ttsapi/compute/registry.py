"""主控的算力注册：本机、局域网设备（手填地址 + 配对码换来的令牌）、云端 GPU（每种卡一项，来自 ColabPool）。
存在主控本地的 COMPUTE_FILE（0600，里面有设备令牌），不进资料库：

{"devices": [{"id": "lan-xxxxxx", "name": "gpu-worker", "address": "http://192.168.1.20:9880", "token": "...",
              "hw": {...}, "machine": "gpu-worker", "added": "...", "engines": [...]（最近一次连上时报的，睡着时估时用）,
              "wake": {"mac": "aa:bb:cc:dd:ee:ff", "broadcast": null}（可选）,
              "machine_id": "..."、"last_seen": "..."（自动连回配对的：设备上 core 的本机 id、最近一次心跳的时间）,
              "pending_token": "..."（可选：重装时发的新令牌，设备带它报第一次心跳时和 token 对调，之前旧令牌照样用）}],
 "paused": ["colab-T4"],
 "local_name": "studio-host"（可选，本机在界面上的名字；不设时用机器名）,
 "auto": {"mode": "auto" | "fixed", "device": null, "ask_over": 2, "ask_month": 500},
 "cloud": {"gpu_pref": "auto" | "L4" ..., "max_minutes": null | 分钟, "keep_minutes": 0}}

snapshot() 给调度器和 GET /compute 用：每台设备的状态、能跑的引擎、硬件、已加载的引擎、排队秒数、每个引擎的速度和来源。
"""
import asyncio
import hmac
import json
import logging
import os
import secrets
import socket
import time
from pathlib import Path

import httpx

from ttsapi import jobs, settings, storage, workers
from ttsapi.compute import hw, scheduler, speeds
from ttsapi.compute.colab.pool import PREMIUM, RATES
from ttsapi.compute.remote import Remote
from ttsapi.library.store import now_iso

DEFAULT_AUTO = dict(mode="auto", device=None, ask_over=2.0, ask_month=500.0)
# 云端 GPU：开哪张卡（auto = 按推荐，训练用 L4）、最长用多久（None = 预计时间的 2 倍，30 分钟到 24 小时）、
# 做完先别关多少分钟（0 = 马上关）
DEFAULT_CLOUD = dict(gpu_pref="auto", max_minutes=None, keep_minutes=0)
GPU_LABEL = {"G4": "RTX PRO 6000"}
pool = None  # 控制端 lifespan 里装了 colab CLI 时注入 ColabPool
# 正在跑的任务在哪台设备上、预计什么时候做完：{job_id: (device_id, 预计结束时间戳)}，估排队时间用
assigned: dict = {}
_hw = None
# 局域网设备最近一次 /worker/health 的结果（或连不上的异常）：15 秒内复用，睡着的设备不会让每次 /tts 都多等 3 秒
HEALTH_TTL = 15
_health: dict = {}  # 设备 id -> (时间戳, 结果或异常)
# 唤醒：发魔术包后每 WAKE_POLL 秒查一次 /worker/health，WAKE_RESEND 秒重发一次，WAKE_TIMEOUT 秒还没上线算离线，
# 之后 WAKE_RETRY 秒内不再自动唤醒（不进推荐），状态显示唤醒超时
WAKE_TIMEOUT, WAKE_POLL, WAKE_RESEND, WAKE_RETRY = 120, 3, 15, 600
waking: dict = {}       # 设备 id -> 开始唤醒的时间戳
_wake_failed: dict = {}  # 设备 id -> 到这个时间戳之前不再自动唤醒
# 心跳：自动连回配对的设备每 30 秒报一次，OFFLINE_AFTER 秒没收到就算离线、不再去连它。记 time.monotonic()：macOS、Linux 上
# 主控睡眠时它不走，醒来后不会因为睡着的那段把设备都当成离线（Windows 上它会走：醒来后到各台下一次心跳前，最多 30 秒，
# 算离线）；主控重启后、收到这台的第一次心跳之前，照旧按连不连得上算
OFFLINE_AFTER = 90
beats: dict = {}  # 设备 id -> 最近一次心跳的 time.monotonic()


class WakeTimeout(Exception):
    pass


class BadToken(Exception):
    """心跳带的令牌不是这台设备的（接口 401）。"""


class Silent(Exception):
    """有心跳的设备 OFFLINE_AFTER 秒没报心跳。"""


def load() -> dict:
    data = json.loads(settings.COMPUTE_FILE.read_text(encoding="utf-8")) if settings.COMPUTE_FILE.exists() else {}
    data.setdefault("devices", [])
    data.setdefault("paused", [])
    data["auto"] = {**DEFAULT_AUTO, **data.get("auto", {})}
    data["cloud"] = {**DEFAULT_CLOUD, **data.get("cloud", {})}
    return data


def save(data: dict):
    path: Path = settings.COMPUTE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def lan_device(device_id: str) -> dict:
    dev = next((d for d in load()["devices"] if d["id"] == device_id), None)
    if dev is None:
        raise KeyError(f"设备 {device_id}")
    return dev


class PairError(Exception):
    """配对码不对、过期或作废（worker 的 POST /pair 回 403）。"""


async def pair(address: str, code: str) -> dict:
    """拿配对码找 worker 换长期令牌：返回 worker 的回复（token，和登记要的 machine、machine_id、hw、engines）。
    连不上时 httpx / OSError 原样抛出。"""
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.post(address.rstrip("/") + "/pair", json=dict(code=code))
    if r.status_code == 403:
        raise PairError(r.json()["detail"])
    r.raise_for_status()
    return r.json()


def upsert(machine_id: str, address: str, token: str, machine: str, hw: dict, engines: list, name: str = None,
           wake: dict = None, caps: list = None) -> dict:
    """登记配对好的设备。同一台机器（设备上 core 的本机 id）已经在列表里时更新那一项：换地址、令牌（旧令牌和重装时
    等着换上的令牌都作废）、硬件和引擎，id、暂停、唤醒、名字都留着（给了 name 时改名）；不另加一台。新设备的名字是 name，
    没给时用它报的机器名。last_seen 清掉：这次配对之后还没有心跳（手动填配对的一直没有）。machine_id 为 None（这一版之前的
    worker 不报）时不去重。"""
    data = load()
    dev = find(data, machine_id)
    if dev is None:
        dev = dict(id=f"lan-{secrets.token_hex(3)}", name=name or machine, added=now_iso())
        data["devices"].append(dev)
    elif name:
        dev["name"] = name
    dev.update(address=address.rstrip("/"), token=token, machine_id=machine_id, machine=machine, hw=hw, engines=engines)
    if caps is not None:
        dev["caps"] = list(caps)
    dev.pop("last_seen", None)
    dev.pop("pending_token", None)
    if wake:
        dev["wake"] = wake
    save(data)
    _health.pop(dev["id"], None)
    beats.pop(dev["id"], None)
    return dev


def find(data: dict, machine_id: str):
    """列表里同一台机器（设备上 core 的本机 id）的那一项，没有时 None；machine_id 为 None 时不认。"""
    return next((d for d in data["devices"] if machine_id and d.get("machine_id") == machine_id), None)


async def add_device(address: str, pair_code: str, name: str = None, wake: dict = None) -> dict:
    """手填地址和配对码加一台局域网设备：用配对码找它换令牌（令牌只存在这里，界面看不到），按它回的本机 id、机器名、硬件和
    引擎马上登记。它那边这时已经换了令牌，所以先存下来：之后查状态连不上只算离线，不当成没配上。名字（name 不给时）和
    同一台去重见 upsert。这一版之前的 worker 只回 token、machine：本机 id、硬件和引擎照旧从 /worker/health 拿（更早的
    health 里也没有本机 id，就不去重）。"""
    r = await pair(address, pair_code)
    if "machine_id" not in r:
        r.update(await Remote(dict(id="new", name=address, address=address, token=r["token"])).health(timeout=5))
    return upsert(r.get("machine_id"), address, r["token"], r["machine"], r["hw"], r["engines"], name, wake,
                  caps=r.get("caps"))


def join(machine_id: str, address: str, machine: str, hw: dict, engines: list) -> tuple:
    """自动连回的设备（POST /devices/pair，配对码已经核对过）登记进来，发一个新的长期令牌，返回 (设备, 令牌)。
    同一台已经在列表里时（重装）先不换：新令牌记成 pending_token，原来的令牌照样能用，到设备第一次带新令牌报心跳
    （新版本装好起来了）才换上（heartbeat）。重装中途失败、旧服务接着跑时，主控这边什么都没变。"""
    token = secrets.token_urlsafe(24)
    data = load()
    dev = find(data, machine_id)
    if dev is None:
        return upsert(machine_id, address, token, machine, hw, engines), token
    dev["pending_token"] = token
    save(data)
    return dev, token


def local_name() -> str:
    """本机在设备列表里的名字（改过名用改的，没改是机器名）。"""
    return load().get("local_name") or settings.MACHINE_NAME


def heartbeat(device_id: str, token: str, address: str):
    """设备报心跳：记下时间（GET /compute 的 last_seen），地址变了跟着改。带的是 pending_token（重装时发的新令牌）时
    和 token 对调：换上它，旧的留作 pending_token——新服务起来报过心跳、安装脚本却核对失败换回旧服务时，旧服务带旧令牌
    报心跳又对调回来；之后的重装、手动配对换掉它。令牌都不对 BadToken，没有这台 KeyError。
    第一次心跳（设备刚起来、刚换上新令牌）或换了地址时扔掉缓存的 /worker/health 结果，下次查状态马上重新连。"""
    data = load()
    dev = next((d for d in data["devices"] if d["id"] == device_id), None)
    if dev is None:
        raise KeyError(f"设备 {device_id}")
    if dev.get("pending_token") and hmac.compare_digest(dev["pending_token"].encode(), token.encode()):
        dev["token"], dev["pending_token"] = dev["pending_token"], dev["token"]
        beats.pop(device_id, None)
    elif not hmac.compare_digest(dev["token"].encode(), token.encode()):
        raise BadToken(device_id)
    address = address.rstrip("/")
    if device_id not in beats or address != dev["address"]:
        _health.pop(device_id, None)
    beats[device_id] = time.monotonic()
    dev.update(address=address, last_seen=now_iso())
    save(data)


def patch_device(device_id: str, **fields):
    """fields 里有 wake 时：dict 设唤醒、None 去掉唤醒；没有 wake 时不动。token 是重新配对换来的令牌：设备那边换了令牌就
    不再报心跳（同手动填配对的），心跳记录、last_seen 和重装时等着换上的令牌清掉，之后按连不连得上算。
    本机（local）只能改名（name 存成 local_name，空字符串回到机器名）和暂停。"""
    _health.pop(device_id, None)
    if fields.get("token"):
        beats.pop(device_id, None)
    _wake_failed.pop(device_id, None)
    data = load()
    if fields.get("paused") is not None:
        data["paused"] = [d for d in data["paused"] if d != device_id] + ([device_id] if fields["paused"] else [])
    if device_id == "local" and fields.get("name") is not None:
        data["local_name"] = fields["name"].strip() or None
    for d in data["devices"]:
        if d["id"] == device_id:
            d.update({k: v for k, v in fields.items() if k in ("name", "address", "token") and v is not None})
            if fields.get("token"):
                d.pop("last_seen", None)
                d.pop("pending_token", None)
            if "wake" in fields:
                d.pop("wake", None)
                if fields["wake"] is not None:
                    d["wake"] = fields["wake"]
    save(data)


def remove_device(device_id: str):
    lan_device(device_id)
    beats.pop(device_id, None)
    data = load()
    data["devices"] = [d for d in data["devices"] if d["id"] != device_id]
    data["paused"] = [d for d in data["paused"] if d != device_id]
    save(data)


def set_auto(**fields):
    data = load()
    data["auto"].update({k: v for k, v in fields.items() if v is not None or k == "device"})
    save(data)
    return data["auto"]


def cloud_settings() -> dict:
    return load()["cloud"]


def set_cloud(**fields) -> dict:
    """只改给了的字段（max_minutes 可以设成 None = 自动）。"""
    data = load()
    data["cloud"].update(fields)
    save(data)
    return data["cloud"]


# ---------------------------------------------------------------- 唤醒

def send_magic(mac: str, broadcast: str = None):
    """Wake-on-LAN 魔术包：6 个 0xFF + MAC 重复 16 次，UDP 发到广播地址的 9 端口。"""
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(b"\xff" * 6 + raw * 16, (broadcast or "255.255.255.255", 9))


async def wake(device_id: str):
    """唤醒一台配了 wake 的局域网设备，等它上线（/worker/health 通）。WAKE_TIMEOUT 秒还没上线时 WakeTimeout，
    之后 WAKE_RETRY 秒内这台按离线算。唤醒期间 GET /compute 里它的状态是 waking。"""
    dev = lan_device(device_id)
    waking[device_id] = time.time()
    try:
        deadline, resend = time.time() + WAKE_TIMEOUT, 0.0
        while time.time() < deadline:
            if time.time() >= resend:
                try:
                    send_magic(dev["wake"]["mac"], dev["wake"].get("broadcast"))
                except OSError as e:
                    logging.getLogger("tts-api").warning("wake %s: %s", device_id, e)
                resend = time.time() + WAKE_RESEND
            try:
                h = await Remote(dev).health()
            except (httpx.HTTPError, OSError):
                await asyncio.sleep(WAKE_POLL)
                continue
            _health[device_id] = (time.time(), h)
            if device_id in beats:  # 醒了：下一次心跳来之前不按「没有心跳」算离线
                beats[device_id] = time.monotonic()
            return h
        _wake_failed[device_id] = time.time() + WAKE_RETRY
        _health.pop(device_id, None)
        raise WakeTimeout(f"{dev['name']} 唤醒 {WAKE_TIMEOUT} 秒还没上线，按离线处理")
    finally:
        waking.pop(device_id, None)


def local_hw() -> dict:
    global _hw
    if _hw is None:
        _hw = hw.hardware()
    return _hw


# ---------------------------------------------------------------- 状态快照

def queue_sec(device_id: str) -> float:
    now = time.time()
    return sum(max(0, end - now) for job_id, (dev, end) in assigned.items() if dev == device_id and job_id in jobs.runs)


def _activity_from_kinds(kinds: list) -> str | None:
    if not kinds:
        return None
    if any(k in ("train", "sweep") for k in kinds):
        return "正在训练"
    if "eval" in kinds:
        return "正在评测"
    return "正在生成"


def busy_kind(device_id: str):
    """这台设备上正在跑的任务的说法（正在评测 / 正在生成 / 正在训练）；没有时 None。仅统计 status == 'running'。"""
    kinds = [
        run.job.kind
        for j, (dev, _) in assigned.items()
        if dev == device_id and (run := jobs.runs.get(j)) is not None and run.job.status == "running"
    ]
    return _activity_from_kinds(kinds)


def cloud_activities() -> dict[str, str]:
    """Snapshot active running cloud job activities on the event loop for thread-safe cloud_states()."""
    by_dev: dict[str, list[str]] = {}
    for j, (dev, _) in list(assigned.items()):
        if not dev.startswith("colab-"):
            continue
        run = jobs.runs.get(j)
        if run is not None and run.job.status == "running":
            by_dev.setdefault(dev, []).append(run.job.kind)
    return {dev: act for dev, kinds in by_dev.items() if (act := _activity_from_kinds(kinds))}


def _with_speeds(dev: dict, engines: list) -> dict:
    keys = [*engines, "scorer", "train"]
    dev["speeds"] = {k: speeds.lookup(dev, k) for k in keys}
    return dev


def local_state(paused: list, name: str = None) -> dict:
    h = local_hw()
    busy = any(w.busy for w in workers.ALL)
    status = "paused" if "local" in paused else "training" if workers.training_job else "busy" if busy else "idle"
    disk = storage.device_disk_stats()
    dev = dict(id="local", name=name or settings.MACHINE_NAME, type="local", machine=settings.MACHINE_NAME, hw=h,
               ram_gb=None if h["accel"] == "cuda" else h["ram_gb"], engines=settings.installed_engines(),
               loaded=[w.name for w in workers.ALL if w.running], status=status, status_reason=None,
               activity=busy_kind("local") or ("正在训练" if workers.training_job else None),
               queue_sec=queue_sec("local"), **disk)
    return _with_speeds(dev, dev["engines"])


async def lan_state(d: dict, paused: list) -> dict:
    """有心跳的设备 OFFLINE_AFTER 秒没报心跳就算离线，不再去连（不用每次等 3 秒超时）；其余按 /worker/health 连不连得上。"""
    dev = dict(id=d["id"], name=d["name"], type="lan", address=d["address"], machine=d.get("machine"),
               hw=d.get("hw"), ram_gb=(d.get("hw") or {}).get("ram_gb", 0), engines=[], loaded=[],
               caps=list(d.get("caps") or []),
               queue_sec=queue_sec(d["id"]), status_reason=None, activity=busy_kind(d["id"]), wake=d.get("wake"),
               wakeable=False, last_seen=d.get("last_seen"),
               disk_free_bytes=None, disk_total_bytes=None, cache_bytes=None)
    if d["id"] in waking:
        dev.update(status="waking", status_reason="唤醒中", engines=d.get("engines", []))
        return _with_speeds(dev, dev["engines"])
    at, h = _health.get(d["id"], (0, None))
    if d["id"] in beats and time.monotonic() - beats[d["id"]] > OFFLINE_AFTER:
        h = Silent(f"{OFFLINE_AFTER} 秒没有心跳")
    elif time.time() - at > HEALTH_TTL:
        try:
            h = await Remote(d).health()
        except (httpx.HTTPError, OSError) as e:
            h = e
        _health[d["id"]] = (time.time(), h)
    if isinstance(h, Exception):
        if d.get("wake") and time.time() >= _wake_failed.get(d["id"], 0) and d["id"] not in paused:
            # 睡着了、配了唤醒：派任务前先唤醒（dispatch.prepare），估时按最近一次连上时报的引擎
            dev.update(status="offline", status_reason="睡眠中，派任务时先唤醒", wakeable=True,
                       engines=d.get("engines", []))
            return _with_speeds(dev, dev["engines"])
        why = "唤醒超时" if d["id"] in _wake_failed and time.time() < _wake_failed[d["id"]] else \
            str(h) if isinstance(h, Silent) else type(h).__name__
        dev.update(status="offline", status_reason=f"离线（{why}）")
        return _with_speeds(dev, [])
    caps = list(h.get("caps") or [])
    if d.get("engines") != h["engines"] or d.get("caps") != caps:  # 记下能跑的引擎与能力：睡着时也能估时、推荐
        data = load()
        for x in data["devices"]:
            if x["id"] == d["id"]:
                x["engines"] = h["engines"]
                x["caps"] = caps
        save(data)
    dev.update(hw=h["hw"], machine=h["machine"], ram_gb=None if h["hw"]["accel"] == "cuda" else h["hw"]["ram_gb"],
               engines=h["engines"], loaded=h["loaded"], caps=caps,
               status="paused" if d["id"] in paused else "training" if h["training"] else
               "busy" if h["busy"] else "idle",
               activity=dev["activity"] or ("正在训练" if h["training"] else None),
               disk_free_bytes=h.get("disk_free_bytes"),
               disk_total_bytes=h.get("disk_total_bytes"),
               cache_bytes=h.get("cache_bytes"))
    return _with_speeds(dev, dev["engines"])


def cloud_states(paused: list, cloud: dict = None, activities: dict = None) -> list:
    """每种卡一项。没装 colab CLI 时也列出来（不可用，写原因），速度照样按参考表给。
    cloud：云端设置（max_minutes 给调度器挡超时的卡）；warm：做完先别关、等着接手的同型号 VM 台数（开机准备短）。
    activities：事件循环侧提前快照的 {device_id: activity}，后台线程只读该快照，不直接访问 assigned / jobs.runs。"""
    cloud = cloud or DEFAULT_CLOUD
    acts = cloud_activities() if activities is None else activities
    idle = {}
    if pool is None:
        types = [dict(gpu=g, account_count=0, hours=None, reason="没有装云端 GPU 组件") for g in [*RATES, "H100"]]
        rates = RATES
    else:
        idle = pool.idle_vms()
        accounts = pool.accounts(refresh=False)
        types = [dict(gpu=t.gpu, account_count=t.account_count, hours=t.hours, reason=t.reason,
                      balances=[a.balance for a in accounts if t.gpu in a.gpus and a.balance is not None])
                 for t in pool.gpu_types()]
        rates = pool.rates()
    out = []
    for t in types:
        did = f"colab-{t['gpu']}"
        act = acts.get(did)
        warm_count = idle.get(t["gpu"], 0)
        if act:
            status = "training" if act == "正在训练" else "busy"
        else:
            status = "paused" if did in paused else "unavailable" if t["reason"] else "idle"
        dev = dict(id=did, name=f"云端 {GPU_LABEL.get(t['gpu'], t['gpu'])}", type="cloud", gpu=t["gpu"], machine=None,
                   hw=None, ram_gb=None, engines=list(settings.CLOUD_ENGINES), loaded=[], status=status,
                   status_reason=t["reason"], activity=act, queue_sec=0, rate=rates.get(t["gpu"]),
                   premium=t["gpu"] in PREMIUM, accounts=t["account_count"], hours=t["hours"],
                   balances=t.get("balances"), max_minutes=cloud["max_minutes"], warm=warm_count,
                   paused=did in paused)
        out.append(_with_speeds(dev, dev["engines"]))
    return out


async def snapshot() -> tuple:
    """(设备列表, 设置)。局域网设备并发查一次 /worker/health（3 秒超时）。设置是自动选择（auto）加上云端的 gpu_pref。"""
    data = load()
    lan = await asyncio.gather(*(lan_state(d, data["paused"]) for d in data["devices"]))
    activities = cloud_activities()
    cloud = await asyncio.to_thread(cloud_states, data["paused"], data["cloud"], activities)
    return ([local_state(data["paused"], data.get("local_name")), *lan, *cloud],
            {**data["auto"], "gpu_pref": data["cloud"]["gpu_pref"]})


async def gen_engines() -> set:
    """Engines eligible for automatic zero-shot generation (local/LAN only; cloud is opt-in)."""
    devices, auto = await snapshot()
    return {e for e in {e for d in devices for e in d["engines"]}
            if scheduler.plan(devices, scheduler.Task("gen", [(e, 1)], cloud_capable=False), auto)["recommended"]}


def month_used() -> float:
    """账号池本月合计已用（本机账本记的，含停用、移除了的账号），提醒线用。"""
    if pool is None:
        return 0.0
    return pool.month_used()
