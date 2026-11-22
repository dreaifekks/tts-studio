"""POST /runs/import：别处（Colab VM）的资料库里的训练结果写进当前资料库：run、权重（sha256 核对）、评测、角色、任务记录；
重复导入跳过，中途失败可以重试。"""
import json
import sqlite3
import tarfile

import pytest

from conftest import seed_carol
from ttsapi import db, importer, jobs, library, settings
from ttsapi.catalog import Catalog
from ttsapi.models import ComboEval, Job, TrainEval, TrainResult
from ttsapi.refs import RefLibrary

RUN = "carol-imp-20261118-184755"
JOB = "train-20261118-184755-16d2"


def make_source(root, slug="carol-imp", run=RUN, dataset_id="SAMPLE_DS.sp0001", job=JOB):
    """仿照 Colab results.tar 里的 tts-api/：VM 上的资料库（library/）加 data/tts.db（任务记录）和 data/jobs/<job>.log。
    run 产出 GPT e2、e4 和 SoVITS e2；评测 e4 分数更高；e2 有显示名。"""
    api = root / "tts-api"
    mine = library.selected()
    src = library.open_or_create(api / "library")
    library.use(src)
    try:
        train = root / "vm-training" / run
        (train / "exp" / "logs_s1").mkdir(parents=True)
        (train / "train.list").write_text(
            "/content/GPTSoVits/datasets/SAMPLE_DS/characters/sp0001_alice/audio/14000140.wav|carol|ja|明日の朝は\n",
            encoding="utf-8")
        (train / "runner_spec.json").write_text("{}", encoding="utf-8")
        (train / "runner_pid.json").write_text("{}", encoding="utf-8")
        (train / "exp" / "logs_s1" / "metrics.txt").write_text("loss", encoding="utf-8")
        (train / "exp" / "feat.npy").write_bytes(b"x")
        gpt, sovits = [f"{run}-e2.ckpt", f"{run}-e4.ckpt"], [f"{run}_e2_s28.pth"]
        (root / "vm-weights").mkdir()
        files = {}
        for kind, names in (("gpt", gpt), ("sovits", sovits)):
            files[kind] = []
            for n in names:
                (root / "vm-weights" / n).write_bytes(n.encode() * 100)
                files[kind].append(root / "vm-weights" / n)
        cat = Catalog()
        cat.register_run(slug, dataset_id, dict(
            run_name=run, engine="v2pro", job_id=job, best=dict(gpt=gpt[0], sovits=sovits[0]),
            filter=dict(min_sec=1.0, max_sec=12.0, exclude_interjections=True), lines_used=2, minutes_used=0.2,
            params={}), files, train_dir=train)
        for i, g in enumerate(gpt):
            cat.record_eval("v2pro", slug, "carol-14200770", job, "hard", ComboEval(
                gpt=g, sovits=sovits[0], n=12, cer_median=0.0, spk_sim_median=0.9, score_median=95.0 + i,
                score_mean=94.0))
        # 不属于这个 run 的评测（7 月权重的对照）：不导
        cat.record_eval("v2pro", "carol", "carol-1", "eval-1", "hard", ComboEval(
            gpt="carol-e15.ckpt", sovits="carol_e8_s144.pth", n=12, cer_median=0, spk_sim_median=0, score_median=95))
        cat.set_meta(f"v2pro:{gpt[0]}", display_name="colab e2")
    finally:
        library.use(mine)
    (api / "data" / "jobs").mkdir(parents=True)
    conn = sqlite3.connect(api / "data" / "tts.db")
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO jobs VALUES(?,?,?)", (job, 1.0, json.dumps(dict(id=job, kind="train"))))
    conn.commit()
    conn.close()
    (api / "data" / "jobs" / f"{job}.log").write_text("log", encoding="utf-8")
    return root


@pytest.fixture
def carol(lib):
    seed_carol(lib)
    return lib


def test_import_dir_then_skip(tmp_path, carol):
    src = make_source(tmp_path / "src")
    cat = Catalog()
    refs = RefLibrary(cat)
    out = importer.import_runs(src, cat, refs)
    assert [r["run"] for r in out["imported"]] == [RUN] and out["evals"] == 2 and out["jobs"] == [JOB]

    wdir = carol.path("weights/carol-imp/v2pro")
    assert sorted(p.name for p in wdir.iterdir()) == [f"{RUN}-e2", f"{RUN}-e4", f"{RUN}_e2_s28"]
    assert (wdir / f"{RUN}-e2" / "gpt.ckpt").read_bytes() == f"{RUN}-e2.ckpt".encode() * 100
    rdir = carol.path(f"runs/{RUN}")
    assert (rdir / "runner_spec.json").exists() and (rdir / "exp" / "logs_s1" / "metrics.txt").exists()
    assert not (rdir / "runner_pid.json").exists() and not (rdir / "exp" / "feat.npy").exists()
    line = (rdir / "train.list").read_text(encoding="utf-8").splitlines()[0]
    assert line.split("|")[0] == str(settings.DATASET_ROOT / "SAMPLE_DS" / "characters" / "sp0001_alice" / "audio" / "14000140.wav")

    char = cat.character("carol-imp")
    assert char["source"] == "user" and char["train_list"] == f"runs/{RUN}/train.list"
    assert cat.run(RUN)["character"] == "carol-imp" and cat.builtin_of("carol-imp") == "carol"
    assert cat.defaults("v2pro", "carol-imp") == (f"{RUN}-e4.ckpt", f"{RUN}_e2_s28.pth")  # 按导入的评测分选
    assert refs.has_character("carol-imp") and "carol-imp-14000140" in refs.index
    assert carol.get(f"weights/carol-imp/v2pro/{RUN}-e2/meta.json")["display_name"] == "colab e2"
    evals = carol.docs("evals")
    assert [len(e["combos"]) for e in evals] == [2] and evals[0]["job_id"] == JOB
    assert (settings.JOBS_DIR / f"{JOB}.log").exists()
    assert json.loads(db.one("SELECT data FROM jobs WHERE id=?", JOB)["data"])["kind"] == "train"

    again = importer.import_runs(src, cat, refs)
    assert again["imported"] == [] and again["evals"] == 0
    assert again["skipped"] == [dict(run=RUN, reason="资料库里已有")]


def test_import_into_builtin(tmp_path, carol):
    """into_builtin：用 carol 的数据集训练的角色并到 carol 名下，评测和来源里手动设的默认记到 carol。"""
    run = "carol-ib-20261120-000000"
    src = make_source(tmp_path / "src", slug="carol-ib", run=run, job="train-ib")
    library.use(library.open_or_create(src / "tts-api" / "library"))
    Catalog().set_default("v2pro", "carol-ib", "gpt", f"{run}-e2.ckpt")
    library.use(carol)
    cat = Catalog()
    out = importer.import_runs(src, cat, into_builtin=True)
    imp = out["imported"][0]
    assert imp["character"] == "carol" and imp["source_character"] == "carol-ib" and out["evals"] == 2
    assert "carol-ib" not in cat.slugs() and [r["run_name"] for r in cat.runs("carol")] == [run]
    assert carol.path(f"weights/carol/v2pro/{run}-e2/gpt.ckpt").exists()
    assert {e["character"] for e in carol.docs("evals")} == {"carol"}
    # 评测时记下的权重所属角色也改成 carol，成绩照样算 carol 的
    assert {c[f"{k}_owner"] for e in carol.docs("evals") for c in e["combos"] for k in ("gpt", "sovits")} == {"carol"}
    assert cat.latest_evals()["v2pro", "carol", f"{run}-e4.ckpt", f"{run}_e2_s28.pth", "hard"].score_median == 96.0
    assert cat.defaults("v2pro", "carol") == (f"{run}-e2.ckpt", f"{run}_e2_s28.pth")  # 手动设的 GPT 跟过来
    again = importer.import_runs(src, cat, into_builtin=True)
    assert again["imported"] == [] and again["skipped"] == [dict(run=run, reason="资料库里已有")]


def test_import_tar_and_run_filter(tmp_path, carol):
    run = "carol-tar-20261119-000000"
    src = make_source(tmp_path / "src", slug="carol-tar", run=run, job="train-tar")
    tar = tmp_path / "results.tar"
    with tarfile.open(tar, "w") as tf:
        tf.add(src / "tts-api", arcname="tts-api")
    assert importer.import_runs(tar, runs=["other"])["imported"] == []
    out = importer.import_runs(tar, runs=[run])
    assert [r["run"] for r in out["imported"]] == [run] and out["jobs"] == ["train-tar"]
    assert carol.path(f"weights/carol-tar/v2pro/{run}_e2_s28/sovits.pth").exists()


def test_import_dataset_conflict(tmp_path, carol):
    importer.import_runs(make_source(tmp_path / "a", slug="carol-cf", run="carol-cf-1", job="j-a"))
    out = importer.import_runs(make_source(tmp_path / "b", slug="carol-cf", run="carol-cf-2", dataset_id="OTHER.x",
                                           job="j-b"))
    assert out["imported"] == [] and "数据集" in out["skipped"][0]["reason"]


def test_import_partial_weights(tmp_path, carol):
    """来源只带了部分权重（扫参只传前几名）：导入在的，缺的不登记，用到缺失权重的评测不导。"""
    src = make_source(tmp_path / "src", slug="carol-mw", run="carol-mw-1", job="j-mw")
    (src / "tts-api" / "library" / "weights" / "carol-mw" / "v2pro" / "carol-mw-1-e4" / "gpt.ckpt").unlink()
    cat = Catalog()
    out = importer.import_runs(src, cat)
    imp = out["imported"][0]
    assert imp["gpt"] == ["carol-mw-1-e2.ckpt"] and imp["missing"] == ["carol-mw-1-e4.ckpt"] and out["evals"] == 1
    assert sorted(p.name for p in carol.path("weights/carol-mw/v2pro").iterdir()) == ["carol-mw-1-e2", "carol-mw-1_e2_s28"]
    run = cat.run("carol-mw-1")
    assert run["gpt"] == ["carol-mw-1-e2.ckpt"]
    assert run["best"] == dict(gpt="carol-mw-1-e2.ckpt", sovits="carol-mw-1_e2_s28.pth")
    assert cat.defaults("v2pro", "carol-mw") == ("carol-mw-1-e2.ckpt", "carol-mw-1_e2_s28.pth")


def test_import_no_gpt_weights(tmp_path, carol):
    src = make_source(tmp_path / "src", slug="carol-ng", run="carol-ng-1", job="j-ng")
    for n in ("carol-ng-1-e2", "carol-ng-1-e4"):
        (src / "tts-api" / "library" / "weights" / "carol-ng" / "v2pro" / n / "gpt.ckpt").unlink()
    out = importer.import_runs(src)
    assert out["imported"] == [] and "缺权重" in out["skipped"][0]["reason"]
    assert not carol.path("weights/carol-ng").exists() and "carol-ng" not in Catalog().characters()


def add_july(cat, tmp_path):
    """资料库里 carol 的 7 月权重（没有 run）。"""
    for kind, name in (("gpt", "carol-e15.ckpt"), ("sovits", "carol_e8_s144.pth")):
        (tmp_path / name).write_bytes(name.encode())
        cat.add_weight("carol", "v2pro", kind, tmp_path / name, "july")


def test_import_sha256_mismatch(tmp_path, carol):
    """传输损坏（第二个 GPT 和来源 meta.json 的 sha256 对不上）：先整批核对，报错，资料库里什么都不写——
    并到 carol 时坏文件（评测分更高的 e4）不会被选成 carol 的默认，也不在任何权重列表里。"""
    cat = Catalog()
    add_july(cat, tmp_path)
    src = make_source(tmp_path / "src", slug="carol-sha", run="carol-sha-1", job="j-sha")
    (src / "tts-api" / "library" / "weights" / "carol-sha" / "v2pro" / "carol-sha-1-e4" / "gpt.ckpt").write_bytes(b"bad")
    with pytest.raises(RuntimeError, match="carol-sha-1-e4.ckpt.*sha256|sha256.*carol-sha-1-e4.ckpt"):
        importer.import_runs(src, cat, into_builtin=True)
    assert not cat.has_run("carol-sha-1")
    assert cat.defaults("v2pro", "carol") == ("carol-e15.ckpt", "carol_e8_s144.pth")
    assert not [x for x in cat.entries() if x["run"]] and carol.docs("evals") == []
    assert sorted(p.name for p in carol.path("weights/carol/v2pro").iterdir()) == ["carol-e15", "carol_e8_s144"]
    assert not carol.path("runs").exists() and "carol-sha" not in cat.characters()
    assert not db.one("SELECT 1 FROM jobs WHERE id=?", "j-sha")


def test_import_retry_after_failure(tmp_path, carol, monkeypatch):
    """导入到一半（写评测时）失败：run.json 没写，这个 run 不算导入；重试能完整导入。"""
    run = "carol-rt-20261119-000000"
    src = make_source(tmp_path / "src", slug="carol-rt", run=run, job="j-rt")
    real_put = type(carol).put

    def failing(self, rel, doc):
        if rel.startswith("evals/"):
            raise OSError("disk I/O error")
        return real_put(self, rel, doc)

    monkeypatch.setattr(type(carol), "put", failing)
    with pytest.raises(OSError):
        importer.import_runs(src)
    monkeypatch.setattr(type(carol), "put", real_put)
    assert not Catalog.has_run(run) and carol.docs("evals") == []
    # 权重已经放进去了（meta.json 在），但 run 没提交：目录里哪里都选不到
    assert len(carol.docs("weights")) == 3 and not [x for x in Catalog.entries() if x["run"]]
    with pytest.raises(KeyError):
        Catalog().find(f"v2pro:{run}-e4.ckpt")
    with pytest.raises(KeyError):
        Catalog().resolve("v2pro", "carol-rt", gpt=f"{run}-e4.ckpt")
    cfg = settings.ENGINES["v2pro"]
    assert Catalog().defaults("v2pro", "carol-rt") == (cfg["pretrained_gpt"].name, cfg["pretrained_sovits"].name)

    out = importer.import_runs(src)
    assert [r["run"] for r in out["imported"]] == [run] and out["evals"] == 2
    assert Catalog().defaults("v2pro", "carol-rt") == (f"{run}-e4.ckpt", f"{run}_e2_s28.pth")
    carol.scan()  # 重新扫描后也一样（记录都在文件里）
    assert Catalog.has_run(run) and len(carol.docs("weights")) == 3


def test_import_keeps_original_name_from_source(tmp_path, carol):
    """来源（VM 跑同一套代码）登记时已经把和底模同名的权重改了名、记了原名：并到 carol 时名字不再改，原名跟着过来。"""
    g = settings.ENGINES["v2pro"]["pretrained_gpt"].name
    mine = library.selected()
    library.use(library.open_or_create(tmp_path / "src" / "library"))
    try:
        for n in (g, "on_e2_s28.pth"):
            (tmp_path / n).write_bytes(n.encode())
        Catalog().register_run("carol-on", "SAMPLE_DS.sp0001", dict(run_name="carol-on-1", engine="v2pro"),
                               dict(gpt=[tmp_path / g], sovits=[tmp_path / "on_e2_s28.pth"]))
    finally:
        library.use(mine)
    out = importer.import_runs(tmp_path / "src", Catalog(), into_builtin=True)
    assert out["imported"][0]["gpt"] == [f"carol-on-{g}"]
    meta = carol.get(f"weights/carol/v2pro/carol-on-{g.rsplit('.', 1)[0]}/meta.json")
    assert (meta["character"], meta["original_name"]) == ("carol", g)


def test_import_needs_owned_library(tmp_path, carol):
    src = make_source(tmp_path / "src")
    carol.info["owner"] = dict(machine_id="x", name="mba")
    with pytest.raises(library.NotOwner):
        importer.import_runs(src)


def test_import_evals_of_existing_weights(tmp_path, carol):
    """Colab 评测作业：来源里没有新 run，评测的是本库已有的权重（7 月权重）。这些评测也导入，并进同一任务的文件；
    用到本库没有的权重的组合不导；重复导入不重复。"""
    cat = Catalog()
    for kind, name in (("gpt", "carol-e15.ckpt"), ("sovits", "carol_e8_s144.pth")):
        (tmp_path / name).write_bytes(name.encode())
        cat.add_weight("carol", "v2pro", kind, tmp_path / name, "july")
    mine = library.selected()
    src = library.open_or_create(tmp_path / "src" / "library")
    library.use(src)
    try:
        for g, s in (("carol-e15.ckpt", "carol_e8_s144.pth"), ("carol-e15.ckpt", "other_e8_s1.pth")):
            Catalog().record_eval("v2pro", "carol", "carol-14000140", "eval-colab-1", "hard", ComboEval(
                gpt=g, sovits=s, n=36, cer_median=0.0, spk_sim_median=0.9, score_median=93.0))
    finally:
        library.use(mine)
    out = importer.import_runs(tmp_path / "src", cat)
    assert out["imported"] == [] and out["evals"] == 1
    (doc,) = carol.docs("evals")
    assert doc["job_id"] == "eval-colab-1" and doc["character"] == "carol"
    assert [(c["gpt"], c["sovits"]) for c in doc["combos"]] == [("carol-e15.ckpt", "carol_e8_s144.pth")]
    assert cat.latest_evals("v2pro")["v2pro", "carol", "carol-e15.ckpt", "carol_e8_s144.pth", "hard"].score_median == 93.0
    assert importer.import_runs(tmp_path / "src", cat)["evals"] == 0


def test_import_partial_rebuilds_job_eval(tmp_path, carol):
    """只导入了 GPT e2（e4 缺）并到 carol：导入的训练任务记录里的评测结果按导入的评测记录重算——缺失的 e4 不在组合里、
    不是最好的组合，角色是 carol；跨 run 的组合另一半不在资料库里的不导，另一半是本库已有权重（7 月）的照导。"""
    cat = Catalog()
    add_july(cat, tmp_path)
    run, job = "carol-pi-1", "j-pi"
    src = make_source(tmp_path / "src", slug="carol-pi", run=run, job=job)
    srclib = src / "tts-api" / "library"
    (srclib / "weights" / "carol-pi" / "v2pro" / f"{run}-e4" / "gpt.ckpt").unlink()
    combos = [ComboEval(gpt=f"{run}-e{e}.ckpt", sovits=f"{run}_e2_s28.pth", n=12, cer_median=0.0, spk_sim_median=0.9,
                        score_median=95.0 + i, score_mean=94.0) for i, e in enumerate((2, 4))]
    tr = TrainResult(slug="carol-pi", engine="v2pro", run_name=run, lines_used=2, minutes_used=0.2,
                     gpt_weights=[f"{run}-e2.ckpt", f"{run}-e4.ckpt"], sovits_weights=[f"{run}_e2_s28.pth"],
                     eval=TrainEval(job_id=job, engine="v2pro", character="carol-pi", ref_id="carol-14200770",
                                    eval_set="hard", sentences=["s"], candidates_per_sentence=1,
                                    combos=combos[::-1], best=combos[1]))
    conn = sqlite3.connect(src / "tts-api" / "data" / "tts.db")
    conn.execute("UPDATE jobs SET data=? WHERE id=?", (Job(
        id=job, kind="train", target="carol-pi", status="succeeded", progress=1.0, stage="", created="",
        train_result=tr).model_dump_json(), job))
    conn.commit()
    conn.close()
    mine = library.selected()
    library.use(library.open_or_create(srclib))
    try:  # 另一个任务评的跨 run 组合：e2 + 别的 run 的 SoVITS（从没导入过）、e2 + 本库的 7 月 SoVITS
        for s in ("carol-other-1_e9_s1.pth", "carol_e8_s144.pth"):
            Catalog().record_eval("v2pro", "carol", "carol-1", "eval-x", "hard", ComboEval(
                gpt=f"{run}-e2.ckpt", sovits=s, n=12, cer_median=0, spk_sim_median=0, score_median=99))
    finally:
        library.use(mine)

    out = importer.import_runs(src, cat, into_builtin=True)
    assert out["imported"][0]["missing"] == [f"{run}-e4.ckpt"] and out["jobs"] == [job]
    x = {e["job_id"]: [(c["gpt"], c["sovits"]) for c in e["combos"]] for e in carol.docs("evals")}
    assert x == {job: [(f"{run}-e2.ckpt", f"{run}_e2_s28.pth")], "eval-x": [(f"{run}-e2.ckpt", "carol_e8_s144.pth")],
                 "eval-1": [("carol-e15.ckpt", "carol_e8_s144.pth")]}  # 来源里 7 月权重的对照，本库有 7 月权重就导
    got = jobs.get(job)
    assert got.target == "carol" and got.train_result.slug == "carol"
    assert got.train_result.gpt_weights == [f"{run}-e2.ckpt"]
    ev = got.train_result.eval
    assert ev.character == "carol" and [c.gpt for c in ev.combos] == [f"{run}-e2.ckpt"]
    assert ev.best.gpt == f"{run}-e2.ckpt" and ev.best.score_median == 95.0


def test_import_fills_result_of_existing_job(tmp_path, carol):
    """Colab 作业写库失败后用结果重试：本机 tts.db 已有这条任务（已结束、结果为空）时按任务 id 补上 train_result（对上
    资料库），run 已在资料库里（跳过）也补；在跑的、已有结果的不动。"""
    run, job = "carol-fl-1", "j-fl"
    src = make_source(tmp_path / "src", slug="carol-fl", run=run, job=job)
    tr = TrainResult(slug="carol-fl", engine="v2pro", run_name=run, lines_used=2, minutes_used=0.2,
                     gpt_weights=[f"{run}-e2.ckpt", f"{run}-e4.ckpt"], sovits_weights=[f"{run}_e2_s28.pth"])
    conn = sqlite3.connect(src / "tts-api" / "data" / "tts.db")
    conn.execute("UPDATE jobs SET data=? WHERE id=?", (Job(
        id=job, kind="train", target="carol-fl", status="succeeded", progress=1.0, stage="", created="",
        train_result=tr).model_dump_json(), job))
    conn.commit()
    conn.close()
    mine = Job(id=job, kind="train", target="carol-fl", status="running", progress=0.97, stage="写进资料库", created="")
    db.put_doc("jobs", job, 1.0, mine.model_dump())

    out = importer.import_runs(src)
    assert [r["run"] for r in out["imported"]] == [run] and out["jobs"] == [] and out["filled"] == []  # 在跑的不补
    db.put_doc("jobs", job, 1.0, dict(mine.model_dump(), status="failed"))
    out = importer.import_runs(src)
    assert out["skipped"] == [dict(run=run, reason="资料库里已有")] and out["filled"] == [job]
    got = jobs.get(job)
    assert got.status == "failed" and got.train_result.run_name == run and got.train_result.slug == "carol-fl"
    assert importer.import_runs(src)["filled"] == []  # 已有结果的不动
