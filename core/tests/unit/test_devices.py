"""阶段 3：添加局域网设备（邀请和配对码、设备自动连回、心跳和离线、手动填、安装脚本和代码包、80 端口只给 /install），
主控在 /ui/ 提供 Web。"""
import asyncio
import hashlib
import io
import json
import logging
import os
import shlex
import shutil
import subprocess
import tarfile
import time
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from ttsapi import pair, settings
from ttsapi.compute import enroll, registry, remote, worker_api

HW = dict(cpu="Intel N100", cores=4, ram_gb=16, accel="cpu", gpu=None, os="linux", torch="2.11.0+cpu")


@pytest.fixture
def ctl(monkeypatch, tmp_path):
    """主控的 gateway（不进 lifespan），没有邀请、心跳；局域网设备的 /worker/health 用假的（记下被连了几次）。"""
    from ttsapi import gateway
    monkeypatch.setattr(settings, "COMPUTE_FILE", tmp_path / "compute.json")
    monkeypatch.setattr(settings, "SPEEDS_FILE", tmp_path / "speeds.json")
    monkeypatch.setattr(settings, "installed_engines", lambda: ["v2pro"])
    monkeypatch.setattr(registry, "_hw", dict(HW, cpu="Apple M6", accel="mps", os="darwin"))
    monkeypatch.setattr(enroll, "invites", {})
    monkeypatch.setattr(registry, "beats", {})
    monkeypatch.setattr(registry, "_health", {})
    probes = []

    async def health(self, timeout=3):
        probes.append(self.url)
        return dict(role="worker", machine="worker-host", machine_id="nuc-1", hw=HW, engines=["v2pro"], loaded=[],
                    busy=False, training=None)
    monkeypatch.setattr(remote.Remote, "health", health)

    async def app(scope, receive, send):  # 局域网里的设备连到主控的 core 端口
        if scope["type"] == "http":
            scope = dict(scope, client=("192.168.1.5", 50000), server=("192.168.1.28", settings.PORT))
        await gateway.app(scope, receive, send)
    client = TestClient(app)
    client.probes = probes
    return client


def join_body(code, **kw):
    return dict(dict(code=code, address="http://worker-host.local:9880", machine="worker-host", machine_id="nuc-1",
                     hardware=HW, engines=["v2pro", "v2proplus"]), **kw)


def lan(client) -> list:
    return [d for d in client.get("/compute").json()["devices"] if d["type"] == "lan"]


def test_invite_auto_join_and_heartbeat(ctl):
    r = ctl.post("/compute/invites", json=dict(network="lan"))
    assert r.status_code == 200, r.text
    inv = r.json()
    code = inv["code"]
    assert len(code) == 7 and code[3] == "-" and set(code.replace("-", "")) <= set(pair.ALPHABET)
    assert inv["host"] == pair.mdns_host() and 590 <= inv["expires_in"] <= 600
    assert datetime.fromisoformat(inv["expires_at"]).utcoffset() is not None          # 带时区
    assert inv["commands"] == dict(unix=f"curl -fsSL 'http://{pair.mdns_host()}:{settings.PORT}/install' | sh -s -- "
                                        f"--pair {code}", windows=None)                # URL 在单引号里
    assert ctl.get(f"/compute/invites/{inv['id']}").json()["state"] == "waiting"
    # 设备连回来：配对码不对 403；对的（大小写、横线都行）登记设备、发令牌
    assert ctl.post("/devices/pair", json=join_body("ZZZ-ZZZ" if code != "ZZZ-ZZZ" else "YYY-YYY")).status_code == 403
    r = ctl.post("/devices/pair", json=join_body(code.lower().replace("-", "")))
    assert r.status_code == 200, r.text
    paired = r.json()
    assert paired["device_id"].startswith("lan-") and len(paired["token"]) >= 24 and paired["heartbeat_sec"] == 30
    r = ctl.post("/devices/pair", json=join_body(code))                                  # 只核销一次
    assert r.status_code == 409 and r.json()["code"] == "invite_used"
    saved = registry.load()["devices"]
    assert len(saved) == 1 and saved[0]["token"] == paired["token"] and saved[0]["machine_id"] == "nuc-1"
    assert saved[0]["name"] == "worker-host" and saved[0]["engines"] == ["v2pro", "v2proplus"]
    # 服务还没起来（没心跳）：邀请仍是 waiting，令牌不在接口里
    st = ctl.get(f"/compute/invites/{inv['id']}").json()
    assert (st["state"], st["device"], st["expires_in"]) == ("waiting", None, 0)
    assert paired["token"] not in ctl.get("/compute").text
    # 心跳：令牌不对 401，没有这台 404；对的 204，邀请 connected，last_seen 带时区
    dev_id, h = paired["device_id"], {"Authorization": f"Bearer {paired['token']}"}
    beat = dict(address="http://worker-host.local:9880")
    assert ctl.post(f"/devices/{dev_id}/heartbeat", json=beat, headers={"Authorization": "Bearer x"}).status_code == 401
    assert ctl.post("/devices/lan-nope/heartbeat", json=beat, headers=h).status_code == 404
    assert ctl.post(f"/devices/{dev_id}/heartbeat", json=beat, headers=h).status_code == 204
    st = ctl.get(f"/compute/invites/{inv['id']}").json()
    assert st["state"] == "connected" and st["device"]["id"] == dev_id and st["device"]["status"] == "idle"
    assert st["device"]["hw"]["cpu"] == "Intel N100"
    (d,) = lan(ctl)
    assert datetime.fromisoformat(d["last_seen"]).utcoffset() is not None
    # 换了地址：心跳带新地址，主控跟着改，下次查状态连新地址
    ctl.post(f"/devices/{dev_id}/heartbeat", json=dict(address="http://192.168.1.77:9880/"), headers=h)
    assert registry.load()["devices"][0]["address"] == "http://192.168.1.77:9880"
    lan(ctl)
    assert ctl.probes[-1] == "http://192.168.1.77:9880"


def test_regenerate_cancel_and_rejoin_same_machine(ctl):
    first = ctl.post("/compute/invites", json={}).json()
    second = ctl.post("/compute/invites", json={}).json()                              # 重新生成：旧码马上作废
    assert ctl.get(f"/compute/invites/{first['id']}").json()["state"] == "expired"
    assert ctl.post("/devices/pair", json=join_body(first["code"])).status_code == 403
    a = ctl.post("/devices/pair", json=join_body(second["code"])).json()
    # 同一台机器重装、拿新码再连回来：还是原来那一项（id、改过的名字、暂停都留着），发新令牌（等新服务报心跳才换上），不另加
    ctl.patch(f"/compute/devices/{a['device_id']}", json=dict(name="工作站", paused=True))
    third = ctl.post("/compute/invites", json={}).json()
    assert ctl.get(f"/compute/invites/{second['id']}").json()["state"] == "waiting"     # 连回来过的不受重新生成影响
    b = ctl.post("/devices/pair", json=join_body(third["code"], address="http://nuc.local:9880")).json()
    (d,) = registry.load()["devices"]
    assert b["device_id"] == a["device_id"] and b["token"] != a["token"] and d["pending_token"] == b["token"]
    assert d["name"] == "工作站" and registry.load()["paused"] == [a["device_id"]]
    # 取消：重复调、查不到都 204；查不到的邀请是 200 expired；设备不删
    assert ctl.delete(f"/compute/invites/{third['id']}").status_code == 204
    assert ctl.delete(f"/compute/invites/{third['id']}").status_code == 204
    st = ctl.get(f"/compute/invites/{third['id']}").json()
    assert (st["state"], st["device"], st["expires_at"], st["expires_in"]) == ("expired", None, None, 0)
    assert ctl.get("/compute/invites/inv-nope").json()["state"] == "expired"
    assert len(registry.load()["devices"]) == 1
    # 过了 10 分钟没核销：expired；输错 5 次：作废
    inv = ctl.post("/compute/invites", json={}).json()
    enroll.invites[inv["id"]]["expires"] = time.time() - 1
    assert ctl.get(f"/compute/invites/{inv['id']}").json()["state"] == "expired"
    assert ctl.post("/devices/pair", json=join_body(inv["code"])).status_code == 403
    inv = ctl.post("/compute/invites", json={}).json()
    wrong = "ZZZ-ZZZ" if inv["code"] != "ZZZ-ZZZ" else "YYY-YYY"
    for _ in range(pair.MAX_TRIES):
        ctl.post("/devices/pair", json=join_body(wrong))
    assert ctl.post("/devices/pair", json=join_body(inv["code"])).status_code == 403


def test_reinstall_keeps_old_token_until_new_service_beats(ctl):
    """装过的设备重跑安装命令：主控发新令牌，但旧服务（旧令牌）照样能用、它的心跳不会让新邀请 connected；新服务带新令牌报
    第一次心跳时才换上（地址跟着改）。新服务报过心跳、安装脚本却核对失败换回旧服务时，旧服务带旧令牌报心跳又换回来
    （自审：不然两边各拿一把令牌）。重装中途失败时新令牌一直没人用，什么都没变；手动配对后两个都作废。"""
    inv = ctl.post("/compute/invites", json={}).json()
    a = ctl.post("/devices/pair", json=join_body(inv["code"])).json()
    old = {"Authorization": f"Bearer {a['token']}"}
    beat = dict(address="http://worker-host.local:9880")
    assert ctl.post(f"/devices/{a['device_id']}/heartbeat", json=beat, headers=old).status_code == 204
    again = ctl.post("/compute/invites", json={}).json()
    b = ctl.post("/devices/pair", json=join_body(again["code"])).json()
    assert b["device_id"] == a["device_id"] and b["token"] != a["token"]
    (d,) = registry.load()["devices"]
    assert d["token"] == a["token"] and remote.Remote(d).headers["Authorization"] == f"Bearer {a['token']}"
    assert ctl.post(f"/devices/{a['device_id']}/heartbeat", json=beat, headers=old).status_code == 204
    assert ctl.get(f"/compute/invites/{again['id']}").json()["state"] == "waiting"
    new = {"Authorization": f"Bearer {b['token']}"}
    r = ctl.post(f"/devices/{a['device_id']}/heartbeat", json=dict(address="http://192.168.1.77:9880"), headers=new)
    assert r.status_code == 204
    (d,) = registry.load()["devices"]
    assert (d["token"], d["pending_token"], d["address"]) == (b["token"], a["token"], "http://192.168.1.77:9880")
    assert ctl.get(f"/compute/invites/{again['id']}").json()["state"] == "connected"
    # 换回了旧服务：它带旧令牌报心跳，主控又用旧令牌；新服务再起来时又换过去
    assert ctl.post(f"/devices/{a['device_id']}/heartbeat", json=beat, headers=old).status_code == 204
    assert registry.load()["devices"][0]["token"] == a["token"]
    assert ctl.post(f"/devices/{a['device_id']}/heartbeat", json=beat, headers=new).status_code == 204
    assert registry.load()["devices"][0]["token"] == b["token"]
    # 又重装了一次但没装完，之后在设置里手动重新配对：没用上的新令牌和留着的旧令牌都作废
    third = ctl.post("/compute/invites", json={}).json()
    c = ctl.post("/devices/pair", json=join_body(third["code"])).json()
    registry.patch_device(a["device_id"], token="tok-manual")
    assert "pending_token" not in registry.load()["devices"][0]
    for tok in (a["token"], b["token"], c["token"]):
        r = ctl.post(f"/devices/{a['device_id']}/heartbeat", json=beat, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401


def test_tailscale_invite(ctl, monkeypatch):
    monkeypatch.setattr(pair, "tailscale_host", lambda: "studio-host.tail0000.ts.net")
    inv = ctl.post("/compute/invites", json=dict(network="tailscale")).json()
    assert inv["host"] == "studio-host.tail0000.ts.net" and "http://studio-host.tail0000.ts.net:" in inv["commands"]["unix"]

    def missing():
        raise pair.TailscaleUnavailable("这台机器没装 Tailscale")
    monkeypatch.setattr(pair, "tailscale_host", missing)
    r = ctl.post("/compute/invites", json=dict(network="tailscale"))
    assert r.status_code == 409 and r.json()["code"] == "tailscale_unavailable"


def test_silent_device_goes_offline_without_probing(ctl):
    inv = ctl.post("/compute/invites", json={}).json()
    a = ctl.post("/devices/pair", json=join_body(inv["code"])).json()
    h = {"Authorization": f"Bearer {a['token']}"}
    assert lan(ctl)[0]["status"] == "idle"                                             # 还没心跳：照旧去连
    dev_id = a["device_id"]
    ctl.post(f"/devices/{dev_id}/heartbeat", json=dict(address="http://worker-host.local:9880"), headers=h)
    assert lan(ctl)[0]["status"] == "idle"
    registry.beats[dev_id] -= registry.OFFLINE_AFTER + 1                               # 90 多秒没心跳
    n = len(ctl.probes)
    (d,) = lan(ctl)
    assert (d["status"], d["status_reason"]) == ("offline", f"离线（{registry.OFFLINE_AFTER} 秒没有心跳）")
    assert len(ctl.probes) == n                                                        # 没去连它
    ctl.post(f"/devices/{dev_id}/heartbeat", json=dict(address="http://worker-host.local:9880"), headers=h)
    assert lan(ctl)[0]["status"] == "idle"
    # 配了唤醒的：没心跳时照样是能唤醒的候选
    registry.patch_device(dev_id, wake=dict(mac="aa:bb:cc:dd:ee:ff", broadcast=None))
    registry.beats[dev_id] -= registry.OFFLINE_AFTER + 1
    (d,) = lan(ctl)
    assert (d["status"], d["wakeable"]) == ("offline", True)
    # 移除后再报心跳：404
    assert ctl.delete(f"/compute/devices/{dev_id}").status_code == 204
    r = ctl.post(f"/devices/{dev_id}/heartbeat", json=dict(address="http://worker-host.local:9880"), headers=h)
    assert r.status_code == 404 and dev_id not in registry.beats


# ---------------------------------------------------------------- 手动填（设备连不上主控时）

@pytest.fixture
def device(monkeypatch, tmp_path):
    """还没配对的设备（worker）。"""
    monkeypatch.setattr(settings, "WORKER_TOKEN", None)
    monkeypatch.setattr(settings, "DATA", tmp_path / "dev")
    monkeypatch.setattr(settings, "DEVICE_FILE", tmp_path / "dev" / "device.json")
    monkeypatch.setattr(settings, "MACHINE_FILE", tmp_path / "dev" / "machine.json")
    monkeypatch.setattr(settings, "BLOB_DIR", tmp_path / "dev" / "blobs")
    monkeypatch.setattr(worker_api.app.state, "token", None, raising=False)
    return tmp_path / "dev"


@pytest.fixture
def reachable(device, monkeypatch):
    """主控调这台设备（POST /pair、GET /worker/health）都经 ASGI 直接进 worker_api.app。"""
    transport = httpx.ASGITransport(app=worker_api.app)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    monkeypatch.setattr(remote.Remote, "health", worker_api_health)


def test_invite_manual_pairing(ctl, reachable):
    from ttsapi import library
    inv = ctl.post("/compute/invites", json={}).json()
    assert ctl.post("/compute/invites/inv-nope/manual", json=dict(address="http://w")).json()["code"] == "invite_expired"
    # 设备自动连回失败：安装脚本把同一个配对码记成它的配对码，等主控来配
    pair.remember(inv["code"])
    r = ctl.post(f"/compute/invites/{inv['id']}/manual", json=dict(address="http://worker-host.local:9880"))
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["state"] == "connected" and st["device"]["name"] == settings.MACHINE_NAME   # 名字是它报的机器名
    assert st["device"]["status"] == "idle" and st["device"]["last_seen"] is None
    (d,) = registry.load()["devices"]
    assert d["token"] == worker_api.app.state.token == pair.token() and d["machine_id"] == library.machine()["id"]
    assert not pair.pending()                                                          # 配上了：设备不再等配对
    # 一个邀请只核销一次：设备之后自动连回、再手动填都 409
    assert ctl.post("/devices/pair", json=join_body(inv["code"])).json()["code"] == "invite_used"
    assert ctl.post(f"/compute/invites/{inv['id']}/manual", json=dict(address="http://w")).json()["code"] == "invite_used"
    assert ctl.get(f"/compute/invites/{inv['id']}").json()["state"] == "connected"
    # 同一台重新配对（新邀请）：替换旧记录和令牌，不另加；之前自动连回、报过心跳的，手动配上后不再有 last_seen
    registry.heartbeat(d["id"], d["token"], d["address"])
    assert lan(ctl)[0]["last_seen"]
    inv2 = ctl.post("/compute/invites", json={}).json()
    pair.remember(inv2["code"])
    old = d["token"]
    st = ctl.post(f"/compute/invites/{inv2['id']}/manual", json=dict(address="http://nuc.local:9880")).json()
    (d,) = registry.load()["devices"]
    assert st["device"]["id"] == d["id"] and d["token"] != old and d["address"] == "http://nuc.local:9880"
    assert st["device"]["last_seen"] is None and "last_seen" not in d


async def worker_api_health(self, timeout=3):
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(self.url + "/worker/health", headers=self.headers)
    r.raise_for_status()
    return r.json()


def test_manual_pairing_keeps_token_when_health_check_fails(ctl, reachable, monkeypatch):
    """手动填：那台的 POST /pair 换了令牌、主控随后查状态超时（sol 的复现）：令牌已经存下，邀请 connected（不能再用），
    设备显示离线；之后连得上时就是正常的设备，不会两边各拿一把令牌。"""
    inv = ctl.post("/compute/invites", json={}).json()
    pair.remember(inv["code"])

    async def timeout(self, timeout=3):
        raise httpx.ReadTimeout("timed out")
    monkeypatch.setattr(remote.Remote, "health", timeout)
    r = ctl.post(f"/compute/invites/{inv['id']}/manual", json=dict(address="http://worker-host.local:9880"))
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["state"] == "connected" and st["device"]["status"] == "offline"
    assert st["device"]["name"] == settings.MACHINE_NAME
    (d,) = registry.load()["devices"]
    assert d["token"] == worker_api.app.state.token == pair.token() and d["hw"]["cpu"]
    assert ctl.post(f"/compute/invites/{inv['id']}/manual", json=dict(address="http://w")).json()["code"] == "invite_used"
    monkeypatch.setattr(remote.Remote, "health", worker_api_health)
    registry._health.clear()
    assert lan(ctl)[0]["status"] == "idle"


def test_manual_pairing_failure_keeps_invite_usable(ctl, reachable):
    inv = ctl.post("/compute/invites", json={}).json()
    pair.remember("ABC-DEF" if inv["code"] != "ABC-DEF" else "ABC-DEG")                 # 设备上记的不是这个码
    r = ctl.post(f"/compute/invites/{inv['id']}/manual", json=dict(address="http://worker-host.local:9880"))
    assert r.status_code == 400 and registry.load()["devices"] == []
    st = ctl.get(f"/compute/invites/{inv['id']}").json()
    assert st["state"] == "waiting" and st["expires_in"] > 0                              # 邀请还能用
    assert ctl.post("/devices/pair", json=join_body(inv["code"])).status_code == 200


def test_invite_seconds_round_up_until_redeemed(ctl):
    """没核销的邀请到期前最后不到半秒、手动配对进行中都至少报 1 秒（以前四舍五入成 0，Studio 当成「正在安装」）；
    只有核销了、过期了是 0，核销了 redeemed。"""
    inv = ctl.post("/compute/invites", json={}).json()
    assert inv["expires_in"] == 600
    enroll.invites[inv["id"]]["expires"] = time.time() + 0.4
    st = ctl.get(f"/compute/invites/{inv['id']}").json()
    assert (st["state"], st["expires_in"], st["redeemed"]) == ("waiting", 1, False)
    enroll.begin_manual(inv["id"])                                                     # 手动配对进行中
    st = ctl.get(f"/compute/invites/{inv['id']}").json()
    assert (st["state"], st["expires_in"], st["redeemed"]) == ("waiting", 1, False)
    enroll.invites[inv["id"]].update(busy=False, expires=time.time() + 60)
    paired = ctl.post("/devices/pair", json=join_body(inv["code"])).json()            # 核销了：等它装好、报第一次心跳
    st = ctl.get(f"/compute/invites/{inv['id']}").json()
    assert (st["state"], st["expires_in"], st["redeemed"]) == ("waiting", 0, True)
    ctl.post(f"/devices/{paired['device_id']}/heartbeat", json=dict(address="http://worker-host.local:9880"),
             headers={"Authorization": f"Bearer {paired['token']}"})
    st = ctl.get(f"/compute/invites/{inv['id']}").json()
    assert (st["state"], st["expires_in"], st["redeemed"]) == ("connected", 0, True)
    st = ctl.get("/compute/invites/inv-nope").json()
    assert (st["state"], st["expires_in"], st["redeemed"]) == ("expired", 0, False)


# ---------------------------------------------------------------- 设备这一侧

def test_device_join_and_fallback(device, monkeypatch, capsys):
    from ttsapi import library
    sent = []

    def post(url, json, timeout):
        sent.append((url, json))
        return httpx.Response(200, json=dict(device_id="lan-abc123", token="tok-1", heartbeat_sec=30),
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(pair.httpx, "post", post)
    assert pair.main(["join", "http://127.0.0.1:9881/", "7q4-k9m"]) == 0
    url, body = sent[0]
    assert url == "http://127.0.0.1:9881/devices/pair" and body["code"] == "7q4-k9m"
    # 地址是连主控走的那块网卡的 IPv4（这里主控在本机：127.0.0.1），不是 mDNS 名
    assert body["address"] == f"http://127.0.0.1:{settings.PORT}" and body["machine"] == settings.MACHINE_NAME
    assert body["machine_id"] == library.machine()["id"] and set(body["hardware"]) >= {"cpu", "accel", "os"}
    saved = json.loads(settings.DEVICE_FILE.read_text())
    assert saved["token"] == "tok-1" and saved["device_id"] == "lan-abc123" and saved["control"] == "http://127.0.0.1:9881"
    assert datetime.fromisoformat(saved["created"]).utcoffset() is not None
    assert oct(os.stat(settings.DEVICE_FILE).st_mode & 0o777) == "0o600" and pair.token() == "tok-1"
    # 主控不认配对码：4，什么都不记
    settings.DEVICE_FILE.unlink()
    monkeypatch.setattr(pair.httpx, "post", lambda url, json, timeout: httpx.Response(
        403, json=dict(detail="配对码不对或过期了"), request=httpx.Request("POST", url)))
    assert pair.main(["join", "http://127.0.0.1:9881", "7Q4-K9M"]) == 4
    assert not pair.pending() and not settings.DEVICE_FILE.exists()
    monkeypatch.setattr(pair.httpx, "post", lambda url, json, timeout: httpx.Response(               # 邀请已经核销
        409, json=dict(detail="这个配对码已经用过了", code="invite_used"), request=httpx.Request("POST", url)))
    assert pair.main(["join", "http://127.0.0.1:9881", "7Q4-K9M"]) == 4 and not pair.pending()

    # 连不上主控：3，同一个码记成这台的配对码，终端里打印连接地址和配对码；服务起来时不另生成配对码
    def down(url, json, timeout):
        raise httpx.ConnectError("Connection refused")
    monkeypatch.setattr(pair.httpx, "post", down)
    capsys.readouterr()
    assert pair.main(["join", "http://127.0.0.1:9881", "7Q4-K9M"]) == 3
    out = capsys.readouterr().out
    assert f"连接地址：http://127.0.0.1:{settings.PORT}" in out and "配对码 7Q4-K9M" in out
    assert pair.pending() and pair.token() is None
    with TestClient(worker_api.app) as client:
        out = capsys.readouterr().out
        assert "等主控配对" in out and "配对码：" not in out
        tok = client.post("/pair", json=dict(code="7q4k9m")).json()["token"]          # 主控拿同一个码来配
    assert pair.token() == tok and not pair.pending()


def test_pair_check(device, monkeypatch, capsys):
    """安装脚本起服务后的核对（python -m ttsapi.pair check）：本机端口上答话的是这一份 worker（带这一份的令牌、机器 id 对得上，
    或者等手动填、还没令牌时它回的 401）才是 0；端口被别的服务占着（sol 的复现：回 404 也被当成装好了）、令牌或机器 id
    对不上是 6；还连不上是 5（接着等）。"""
    client = TestClient(worker_api.app)
    seen = []

    def get(url, headers, **kw):
        seen.append(kw)
        return client.get(url, headers=headers)
    monkeypatch.setattr(pair.httpx, "get", get)
    assert pair.main(["check"]) == 0 and "等主控配对" in capsys.readouterr().out          # 还没配对
    assert seen[0]["trust_env"] is False                                               # 只连本机，不走 http_proxy
    pair._write(settings.DEVICE_FILE, dict(token="tok-1"))
    worker_api.app.state.token = "tok-1"
    assert pair.main(["check"]) == 0 and "服务起来了（core" in capsys.readouterr().out
    worker_api.app.state.token = "tok-2"                                               # 答话的是别的令牌的 worker
    assert pair.main(["check"]) == 6
    for other in (httpx.Response(404, text="Not Found"),                              # 别的服务占着端口
                  httpx.Response(200, json=dict(role="worker", machine_id="another", version="x"))):
        monkeypatch.setattr(pair.httpx, "get", lambda url, r=other, **kw: r)
        assert pair.main(["check"]) == 6 and "端口上答话的不是这次装的服务" in capsys.readouterr().out

    def refused(url, **kw):
        raise httpx.ConnectError("Connection refused")
    monkeypatch.setattr(pair.httpx, "get", refused)
    assert pair.main(["check"]) == 5


def test_pair_rotates_token_last(device, monkeypatch):
    """worker 的 POST /pair：本机 id、硬件这些先备好再换令牌（自审）：备的时候出错，令牌没换、配对码还能用。"""
    pair.remember("7Q4-K9M")

    def broken():
        raise OSError("nvidia-smi hung")
    monkeypatch.setattr(worker_api.hw, "hardware", broken)
    with pytest.raises(OSError):
        TestClient(worker_api.app).post("/pair", json=dict(code="7Q4-K9M"))
    assert pair.token() is None and pair.pending()


def test_reinstall_pairs_beside_the_running_one(device, monkeypatch):
    """重装时新的那一份用自己的令牌文件（安装脚本设 TTSAPI_DEVICE_FILE）：配对码、令牌都写在它旁边，正在跑的旧服务读的
    那份不动（旧服务每次心跳都重读令牌文件，写到一起它就带着新令牌去报心跳了）。"""
    live = settings.DEVICE_FILE
    pair._write(live, dict(token="tok-old", device_id="lan-abc123", control="http://127.0.0.1:9881"))
    monkeypatch.setattr(settings, "DEVICE_FILE", device / "releases" / "new" / "device.json")
    monkeypatch.setattr(pair.httpx, "post", lambda url, json, timeout: httpx.Response(
        200, json=dict(device_id="lan-abc123", token="tok-new", heartbeat_sec=30), request=httpx.Request("POST", url)))
    assert pair.main(["join", "http://127.0.0.1:9881", "7Q4-K9M"]) == 0
    assert json.loads(live.read_text())["token"] == "tok-old" and pair.token() == "tok-new"

    def down(url, json, timeout):
        raise httpx.ConnectError("Connection refused")
    monkeypatch.setattr(pair.httpx, "post", down)
    assert pair.main(["join", "http://127.0.0.1:9881", "7Q4-K9M"]) == 3
    assert (device / "releases" / "new" / "pair.json").exists() and not (device / "pair.json").exists()


def test_worker_heartbeat_loop(device, monkeypatch, caplog):
    """自动连回配对的设备定时报心跳（带令牌和现在的地址）；报不上只在状态变化时记日志；改成手动配对（令牌换了）后停。"""
    pair._write(settings.DEVICE_FILE, dict(token="tok-1", device_id="lan-abc123", control="http://127.0.0.1:9881"))
    monkeypatch.setattr(worker_api, "HEARTBEAT_SEC", 0)
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        if len(seen) <= 2:
            return httpx.Response(500)
        if len(seen) == 4:  # 用户在 Studio 里手动重新配对了：DEVICE_FILE 只剩新令牌
            pair._write(settings.DEVICE_FILE, dict(token="tok-2"))
        return httpx.Response(204)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with caplog.at_level(logging.INFO, logger="tts-api"):
        asyncio.run(asyncio.wait_for(worker_api.heartbeat(), 5))
    assert len(seen) == 4
    assert seen[0] == ("http://127.0.0.1:9881/devices/lan-abc123/heartbeat", "Bearer tok-1",
                       dict(address=f"http://127.0.0.1:{settings.PORT}"))
    logs = [r.getMessage() for r in caplog.records if "heartbeat" in r.getMessage()]
    assert len(logs) == 2 and "HTTPStatusError" in logs[0] and logs[1].endswith(": ok")


# ---------------------------------------------------------------- 安装脚本、代码包、80 端口

def test_install_script_and_code(ctl, monkeypatch, tmp_path):
    r = ctl.get("/install", headers={"host": f"Studio-Host.local:{settings.PORT}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-shellscript")
    assert f"CONTROL='http://studio-host.local:{settings.PORT}'" in r.text and "__CONTROL__" not in r.text
    assert f"INSTALL='http://studio-host.local:{settings.PORT}'" in r.text and "__INSTALL__" not in r.text  # 代码包从取脚本的地方取
    assert subprocess.run(["sh", "-n"], input=r.text, text=True).returncode == 0
    # Host 头里带了 shell 的字符（sol 的复现）：换成主控的 mDNS 名，脚本里的地址是单引号里的字面量
    for evil in ('x";touch /tmp/tts-pwn;"', "$(touch${IFS}/tmp/tts-pwn)", "a`id`b", "-x.local"):
        text = ctl.get("/install", headers={"host": evil}).text
        assert f"CONTROL='http://{pair.mdns_host()}:{settings.PORT}'" in text and "pwn" not in text and "`" not in text
        lines = "\n".join(line for line in text.splitlines() if line.strip().startswith(("CONTROL=", "INSTALL=")))
        out = subprocess.run(["sh", "-c", lines + '\nprintf "%s %s" "$CONTROL" "$INSTALL"'], capture_output=True, text=True)
        assert out.stdout == f"http://{pair.mdns_host()}:{settings.PORT} http://{pair.mdns_host()}:{settings.PORT}"

    fake_bundles(monkeypatch)
    r = ctl.get("/install/code.tar")
    with tarfile.open(fileobj=io.BytesIO(r.content)) as tf:
        assert sorted(tf.getnames()) == ["gsv-bbb.patch", "install.env", "tts-core-aaa.bundle"]
        env = dict(line.split("=", 1) for line in tf.extractfile("install.env").read().decode().splitlines())
        patch = tf.extractfile(env["GSV_PATCH"]).read()
    assert env["CORE_BUNDLE"] == "tts-core-aaa.bundle" and env["CORE_COMMIT"] == "c0ffee"
    assert env["GSV_PATCH"] == "gsv-bbb.patch" and env["GSV_BRANCH"] == "tts-studio" and env["VERSION"]
    assert env["GSV_PATCH_SHA256"] == hashlib.sha256(patch).hexdigest()


def fake_bundles(monkeypatch):
    def bundle_code(folder):
        for n in ("tts-core-aaa.bundle", "gsv-bbb.patch"):
            (folder / n).write_bytes(n.encode())
        return ({f"code/{n}": folder / n for n in ("tts-core-aaa.bundle", "gsv-bbb.patch")},
                dict(ttsapi_bundle="code/tts-core-aaa.bundle", ttsapi_commit="c0ffee", gsv_patch="code/gsv-bbb.patch",
                     gsv_patch_sha256=hashlib.sha256(b"gsv-bbb.patch").hexdigest(), gsv_base="bf81cdb", gsv_branch="tts-studio"), None)
    from ttsapi.compute import colab_jobs
    monkeypatch.setattr(colab_jobs, "bundle_code", bundle_code)


def test_worker_installer_applies_patch_from_code_tar(ctl, monkeypatch, tmp_path):
    from test_control_installer import SAMPLE_PATCH, _git
    from ttsapi.compute import colab_jobs

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    (upstream / "base.txt").write_text("base\n")
    _git(upstream, "add", "base.txt")
    _git(upstream, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
    base = _git(upstream, "rev-parse", "HEAD")

    core = tmp_path / "core"
    (core / "scripts" / "device").mkdir(parents=True)
    shutil.copy2(settings.ROOT / "scripts" / "gptsovits.sh", core / "scripts" / "gptsovits.sh")
    (core / "scripts" / "device" / "uninstall.sh").write_text("#!/bin/sh\n")
    (core / "scripts" / "pretrained.sha256").write_text("# empty fixture\n")
    (core / "scripts" / "install_linux.sh").write_text("""
mkdir -p "$GSV_ROOT/.venv/bin"
cat > "$GSV_ROOT/.venv/bin/python" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$GSV_ROOT/.venv/bin/python"
""")
    _git(core, "init", "-q")
    _git(core, "add", ".")
    _git(core, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "core")
    core_commit = _git(core, "rev-parse", "HEAD")

    def bundle_code(folder):
        bundle, patch = folder / "tts-core-test.bundle", folder / "gsv-test.patch"
        _git(core, "bundle", "create", str(bundle), "HEAD")
        patch.write_bytes(SAMPLE_PATCH)
        files = {f"code/{file.name}": file for file in (bundle, patch)}
        return files, dict(ttsapi_bundle=f"code/{bundle.name}", ttsapi_commit=core_commit,
                           gsv_patch=f"code/{patch.name}", gsv_patch_sha256=hashlib.sha256(SAMPLE_PATCH).hexdigest(),
                           gsv_base=base, gsv_branch=colab_jobs.GSV_BRANCH), None

    monkeypatch.setattr(colab_jobs, "bundle_code", bundle_code)
    response = ctl.get("/install/code.tar")
    assert response.status_code == 200
    archive = tmp_path / "code.tar"
    archive.write_bytes(response.content)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "curl": f'case "$*" in */install/code.tar) cat {shlex.quote(str(archive))} ;; *) exit 7 ;; esac',
        "uname": "echo Linux", "uv": "exit 0", "systemctl": "exit 0", "loginctl": "echo yes",
    }.items():
        stub = fake_bin / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)

    stub_home = tmp_path / "worker-home"
    env = dict(os.environ, HOME=str(stub_home), PATH=f"{fake_bin}:{os.environ['PATH']}",
               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.{upstream.as_uri()}.insteadOf",
               GIT_CONFIG_VALUE_0="https://github.com/RVC-Boss/GPT-SoVITS.git")
    install = ctl.get("/install", headers={"host": f"studio-host.local:{settings.PORT}"})
    result = subprocess.run(["sh", "-s", "--", "--pair", "7Q4-K9M"], input=install.text, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    engine = stub_home / ".tts-core" / "current" / "GPT-SoVITS"
    assert _git(engine, "rev-parse", "HEAD^") == base
    assert _git(engine, "branch", "--show-current") == "tts-studio"
    assert (engine / "base.txt").read_text() == "studio patch\n"


def test_install_port_serves_only_install(ctl, monkeypatch):
    from ttsapi import gateway
    monkeypatch.setattr(gateway, "install_port", 80)
    fake_bundles(monkeypatch)
    inv = ctl.post("/compute/invites", json={}).json()                                 # 80 端口开着：命令不带端口
    assert inv["commands"]["unix"] == f"curl -fsSL 'http://{pair.mdns_host()}/install' | sh -s -- --pair {inv['code']}"

    async def on_80(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=("192.168.1.5", 50000), server=("0.0.0.0", 80))
        await gateway.app(scope, receive, send)
    c80 = TestClient(on_80)
    r = c80.get("/install", headers={"host": "studio-host.local"})
    assert r.status_code == 200 and f"CONTROL='http://studio-host.local:{settings.PORT}'" in r.text   # 连回主控用 core 端口
    assert "INSTALL='http://studio-host.local'" in r.text                                         # 代码包从 80 取
    assert c80.get("/install/code.tar").status_code == 200
    for path in ("/compute", "/health", "/install/x", "/ui/"):
        assert c80.get(path).status_code == 404
    assert ctl.get("/health").status_code == 200                                         # core 端口照常


# ---------------------------------------------------------------- /ui/

def test_web_ui_mount(ctl, monkeypatch, tmp_path):
    from ttsapi import gateway
    # 测完把挂载撤掉（仓库里构建过 web/dist 时 import 时已经挂上了，先去掉）
    monkeypatch.setattr(gateway.app.router, "routes", [r for r in gateway.app.router.routes
                                                        if getattr(r, "path", None) != "/ui"])
    assert ctl.get("/system/info").json()["web_available"] is False
    assert gateway.mount_web(tmp_path / "nope") is False
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>studio</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    assert gateway.mount_web(dist) is True
    assert ctl.get("/system/info").json()["web_available"] is True
    assert ctl.get("/ui/").text == "<html>studio</html>"
    r = ctl.get("/ui", follow_redirects=False)
    assert r.status_code in (307, 308) and r.headers["location"].endswith("/ui/")
    assert ctl.get("/ui/settings/compute").text == "<html>studio</html>"                # 前端路由：回 index.html
    assert ctl.get("/ui/assets/app.js").text == "console.log(1)"
    assert ctl.get("/ui/assets/missing.js").status_code == 404                            # 缺的资源文件照样 404


# ---------------------------------------------------------------- 自审补的

def test_patch_repair_turns_device_manual(ctl, reachable):
    """自动连回的设备在设置里重新配对（PATCH pair_code）：设备换了令牌就不再报心跳，主控也不再按心跳算（不会 90 秒后
    一直离线、再也不去连它），last_seen 清掉。"""
    inv = ctl.post("/compute/invites", json={}).json()
    a = ctl.post("/devices/pair", json=join_body(inv["code"], address="http://worker-host.local:9880")).json()
    registry.heartbeat(a["device_id"], a["token"], "http://worker-host.local:9880")
    pair.remember("ABC-DEF")
    r = ctl.patch(f"/compute/devices/{a['device_id']}", json=dict(pair_code="ABC-DEF"))
    assert r.status_code == 200, r.text
    assert a["device_id"] not in registry.beats and r.json()["last_seen"] is None and r.json()["status"] == "idle"
    assert registry.load()["devices"][0]["token"] == pair.token() != a["token"]


def test_old_worker_without_machine_id(ctl, monkeypatch):
    """这一版之前的 worker（POST /pair 只回 token、machine，/worker/health 不报 machine_id）：手填照样加得上，不去重、
    不覆盖别的设备。"""
    async def old_health(self, timeout=3):
        return dict(role="worker", machine="win-gpu", hw=HW, engines=["v2pro"], loaded=[], busy=False, training=None)
    monkeypatch.setattr(remote.Remote, "health", old_health)

    async def token(address, code):
        return dict(token="tok-" + address[-4:], machine="win-gpu")
    monkeypatch.setattr(registry, "pair", token)
    first = ctl.post("/compute/devices", json=dict(address="http://win-gpu.local:9880", pair_code="ABC-DEF"))
    second = ctl.post("/compute/devices", json=dict(address="http://win-gpu.local:9881", pair_code="ABC-DEF"))
    assert first.status_code == second.status_code == 200, second.text
    assert [d["address"] for d in registry.load()["devices"]] == ["http://win-gpu.local:9880", "http://win-gpu.local:9881"]
    # 上一版的 worker：/pair 不回本机 id，/worker/health 回（自审）：从 health 拿，同一台再加不另加一台
    monkeypatch.setattr(remote.Remote, "health", lambda self, timeout=3: _nuc(self))
    for port in (9882, 9883):
        assert ctl.post("/compute/devices", json=dict(address=f"http://nuc.local:{port}", pair_code="ABC-DEF")).status_code == 200
    nucs = [d for d in registry.load()["devices"] if d.get("machine_id") == "nuc-1"]
    assert len(nucs) == 1 and nucs[0]["address"] == "http://nuc.local:9883" and nucs[0]["hw"] == HW


async def _nuc(self):
    return dict(role="worker", machine="worker-host", machine_id="nuc-1", hw=HW, engines=["v2pro"], loaded=[],
                busy=False, training=None)


def test_regenerate_while_manual_pairing(ctl):
    """手动填正在进行时点了「重新生成」：旧码作废；手动填没配上，旧码也不会重新能用。"""
    old = ctl.post("/compute/invites", json={}).json()
    inv = enroll.begin_manual(old["id"])
    ctl.post("/compute/invites", json={})
    inv["busy"] = False                                                                # 手动填失败，放开
    assert ctl.get(f"/compute/invites/{old['id']}").json()["state"] == "expired"
    assert ctl.post("/devices/pair", json=join_body(old["code"])).status_code == 403


def test_join_when_controller_name_does_not_resolve(device, capsys):
    """主控的名字解析不了（选不出网卡）：照样退回手动填（3），连接地址给 mDNS 名。"""
    assert pair.main(["join", "http://no-such-controller.invalid:9881", "7Q4-K9M"]) == 3
    assert f"连接地址：http://{pair.mdns_host()}:{settings.PORT}" in capsys.readouterr().out and pair.pending()
