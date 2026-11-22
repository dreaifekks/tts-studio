"""训练结束后登记权重（training.after_training）：权重复制进资料库，run.json 写好之前不删本机暂存目录；
中途失败（写 run.json 时出错、服务退出）后再进登记路径，能按完整的暂存文件登记全部权重。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from conftest import seed_carol
from ttsapi import settings, training
from ttsapi.catalog import Catalog, run_rel
from ttsapi.models import ComboEval, TrainEval, TrainRequest, TrainResult

RUN = "carol-reg-20261120-000000"
GPT = [f"{RUN}-e5.ckpt", f"{RUN}-e10.ckpt"]
SOVITS = [f"{RUN}_e4_s8.pth", f"{RUN}_e8_s16.pth"]


class Refs:
    def reload(self):
        pass

    def default_for(self, slug):
        return "carol-14000140"


async def fake_evaluate(run, slug, engine, combos, ref_id, catalog, refs, eval_set, n):
    out = [ComboEval(gpt=g, sovits=s, n=1, cer_median=0.0, spk_sim_median=0.9, score_median=90.0 + i)
           for i, (g, s) in enumerate(combos)][::-1]
    return TrainEval(job_id=run.job.id, engine=engine, character=slug, ref_id=ref_id, eval_set=eval_set,
                     sentences=["s"], candidates_per_sentence=n, combos=out, best=out[0])


@pytest.fixture
def trained(lib, monkeypatch, tmp_path):
    """训练进程跑完的样子：training/<run>/ 有 runner_spec.json 和 train.list，暂存目录里有 2 个 GPT、2 个 SoVITS。"""
    seed_carol(lib)
    monkeypatch.setattr(settings, "TRAIN_DIR", tmp_path / "training")
    monkeypatch.setattr(settings, "WEIGHTS_DIR", tmp_path / "weights")
    run_dir = settings.TRAIN_DIR / RUN
    run_dir.mkdir(parents=True)
    req = TrainRequest(slug="carol-reg", dataset_id="SAMPLE_DS.sp0001")
    (run_dir / "runner_spec.json").write_text(json.dumps(dict(req=req.model_dump())), encoding="utf-8")
    (run_dir / "train.list").write_text("14000140.wav|carol-reg|ja|明日の朝は\n", encoding="utf-8")
    wdir = settings.WEIGHTS_DIR / "v2pro" / RUN
    wdir.mkdir(parents=True)
    for n in (*GPT, *SOVITS):
        (wdir / n).write_bytes(n.encode() * 100)
    monkeypatch.setattr(training, "evaluate", fake_evaluate)
    job = SimpleNamespace(id="train-reg-1", train_result=TrainResult(
        slug="carol-reg", engine="v2pro", run_name=RUN, lines_used=1, minutes_used=0.1, gpt_weights=[],
        sovits_weights=[]))
    return SimpleNamespace(job=job), wdir


def test_register_retry_after_run_json_failure(lib, trained, monkeypatch):
    run, wdir = trained
    real_put = type(lib).put

    def failing(self, rel, doc):
        if rel == run_rel(RUN):
            raise OSError("disk I/O error")
        return real_put(self, rel, doc)

    monkeypatch.setattr(type(lib), "put", failing)
    with pytest.raises(OSError):
        asyncio.run(training.after_training(run, Catalog(), Refs()))
    monkeypatch.setattr(type(lib), "put", real_put)
    # 没提交：暂存文件一个不少，放进资料库的权重不出现在目录里
    assert sorted(p.name for p in wdir.iterdir()) == sorted((*GPT, *SOVITS))
    assert not Catalog.has_run(RUN) and not [x for x in Catalog.entries() if x["run"]]

    asyncio.run(training.after_training(run, Catalog(), Refs()))  # 服务重启接管时同样走这里
    reg = Catalog.run(RUN)
    assert reg["gpt"] == GPT and reg["sovits"] == SOVITS and reg["best"] == dict(gpt=GPT[1], sovits=SOVITS[1])
    assert sorted(x["name"] for x in Catalog.entries() if x["run"] == RUN) == sorted((*GPT, *SOVITS))
    for n, kind in ((GPT[0], "gpt"), (SOVITS[1], "sovits")):
        p = lib.path(f"weights/carol-reg/v2pro/{n.rsplit('.', 1)[0]}/{'gpt.ckpt' if kind == 'gpt' else 'sovits.pth'}")
        assert p.read_bytes() == n.encode() * 100
    assert run.job.train_result.gpt_weights == GPT and not wdir.exists()


def test_register_cleans_staging_left_after_commit(lib, trained):
    """run.json 写好、删暂存目录前退出：再进来按已登记的 run 走，暂存目录照样清掉。"""
    run, wdir = trained
    asyncio.run(training.after_training(run, Catalog(), Refs()))
    wdir.mkdir(parents=True)
    (wdir / GPT[0]).write_bytes(b"x")
    run.job.train_result.gpt_weights = []
    asyncio.run(training.after_training(run, Catalog(), Refs()))
    assert run.job.train_result.gpt_weights == GPT and not wdir.exists()


def test_register_renames_weight_named_like_pretrained(lib, trained, monkeypatch):
    """导出的权重和这个引擎的底模同名（底模配置换了名字时才会）：登记时改成 <角色>-<原名>，训后评测、best、
    train_result 都用资料库里的名字。"""
    run, _ = trained
    pre = settings.ENGINES["v2pro"]["pretrained_gpt"]
    monkeypatch.setitem(settings.ENGINES["v2pro"], "pretrained_gpt", pre.with_name(GPT[1]))
    asyncio.run(training.after_training(run, Catalog(), Refs()))
    renamed = f"carol-reg-{GPT[1]}"
    reg = Catalog.run(RUN)
    assert reg["gpt"] == [GPT[0], renamed] and reg["best"] == dict(gpt=renamed, sovits=SOVITS[1])
    assert run.job.train_result.gpt_weights == [GPT[0], renamed]
    assert lib.get(f"weights/carol-reg/v2pro/{renamed.rsplit('.', 1)[0]}/meta.json")["original_name"] == GPT[1]
