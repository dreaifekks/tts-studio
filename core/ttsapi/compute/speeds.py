"""「设备 × 引擎」每句秒数和来源：已实测（measured）> 参考值（reference）> 未测（untested）。

实测有两处：主控本地的 SPEEDS_FILE（任务完成后按实际耗时做的移动平均，以及 POST /bench 的结果），
资料库里的测速结果 compute/bench/<机器>/<时间>.json（命令行测速也写这里）。两处都有时用较新的。
键：引擎名（合成每句秒数）、"scorer"（评测时每条打分秒数）、"train"（训练每句秒数）。
"""
import json
import statistics
from datetime import datetime
from pathlib import Path

from ttsapi import library, settings
from ttsapi.library.store import _atomic

REFERENCE = json.loads(Path(__file__).with_name("reference-speeds.json").read_text(encoding="utf-8"))
ALPHA = 0.3  # 移动平均里新一次的权重
# 速度按 hard 难句计；实际任务的句子长短不一，按字数折算成「每句 hard」再进移动平均
HARD_CHARS = statistics.mean(len(s["text"]) for s in settings.EVAL_SETS["hard"]["sentences"])


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def per_hard(sec: float, text: str) -> float:
    """一句 text 用了 sec 秒，折算成一句 hard 难句的秒数。"""
    return sec * HARD_CHARS / max(len(text), 8)


def engine_mem_gb(engine: str) -> float:
    mem = REFERENCE["engine_mem_gb"]
    return mem.get(engine, mem["default"])


def reference_rows(dev: dict) -> list:
    """按设备类型查参考表：云端卡按卡型相等；本机和局域网设备按硬件信息包含 match 里的字符串，从上往下命中的几行，
    最后一行「通用 CPU 8 核」（match 为空）谁都命中：具体硬件的行里没有这一项时退到它。"""
    if dev["type"] == "cloud":
        return [r for r in REFERENCE["devices"] if r["id"].startswith("colab-") and r["match"].get("gpu") == dev["gpu"]]
    hw = dev.get("hw") or {}
    return [r for r in REFERENCE["devices"] if not r["id"].startswith("colab-")
            and all(v in str(hw.get(k) or "") for k, v in r["match"].items())]


# ---------------------------------------------------------------- 实测

def _load() -> dict:
    if not settings.SPEEDS_FILE.exists():
        return {}
    return json.loads(settings.SPEEDS_FILE.read_text(encoding="utf-8"))


def _save(data: dict):
    _atomic(settings.SPEEDS_FILE, lambda f: f.write(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")))


def observe(device_id: str, key: str, sec: float):
    """任务里实际跑了一句（或一条打分、训练一句）：移动平均进这台设备的实测值。第一次直接用这个数。"""
    data = _load()
    cur = data.setdefault(device_id, {}).get(key)
    if cur is None:
        cur = dict(sec=sec, n=0)
    else:
        cur["sec"] = (1 - ALPHA) * cur["sec"] + ALPHA * sec
    cur.update(sec=round(cur["sec"], 3), n=cur["n"] + 1, at=now_iso(), via="task")
    data[device_id][key] = cur
    _save(data)


def _bench_value(doc: dict, key: str):
    """测速结果里这一项的每句秒数；这次没测它时 None（只测一个引擎时没有别的引擎，可能也没有打分器）。"""
    r = doc.get("scorer") if key == "scorer" else doc["engines"].get(key)
    return r["sec_per_item"] if r else None


def set_bench(device_id: str, result: dict):
    """测速结果写进实测值（这次测了的几项覆盖移动平均）。"""
    data = _load()
    mine = data.setdefault(device_id, {})
    for key in [*result["engines"], "scorer"]:
        if (sec := _bench_value(result, key)) is not None:
            mine[key] = dict(sec=sec, n=1, at=result["at"], via="bench")
    _save(data)


def latest_bench(machine: str, key: str):
    """资料库里这台机器测过这一项的最新测速结果 (时间, 每句秒数)；没选资料库或没测过时 None。"""
    lib = library.selected()
    if lib is None:
        return None
    found = [(doc["at"], sec) for rel, doc in lib.items("bench").items()
             if rel.split("/")[2] == machine and (sec := _bench_value(doc, key)) is not None]
    return max(found) if found else None


def lookup(dev: dict, key: str):
    """(每句秒数, 来源)；没有实测也没有参考值时 (None, "untested")。dev：registry 里的设备（id、type、hw、gpu、machine）。"""
    measured = _load().get(dev["id"], {}).get(key)
    bench = latest_bench(dev["machine"], key) if dev.get("machine") else None
    if bench and (measured is None or bench[0] > measured["at"]):
        measured = dict(sec=bench[1])
    if measured:
        return measured["sec"], "measured"
    for row in reference_rows(dev):
        v = {"scorer": row.get("scorer"), "train": row.get("train_sec_per_line")}.get(key, row["engines"].get(key))
        if v is not None:
            return v, "reference"
    return None, "untested"
