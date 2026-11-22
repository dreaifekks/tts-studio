"""主控这边的「添加局域网设备」（design.md 第 13 节「局域网或远程电脑」）：邀请（配对码）、安装脚本、安装用的代码包。

1. POST /compute/invites：生成配对码和一行安装命令。邀请只在主控内存里（主控重启就没了，反正 10 分钟就过期；查不到的按过期算），
   配对码原文也只在这里（手动填时主控要拿它去找设备）。同一时间只有一个能用：再生成一个，没核销的旧码马上作废。
2. 那台电脑跑 `curl -fsSL http://<主控>/install | sh -s -- --pair <配对码>`：GET /install 是 scripts/device/install.sh，
   写好主控地址（请求里用的主机名 + core 端口）和取脚本的地址。脚本从取脚本的地址的 /install/code.tar 取 core 和
   GPT-SoVITS 补丁，装进 ~/.tts-core：取到 core 就 python -m ttsapi.pair join 带配对码 POST /devices/pair 连回主控
   （配对码 10 分钟有效，不等后面几 GB 的下载），装好环境再起服务。
3. 一个邀请只核销一次，自动连回和手动填谁先到算谁（后到的 409）：
   - 自动连回：设备 POST /devices/pair（redeem），主控登记设备、发令牌；设备的服务起来、带这个令牌报第一次心跳时 connected
     （connect；重装期间旧服务带旧令牌报的心跳不算）。
   - 手动填：连不上主控时那台电脑终端里打印自己的地址和同一个配对码，用户在 Studio 里填地址
     （POST /compute/invites/{id}/manual），主控拿这个邀请的配对码调设备的 POST /pair，成功就 connected。
"""
import hmac
import io
import math
import secrets
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

from ttsapi import pair, settings, version

# id -> {id, network, host, code, expires, tries, state, device_id（核销给了哪台设备）, token（自动连回时发的令牌）,
#        busy（正在手动配对）}
invites: dict = {}


class Used(Exception):
    """邀请已经核销过（另一边先连上了），或者正在手动配对。"""


def host_for(network: str) -> str:
    """命令里主控的名字：局域网用 mDNS 名，Tailscale 用 MagicDNS 名（没有时 Tailscale IP）。"""
    return pair.tailscale_host() if network == "tailscale" else pair.mdns_host()


def _usable(inv: dict) -> bool:
    return inv["state"] == "waiting" and inv["device_id"] is None and not inv["busy"]


def create(network: str, host: str) -> dict:
    """新邀请。没核销的旧邀请作废，正在手动配对的也是（配上了照样算 connected，没配上不再能用）；已经用配对码连回来、
    在等心跳的不受影响。"""
    for inv in invites.values():
        if inv["state"] == "waiting" and inv["device_id"] is None:
            inv["state"] = "expired"
    inv = dict(id=f"inv-{secrets.token_hex(4)}", network=network, host=host, code=pair.random_code(),
               expires=time.time() + pair.TTL, tries=0, state="waiting", device_id=None, token=None, busy=False)
    invites[inv["id"]] = inv
    return inv


def get(invite_id: str):
    """没有这个邀请（取消了、主控重启过）时 None。没核销、过了 10 分钟的标成 expired。"""
    inv = invites.get(invite_id)
    if inv is not None and _usable(inv) and time.time() >= inv["expires"]:
        inv["state"] = "expired"
    return inv


def redeem(code: str):
    """设备自动连回（POST /devices/pair）：配对码对上能用的邀请就返回它（调用方记上 device_id，核销）；不对、过期返回 None；
    码是某个邀请的、它已经核销过或正在手动配对时 Used。输错的码记在能用的那个邀请上，pair.MAX_TRIES 次它作废。"""
    for inv in list(invites.values()):
        get(inv["id"])
    inv = next((i for i in invites.values() if hmac.compare_digest(pair.norm(i["code"]), pair.norm(code))), None)
    if inv is None:
        if (open_ := next((i for i in invites.values() if _usable(i)), None)) is not None:
            open_["tries"] += 1
            if open_["tries"] >= pair.MAX_TRIES:
                open_["state"] = "expired"
        return None
    if inv["state"] == "expired":
        return None
    if not _usable(inv):
        raise Used(inv["id"])
    return inv


def begin_manual(invite_id: str) -> dict:
    """手动填：占住这个邀请（busy，这期间自动连回的设备 Used），返回它；查不到、过期 KeyError，核销过 Used。
    配对失败时调用方放开（busy=False），成功时记上 device_id。"""
    inv = get(invite_id)
    if inv is None or inv["state"] == "expired":
        raise KeyError(invite_id)
    if not _usable(inv):
        raise Used(invite_id)
    inv["busy"] = True
    return inv


def connect(device_id: str, token: str):
    """设备报心跳：带的是哪个在等它起来的邀请发的令牌，那个邀请就变成 connected。"""
    for inv in invites.values():
        if inv["device_id"] == device_id and inv["state"] == "waiting" and hmac.compare_digest(inv["token"], token):
            inv["state"] = "connected"


def cancel(invite_id: str):
    """取消（重复调、没有这个邀请都行）。已经连上的设备不受影响。"""
    invites.pop(invite_id, None)


def expires_at(inv: dict) -> str:
    """过期时间，带时区（ISO 8601 带 offset）。"""
    return datetime.fromtimestamp(inv["expires"]).astimezone().isoformat(timespec="seconds")


def expires_in(inv: dict) -> int:
    """还剩几秒能用，向上取整：没核销、没过期时至少 1（手动配对进行中也是）；只有核销了、过期了是 0。"""
    if inv["state"] == "expired" or inv["device_id"] is not None:
        return 0
    return max(1, math.ceil(inv["expires"] - time.time()))


# ---------------------------------------------------------------- 安装脚本和代码包

def script(control: str, install: str) -> str:
    """scripts/device/install.sh，写好主控地址（control）和取脚本的地址（install，代码包也从这里取）。脚本里两处都在单引号里
    （shell 字面量）；调用方保证地址里没有单引号（主机名只认 DNS 名和 IPv4）。"""
    text = (settings.ROOT / "scripts" / "device" / "install.sh").read_text(encoding="utf-8")
    return text.replace("__CONTROL__", control).replace("__INSTALL__", install)


def code_tar() -> bytes:
    """安装脚本要的代码：core（主控已提交的 HEAD 里的 core/，带和主控同名的版本 tag）和 GPT-SoVITS 补丁，
    由 colab_jobs.bundle_code 打包。install.env 记录文件名、版本、补丁校验值和上游基点。"""
    from ttsapi.compute.colab_jobs import bundle_code
    with tempfile.TemporaryDirectory() as tmp:
        files, code, _ = bundle_code(Path(tmp))
        env = dict(CORE_BUNDLE=Path(code["ttsapi_bundle"]).name, CORE_COMMIT=code["ttsapi_commit"],
                   GSV_PATCH=Path(code["gsv_patch"]).name, GSV_PATCH_SHA256=code["gsv_patch_sha256"], GSV_BASE=code["gsv_base"],
                   GSV_BRANCH=code["gsv_branch"], VERSION=version.describe()[0])
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for path in files.values():
                tf.add(path, arcname=path.name)
            data = "".join(f"{k}={v}\n" for k, v in env.items()).encode()
            info = tarfile.TarInfo("install.env")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        return buf.getvalue()
