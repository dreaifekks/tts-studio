"""阶段 2 第 3 轮给 Studio 补的接口：角色的模型版本（GET /characters/{id}/versions、生成等接口的 version）、
/plan 多引擎、GET /system/info 的浏览器链接、参考音频的显示名。"""
from datetime import datetime

import pytest
from conftest import seed_carol
from pydantic import ValidationError

from ttsapi.catalog import Catalog, VersionUnavailable, versions
from ttsapi.models import BlindSide, ComboEval, TTSParams, WorkLine

Y = datetime.now().year


@pytest.fixture
def api(lib, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from ttsapi import gateway, settings
    from ttsapi.compute import registry
    monkeypatch.setattr(settings, "COMPUTE_FILE", tmp_path / "compute.json")
    monkeypatch.setattr(settings, "SPEEDS_FILE", tmp_path / "speeds.json")
    monkeypatch.setattr(settings, "installed_engines", lambda: ["v2pro", "v2proplus", "v4"])
    monkeypatch.setattr(registry, "_hw", dict(cpu="Apple M6", cores=12, ram_gb=32, accel="mps", gpu=None, os="darwin",
                                              torch="2.11.0"))

    async def local(scope, receive, send):
        await gateway.app(dict(scope, client=("127.0.0.1", 50000)) if scope["type"] == "http" else scope, receive, send)
    return TestClient(local)


# ---------------------------------------------------------------- 模型版本

def weight(tmp_path, name):
    f = tmp_path / "w" / name
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(name.encode() * 10)
    return f


def add_run(cat, tmp_path, name, created, gpts, sovits, best=None):
    cat.register_run("carol", "SAMPLE_DS.sp0001", dict(run_name=name, engine="v2pro", created=created),
                     dict(gpt=[weight(tmp_path, g) for g in gpts], sovits=[weight(tmp_path, s) for s in sovits]))
    if best:
        cat.set_best(name, *best)


def add_eval(lib, job, combos):
    lib.put(f"evals/{Y}-11/{job}.json", dict(
        id=job, job_id=job, engine="v2pro", character="carol", ref_id="r", eval_set="hard", created=f"{Y}-11-20T00:00:00",
        combos=[dict(gpt=g, sovits=s, n=1, cer_median=0.1, spk_sim_median=0.8, score_median=sc,
                     evaluated_at=f"{Y}-11-20T00:00:00") for g, s, sc in combos]))


@pytest.fixture
def carol(lib, tmp_path, monkeypatch):
    """内置角色 carol：7 月训练的权重、今年 11 月两次训练（一次设了 best，一次靠评测分）、去年 12 月一次（没评测）。"""
    from ttsapi import settings
    monkeypatch.setattr(settings, "JULY_TRAINED", f"{Y}-07-01T00:00:00+09:00")
    seed_carol(lib)
    cat = Catalog()
    for kind, name in (("gpt", "carol-e10.ckpt"), ("gpt", "carol-e15.ckpt"), ("sovits", "carol_e8.pth")):
        cat.add_weight("carol", "v2pro", kind, weight(tmp_path, name), "july", created=f"{Y}-07-05T10:00:00+09:00")
    add_run(cat, tmp_path, "carol-c", f"{Y - 1}-12-01T00:00:00+09:00", ["c-e5.ckpt", "c-e10.ckpt"], ["c_e4.pth", "c_e8.pth"])
    add_run(cat, tmp_path, "carol-a", f"{Y}-11-01T00:00:00+09:00", ["a-e5.ckpt", "a-e10.ckpt"], ["a_e4.pth", "a_e8.pth"],
            best=("a-e5.ckpt", "a_e8.pth"))
    add_run(cat, tmp_path, "carol-b", f"{Y}-11-15T00:00:00+09:00", ["b-e5.ckpt", "b-e10.ckpt"], ["b_e4.pth", "b_e8.pth"])
    add_eval(lib, "j1", [("b-e5.ckpt", "b_e4.pth", 0.7), ("b-e10.ckpt", "b_e8.pth", 0.9),
                         ("carol-e15.ckpt", "carol_e8.pth", 0.5)])
    return cat


def test_versions_from_runs_july_and_labels(carol):
    vs = versions(carol, "carol")
    assert [(v["label"], v["gpt"], v["sovits"], v["score"], v["source_run"], v["recommended"]) for v in vs] == [
        ("11 月训练 2", "b-e10.ckpt", "b_e8.pth", 0.9, "carol-b", True),       # 没设 best：评测分最高；全场最高分 → 推荐
        ("11 月训练 1", "a-e5.ckpt", "a_e8.pth", None, "carol-a", False),      # 设了 best 用它
        ("7 月训练", "carol-e15.ckpt", "carol_e8.pth", 0.5, None, False),
        (f"{Y - 1} 年 12 月训练", "c-e10.ckpt", "c_e8.pth", None, "carol-c", False),  # 没评测：最后一个 checkpoint
    ]
    assert vs[0]["id"] == "v2pro/b-e10.ckpt/b_e8.pth" and vs[0]["engine"] == "v2pro"


def test_version_label_is_training_time_not_import_time(lib, tmp_path, monkeypatch):
    """run.json 的 created 是登记进资料库的时间（导入、开发建库时就是今天）：label 按 run 名末尾的训练启动时间，
    名字里没有时间的才用 created。7 月那批（source=july）复制到别的机器时文件时间丢了：按 settings.JULY_TRAINED。"""
    from ttsapi import settings
    from ttsapi.catalog import trained_at
    seed_carol(lib)
    cat = Catalog()
    today = f"{Y}-11-20T12:00:00+09:00"
    for kind, name in (("gpt", "carol-e15.ckpt"), ("sovits", "carol_e8.pth")):
        cat.add_weight("carol", "v2pro", kind, weight(tmp_path, name), "july", created=today)
    add_run(cat, tmp_path, f"carol-{Y}0812-093000", today, ["a-e5.ckpt"], ["a_e4.pth"])
    add_run(cat, tmp_path, f"carol-{Y - 1}1001-235959", today, ["b-e5.ckpt"], ["b_e4.pth"])
    add_run(cat, tmp_path, "carol-imported", f"{Y}-09-03T00:00:00+09:00", ["c-e5.ckpt"], ["c_e4.pth"])
    monkeypatch.setattr(settings, "JULY_TRAINED", f"{Y}-07-01T00:00:00+09:00")
    vs = {v["source_run"]: v for v in versions(cat, "carol")}
    assert {k: v["label"] for k, v in vs.items()} == {
        f"carol-{Y}0812-093000": "8 月训练", f"carol-{Y - 1}1001-235959": f"{Y - 1} 年 10 月训练",
        "carol-imported": "9 月训练", None: "7 月训练"}
    started = datetime.fromisoformat(vs[f"carol-{Y}0812-093000"]["created"])
    assert started.utcoffset() is not None and started.replace(tzinfo=None) == datetime(Y, 8, 12, 9, 30)
    assert trained_at(dict(run_name="carol-v4", created=today)) == today


def test_versions_manual_default(carol):
    carol.set_default("v2pro", "carol", "gpt", "a-e10.ckpt")              # 只设了 GPT：SoVITS 用自动默认（b_e8）
    vs = versions(carol, "carol")
    mine = next(v for v in vs if v["recommended"])
    assert (mine["label"], mine["gpt"], mine["sovits"], mine["source_run"]) == \
        ("我设的默认", "a-e10.ckpt", "b_e8.pth", "carol-a")
    assert len(vs) == 5
    carol.set_default("v2pro", "carol", "sovits", "a_e8.pth")
    carol.set_default("v2pro", "carol", "gpt", "a-e5.ckpt")              # 和 11 月第一次训练的 best 一样：不另列
    vs = versions(carol, "carol")
    assert len(vs) == 4 and [v["label"] for v in vs if v["recommended"]] == ["11 月训练 1"]


def test_versions_newest_when_unscored_and_zero_shot(lib, tmp_path):
    seed_carol(lib)
    cat = Catalog()
    vs = versions(cat, "carol", {"v2pro", "v4"})                        # 一个自己的权重都没有：底模零样本
    assert [(v["label"], v["engine"], v["recommended"], v["source_run"]) for v in vs] == \
        [("底模（零样本）", "v2pro", True, None)]
    assert [v["engine"] for v in versions(cat, "carol", {"v4", "v5turbo"})] == ["v4"]   # 只从装了的引擎里出
    assert versions(cat, "carol", set()) == [] and versions(cat, "carol") == []           # 没有能用的引擎
    add_run(cat, tmp_path, "carol-x", f"{Y}-03-01T00:00:00+09:00", ["x-e1.ckpt"], ["x_e1.pth"])
    add_run(cat, tmp_path, "carol-y", f"{Y}-04-01T00:00:00+09:00", ["y-e1.ckpt"], ["y_e1.pth"])
    assert [(v["label"], v["recommended"]) for v in versions(cat, "carol")] == [("4 月训练", True), ("3 月训练", False)]


def test_versions_api(api, carol):
    r = api.get("/characters/carol/versions")
    assert r.status_code == 200 and r.json()[0]["label"] == "11 月训练 2"
    assert api.get("/characters/nope/versions").status_code == 404


def test_version_param_splits_into_weights(carol):
    p = TTSParams(text="x", character="carol", version="v2pro/a-e5.ckpt/a_e8.pth")
    assert (p.engine, p.gpt, p.sovits) == ("v2pro", "a-e5.ckpt", "a_e8.pth")
    assert carol.resolve(p.engine, p.character, p.gpt, p.sovits)[0] == "a-e5.ckpt"
    p = TTSParams(text="x", version="v4/g.ckpt/s.pth", gpt="g.ckpt")    # 一致的不算冲突（存下来再读回来时就是这样）
    assert p.engine == "v4"
    for bad in (dict(version="v2pro/a.ckpt/b.pth", gpt="other.ckpt"), dict(version="v2pro/a.ckpt/b.pth", engine="v4"),
                dict(version="v2pro/a.ckpt"), dict(version="v9/a.ckpt/b.pth")):
        with pytest.raises(ValidationError):
            TTSParams(text="x", **bad)
    side = BlindSide(version="v2proplus/g.ckpt/s.pth")
    assert (side.engine, side.gpt, side.sovits) == ("v2proplus", "g.ckpt", "s.pth")
    # 存下来的作品行（version 已拆好）读回来照常
    line = WorkLine(id="l1", character="carol", text="x", candidates=[], picked=None,
                    **TTSParams(text="x", version="v2pro/g.ckpt/s.pth").model_dump(include={"engine", "gpt", "sovits",
                                                                                             "version"}))
    assert line.gpt == "g.ckpt"


# ---------------------------------------------------------------- /plan 多引擎

def test_plan_multi_engine(api):
    def secs(**kw):
        r = api.post("/plan", json=dict(kind="gen", **kw))
        assert r.status_code == 200, r.text
        return next(c["seconds"] for c in r.json()["candidates"] if c["device"] == "local")
    prep = secs(engine="v2pro", items=0)
    v2, v4 = secs(engine="v2pro", items=10), secs(engine="v4", items=10)
    assert v4 > v2 > prep
    assert abs(secs(engines=["v2pro", "v4"], items=10) - (v2 + v4 - prep)) <= 1      # 各引擎分别算再累加
    assert abs(secs(engines=["v4", "v4"], items=10) - (2 * v4 - prep)) <= 1          # 盲听两边同引擎
    assert abs(secs(parts=[dict(engine="v2pro", items=10), dict(engine="v4", items=3)]) -
               (v2 + secs(engine="v4", items=3) - prep)) <= 1
    for bad in (dict(), dict(engine="v2pro", engines=["v4"]), dict(engines=[]),
                dict(kind="train", engines=["v2pro"], lines=100), dict(kind="eval", engines=["v2pro", "v4"]),
                dict(kind="eval", parts=[dict(engine="v2pro", items=3)])):   # 多引擎只给生成类
        assert api.post("/plan", json=dict(dict(kind="gen"), **bad)).status_code == 422


# ---------------------------------------------------------------- 浏览器链接、参考音频显示名

def test_system_info(api, monkeypatch):
    from ttsapi import pair, settings
    monkeypatch.setattr(pair.socket, "gethostname", lambda: "studio-host")
    monkeypatch.setattr(settings, "PORT", 9881)
    assert api.get("/system/info").json() == dict(web_url="http://studio-host.local:9881/ui/", host="studio-host.local", port=9881,
                                                  lan_reachable=True, web_available=False, machine=settings.MACHINE_NAME)
    from starlette.routing import Mount

    from ttsapi import gateway
    gateway.app.router.routes.append(Mount("/ui", app=lambda scope, receive, send: None))   # 挂了 Web 界面
    try:
        assert api.get("/system/info").json()["web_available"]
    finally:
        gateway.app.router.routes.pop()
    monkeypatch.setattr(pair.socket, "gethostname", lambda: "gpu-worker.lan")
    monkeypatch.setattr(settings, "HOST", "127.0.0.1")
    r = api.get("/system/info").json()
    assert (r["web_url"], r["lan_reachable"]) == ("http://gpu-worker.local:9881/ui/", False)
    monkeypatch.setattr(pair.socket, "gethostname", lambda: "mba.local")
    assert api.get("/system/info").json()["host"] == "mba.local"


def test_refs_api_has_label(api, lib):
    seed_carol(lib)
    from ttsapi import gateway
    gateway.refs.reload()
    refs = api.get("/refs", params=dict(character="carol")).json()["refs"]
    assert {r["label"] for r in refs} == {"默认参考音频 · 6.1 秒", "……っ · 5.9 秒"}  # 1 秒那条不进参考库


# ---------------------------------------------------------------- 第 4 轮审查修正

def test_versions_api_zero_shot_uses_gen_engines(api, lib, monkeypatch):
    from ttsapi.compute import registry
    seed_carol(lib)

    async def engines():
        return {"v4"}
    monkeypatch.setattr(registry, "gen_engines", engines)
    assert [v["engine"] for v in api.get("/characters/carol/versions").json()] == ["v4"]


def test_gen_engines_skip_unusable_devices(api, monkeypatch):
    import asyncio

    from ttsapi.compute import registry
    assert asyncio.run(registry.gen_engines()) == {"v2pro", "v2proplus", "v4"}   # 本机；云端卡没装组件
    api.patch("/compute/devices/local", json=dict(paused=True))
    assert asyncio.run(registry.gen_engines()) == set()


def test_version_resolves_own_weights_when_names_clash(lib, tmp_path):
    """两个角色有同名权重：按 carol 的版本解析到 carol 自己的文件，按另一个角色解析到它的；不同名的照旧能用任何角色的。"""
    seed_carol(lib)
    cat = Catalog()
    Catalog.add_character("carol-b", "user", "SAMPLE_DS.sp0001")
    for slug in ("carol", "carol-b"):
        for kind, name in (("gpt", "same-e5.ckpt"), ("sovits", "same_e5.pth")):
            cat.add_weight(slug, "v2pro", kind, weight(tmp_path, name), "july", created=f"{Y}-07-05T10:00:00+09:00")
    cat.add_weight("carol-b", "v2pro", "gpt", weight(tmp_path, "only-b-e1.ckpt"), "july")
    for slug in ("carol", "carol-b"):
        v = versions(cat, slug)[0]
        p = TTSParams(text="x", character=slug, version=v["id"])
        _, gpt_path, _, sovits_path = cat.resolve(p.engine, p.character, p.gpt, p.sovits)
        assert f"/weights/{slug}/v2pro/" in gpt_path and f"/weights/{slug}/v2pro/" in sovits_path
    assert "/weights/carol-b/" in cat.resolve("v2pro", "carol", "only-b-e1.ckpt", "same_e5.pth")[1]


def test_version_recommendation_ranks_within_best_eval_set(carol, lib):
    """只有 mini 成绩的版本分再高，也不压过有 hard 成绩的（先挑这个角色有成绩的最优先评测集，在集内排）。"""
    lib.put(f"evals/{Y}-11/j2.json", dict(
        id="j2", job_id="j2", engine="v2pro", character="carol", ref_id="r", eval_set="mini", created=f"{Y}-11-21T00:00:00",
        combos=[dict(gpt="a-e5.ckpt", sovits="a_e8.pth", n=1, cer_median=0.0, spk_sim_median=1.0, score_median=0.99,
                     evaluated_at=f"{Y}-11-21T00:00:00")]))
    vs = {v["label"]: v for v in versions(carol, "carol")}
    assert (vs["11 月训练 1"]["score"], vs["11 月训练 1"]["score_set"]) == (0.99, "mini")
    assert (vs["11 月训练 2"]["score_set"], vs["11 月训练 2"]["recommended"]) == ("hard", True)
    assert not vs["11 月训练 1"]["recommended"]


def test_compare_version_engine():
    from ttsapi.models import CompareParams
    p = CompareParams(text="x", version="v4/g.ckpt/s.pth")
    assert p.engines == ["v4", "v2pro", "v5turbo"] and (p.engine, p.gpt) == ("v4", "g.ckpt")
    assert CompareParams(text="x", version="v2pro/g.ckpt/s.pth").engines == ["v2pro", "v5turbo"]
    assert CompareParams(text="x", version="v4/g.ckpt/s.pth", engines=["v2pro", "v4"]).engines == ["v2pro", "v4"]
    with pytest.raises(ValidationError):
        CompareParams(text="x", version="v4/g.ckpt/s.pth", engines=["v2pro", "v5turbo"])
    assert CompareParams(text="x").engines == ["v2pro", "v5turbo"]


# ---------------------------------------------------------------- 第 5 轮审查修正


def two_chars_same_names(lib, tmp_path, names=(("gpt", "same-e5.ckpt"), ("sovits", "same_e5.pth"))):
    """carol 和训练出来的 carol-b 各有一套同名的 7 月权重。"""
    seed_carol(lib)
    cat = Catalog()
    Catalog.add_character("carol-b", "user", "SAMPLE_DS.sp0001")
    for slug in ("carol", "carol-b"):
        for kind, name in names:
            cat.add_weight(slug, "v2pro", kind, weight(tmp_path, name), "july", created=f"{Y}-07-05T10:00:00+09:00")
    return cat


def test_version_missing_weight_does_not_borrow_other_characters(api, lib, tmp_path):
    """carol 的版本缺了 GPT（删掉了），carol-b 有同名 GPT：给 version 时 409 version_unavailable，不混用 carol-b 的；
    手填 gpt / sovits 照旧（同名先用自己的，自己没有就用别的角色的）。任务版、作品行、盲听在提交时就 409。"""
    from ttsapi import gateway
    cat = two_chars_same_names(lib, tmp_path)
    gateway.refs.reload()
    vid = versions(cat, "carol")[0]["id"]
    cat.delete_weight(next(x for x in cat.entries("v2pro") if x["character"] == "carol" and x["kind"] == "gpt"))
    p = TTSParams(text="x", character="carol", version=vid)
    with pytest.raises(VersionUnavailable):
        cat.resolve(p.engine, p.character, p.gpt, p.sovits, version=p.version)
    _, gpt_path, _, sovits_path = cat.resolve(p.engine, p.character, p.gpt, p.sovits)    # 手填：行为不变
    assert "/weights/carol-b/" in gpt_path and "/weights/carol/" in sovits_path
    body = dict(text="x", character="carol", version=vid)
    for path in ("/tts", "/tts/candidates/jobs", "/compare/jobs"):
        r = api.post(path, json=dict(body, engines=["v2pro"]) if path == "/compare/jobs" else body)  # 本机没装 v5turbo
        assert r.status_code == 409 and r.json()["code"] == "version_unavailable", path
    w = api.post("/works", json=dict(title="t", lines=[dict(character="carol", text="x", version=vid)])).json()
    r = api.post(f"/works/{w['id']}/generate", json=dict(n=1))
    assert r.status_code == 409 and r.json()["code"] == "version_unavailable"
    r = api.post("/blind", json=dict(character="carol", a=dict(version=vid), b=dict(engine="v2pro")))
    assert r.status_code == 409 and r.json()["code"] == "version_unavailable"


def test_evals_of_same_named_weights_stay_with_their_character(lib, tmp_path):
    """同名权重的成绩按「评测角色 + 权重记录」认：carol-b 的同名权重出了新成绩，carol 的版本分数和推荐不变；
    用 carol 的参考评 carol-b 才有的权重，之后 carol 有了同名权重也不算 carol 的。/weights 里各自只列自己的成绩。"""
    cat = two_chars_same_names(lib, tmp_path)
    add_run(cat, tmp_path, "carol-a", f"{Y}-11-01T00:00:00+09:00", ["a-e5.ckpt"], ["a_e8.pth"])

    def score(slug, g, s, sc):
        cat.record_eval("v2pro", slug, "r", f"j-{slug}-{g}", "hard", ComboEval(
            gpt=g, sovits=s, n=1, cer_median=0.1, spk_sim_median=0.8, score_median=sc))
    score("carol", "same-e5.ckpt", "same_e5.pth", 0.5)
    score("carol", "a-e5.ckpt", "a_e8.pth", 0.7)
    score("carol-b", "same-e5.ckpt", "same_e5.pth", 0.99)                 # carol-b 的同名权重，最新、分最高
    vs = {v["label"]: v for v in versions(cat, "carol")}
    assert (vs["7 月训练"]["score"], vs["7 月训练"]["recommended"]) == (0.5, False)
    assert (vs["11 月训练"]["score"], vs["11 月训练"]["recommended"]) == (0.7, True)
    assert versions(cat, "carol-b")[0]["score"] == 0.99
    entries = {(e.character, e.name): e for e in cat.weight_entries()}
    assert [c.score_median for c in entries["carol", "same-e5.ckpt"].evals] == [0.5]
    assert [c.score_median for c in entries["carol-b", "same-e5.ckpt"].evals] == [0.99]
    for kind, name in (("gpt", "late-e1.ckpt"), ("sovits", "late_e1.pth")):
        cat.add_weight("carol-b", "v2pro", kind, weight(tmp_path, name), "july")
    score("carol", "late-e1.ckpt", "late_e1.pth", 1.0)                   # 用 carol 的参考评 carol-b 的权重
    lib.put(f"evals/{Y}-11/legacy.json", dict(                          # 没记 owner 的旧记录：按现在的目录推断
        id="legacy", job_id="legacy", engine="v2pro", character="carol", ref_id="r", eval_set="hard",
        created=f"{Y}-11-20T00:00:00", combos=[dict(gpt="same-e5.ckpt", sovits="late_e1.pth", n=1, cer_median=0.1,
                                                    spk_sim_median=0.8, score_median=1.0,
                                                    evaluated_at=f"{Y}-11-20T00:00:00")]))
    for kind, name in (("gpt", "late-e1.ckpt"), ("sovits", "late_e1.pth")):
        cat.add_weight("carol", "v2pro", kind, weight(tmp_path, name), "july", created=f"{Y}-07-05T10:00:00+09:00")
    evals = cat.latest_evals()
    assert ("v2pro", "carol", "late-e1.ckpt", "late_e1.pth", "hard") not in evals
    assert ("v2pro", "carol", "same-e5.ckpt", "late_e1.pth", "hard") in evals   # 旧记录：carol 现在有这两个权重
    assert ("v2pro", "carol-b", "late-e1.ckpt", "late_e1.pth", "hard") not in evals


def test_zero_shot_versions_follow_gen_eligibility(api, lib, monkeypatch):
    """零样本只从生成任务能派到的设备能跑的引擎里出：本机只装了 v4、v2pro 只有云端能跑时推荐 v4（生成不派云端）；
    局域网设备没测过 v2pro 的速度时也不算，测过了才优先 v2pro。"""
    import asyncio

    from test_compute import AUTO, l4, lan, mini

    from ttsapi.compute import registry
    seed_carol(lib)
    devices = [mini(engines=["v4"]), l4()]

    async def snapshot():
        return devices, AUTO
    monkeypatch.setattr(registry, "snapshot", snapshot)
    assert asyncio.run(registry.gen_engines()) == {"v4"}
    assert [(v["engine"], v["recommended"]) for v in api.get("/characters/carol/versions").json()] == [("v4", True)]
    devices.append(lan(speeds={"v4": (10, "measured"), "scorer": (2, "measured")}))   # v2pro 没测过、没参考值
    assert asyncio.run(registry.gen_engines()) == {"v4"}
    devices[-1] = lan()
    assert asyncio.run(registry.gen_engines()) == {"v2pro", "v2proplus", "v4"}
    assert [v["engine"] for v in api.get("/characters/carol/versions").json()] == ["v2pro"]


def test_version_bound_in_running_jobs(api, lib, tmp_path):
    """任务跑起来时同样只认这个角色的权重：候选、作品行（行上存了 version）、盲听补生成，提交后版本的权重被删了也不混用。"""
    import asyncio
    from types import SimpleNamespace

    from ttsapi import blind, gateway, synth, works
    from ttsapi.models import BlindCreate, GenerateRequest
    cat = two_chars_same_names(lib, tmp_path)
    gateway.refs.reload()
    vid = versions(cat, "carol")[0]["id"]
    w = api.post("/works", json=dict(title="t", lines=[dict(character="carol", text="x", version=vid)])).json()
    doc = blind.create(BlindCreate(character="carol", a=BlindSide(version=vid), b=BlindSide()), cat, gateway.refs)
    cat.delete_weight(next(x for x in cat.entries("v2pro") if x["character"] == "carol" and x["kind"] == "sovits"))
    run = SimpleNamespace(job=SimpleNamespace(), stage=lambda *a: None)
    for job in (synth.run_candidates(TTSParams(text="x", character="carol", version=vid), "v2pro", 1, {}, 0),
                works.generate_job(run, w["id"], GenerateRequest(n=1)),
                blind._generate(run, doc["id"])):
        with pytest.raises(VersionUnavailable):
            asyncio.run(job)


def test_fixed_compute_zero_shot_only_from_pinned_device(api, lib, monkeypatch):
    """固定了算力设备时，零样本只从那一台能跑的引擎里出（和不指定设备提交时的派发一样）：固定在只装 v4 的本机，
    局域网设备能跑 v2pro 也只推荐 v4；改成自动才优先 v2pro。"""
    import asyncio

    from test_compute import AUTO, lan, mini

    from ttsapi.compute import registry
    seed_carol(lib)
    devices, auto = [mini(engines=["v4"]), lan()], dict(AUTO, mode="fixed", device="local")

    async def snapshot():
        return devices, auto
    monkeypatch.setattr(registry, "snapshot", snapshot)
    assert asyncio.run(registry.gen_engines()) == {"v4"}
    assert [(v["engine"], v["recommended"]) for v in api.get("/characters/carol/versions").json()] == [("v4", True)]
    auto["device"] = "lan-aaaaaa"
    assert asyncio.run(registry.gen_engines()) == {"v2pro", "v2proplus", "v4"}
    auto.update(mode="auto", device=None)
    assert [v["engine"] for v in api.get("/characters/carol/versions").json()] == ["v2pro"]


def legacy_names(monkeypatch):
    """旧数据：改名规则之前进库的训练权重按原名放着，可能和底模同名（不迁移）。版本 id、手动默认、成绩照样按记录分开。"""
    from ttsapi import catalog as catalog_module
    monkeypatch.setattr(catalog_module, "stored_name", lambda slug, engine, name: name)


def test_version_does_not_fall_back_to_same_named_pretrained(api, lib, tmp_path, monkeypatch):
    """版本 id 里底模写成 pretrained，其余是角色训练出的权重：carol 训练出的 GPT 和底模同名（旧数据），删了它之后提交旧版本
    409 version_unavailable，不换成底模；底模版本照常解析到底模。"""
    from ttsapi import gateway, settings
    seed_carol(lib)
    cat = Catalog()
    gateway.refs.reload()
    legacy_names(monkeypatch)
    pre = settings.ENGINES["v2pro"]["pretrained_gpt"].name
    for kind, name in (("gpt", pre), ("sovits", "carol_e8.pth")):
        cat.add_weight("carol", "v2pro", kind, weight(tmp_path, name), "july", created=f"{Y}-07-05T10:00:00+09:00")
    vid = versions(cat, "carol")[0]["id"]
    assert vid == f"v2pro/{pre}/carol_e8.pth"
    cat.delete_weight(next(x for x in cat.entries("v2pro") if x["character"] == "carol" and x["kind"] == "gpt"))
    p = TTSParams(text="x", character="carol", version=vid)
    with pytest.raises(VersionUnavailable):
        cat.resolve(p.engine, p.character, p.gpt, p.sovits, remote=True, version=p.version)
    r = api.post("/tts", json=dict(text="x", character="carol", version=vid))
    assert r.status_code == 409 and r.json()["code"] == "version_unavailable"
    zero = TTSParams(text="x", character="carol", version="v2pro/pretrained/carol_e8.pth")    # GPT 用底模
    assert zero.gpt == pre
    assert cat.resolve(zero.engine, "carol", zero.gpt, zero.sovits, remote=True, version=zero.version)[1] == \
        str(settings.ENGINES["v2pro"]["pretrained_gpt"])
    Catalog.add_character("blank", "user", "SAMPLE_DS.sp0001")
    assert versions(cat, "blank", {"v2pro"})[0]["id"] == "v2pro/pretrained/pretrained"


# ---------------------------------------------------------------- 按版本评测

def test_eval_by_version(api, lib, tmp_path, monkeypatch):
    """POST /weights/eval 收 version，和生成一样按版本绑定的记录评测：版本的 GPT 是底模、carol 恰好有同名的训练 GPT
    （旧数据）时，跑的是底模，成绩落库记底模（gpt_owner null），索引里写 pretrained，不算到 carol 训练的组合上；反过来版本是
    carol 训练的同名 GPT 时，云端评测带上的是 carol 的那个文件。缺了 409 version_unavailable；不给 character 422。"""
    import asyncio
    from types import SimpleNamespace

    from ttsapi import gateway, settings, synth, training
    from ttsapi.compute import colab_jobs
    from ttsapi.models import EvalRequest
    seed_carol(lib)
    cat = Catalog()
    gateway.refs.reload()
    pre = tmp_path / "pre" / settings.ENGINES["v2pro"]["pretrained_gpt"].name   # 本机装了的底模（假文件）
    pre.parent.mkdir()
    pre.write_bytes(b"pretrained")
    monkeypatch.setitem(settings.ENGINES["v2pro"], "pretrained_gpt", pre)
    legacy_names(monkeypatch)
    for kind, name in (("gpt", pre.name), ("sovits", "carol_e8.pth")):
        cat.add_weight("carol", "v2pro", kind, weight(tmp_path, name), "july", created=f"{Y}-07-05T10:00:00+09:00")
    r = api.post("/weights/eval", json=dict(version="v2pro/pretrained/carol_e8.pth"))
    assert r.status_code == 422
    assert api.post("/weights/eval", json=dict(gpt=pre.name, sovits="carol_e8.pth")).status_code == 422   # 没 engine
    r = api.post("/weights/eval", json=dict(version="v2pro/pretrained/gone.pth", character="carol"))
    assert r.status_code == 409 and r.json()["code"] == "version_unavailable"

    req = EvalRequest(version="v2pro/pretrained/carol_e8.pth", character="carol", gpt="ignored.ckpt", eval_set="mini",
                      candidates=1)
    assert (req.engine, req.gpt, req.sovits) == ("v2pro", pre.name, "carol_e8.pth")
    used = []

    async def fake_candidates(p, engine, n, ref, seed, **kw):
        used.append(synth.resolve(engine, p.character, p.gpt, p.sovits, version=p.version)[1])
        return SimpleNamespace(candidates=[SimpleNamespace(cer=0.1, cer_kana=None, spk_sim=0.8, score=90.0)] * n)
    synth.EXEC.set(SimpleNamespace())  # 当成派到了别的设备：底模用那台的，本机不用装
    run = SimpleNamespace(job=SimpleNamespace(kind="eval", id="eval-v1", status=None), stage=lambda *a: None,
                          log=lambda *a: None)
    try:
        training.synth.run_candidates, orig = fake_candidates, training.synth.run_candidates
        asyncio.run(training.eval_job(run, req, "carol", gateway.refs.default_for("carol"), cat, gateway.refs))
    finally:
        training.synth.run_candidates = orig
        synth.EXEC.set(synth.remote.LOCAL)
    assert used and set(used) == {str(pre)}
    (doc,) = lib.docs("evals")
    assert [(c["gpt_owner"], c["sovits_owner"]) for c in doc["combos"]] == [(None, "carol")]
    evals = cat.latest_evals()
    assert ("v2pro", "carol", "pretrained", "carol_e8.pth", "mini") in evals
    assert ("v2pro", "carol", pre.name, "carol_e8.pth", "mini") not in evals
    assert versions(cat, "carol")[0]["score"] is None                    # carol 训练的那个版本（同名 GPT）没有这个分
    entries = {(e.character, e.name): e for e in cat.weight_entries(eval_set="mini")}
    assert [c.score_median for c in entries[None, pre.name].evals] == [90.0]
    assert entries["carol", pre.name].evals == []

    own = EvalRequest(version=f"v2pro/{pre.name}/carol_e8.pth", character="carol")
    rels, _ = colab_jobs._inputs("eval", [own], cat, gateway.refs, "carol", gateway.refs.default_for("carol"))
    assert f"weights/carol/v2pro/{pre.stem}/gpt.ckpt" in rels and "weights/carol/v2pro/carol_e8/sovits.pth" in rels


# ---------------------------------------------------------------- 手动默认的来源


def installed_pretrained(tmp_path, monkeypatch, engine="v2pro"):
    """本机装了这个引擎的 GPT 底模（假文件）。返回文件名。"""
    from ttsapi import settings
    pre = tmp_path / "pre" / engine / settings.ENGINES[engine]["pretrained_gpt"].name
    pre.parent.mkdir(parents=True)
    pre.write_bytes(b"pretrained")
    monkeypatch.setitem(settings.ENGINES[engine], "pretrained_gpt", pre)
    return pre.name


def test_manual_default_remembers_pretrained(api, lib, tmp_path, monkeypatch):
    """把底模 GPT 设为 carol 的默认，carol 恰好有同名的训练 GPT（旧数据）：默认记的是底模（pretrained），推荐版本的 id 里
    也是底模；/weights 里标默认的是底模那条。取消也按来源认。"""
    seed_carol(lib)
    cat = Catalog()
    pre = installed_pretrained(tmp_path, monkeypatch)
    legacy_names(monkeypatch)
    for kind, name in (("gpt", pre), ("sovits", "carol_e8.pth")):
        cat.add_weight("carol", "v2pro", kind, weight(tmp_path, name), "july", created=f"{Y}-07-05T10:00:00+09:00")
    r = api.patch(f"/weights/v2pro:{pre}", json=dict(is_default=True, character="carol"))
    assert r.status_code == 200 and r.json()["source"] == "pretrained" and r.json()["default_for"] == ["carol"]
    assert cat.manual_default("v2pro", "carol", "gpt") == "pretrained"
    vs = {v["label"]: v for v in versions(cat, "carol")}
    assert (vs["我设的默认"]["id"], vs["我设的默认"]["recommended"]) == ("v2pro/pretrained/carol_e8.pth", True)
    assert vs["7 月训练"]["id"] == f"v2pro/{pre}/carol_e8.pth"
    entries = {(e.character, e.name): e for e in cat.weight_entries()}
    assert entries["carol", pre].default_for == [] and entries[None, pre].default_for == ["carol"]
    api.patch(f"/weights/v2pro:{pre}", json=dict(is_default=False, character="carol"))
    assert cat.manual_default("v2pro", "carol", "gpt") is None


def test_manual_zero_shot_default_follows_gen_engines(lib, tmp_path, monkeypatch):
    """没有自己权重的角色手动设了 v2pro 底模当默认：能派发的引擎里没有 v2pro（固定在只装 v4 的设备）时不出这个版本，
    改出能派发的零样本；v2pro 能派发时还是「我设的默认」并推荐。"""
    seed_carol(lib)
    cat = Catalog()
    installed_pretrained(tmp_path, monkeypatch)
    cat.set_default("v2pro", "carol", "gpt", "pretrained")
    assert [(v["id"], v["recommended"]) for v in versions(cat, "carol", {"v4"})] == [("v4/pretrained/pretrained", True)]
    assert [(v["label"], v["id"], v["recommended"]) for v in versions(cat, "carol", {"v2pro", "v4"})] == [
        ("我设的默认", "v2pro/pretrained/pretrained", True)]


# ---------------------------------------------------------------- 训练出的权重不和底模同名

def test_weights_named_like_pretrained_are_renamed(lib, tmp_path):
    """训练出的权重进资料库时不和这个引擎的底模同名（按引擎配置认，控制端没装底模也一样）：GPT、SoVITS 都改成
    <角色>-<原名>，原名记在 meta；训练登记的 run.json 里是新名字。只看同一引擎的底模（v1 的底模不叫 s1v3.ckpt）。"""
    from ttsapi import settings
    seed_carol(lib)
    cat = Catalog()
    cfg = settings.ENGINES["v2pro"]
    g, s = cfg["pretrained_gpt"].name, cfg["pretrained_sovits"].name
    assert not cfg["pretrained_gpt"].exists()                           # 单测环境没装底模
    cat.register_run("carol", "SAMPLE_DS.sp0001", dict(run_name="carol-pre", engine="v2pro"),
                     dict(gpt=[weight(tmp_path, g)], sovits=[weight(tmp_path, s)]))
    run = cat.run("carol-pre")
    assert (run["gpt"], run["sovits"]) == ([f"carol-{g}"], [f"carol-{s}"])
    for kind, name in (("gpt", g), ("sovits", s)):
        meta = next(m for m in lib.docs("weights") if m["kind"] == kind)
        assert (meta["id"], meta["name"], meta["original_name"]) == (f"v2pro:carol-{name}", f"carol-{name}", name)
        assert lib.path(f"weights/carol/v2pro/carol-{name.rsplit('.', 1)[0]}/{meta['file']}").is_file()
    m = cat.add_weight("carol", "v1", "gpt", weight(tmp_path, g), "july")
    assert m["name"] == g and "original_name" not in m


def old_source(root, monkeypatch):
    """改名规则之前的 core 产出的结果（Colab results.tar 解开的样子）：carol-imp 训练出的 GPT、SoVITS 和 v2pro 的底模同名，
    best、手动默认、评测（记了归属）写的都是原名。"""
    from ttsapi import library, settings
    g, s = settings.ENGINES["v2pro"]["pretrained_gpt"].name, settings.ENGINES["v2pro"]["pretrained_sovits"].name
    mine = library.selected()
    library.use(library.open_or_create(root / "library"))
    try:
        with monkeypatch.context() as m:
            legacy_names(m)
            cat = Catalog()
            cat.register_run("carol-imp", "SAMPLE_DS.sp0001", dict(
                run_name="carol-imp-1", engine="v2pro", job_id=None, best=dict(gpt=g, sovits=s),
                created=f"{Y}-11-01T00:00:00+09:00"), dict(gpt=[weight(root, g)], sovits=[weight(root, s)]))
            cat.set_default("v2pro", "carol-imp", "sovits", s)
            cat.record_eval("v2pro", "carol-imp", "r", "eval-imp", "hard", ComboEval(
                gpt=g, sovits=s, n=1, cer_median=0.1, spk_sim_median=0.8, score_median=0.9))
        assert library.current().docs("evals")[0]["combos"][0]["gpt_owner"] == "carol-imp"   # 记了归属
    finally:
        library.use(mine)
    return root, g, s


def test_import_renames_pretrained_named_weights_and_resolves_right(api, lib, tmp_path, monkeypatch):
    """POST /runs/import 导入和底模同名的训练权重（并到 carol）：改名成 carol-<原名>，run.json、best、手动默认、评测跟着改。
    之后（审查第 1、2 条）：
    - 把底模 GPT 设成 carol 的默认，/tts 省略 gpt 时用的是底模，不是 carol 训练的那个
    - 控制端没装这个底模、固定的局域网设备能跑：推荐版本仍是「我设的默认」v2pro/pretrained/…，提交解析到那台的底模；
      训练的版本解析到 carol 的文件"""
    import asyncio

    from test_compute import AUTO, lan, mini

    from ttsapi import blind, gateway, settings, synth
    from ttsapi.compute import registry, remote
    from ttsapi.models import BlindCreate
    seed_carol(lib)
    cat = Catalog()
    gateway.refs.reload()
    src, g, s = old_source(tmp_path / "src", monkeypatch)
    G, S = f"carol-{g}", f"carol-{s}"
    r = api.post("/runs/import", json=dict(path=str(src), into_builtin=True))
    assert r.status_code == 200, r.text
    imp = r.json()["imported"][0]
    assert (imp["character"], imp["gpt"], imp["sovits"], imp["evals"]) == ("carol", [G], [S], 1)
    run = cat.run("carol-imp-1")
    assert (run["gpt"], run["sovits"], run["best"]) == ([G], [S], dict(gpt=G, sovits=S))
    assert lib.get(f"weights/carol/v2pro/{G.rsplit('.', 1)[0]}/meta.json")["original_name"] == g
    assert cat.manual_default("v2pro", "carol", "sovits") == S
    assert cat.latest_evals()["v2pro", "carol", G, S, "hard"].score_median == 0.9

    class Stop(Exception):
        pass
    seen = []

    async def fake_synth(p, engine, ref, weights, seed, score):
        seen.append(weights)
        raise Stop
    monkeypatch.setattr(synth, "synth", fake_synth)

    def tts(**kw):
        with pytest.raises(Stop):
            asyncio.run(gateway.tts(TTSParams(text="x", character="carol", **kw)))
        return seen[-1]

    pre = installed_pretrained(tmp_path, monkeypatch)                   # 本机装了 v2pro 的 GPT 底模
    r = api.patch(f"/weights/v2pro:{pre}", json=dict(is_default=True, character="carol"))
    assert r.json()["source"] == "pretrained"
    _, gpt_path, sovits, _ = tts(engine="v2pro")                        # 本机跑，省略 gpt / sovits
    assert (gpt_path, sovits) == (str(settings.ENGINES["v2pro"]["pretrained_gpt"]), S)

    monkeypatch.setitem(settings.ENGINES["v2pro"], "pretrained_gpt", tmp_path / "not-installed" / g)
    devices, auto = [mini(engines=["v4"]), lan()], dict(AUTO, mode="fixed", device="lan-aaaaaa")

    async def snapshot():
        return devices, auto
    monkeypatch.setattr(registry, "snapshot", snapshot)
    vs = {v["id"]: v for v in api.get("/characters/carol/versions").json()}
    assert [(i, v["label"], v["recommended"]) for i, v in vs.items()] == [
        (f"v2pro/pretrained/{S}", "我设的默认", True), (f"v2pro/{G}/{S}", "11 月训练", False)]

    async def on_lan(task, choice=None):
        return dict(device="lan-aaaaaa", type="lan")
    monkeypatch.setattr(gateway.dispatch, "choose", on_lan)
    monkeypatch.setattr(gateway.dispatch, "executor", lambda chosen: remote.Remote(dict(
        id="lan-aaaaaa", name="gpu-worker", address="http://127.0.0.1:9", token="t")))
    for kw in (dict(version=f"v2pro/pretrained/{S}"), dict(engine="v2pro")):
        _, gpt_path, sovits, _ = tts(**kw)
        assert remote.pretrained_key("v2pro", gpt_path) == "pretrained_gpt" and sovits == S, kw
    _, gpt_path, _, _ = tts(version=f"v2pro/{G}/{S}")
    assert gpt_path == str(lib.path(f"weights/carol/v2pro/{G.rsplit('.', 1)[0]}/gpt.ckpt"))
    # 盲听也能用这两个版本：本机没装的底模照样认得出是底模（建任务时算用到的角色）
    doc = blind.create(BlindCreate(character="carol", a=BlindSide(version=f"v2pro/pretrained/{S}"),
                                   b=BlindSide(version=f"v2pro/{G}/{S}")), cat, gateway.refs, remote=True)
    assert blind.involves(doc, cat) == {"carol"}


# ---------------------------------------------------------------- 纯底模组合按可派发筛选（审查第 3 条）

def test_pure_pretrained_default_follows_pinned_device_with_own_weights(api, lib, tmp_path, monkeypatch):
    """carol 在 v4 有训练出的权重、v2pro 的手动默认两半都是底模，固定的设备只能跑 v4：v2pro 的纯底模组合不列、不推荐，
    推荐 v4 训练的；固定到能跑 v2pro 的设备时「我设的默认」照常列出并推荐。含训练出的权重的组合不筛。"""
    from test_compute import AUTO, lan, mini

    from ttsapi.compute import registry
    seed_carol(lib)
    cat = Catalog()
    cat.register_run("carol", "SAMPLE_DS.sp0001", dict(run_name="carol-v4", engine="v4",
                                                        created=f"{Y}-11-01T00:00:00+09:00"),
                     dict(gpt=[weight(tmp_path, "v4x-e5.ckpt")], sovits=[weight(tmp_path, "v4x_e4.pth")]))
    for kind in ("gpt", "sovits"):
        cat.set_default("v2pro", "carol", kind, "pretrained")
    devices, auto = [mini(engines=["v4"]), lan()], dict(AUTO, mode="fixed", device="local")

    async def snapshot():
        return devices, auto
    monkeypatch.setattr(registry, "snapshot", snapshot)

    def listed():
        return [(v["id"], v["recommended"]) for v in api.get("/characters/carol/versions").json()]
    assert listed() == [("v4/v4x-e5.ckpt/v4x_e4.pth", True)]
    auto["device"] = "lan-aaaaaa"
    assert listed() == [("v2pro/pretrained/pretrained", True), ("v4/v4x-e5.ckpt/v4x_e4.pth", False)]
    cat.set_default("v4", "carol", "gpt", "pretrained")                    # 底模 GPT + 训练的 SoVITS：不筛
    assert "v4/pretrained/v4x_e4.pth" in [v["id"] for v in versions(cat, "carol", {"v2pro"})]


def test_no_zero_shot_for_character_with_half_a_run(lib, tmp_path):
    """carol 只剩一次训练的 GPT（SoVITS 删了）：凑不成版本，也不给零样本（零样本只给一个自己的权重都没有的角色），
    不会把训练出的 GPT 标成「底模（零样本）」。"""
    seed_carol(lib)
    cat = Catalog()
    add_run(cat, tmp_path, "carol-p", f"{Y}-11-01T00:00:00+09:00", ["p-e5.ckpt"], ["p_e4.pth"])
    cat.delete_weight(next(x for x in cat.entries("v2pro") if x["kind"] == "sovits"))
    assert versions(cat, "carol", {"v2pro"}) == []


# ---------------------------------------------------------------- 版本的文件不在这台机器上（第 6 轮真机冒烟）

def test_version_file_missing_here_409_before_any_job(api, carol, lib):
    """版本绑定的训练权重只剩 meta.json（文件被移走或还没同步过来）：本机评测（compute=local）和各种生成（含盲听补生成）
    都在建任务之前 409 version_unavailable，一个任务都不建（第 6 轮真机冒烟在 b58f459 上：本机评测返回 200 并建了任务）。"""
    from ttsapi import blind, gateway, jobs
    from ttsapi.models import BlindCreate
    gateway.refs.reload()
    vid = "v2pro/b-e10.ckpt/b_e8.pth"                                   # carol 11 月第二次训练的版本
    assert vid in [v["id"] for v in versions(carol, "carol")]
    # 文件还在时建的盲听，生成任务失败了（不在 jobs.runs 里），之后补生成
    doc = blind.create(BlindCreate(character="carol", a=BlindSide(version=vid), b=BlindSide(engine="v2pro")), carol,
                       gateway.refs)
    blind.save(dict(doc, job_id="blind-gone"))
    f = lib.path("weights/carol/v2pro/b-e10/gpt.ckpt")
    f.rename(f.with_name("gpt.ckpt.hidden"))
    before = jobs.list_jobs()[0], set(jobs.runs)
    r = api.post("/weights/eval", json=dict(version=vid, character="carol", compute="local", eval_set="mini"))
    assert r.status_code == 409 and r.json()["code"] == "version_unavailable"
    body = dict(text="x", character="carol", version=vid, compute="local")
    for path in ("/tts", "/tts/candidates/jobs", "/compare/jobs"):
        r = api.post(path, json=dict(body, engines=["v2pro"]) if path == "/compare/jobs" else body)
        assert r.status_code == 409 and r.json()["code"] == "version_unavailable", path
    w = api.post("/works", json=dict(title="t", lines=[dict(character="carol", text="x", version=vid)])).json()
    r = api.post(f"/works/{w['id']}/generate", json=dict(n=1, compute="local"))
    assert r.status_code == 409 and r.json()["code"] == "version_unavailable"
    r = api.post("/blind", json=dict(character="carol", a=dict(version=vid), b=dict(engine="v2pro"), compute="local"))
    assert r.status_code == 409 and r.json()["code"] == "version_unavailable"
    r = api.post(f"/blind/{doc['id']}/generate")
    assert r.status_code == 409 and r.json()["code"] == "version_unavailable"
    assert (jobs.list_jobs()[0], set(jobs.runs)) == before
