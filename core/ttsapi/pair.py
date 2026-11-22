"""局域网设备（--role worker）这一侧的配对（design.md 第 13 节「局域网或远程电脑」）。

两种配对方式，换到的令牌都存在 DEVICE_FILE（0600），配对码只存 sha256（在 DEVICE_FILE 旁边的 pair.json；安装脚本装的
每个版本各有一份，重装时新版本在旁边配对，正在跑的旧版本看不到）：
- 自动连回：主控 GET /install 的安装脚本取到 core 的代码就运行 python -m ttsapi.pair join <主控地址> <配对码>，向主控
  POST /devices/pair 带上配对码、这台能被访问到的地址（address_for：连主控用的那块网卡的 IPv4）和硬件信息，
  主控登记设备、发长期令牌。DEVICE_FILE 里另记主控地址和设备 id，worker 起来后每 30 秒向主控报心跳（worker_api.heartbeat）。
- 手动填（备用）：join 连不上主控时（安装脚本起服务前会再试一次），把同一个配对码记成这台的配对码，终端打印连接地址和
  配对码；用户在 Studio 里填地址，
  主控（POST /compute/invites/{id}/manual）拿这个配对码调这台 worker 的 POST /pair 换令牌（这种配对不报心跳）。
  worker 没配对、也没有待用的配对码时，启动会自己生成一个打印出来（主控 POST /compute/devices 手填地址和它）；
  python -m ttsapi.pair 重新生成（旧的作废）。
配对码（XXX-XXX，去掉容易看错的 0/O/1/I/L）10 分钟有效、用一次作废，输错 5 次作废。
"""
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from ttsapi import settings

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
TTL = 600
MAX_TRIES = 5  # 输错这么多次配对码作废（防局域网里有人挨个试）


def _write(path, doc: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f)
    os.replace(tmp, path)


def norm(code: str) -> str:
    return code.upper().replace("-", "").replace(" ", "")


def code_hash(code: str) -> str:
    return hashlib.sha256(norm(code).encode()).hexdigest()


def random_code() -> str:
    code = "".join(secrets.choice(ALPHABET) for _ in range(6))
    return f"{code[:3]}-{code[3:]}"


def pair_file():
    return settings.DEVICE_FILE.with_name("pair.json")


def device() -> dict:
    """DEVICE_FILE 的内容：token；自动连回配对的另有 control（主控地址）、device_id。没配对时 {}。"""
    if settings.DEVICE_FILE.exists():
        return json.loads(settings.DEVICE_FILE.read_text(encoding="utf-8"))
    return {}


def token():
    """已配对时的令牌（TTSAPI_WORKER_TOKEN 或 DEVICE_FILE 里的），没配对时 None。"""
    return settings.WORKER_TOKEN or device().get("token")


def remember(code: str):
    """把 code 记成这台的配对码（旧的作废），10 分钟有效。"""
    _write(pair_file(), dict(sha256=code_hash(code), expires=time.time() + TTL, tries=0))


def new_code() -> str:
    """生成配对码（旧的作废），返回 XXX-XXX。"""
    code = random_code()
    remember(code)
    return code


def pending() -> bool:
    """有没过期的配对码，在等主控来配对。"""
    f = pair_file()
    return f.exists() and time.time() < json.loads(f.read_text(encoding="utf-8"))["expires"]


def redeem(code: str):
    """配对码对、没过期：作废它，返回令牌。设了 TTSAPI_WORKER_TOKEN 时就是它；否则生成新令牌写进 DEVICE_FILE
    （换掉旧令牌，之前配对的主控连不上了，自动连回的心跳也停了）。不对返回 None，输错 MAX_TRIES 次或过期时配对码作废。
    调用方（worker 的 POST /pair）在事件循环里串行调用，读改写之间不会插进别的请求。"""
    f = pair_file()
    if not f.exists():
        return None
    doc = json.loads(f.read_text(encoding="utf-8"))
    if time.time() >= doc["expires"]:
        f.unlink(missing_ok=True)
        return None
    if not hmac.compare_digest(doc["sha256"], code_hash(code)):
        if doc["tries"] + 1 >= MAX_TRIES:
            f.unlink(missing_ok=True)
            print("配对码输错太多次，已作废；运行 python -m ttsapi.pair 重新生成", flush=True)
        else:
            _write(f, dict(doc, tries=doc["tries"] + 1))
        return None
    f.unlink(missing_ok=True)
    if settings.WORKER_TOKEN:
        return settings.WORKER_TOKEN
    tok = secrets.token_urlsafe(24)
    _write(settings.DEVICE_FILE, dict(token=tok, created=datetime.now().astimezone().isoformat(timespec="seconds")))
    return tok


# ---------------------------------------------------------------- 本机在网络里的名字

class TailscaleUnavailable(Exception):
    pass


def mdns_host() -> str:
    """本机在局域网里的 mDNS 名（<主机名>.local）。"""
    host = socket.gethostname()
    return host if host.endswith(".local") else f"{host.split('.')[0]}.local"


def tailscale_host() -> str:
    """本机在 Tailscale 里的名字：MagicDNS 名（<主机>.<tailnet>.ts.net），没开 MagicDNS 时是 Tailscale IP。"""
    cli = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    try:
        r = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=10)
    except OSError:
        raise TailscaleUnavailable("这台机器没装 Tailscale") from None
    if r.returncode:
        raise TailscaleUnavailable(f"Tailscale 没在运行：{(r.stderr or r.stdout).strip()[-200:]}")
    me = json.loads(r.stdout)["Self"]
    return me["DNSName"].rstrip(".") or me["TailscaleIPs"][0]


def address() -> str:
    """这台 core 的连接地址（mDNS 名），没配对的 worker 启动时打印。"""
    return f"http://{mdns_host()}:{settings.PORT}"


def address_for(control: str) -> str:
    """主控连这台用的地址：这台连 control 时走的那块网卡的 IPv4（UDP connect 只选路由、不发包）+ core 端口。
    主控是 Tailscale 地址时就是这台的 Tailscale IP。不用 mDNS 名：Linux 上常常没有 mDNS，或者只答 IPv6（worker 只听 IPv4）。
    换了 IP（DHCP）时心跳带上新的，主控跟着改。"""
    u = urlsplit(control)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((u.hostname, u.port or 80))
        return f"http://{s.getsockname()[0]}:{settings.PORT}"


def announce():
    """终端里打印连接地址和新配对码（worker 启动时没配对、或 python -m ttsapi.pair）。"""
    print(f"连接地址：{address()}\n配对码：{new_code()}（10 分钟有效，用一次作废；过期后运行 python -m ttsapi.pair 重新生成）",
          flush=True)


# ---------------------------------------------------------------- 自动连回主控

class Rejected(Exception):
    """主控说配对码不对、过期（403）或这个邀请已经核销了（409）。"""


def join(control: str, code: str) -> dict:
    """向主控 POST /devices/pair（配对码、这台的地址和硬件），换到的令牌、设备 id、主控地址存进 DEVICE_FILE，
    返回主控的回复。连不上主控时 httpx 的异常原样抛出；主控不认配对码时 Rejected。"""
    from ttsapi import library
    from ttsapi.compute import hw
    control = control.rstrip("/")
    body = dict(code=code, address=address_for(control), machine=settings.MACHINE_NAME,
                machine_id=library.machine()["id"], hardware=hw.hardware(), engines=settings.installed_engines())
    r = httpx.post(f"{control}/devices/pair", json=body, timeout=15)
    if r.status_code in (403, 409):
        raise Rejected(r.json()["detail"])
    r.raise_for_status()
    res = r.json()
    _write(settings.DEVICE_FILE, dict(token=res["token"], device_id=res["device_id"], control=control,
                                      created=datetime.now().astimezone().isoformat(timespec="seconds")))
    pair_file().unlink(missing_ok=True)
    return res


class NotThisWorker(Exception):
    """本机 core 端口上答话的不是这一份 worker（端口被别的程序占着，或者令牌、机器 id 对不上）。"""


def check() -> str:
    """安装脚本起服务后核对本机 core 端口上答话的是这一份 worker：带 DEVICE_FILE 的令牌查 /worker/health，机器 id 是这台的。
    还没配对（等手动填）时没有令牌，认 worker 对不带令牌的请求回的 401。返回一句说明；连不上时 httpx / OSError 原样抛出，
    答话的不是它时 NotThisWorker。"""
    from ttsapi import library
    tok = token()
    r = httpx.get(f"http://127.0.0.1:{settings.PORT}/worker/health", timeout=5, trust_env=False,  # 不走 http_proxy
                  headers={"Authorization": f"Bearer {tok}"} if tok else {})
    try:
        body = r.json()
    except ValueError:
        body = {}
    if tok is None and r.status_code == 401 and body.get("detail") == "令牌不对":
        return "服务起来了，等主控配对"
    if r.status_code == 200 and body.get("role") == "worker" and body.get("machine_id") == library.machine()["id"]:
        return f"服务起来了（core {body['version']}）"
    raise NotThisWorker(f"{settings.PORT} 端口上答话的不是这次装的服务（HTTP {r.status_code}），是不是被别的程序占着")


def main(argv: list) -> int:
    """python -m ttsapi.pair：重新生成配对码并打印连接地址（手动填用）。
    python -m ttsapi.pair join <主控地址> <配对码>：自动连回主控（安装脚本里）。退出码 0 连上了；3 连不上主控（包括主控的名字
    解析不了），已经改成等手动填（打印了连接地址和配对码）；4 主控不认这个配对码。
    python -m ttsapi.pair check：安装脚本起服务后核对（check）。退出码 0 是这一份 worker；5 还连不上；6 答话的不是它。"""
    if argv[:1] == ["check"]:
        try:
            print(check(), flush=True)
        except (httpx.HTTPError, OSError):
            return 5
        except NotThisWorker as e:
            print(e, flush=True)
            return 6
        return 0
    if argv[:1] != ["join"]:
        announce()
        return 0
    control, code = argv[1], argv[2]
    try:
        res = join(control, code)
    except Rejected as e:
        print(f"主控没接受这个配对码：{e}", flush=True)
        return 4
    except (httpx.HTTPError, OSError) as e:  # 只通 80 端口、主控刚好在重启、名字解析不了……：改成等主控来配对（手动填）
        remember(code)
        try:
            here = address_for(control)
        except OSError:  # 主控的名字解析不了，选不出网卡：给 mDNS 名
            here = address()
        print(f"连不上主控 {control}（{type(e).__name__}: {e}）。\n"
              f"这台的服务起来后，在 Studio「添加算力」里点「没有自动连上？手动填」，填：\n"
              f"  连接地址：{here}\n"
              f"  （配对码 {code} 在这台上从现在起 10 分钟有效；Studio 里的邀请从生成起算 10 分钟，过期了就重新生成，"
              f"在这台上跑新命令）", flush=True)
        return 3
    print(f"已连上主控 {control}（设备 {res['device_id']}）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
