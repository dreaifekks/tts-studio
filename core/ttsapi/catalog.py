"""角色、权重、默认组合、评测、训练 run 的目录。底模在本机（core 安装的一部分）；其余都在资料库（ttsapi/library）：

- 角色 characters/<slug>.json：source（builtin 内置 / user 训练出来的）、dataset_id、default_ref（默认参考，可空）、
  default_weights（{引擎: {gpt, sovits}}，手动设的默认，每一半可空，空就用自动默认；同版本 id 的一半：训练出的写文件名，
  底模写 pretrained）、train_list（训练出来的角色：
  建参考库用的训练列表，资料库里的相对路径）、created
- 权重 weights/<slug>/<engine>/<weight_id>/：gpt.ckpt 或 sovits.pth 加 meta.json（id「引擎:文件名」、kind、name（文件名，
  /tts 等接口的 gpt / sovits 参数用它）、sha256、size、character、source（july / user）、run、display_name、note、hidden、created；
  和底模同名改过名的另有 original_name，见 stored_name）
- 训练 run runs/<run>/run.json：character、engine、dataset_id、job_id、gpt / sovits（产出的权重名）、best、filter、
  lines_used、minutes_used、params、created；同目录是训练的小文件（train.list、配置、日志）。run.json 是这次训练的提交记录，
  最后写：meta 里 run 指向的 run.json 不存在的权重（登记、导入到一半）不算数，目录里不出现
- 评测 evals/<yyyy-mm>/<job_id>.json：一次评测任务，combos 里每组 GPT × SoVITS 一条（同一组合在同一评测集上以最新一次为准）；
  每组带 gpt_owner / sovits_owner（评测时用的是哪个角色的权重记录，底模为 None），成绩按「评测角色 + 权重记录」认

权重 id 是「引擎:文件名」，接口里用文件名选权重，所以同一引擎下文件名不能重复（训练 run 名带角色和时间，不会撞）。
训练出的权重进资料库时不和这个引擎的底模同名（同名的改名，stored_name），底模和训练出的权重靠文件名就分得开；
不同角色之间仍可能同名（e8_s56.pth），按角色解析（prefer、版本 id）。
"""
import re
import shutil
from datetime import datetime
from pathlib import Path

from ttsapi import library, settings
from ttsapi.datasets import resolve_audio
from ttsapi.library import now_iso
from ttsapi.models import (VERSION_PRETRAINED, CharacterInfo, CharacterPrefs, CharacterSynthParams, ComboScore,
                           EngineInfo, EngineWeights, TrainDefaults, TrainFilter, WeightEntry, WeightFile)

_UNSET = object()

KINDS = ("gpt", "sovits")
FILE_OF = {"gpt": "gpt.ckpt", "sovits": "sovits.pth"}
KIND_LABEL = {"gpt": "GPT", "sovits": "SoVITS"}
STAT_COLUMNS = ("cer_mean", "cer_max", "spk_sim_mean", "spk_sim_min", "score_mean", "score_min")
# 选「评测分最高」时先看默认评测集（hard），这个角色的组合都没在 hard 上评测过才看 mini；不同集的分数不混着比
SET_ORDER = [settings.EVAL_DEFAULT_SET, *(s for s in settings.EVAL_SETS if s != settings.EVAL_DEFAULT_SET)]
# 训练目录里随 run 一起进资料库的小文件（配置、训练列表、日志）
SMALL_SUFFIXES = {".json", ".jsonl", ".yaml", ".list", ".log", ".out", ".txt"}
SKIP_FILES = {"runner_pid.json"}  # 训练机器上的进程号，换台机器没意义


def epoch_of(name: str):
    m = re.search(r"[-_]e(\d+)", name)
    return int(m.group(1)) if m else None


def step_of(name: str):
    m = re.search(r"_s(\d+)", name)
    return int(m.group(1)) if m else None


def rank(c):
    """评测结果的比较规则：先比 score 中位数，相同再比均值（没有均值的排在有均值的后面）。"""
    return c.score_median, -1.0 if c.score_mean is None else c.score_mean


def weight_id(engine: str, name: str):
    return f"{engine}:{name}"


def char_rel(slug: str):
    return f"characters/{slug}.json"


def run_rel(run_name: str):
    return f"runs/{run_name}/run.json"


def weight_dir(slug: str, engine: str, name: str):
    return f"weights/{slug}/{engine}/{Path(name).stem}"


def lib():
    return library.current()


class VersionUnavailable(Exception):
    """给了 version，但这个角色已经没有版本绑定的某个权重了（删掉了）：不换用别的角色的或底模的同名权重，接口返回 409。"""


def stored_name(slug: str, engine: str, name: str) -> str:
    """slug 训练出的权重在资料库里的文件名：和这个引擎的底模（GPT 或 SoVITS，按引擎配置认，不看本机装没装）同名时
    改成「<角色>-<原名>」，否则不变。所有放权重进资料库的入口（训练登记、导入）都经过它（add_weight）。"""
    cfg = settings.ENGINES[engine]
    return f"{slug}-{name}" if name in (cfg["pretrained_gpt"].name, cfg["pretrained_sovits"].name) else name


def half(x: dict) -> str:
    """一个权重条目在版本 id、手动默认、评测成绩的键里的写法：底模写 pretrained，训练出的写文件名。"""
    return VERSION_PRETRAINED if x["character"] is None else x["name"]


def prefer(slug: str):
    """同名权重的优先顺序（排序 key，排在后面的覆盖前面的）：别的角色的 < 底模 < slug 自己的。"""
    return lambda x: 2 if x["character"] == slug else 1 if x["character"] is None else 0


def copy_small_files(src_dir: Path, dst_rel: str, skip=()):
    """训练目录里的小文件（配置、train.list、日志）放进资料库 dst_rel/ 下；train.list 的音频路径换算成本机数据集路径。"""
    for f in sorted(src_dir.rglob("*")):
        if f.is_file() and f.suffix in SMALL_SUFFIXES and f.name not in SKIP_FILES | set(skip) \
                and f.stat().st_size < 50e6:
            rel = f"{dst_rel}/{f.relative_to(src_dir).as_posix()}"
            if f.name == "train.list" and f.parent == src_dir:
                lines = [f"{resolve_audio(line.split('|', 1)[0])}|{line.split('|', 1)[1]}" if line.strip() else line
                         for line in f.read_text(encoding="utf-8").splitlines()]
                lib().put_bytes(rel, ("\n".join(lines) + "\n").encode("utf-8"))
            else:
                lib().put_file(rel, f)


class Catalog:
    """没有自己的状态，每次都读资料库（资料库在内存里有全部记录）。"""

    # ---------------------------------------------------------------- 角色

    @staticmethod
    def characters() -> dict:
        return {c["slug"]: c for c in lib().docs("characters")}

    def slugs(self):
        """内置角色在前，各自按登记时间。"""
        chars = sorted(self.characters().values(), key=lambda c: (c["source"] != "builtin", c["created"], c["slug"]))
        return [c["slug"] for c in chars]

    def character(self, slug: str) -> dict:
        return lib().get(char_rel(slug))

    def builtin_of(self, slug: str):
        """用内置角色的数据集训练出来的角色 -> 那个内置角色（共用默认参考，评测结果才能和内置角色的权重比）。"""
        chars = self.characters()
        c = chars.get(slug)
        if c is None or c["source"] == "builtin":
            return None
        return next((s for s, b in chars.items() if b["source"] == "builtin" and b["dataset_id"] == c["dataset_id"]),
                    None)

    @staticmethod
    def add_character(slug: str, source: str, dataset_id: str, train_list: str = None, default_ref: str = None,
                      default_weights: dict = None):
        doc = dict(slug=slug, source=source, dataset_id=dataset_id, default_ref=default_ref,
                   default_weights=default_weights or {}, train_list=train_list, created=now_iso())
        lib().put(char_rel(slug), doc)
        return doc

    # ---------------------------------------------------------------- 训练 run

    @staticmethod
    def runs(slug: str = None) -> list:
        return sorted((r for r in lib().docs("runs") if slug is None or r["character"] == slug),
                      key=lambda r: (r["created"], r["run_name"]))

    @staticmethod
    def has_run(run_name: str) -> bool:
        return lib().has(run_rel(run_name))

    @staticmethod
    def run(run_name: str) -> dict:
        return lib().get(run_rel(run_name))

    @staticmethod
    def add_weight(slug: str, engine: str, kind: str, src: Path, source: str, run: str = None, name: str = None,
                   move: bool = False, link: bool = False, **meta):
        """一个权重文件放进资料库 weights/<slug>/<engine>/<文件名去后缀>/，meta.json 带 sha256。
        文件名和这个引擎的底模同名时改名（stored_name），原名记在 original_name；返回的 meta 里 name 是资料库里的名字。"""
        original = name or src.name
        name = stored_name(slug, engine, original)
        st = src.stat()
        d = weight_dir(slug, engine, name)
        sha = lib().put_file(f"{d}/{FILE_OF[kind]}", src, move=move, link=link)
        doc = dict(id=weight_id(engine, name), engine=engine, kind=kind, name=name, file=FILE_OF[kind], sha256=sha,
                   size=st.st_size, character=slug, source=source, run=run, display_name=None, note="", hidden=False,
                   created=datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"))
        if name != original:
            doc["original_name"] = original
        doc.update(meta)
        lib().put(f"{d}/meta.json", doc)
        return doc

    def register_run(self, slug: str, dataset_id: str, run: dict, files: dict, train_dir: Path = None,
                     move: bool = False, link: bool = False):
        """登记一次训练：权重文件（files = {"gpt": [路径], "sovits": [路径]}）放进资料库，训练目录的小文件放进 runs/<run>/，
        角色不存在就新建（训练出来的角色，参考库用这次的 train.list），最后写 run.json（有它就算登记完了，之前放进去的
        权重不出现在目录里；中途失败时重新登记一遍会覆盖）。run.json 里的权重名是资料库里的（和底模同名的改过名）。"""
        name = run["run_name"]
        if train_dir is not None:
            copy_small_files(train_dir, f"runs/{name}")
        if slug not in self.characters():
            has_list = lib().path(f"runs/{name}/train.list").exists()
            self.add_character(slug, "user", dataset_id, train_list=f"runs/{name}/train.list" if has_list else None)
        names = {k: [self.add_weight(slug, run["engine"], k, Path(p), "user", run=name, move=move, link=link)["name"]
                     for p in files[k]] for k in KINDS}
        doc = dict(run, character=slug, dataset_id=dataset_id, **names, created=run.get("created") or now_iso())
        lib().put(run_rel(name), doc)
        return doc

    def set_best(self, run_name: str, gpt: str, sovits: str):
        doc = dict(self.run(run_name), best=dict(gpt=gpt, sovits=sovits))
        lib().put(run_rel(run_name), doc)

    def remove(self, slug: str):
        """删掉训练出来的角色：角色记录、它的权重、训练 run、资料库里的参考。返回它的 run 列表。"""
        runs = self.runs(slug)
        for d in (f"weights/{slug}", f"refs/{slug}", *(f"runs/{r['run_name']}" for r in runs)):
            if lib().path(d).exists():
                lib().delete_tree(d)
        lib().delete(char_rel(slug))
        return runs

    # ---------------------------------------------------------------- 权重条目

    @staticmethod
    def entries(engine: str = None, remote: bool = False):
        """所有权重：dict(engine, kind, name, path, source, character, run, meta)。底模的 character、run、meta 为 None。
        默认组合、按名字找权重、推理选权重都从这里取，所以没提交的 run 的权重哪里都选不到。
        资料库里的按角色、来源（july 在前）、run、轮数排（自动默认的「最后一个 checkpoint」靠这个顺序）。
        remote：在别的设备上跑（局域网设备、云端），底模用那台自己装的，本机没装也列出来（路径是本机配置里的，
        remote.pretrained_key 按它认出是底模，不传文件）。"""
        out = []
        for e, cfg in settings.ENGINES.items():
            if engine and e != engine:
                continue
            for k in KINDS:
                p = cfg[f"pretrained_{k}"]
                if remote or p.exists():  # 本机跑时，本机没装的引擎不列它的底模
                    out.append(dict(engine=e, kind=k, name=p.name, path=str(p), source="pretrained", character=None,
                                    run=None, meta=None))
        # 只认已提交的 run 的权重：meta 里有 run 但 run.json 还没写（导入、训练登记到一半）的不算；没有 run 的（7 月权重）照常
        metas = sorted((m for m in lib().docs("weights") if (engine is None or m["engine"] == engine)
                        and (m["run"] is None or lib().has(run_rel(m["run"])))),
                       key=lambda m: (m["character"], m["source"] != "july", m["run"] or "", epoch_of(m["name"]) or 0,
                                      m["name"]))
        out += [dict(engine=m["engine"], kind=m["kind"], name=m["name"], source=m["source"], character=m["character"],
                     run=m["run"], meta=m,
                     path=str(lib().path(weight_dir(m["character"], m["engine"], m["name"])) / m["file"]))
                for m in metas]
        return out

    @staticmethod
    def _char_entries(ents: list, engine: str, slug: str):
        return [x for x in ents if x["engine"] == engine and x["character"] in (None, slug)]

    def find(self, wid: str, remote: bool = False):
        engine, _, name = wid.partition(":")
        for x in self.entries(engine, remote):
            if x["name"] == name:
                return x
        raise KeyError(f"权重 {wid}")

    def owner(self, engine: str, name: str):
        """权重属于哪个角色；底模返回 None（按引擎配置认：只装在局域网设备上的底模也是）。"""
        return self.find(weight_id(engine, name), remote=True)["character"]

    # ---------------------------------------------------------------- 评测

    @classmethod
    def latest_evals(cls, engine: str = None):
        """(engine, character, gpt, sovits, eval_set) -> 这个角色自己的这组权重在这个评测集上最新一次评测的 ComboScore。
        gpt、sovits 和版本 id 的两半一样：这个角色训练出的权重写文件名，底模写 pretrained（底模和训练出的权重可能同名，
        分开记才不会把底模的分算到训练的组合上，反过来也一样）。
        同名权重在不同角色间很常见（e8_s56.pth），只按文件名认会串分：一组成绩只算在评测角色 character 名下，并且两半
        都得是它自己的权重记录或底模（gpt_owner / sovits_owner）。用这个角色的参考评别的角色的权重，不算谁的成绩。
        没记 owner 的旧记录按现在的目录推断（同 resolve：角色自己有这个名字就是它的，否则是底模或别的角色的）。"""
        ents = cls.entries(engine, remote=True)
        own = {(x["engine"], x["character"], x["kind"], x["name"]) for x in ents if x["character"]}
        pre = {(x["engine"], x["kind"], x["name"]) for x in ents if x["character"] is None}

        def halves(doc, c):
            """这组的两半（文件名或 pretrained）；不是这个角色自己的成绩时 None。"""
            out, char = [], doc["character"]
            for k in KINDS:
                if f"{k}_owner" in c:
                    owner = c[f"{k}_owner"]
                else:
                    owner = char if (doc["engine"], char, k, c[k]) in own else \
                        None if (doc["engine"], k, c[k]) in pre else False
                if owner not in (None, char):
                    return None
                out.append(c[k] if owner else VERSION_PRETRAINED)
            return out
        rows = sorted(((doc, c, h) for doc in lib().docs("evals") if engine is None or doc["engine"] == engine
                       for c in doc["combos"] if (h := halves(doc, c))), key=lambda x: x[1]["evaluated_at"])
        return {(doc["engine"], doc["character"], *h, doc["eval_set"]): ComboScore(
            eval_set=doc["eval_set"], evaluated_at=c["evaluated_at"], character=doc["character"],
            ref_id=doc["ref_id"], job_id=doc["job_id"],
            **{k: c.get(k) for k in ("gpt", "sovits", "n", "cer_median", "spk_sim_median", "score_median",
                                     *STAT_COLUMNS)},
        ) for doc, c, h in rows}

    @staticmethod
    def eval_rel(job_id: str):
        """这个任务的评测文件：已有就用原来的（评测跨月也不拆开），没有就放在这个月的目录下。"""
        return next((rel for rel, d in lib().items("evals").items() if d["job_id"] == job_id),
                    f"evals/{datetime.now():%Y-%m}/{job_id}.json")

    def record_eval(self, engine: str, character: str, ref_id: str, job_id: str, eval_set: str, c,
                    version: str = None):
        """一组 GPT × SoVITS 评测完就追加进这个任务的评测文件（c 是 ComboEval，逐句结果一起存）。
        同时记下两半是哪个角色的权重记录（同 resolve 的规则；底模 None；这个库里没有的不记，读的时候再推断），
        之后这个角色多了同名权重也不会认错。version：按版本评测的，按版本绑定的记录记。"""
        owner = {}
        if version:
            for k, x in self.pick(engine, character, c.gpt, c.sovits, remote=True, version=version).items():
                owner[k, x["name"]] = x["character"]
        else:
            for x in sorted(self.entries(engine, remote=True), key=prefer(character)):
                owner[x["kind"], x["name"]] = x["character"]
        rel = self.eval_rel(job_id)
        doc = lib().get(rel) if lib().has(rel) else dict(
            id=job_id, job_id=job_id, engine=engine, character=character, ref_id=ref_id, eval_set=eval_set,
            created=now_iso(), combos=[])
        lib().put(rel, dict(doc, combos=[*doc["combos"], dict(
            c.model_dump(), evaluated_at=now_iso(),
            **{f"{k}_owner": owner[k, n] for k, n in (("gpt", c.gpt), ("sovits", c.sovits)) if (k, n) in owner})]))

    # ---------------------------------------------------------------- 默认组合

    @staticmethod
    def _auto_default(engine: str, slug: str, ents: list, evals: dict):
        """没有手动设默认时：这个角色自己的权重里评测分最高的组合（都没评测过就用最近一次训练的最后一个 checkpoint）；
        没有自己的权重用底模。"""
        cfg = settings.ENGINES[engine]
        own = {k: [x["name"] for x in ents if x["character"] == slug and x["kind"] == k] for k in KINDS}
        for es in SET_ORDER:
            scored = [evals[engine, slug, g, s, es] for g in own["gpt"] for s in own["sovits"]
                      if (engine, slug, g, s, es) in evals]
            if scored:
                best = max(scored, key=rank)
                return best.gpt, best.sovits
        # 只剩一半（比如 GPT 全删了）时，缺的那一半用底模
        return (own["gpt"] or [cfg["pretrained_gpt"].name])[-1], (own["sovits"] or [cfg["pretrained_sovits"].name])[-1]

    def default_halves(self, engine: str, slug: str, ents: list = None, evals: dict = None):
        """默认组合的两半，同版本 id 的写法：训练出的写文件名，底模写 pretrained（同名时也分得开）。
        手动设的那一半按设的时候选的记录（还在才用），否则自动默认。"""
        ents = self._char_entries(self.entries(engine) if ents is None else ents, engine, slug)
        auto = self._auto_default(engine, slug, ents, self.latest_evals(engine) if evals is None else evals)
        manual = self.manual_defaults(engine, slug)
        out = []
        for i, k in enumerate(KINDS):
            halves = {half(x) for x in ents if x["kind"] == k}
            own = {x["name"] for x in ents if x["kind"] == k and x["character"] == slug}
            out.append(manual[k] if manual.get(k) in halves else auto[i] if auto[i] in own else VERSION_PRETRAINED)
        return tuple(out)

    def defaults(self, engine: str, slug: str, ents: list = None, evals: dict = None):
        """默认组合的两个文件名。"""
        return tuple(settings.ENGINES[engine][f"pretrained_{k}"].name if h == VERSION_PRETRAINED else h
                     for k, h in zip(KINDS, self.default_halves(engine, slug, ents, evals), strict=True))

    def manual_defaults(self, engine: str, slug: str) -> dict:
        """手动设的默认 {gpt, sovits}。没登记的角色（上传参考零样本用的 custom 之类）没有。"""
        c = self.characters().get(slug)
        return (c["default_weights"].get(engine) or {}) if c else {}

    def manual_default(self, engine: str, slug: str, kind: str):
        return self.manual_defaults(engine, slug).get(kind)

    def set_default(self, engine: str, slug: str, kind: str, name):
        """name：这一半的写法同版本 id（训练出的权重的文件名，或 pretrained = 底模，见 half）；None 表示回到自动默认。"""
        doc = self.character(slug)
        dw = dict(doc["default_weights"])
        dw[engine] = dict(dw.get(engine) or dict(gpt=None, sovits=None), **{kind: name})
        de = doc.get("default_engine")
        if name is not None:
            de = engine
        elif de == engine and not any(dw[engine].values()):
            de = None
        lib().put(char_rel(slug), dict(doc, default_weights=dw, default_engine=de))

    # ---------------------------------------------------------------- 元数据（显示名、备注、隐藏）

    def set_meta(self, wid: str, **fields):
        """改资料库里权重的 meta.json（底模不在资料库里，没有 meta，调用方先挡掉）。"""
        x = self.find(wid)
        lib().put(f"{weight_dir(x['character'], x['engine'], x['name'])}/meta.json", dict(x["meta"], **fields))

    # ---------------------------------------------------------------- 删除

    def delete_weight(self, x: dict):
        """删掉一个训练出来的权重，从它的 run 里去掉；run 里一个权重都不剩时连 run 一起删。是角色手动默认的那一半回到自动。"""
        lib().delete_tree(weight_dir(x["character"], x["engine"], x["name"]))
        if x["run"] and self.has_run(x["run"]):
            run = dict(self.run(x["run"]))
            run[x["kind"]] = [n for n in run[x["kind"]] if n != x["name"]]
            if run.get("best") and run["best"][x["kind"]] == x["name"]:
                run["best"] = None
            if run["gpt"] or run["sovits"]:
                lib().put(run_rel(x["run"]), run)
            else:
                lib().delete_tree(f"runs/{x['run']}")
        if self.manual_default(x["engine"], x["character"], x["kind"]) == x["name"]:
            self.set_default(x["engine"], x["character"], x["kind"], None)

    # ---------------------------------------------------------------- 对外视图

    def weight_entries(self, eval_set: str = settings.EVAL_DEFAULT_SET):
        """所有权重的完整条目（GET /weights）。best_eval 只在 eval_set 这个评测集里挑。"""
        ents, evals, chars = self.entries(), self.latest_evals(), self.characters()
        runs = {r["run_name"]: r for r in self.runs()}
        defaults = {(e, s): self.default_halves(e, s, ents, evals) for e in settings.ENGINES for s in chars}
        out = []
        for x in ents:
            wid, i, m = weight_id(x["engine"], x["name"]), KINDS.index(x["kind"]), x["meta"] or {}
            run = runs.get(x["run"], {})
            if m:
                created, size = m["created"], m["size"]
            else:
                st = Path(x["path"]).stat()
                created, size = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"), st.st_size
            owners = [x["character"]] if x["character"] else list(chars)
            # 资料库里的权重只列它自己的角色的成绩（同名的别的角色的权重不算）；底模列所有角色的（键里写 pretrained）
            want = x["name"] if x["character"] else VERSION_PRETRAINED
            mine = [c for (e, ch, g, s, _), c in evals.items() if e == x["engine"] and (g, s)[i] == want
                    and x["character"] in (None, ch)]
            scores = sorted(sorted(mine, key=rank, reverse=True), key=lambda c: SET_ORDER.index(c.eval_set))
            in_set = [c for c in scores if c.eval_set == eval_set]
            dataset = run.get("dataset_id") or (chars[x["character"]]["dataset_id"] if x["character"] else None)
            out.append(WeightEntry(
                id=wid, name=x["name"], engine=x["engine"], type=x["kind"], character=x["character"],
                source=x["source"], epoch=epoch_of(x["name"]), step=step_of(x["name"]), job_id=run.get("job_id"),
                run_name=x["run"], dataset_id=dataset,
                filter=TrainFilter(**run["filter"]) if run.get("filter") else None,
                lines_used=run.get("lines_used"), minutes_used=run.get("minutes_used"),
                created_at=created, size_bytes=size, path=x["path"], sha256=m.get("sha256"),
                display_name=m.get("display_name"), note=m.get("note", ""), hidden=bool(m.get("hidden")),
                default_for=(default_for := [c for c in owners if defaults[x["engine"], c][i] == half(x)]),
                is_default=bool(default_for), evals=scores, best_eval=in_set[0] if in_set else None,
                deletable=x["source"] == "user",
            ))
        return out

    def engine_weights(self, engine: str, slug: str, ents: list, evals: dict, include_hidden: bool):
        d = self.defaults(engine, slug, ents, evals)
        lists = {k: [] for k in KINDS}
        runs = {r["run_name"]: r for r in self.runs(slug)}
        for x in self._char_entries(ents, engine, slug):
            m = x["meta"] or {}
            hidden = bool(m.get("hidden"))
            # 隐藏的权重不出现在可选列表里，但当前默认的那个始终保留
            if hidden and not include_hidden and x["name"] not in d:
                continue
            lists[x["kind"]].append(WeightFile(
                name=x["name"], source=x["source"], epoch=epoch_of(x["name"]),
                job_id=runs.get(x["run"], {}).get("job_id"), id=weight_id(engine, x["name"]),
                display_name=m.get("display_name"), hidden=hidden))
        return EngineWeights(gpt=lists["gpt"], sovits=lists["sovits"], default_gpt=d[0], default_sovits=d[1])

    def resolve(self, engine: str, slug: str, gpt: str = None, sovits: str = None, remote: bool = False,
                version: str = None):
        """返回 (gpt 名, gpt 路径, sovits 名, sovits 路径)，规则见 pick。"""
        w = self.pick(engine, slug, gpt, sovits, remote, version)
        return w["gpt"]["name"], w["gpt"]["path"], w["sovits"]["name"], w["sovits"]["path"]

    def pick(self, engine: str, slug: str, gpt: str = None, sovits: str = None, remote: bool = False,
             version: str = None) -> dict:
        """{gpt: 条目, sovits: 条目}（条目同 entries）。显式给的权重名可以是该引擎下任何角色的；
        同名的先用 slug 自己的。remote 见 entries：先定了在哪台设备上跑，再解析。
        version：权重来自这个版本（gpt、sovits 是它拆出来的文件名）：每一半只认版本绑定的记录（slug 训练出的权重，
        或写着 pretrained 的底模），缺了 VersionUnavailable，不换用同名的别的记录。"""
        ents = self.entries(engine, remote)
        if version:
            bound = dict(zip(KINDS, version.split("/")[1:], strict=True))
            ents = [x for x in ents if x["engine"] == engine and (
                x["character"] is None if bound[x["kind"]] == VERSION_PRETRAINED else x["character"] == slug)]
        if not (gpt and sovits):
            d = self.defaults(engine, slug, ents)
            gpt, sovits = gpt or d[0], sovits or d[1]
        # 同名时这个角色自己的权重优先，其次底模，再次别的角色的（不同角色可以有同名权重；版本和存下来的作品行、
        # 盲听组合都只记文件名，按角色解析才不会读成别的角色的文件）。不同名的照旧：显式给的可以是任何角色的
        found = {k: {} for k in KINDS}
        for x in sorted(ents, key=prefer(slug)):
            found[x["kind"]][x["name"]] = x
        for k, name in (("gpt", gpt), ("sovits", sovits)):
            if name not in found[k]:
                if version:
                    raise VersionUnavailable(f"{slug} 已经没有版本里的 {KIND_LABEL[k]} 权重 {name}（删掉了），重新选一个版本")
                raise KeyError(f"{engine} 下没有 {KIND_LABEL[k]} 权重 {name}")
            # 资料库同步到一半（Drive 只到了 meta.json）：版本绑定的训练权重也算不可用，开卡、派发之前就挡下
            if version and found[k][name]["character"] and not Path(found[k][name]["path"]).is_file():
                raise VersionUnavailable(f"{slug} 的 {KIND_LABEL[k]} 权重 {name} 的文件还没同步到这台机器，同步完再试")
        return dict(gpt=found["gpt"][gpt], sovits=found["sovits"][sovits])

    def prefs(self, slug: str) -> CharacterPrefs:
        """Read character preferences from characters/<slug>.json (backward compatible when prefs is absent)."""
        c = self.character(slug)
        raw = c.get("prefs") or {}
        raw_sp = raw.get("synth_params") or {}
        known = {k: v for k, v in raw_sp.items() if k in CharacterSynthParams.model_fields and v is not None}
        return CharacterPrefs(
            slug=slug,
            default_ref=c.get("default_ref"),
            synth_params=CharacterSynthParams(**known),
            last_ref_id=raw.get("last_ref_id"),
            aux_ref_ids=list(raw.get("aux_ref_ids") or []),
        )

    def update_prefs(self, slug: str, *, default_ref=_UNSET, synth_params=_UNSET, last_ref_id=_UNSET,
                     aux_ref_ids=_UNSET) -> CharacterPrefs:
        """Persist updated character preferences and/or default_ref into characters/<slug>.json."""
        doc = dict(self.character(slug))
        prefs = dict(doc.get("prefs") or {})
        if default_ref is not _UNSET:
            doc["default_ref"] = default_ref
        if synth_params is not _UNSET:
            prefs["synth_params"] = synth_params
        if last_ref_id is not _UNSET:
            prefs["last_ref_id"] = last_ref_id
        if aux_ref_ids is not _UNSET:
            prefs["aux_ref_ids"] = aux_ref_ids
        if prefs or "prefs" in doc:
            doc["prefs"] = prefs
        lib().put(char_rel(slug), doc)
        return self.prefs(slug)

    def info(self, slug: str, default_ref: str, ents: list, evals: dict, include_hidden: bool):
        c = self.character(slug)
        return CharacterInfo(
            slug=slug, source=c["source"], dataset_id=c["dataset_id"], default_ref=default_ref,
            prefs=self.prefs(slug),
            engines={e: self.engine_weights(e, slug, ents, evals, include_hidden) for e in settings.ENGINES},
        )


# ---------------------------------------------------------------- 模型版本（Studio 里一个角色只选一个版本）

def version_id(engine: str, gpt: str, sovits: str, pretrained=()) -> str:
    """版本 id 自带引擎和两个权重名（文件名里没有 /），生成等接口收到 version 时直接拆开用。pretrained 里的那几半
    （gpt / sovits）是底模，写成 pretrained：底模和角色训练出的权重可能同名，id 里分开，删了训练的不会换成底模。"""
    return "/".join([engine, *(VERSION_PRETRAINED if k in pretrained else n
                               for k, n in zip(KINDS, (gpt, sovits), strict=True))])


RUN_STARTED = re.compile(r"-(\d{8}-\d{6})$")


def trained_at(run: dict) -> str:
    """一次训练的时间：run 名末尾的启动时间（training.py 起 run 时按训练那台机器的本地时间写，Colab 上按主控的时区）。
    run.json 的 created 是登记进资料库的时间（导入、开发建库时可能晚很多），只在名字里没有时间时用。"""
    m = RUN_STARTED.search(run["run_name"])
    if m is None:
        return run["created"]
    return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").astimezone().isoformat(timespec="seconds")


def _best_of(engine: str, slug: str, gpts: list, sovits: list, evals: dict):
    """slug 训练出的这几个权重里评测分最高的组合（先看默认评测集）；都没评测过时最后一个 checkpoint。"""
    for es in SET_ORDER:
        scored = [evals[engine, slug, g, s, es] for g in gpts for s in sovits if (engine, slug, g, s, es) in evals]
        if scored:
            best = max(scored, key=rank)
            return best.gpt, best.sovits
    return gpts[-1], sovits[-1]


def versions(cat: "Catalog", slug: str, engines=None) -> list:
    """角色的模型版本（一对 GPT + SoVITS），新的在前：每次训练（run）的最佳组合（设过 best 用它，否则评测分最高，
    都没评测过用最后一个 checkpoint）、7 月训练的权重、用户手动设的默认组合（和上面重复时不另列）；
    角色一个自己的权重都没有时给底模零样本，优先 v2pro，没有能用的引擎时没有版本（只剩半套训练权重、凑不成版本的也没有）。
    engines 是能派发生成任务的引擎：纯底模的组合（零样本、两半都是底模的手动默认）只从这些引擎里出，固定的设备跑不了的
    不列、不推荐；含训练出的权重的版本不受它限制。None：不筛手动默认，也不给零样本。
    底模按引擎配置认（不看控制端装没装）：底模只装在局域网设备上时，手动设成底模的那一半照样是底模。
    created 是训练时间（trained_at；7 月那批按 settings.JULY_TRAINED；手动默认跟它 GPT 的 run；底模零样本是角色的登记时间），
    label 按它的月份（「11 月训练」，同月多个加序号，不是今年的带年份），不按进资料库的时间。
    recommended：手动设了默认的那个；没设时按评测分：先挑这个角色的版本里有成绩的最优先的评测集（hard 在前），
    在这个集里排（同目录的默认组合规则）；都没评测过时最新的。"""
    cat.character(slug)  # 没有这个角色时 KeyError
    ents, evals = cat.entries(remote=True), cat.latest_evals()
    have = {(x["engine"], x["kind"], x["name"]) for x in ents if x["character"] == slug}
    out = []

    def add(engine, gpt, sovits, created, source_run, kind, pretrained=None):
        """pretrained：哪几半是底模；不给时有这个角色自己的就是它训练出的权重，没有的那一半是底模。"""
        if pretrained is None:
            pretrained = {k for k, n in zip(KINDS, (gpt, sovits), strict=True) if (engine, k, n) not in have}
        vid = version_id(engine, gpt, sovits, pretrained)
        if all(v["id"] != vid for v in out):
            out.append(dict(id=vid, engine=engine, gpt=gpt, sovits=sovits, created=created, source_run=source_run,
                            kind=kind))
        return vid

    for r in cat.runs(slug):
        gpts = [n for n in r["gpt"] if (r["engine"], "gpt", n) in have]
        sovits = [n for n in r["sovits"] if (r["engine"], "sovits", n) in have]
        if not (gpts and sovits):
            continue
        best = r.get("best")
        if best and best["gpt"] in gpts and best["sovits"] in sovits:
            g, s = best["gpt"], best["sovits"]
        else:
            g, s = _best_of(r["engine"], slug, gpts, sovits, evals)
        add(r["engine"], g, s, trained_at(r), r["run_name"], "train")
    for engine in settings.ENGINES:
        july = [x for x in ents if x["engine"] == engine and x["character"] == slug and x["source"] == "july"]
        gpts = [x["name"] for x in july if x["kind"] == "gpt"]
        sovits = [x["name"] for x in july if x["kind"] == "sovits"]
        if gpts and sovits:
            g, s = _best_of(engine, slug, gpts, sovits, evals)
            add(engine, g, s, settings.JULY_TRAINED, None, "train")
    manual = {}
    for engine in settings.ENGINES:
        if not any(cat.manual_defaults(engine, slug).values()):
            continue
        halves = cat.default_halves(engine, slug, ents, evals)
        # 两半都是底模：和零样本一样只用能派发的引擎
        if engines is not None and engine not in engines and halves == (VERSION_PRETRAINED, VERSION_PRETRAINED):
            continue
        g, s = cat.defaults(engine, slug, ents, evals)
        run = next((x["run"] for x in ents if x["engine"] == engine and x["kind"] == "gpt" and x["name"] == g
                    and x["character"] == slug and halves[0] != VERSION_PRETRAINED), None)
        created = trained_at(cat.run(run)) if run and cat.has_run(run) else now_iso()
        manual[engine] = add(engine, g, s, created, run, "manual",
                             {k for k, h in zip(KINDS, halves, strict=True) if h == VERSION_PRETRAINED})
    if not out:
        usable = [] if have else [e for e in settings.ENGINES if e in (engines or ())]
        if not usable:
            return []
        engine = "v2pro" if "v2pro" in usable else usable[0]
        g, s = cat.defaults(engine, slug, ents, evals)
        add(engine, g, s, cat.character(slug)["created"], None, "pretrained")

    out.sort(key=lambda v: v["created"], reverse=True)

    def key(v, es):  # 评测成绩的键和版本 id 一样分底模（pretrained）和训练出的权重
        return (v["engine"], slug, *v["id"].split("/")[1:], es)
    for v in out:
        v["score_set"] = next((es for es in SET_ORDER if key(v, es) in evals), None)
        v["score"] = evals[key(v, v["score_set"])].score_median if v["score_set"] else None
    in_set = next(([(evals[key(v, es)], v) for v in out if key(v, es) in evals]
                   for es in SET_ORDER if any(v["score_set"] == es for v in out)), [])
    pref_engine = (cat.characters().get(slug) or {}).get("default_engine")
    rec_manual = manual.get(pref_engine) or next(iter(manual.values()), None)
    rec = rec_manual or (max(in_set, key=lambda x: rank(x[0]))[1]["id"] if in_set else out[0]["id"])
    this_year = datetime.now().year
    months = {}
    for v in sorted(out, key=lambda v: v["created"]):
        if v["kind"] == "pretrained":
            v["label"] = "底模（零样本）"
            continue
        if v["kind"] == "manual":
            v["label"] = "我设的默认"
            continue
        at = datetime.fromisoformat(v["created"])
        months.setdefault((at.year, at.month), []).append(v)
    for (year, month), vs in months.items():
        base = f"{month} 月训练" if year == this_year else f"{year} 年 {month} 月训练"
        for i, v in enumerate(vs, 1):
            v["label"] = base if len(vs) == 1 else f"{base} {i}"
    return [dict({k: v[k] for k in ("id", "label", "engine", "gpt", "sovits", "score", "score_set", "source_run",
                                     "created")},
                 recommended=v["id"] == rec) for v in out]


def engine_infos():
    july = {m["engine"] for m in lib().docs("weights") if m["source"] == "july"}
    return [EngineInfo(
        id=e, version=cfg["version"], code=cfg["code"], code_path=settings.CODE_DESC[cfg["code"]], zero_shot=True,
        trainable=True,
        train_method="SoVITS 训 LoRA（rank 32）+ GPT 微调" if "lora" in cfg["s2_script"] else "SoVITS 全量微调 + GPT 微调",
        pretrained_gpt=cfg["pretrained_gpt"].name, pretrained_sovits=cfg["pretrained_sovits"].name,
        has_july_weights=e in july,
        supports_aux_refs=cfg["supports_aux_refs"],
        train_defaults=TrainDefaults(**settings.TRAIN_DEFAULTS[e]),
    ) for e, cfg in settings.ENGINES.items()]


def delete_run_dirs(runs: list):
    """删角色时连本机的训练中间产物（TRAIN_DIR/<run>）一起删。"""
    for r in runs:
        d = settings.TRAIN_DIR / r["run_name"]
        if d.exists():
            shutil.rmtree(d)
