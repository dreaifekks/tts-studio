"""在哪台设备上调 worker：本机直接调 workers.ENGINES / SCORER；局域网设备经它的 /worker/call 调。

文件（参考音频、权重、要打分的音频）按 sha256 传：主控先只发 sha，设备上没有的返回 409 和缺的 sha，
主控把这些文件 PUT /worker/blobs/{sha} 传过去（设备核对 sha256 后缓存），再发一次。
底模不传：设备用它自己装好的同名底模。产出的文件（合成的 wav、频谱图）随响应带回，主控写到本地路径。
"""
import asyncio
import base64
import hashlib
from pathlib import Path

import httpx
from fastapi import HTTPException

from ttsapi import settings
from ttsapi.workers import ENGINES, SCORER

_SHA: dict = {}  # (路径, 大小, mtime_ns) -> sha256


def sha256_cached(path: Path) -> str:
    """文件的 sha256，按路径 + 大小 + 修改时间缓存（权重几百 MB，每次派任务都算太慢）。"""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _SHA:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        _SHA[key] = h.hexdigest()
    return _SHA[key]


def pretrained_key(engine: str, path: str):
    """path 是这个引擎的底模时返回 "pretrained_gpt" / "pretrained_sovits"（设备用自己的），否则 None。"""
    cfg = settings.ENGINES[engine]
    return next((k for k in ("pretrained_gpt", "pretrained_sovits") if Path(path) == cfg[k]), None)


def _worker(name: str):
    return SCORER if name == "scorer" else ENGINES[name]


class Local:
    """本机。"""
    id, type = "local", "local"

    async def call(self, worker: str, path: str, payload: dict, files: dict = None, outputs: dict = None,
                   pretrained: dict = None) -> dict:
        """files / outputs: payload field -> local path (or list of paths for files). pretrained is unused locally."""
        file_fields = {
            k: [str(p) for p in v] if isinstance(v, (list, tuple)) else str(v)
            for k, v in (files or {}).items()
        }
        out_fields = {k: str(v) for k, v in (outputs or {}).items()}
        return await _worker(worker).call(path, {**payload, **file_fields, **out_fields})


class Remote:
    """局域网设备（core 以 --role worker 运行）。dev：registry 里的设备 {id, name, address, token}。"""
    type = "lan"

    def __init__(self, dev: dict):
        self.id, self.name = dev["id"], dev["name"]
        self.url = dev["address"].rstrip("/")
        self.headers = {"Authorization": f"Bearer {dev['token']}"}

    async def health(self, timeout=3) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(self.url + "/worker/health", headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def put_blob(self, client, path: Path, sha: str):
        async def chunks():
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    yield chunk
        r = await client.put(f"{self.url}/worker/blobs/{sha}", content=chunks(), headers=self.headers)
        self._check(r)

    async def _post(self, path: str, body: dict, files: dict, timeout: float):
        """POST，worker 说缺文件（409 missing_blobs）时把缺的传过去再发一次。files：sha -> 本地路径。"""
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10)) as client:
            r = await client.post(self.url + path, json=body, headers=self.headers)
            if r.status_code == 409 and r.json().get("code") == "missing_blobs":
                for sha in r.json()["missing"]:
                    await self.put_blob(client, files[sha], sha)
                r = await client.post(self.url + path, json=body, headers=self.headers)
            self._check(r)
        return r.json()

    async def call(self, worker: str, path: str, payload: dict, files: dict = None, outputs: dict = None,
                   pretrained: dict = None) -> dict:
        blob_refs, by_sha = {}, {}
        for k, v in (files or {}).items():
            if k in (pretrained or {}):
                continue
            if isinstance(v, (list, tuple)):
                if not v:
                    continue
                shas = []
                for p in map(Path, v):
                    s = await asyncio.to_thread(sha256_cached, p)
                    shas.append(s)
                    by_sha[s] = p
                blob_refs[k] = shas
            else:
                p = Path(v)
                s = await asyncio.to_thread(sha256_cached, p)
                blob_refs[k] = s
                by_sha[s] = p
        body = dict(worker=worker, path=path, payload=payload, blobs=blob_refs, pretrained=pretrained or {},
                    outputs={k: Path(v).suffix for k, v in (outputs or {}).items()})
        res = await self._post("/worker/call", body, by_sha, 900)
        for k, dst in (outputs or {}).items():
            Path(dst).write_bytes(base64.b64decode(res["outputs"][k]))
        return res["result"]

    async def bench(self, ref: dict, engines: list = None, quick: bool = False) -> dict:
        """在这台设备上测速（完整测速约 8–15 分钟），返回测速结果；最佳设置写回那台设备自己的 tuning。"""
        sha = await asyncio.to_thread(sha256_cached, Path(ref["path"]))
        body = dict(ref=sha, text=ref["text"], lang=ref["lang"], engines=engines, quick=quick)
        return await self._post("/worker/bench", body, {sha: Path(ref["path"])}, 3600)

    def _check(self, r):
        if r.status_code == 503:
            raise HTTPException(503, f"{self.name}: {r.json()['detail']}", headers={"Retry-After": "120"})
        if r.status_code != 200:
            raise HTTPException(500, f"{self.name}: {r.json().get('detail', r.text)}")


LOCAL = Local()
