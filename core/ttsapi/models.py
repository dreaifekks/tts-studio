"""对外的请求/响应模型。/openapi.json 由这些模型生成，Mac app 按它写 Swift 模型。"""
from typing import Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ttsapi import settings

Engine = Literal["v1", "v2", "v2pro", "v2proplus", "v3", "v4", "v5turbo", "v5dev"]
SplitMethod = Literal["cut0", "cut1", "cut2", "cut3", "cut4", "cut5"]
AudioFormat = Literal["wav", "mp3", "flac"]
TextLang = Literal[
    "ja", "zh", "en", "ko", "yue", "auto", "auto_yue",
    "all_ja", "all_zh", "all_ko", "all_yue",
]
LoraRank = Literal[16, 32, 64, 128]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
JobKind = Literal["train", "sweep", "work_generate", "work_render", "candidates", "compare", "eval", "blind", "bench"]
EvalSetName = Literal["mini", "hard"]
Order = Literal["asc", "desc"]


class ErrorResponse(BaseModel):
    detail: str = Field(..., description="错误说明（中文）。参数校验失败（422）时是 FastAPI 的默认结构")
    code: Optional[str] = Field(None, description="资料库相关：library_not_selected（未选资料库）、"
                                                 "library_not_owned（资料库由别的控制端管理）、not_a_library；"
                                                 "算力相关（409）：confirm_required（云端花费超过提醒线，带 approved=true、"
                                                 "approved_device、approved_units 再提交）、"
                                                 "compute_unavailable（指定的设备现在不能用）；"
                                                 "添加设备（409）：invite_used（邀请已经核销）、invite_expired、"
                                                 "tailscale_unavailable；音频（404）：audio_elsewhere（在 machine 上）")
    plan: Optional["Plan"] = Field(None, description="confirm_required / compute_unavailable 时附上 POST /plan 的结果，给弹窗用")
    machine: Optional[str] = Field(None, description="audio_elsewhere：音频在哪台主控上（它在设备列表里的名字）")


# ---------------------------------------------------------------- 在哪跑（提交任务时可带）

class ComputeChoice(BaseModel):
    compute: Optional[str] = Field(None, description="在哪台设备跑（GET /compute 的设备 id，如 local、lan-1a2b3c、colab-L4）；"
                                                     "不填自动选（POST /plan 的推荐项）")
    approved: bool = Field(False, description="云端花费超过提醒线时，用户在弹窗里确认过：带 true 再提交一次，"
                                              "同时带上弹窗里的 approved_device、approved_units")
    approved_device: Optional[str] = Field(None, description="确认时弹窗里的设备（plan 的 chosen，如 colab-L4）")
    approved_units: Optional[float] = Field(None, description="确认时弹窗里的单元（chosen 候选的 units）：提交时重算的设备不同"
                                                              "或单元超过它，再返回 409 confirm_required（附新 plan）；"
                                                              "扫参执行时把它当确认过的预算")


# ---------------------------------------------------------------- 合成参数

VERSION_DESC = ("模型版本 id（GET /characters/{id}/versions 的 id）：引擎和一对 GPT + SoVITS。给了它就不用给 engine / gpt / "
                "sovits（「更多设置」里手动选权重时才用那几个；同时给了且不一致时 422）。版本绑定这个角色训练出的权重或底模，"
                "绑定的权重已经没有了时 409 version_unavailable")
# 版本 id 里底模那一半写成它（训练出的权重文件名都带后缀，不会叫这个）；其余每一半都是这个角色训练出的权重
VERSION_PRETRAINED = "pretrained"


def parse_version(vid: str) -> tuple:
    """版本 id（「引擎/GPT/SoVITS」，每一半是文件名或 pretrained = 这个引擎的底模）-> (engine, gpt 文件名, sovits 文件名)。"""
    parts = vid.split("/")
    if len(parts) != 3 or parts[0] not in get_args(Engine) or not all(parts):
        raise ValueError(f"version 格式不对：{vid}")
    engine = parts[0]
    return engine, *(settings.ENGINES[engine][f"pretrained_{k}"].name if n == VERSION_PRETRAINED else n
                     for k, n in zip(("gpt", "sovits"), parts[1:], strict=True))


def apply_version(m):
    """version 拆成 engine、gpt、sovits 填进去。"""
    if m.version is None:
        return m
    engine, gpt, sovits = parse_version(m.version)
    if m.gpt not in (None, gpt) or m.sovits not in (None, sovits) or \
            ("engine" in m.model_fields_set and m.engine != engine):
        raise ValueError("version 和 engine / gpt / sovits 不一致：只给 version，或只给 engine / gpt / sovits")
    m.engine, m.gpt, m.sovits = engine, gpt, sovits
    return m


class GenParams(BaseModel):
    engine: Engine = Field("v2pro", description="GPT-SoVITS 版本。每个引擎都能用底模零样本，也能用训练出的权重；"
                                             "角色可配置默认权重。引擎说明见 GET /models 的 engines")
    ref_id: Optional[str] = Field(None, description="参考音频 id（GET /refs 或 POST /refs/upload 得到）")
    emotion: Optional[str] = Field(None, description="不给 ref_id 时，从带这个标签的参考里随机挑一条")
    gpt: Optional[str] = Field(None, description="GPT 权重文件名，可选值见 GET /models；默认用角色的默认组合")
    sovits: Optional[str] = Field(None, description="SoVITS 权重文件名，可选值见 GET /models")
    top_k: int = 15
    top_p: float = 1.0
    temperature: float = 1.0
    repetition_penalty: float = 1.35
    seed: int = Field(-1, description="-1 随机；实际用的 seed 会在结果里返回")
    speed: float = 1.0
    fragment_interval: float = Field(0.3, ge=0.01, le=2.0, description="Pause duration in seconds between split text fragments")
    text_split_method: SplitMethod = Field(
        "cut5", description="cut0 不切；cut1 四句一切；cut2 50 字一切；cut3 按中文句号；cut4 按英文句号；cut5 按标点")
    sample_steps: Optional[int] = Field(None, description="仅 V5：扩散采样步数，默认 v5turbo=4、v5dev=32")
    aux_ref_ids: list[str] = Field(default_factory=list, description="Auxiliary reference audio IDs for multi-reference timbre blending")
    version: Optional[str] = Field(None, description=VERSION_DESC)

    _version = model_validator(mode="after")(apply_version)


class TTSParams(GenParams, ComputeChoice):
    text: str = Field(..., description="要合成的文本")
    character: str = Field("demo", description="角色 slug，见 GET /models")
    text_lang: str = Field("ja", description="文本语言：ja/zh/en/ko/yue/auto，或 all_ja 等")
    format: AudioFormat = "wav"
    debug: bool = Field(False, description="返回 GPT 逐步采样信息和频谱图（响应变成 JSON）")
    return_json: bool = Field(False, description="返回 JSON（含音频 URL）而不是音频本身")


class CandidatesParams(TTSParams):
    n: int = Field(6, ge=1, le=16, description="候选数，seed 依次 +1")


class CompareParams(TTSParams):
    engines: list[Engine] = Field(["v2pro", "v5turbo"], description="参与对比的引擎。给了 version 时：不给 engines 就是"
                                                                    "版本的引擎加上默认的 v2pro、v5turbo；给了但不含版本的引擎 422")
    n: int = Field(1, ge=1, le=16, description="每个引擎出几条候选")

    @model_validator(mode="after")
    def _version_engine(self):
        """version 只用在它自己的引擎上（别的引擎用各自的默认），所以对比里要有这个引擎。"""
        if self.version is None:
            return self
        engine = self.version.split("/")[0]
        if "engines" not in self.model_fields_set:
            self.engines = [engine, *(e for e in self.engines if e != engine)]
        elif engine not in self.engines:
            raise ValueError(f"engines 里没有 version 的引擎 {engine}")
        return self


# ---------------------------------------------------------------- 音频和 debug

class TokenProb(BaseModel):
    token: int
    p: float


class Step(BaseModel):
    step: int
    token: int = Field(..., description="采到的语义 token，1024 是 EOS")
    p: float = Field(..., description="在实际采样分布（重复惩罚、top-p、温度、top-k 之后）里的概率")
    p_raw: float = Field(..., description="在模型原始分布 softmax(logits) 里的概率")
    entropy: float = Field(..., description="原始分布的熵（bit）")
    top5: list[TokenProb]


class Segment(BaseModel):
    n_tokens: int
    text_phones: int
    phones: list[str]
    tokens_per_phone: float
    ref_tokens_per_phone: float
    ratio_vs_ref: float
    hit_max_len: bool
    eos_step: Optional[int] = None
    eos_by: Optional[Literal["sampled", "argmax"]] = None
    steps: Optional[list[Step]] = None


class ReviewIn(BaseModel):
    rating: Optional[int] = Field(None, ge=1, le=5, description="1–5 分，和 verdict 二选一或都填")
    verdict: Optional[Literal["good", "bad"]] = None
    tags: list[str] = Field([], description="问题标签：吞字、多字、电音、声调怪、情绪不对、节奏怪，或自定义")
    note: str = ""


class Review(ReviewIn):
    audio_id: str
    updated: str


class AudioItem(BaseModel):
    id: str
    created: str
    audio_url: str = Field(..., description="相对路径，如 /audio/<id>")
    mel_url: Optional[str] = Field(None, description="debug 时才有的频谱图，相对路径")
    text: str
    text_lang: Optional[str] = None
    character: str
    engine: str
    gpt: str
    sovits: str
    ref_id: str
    seed: int
    top_k: Optional[int] = None
    top_p: Optional[float] = None
    temperature: Optional[float] = None
    repetition_penalty: Optional[float] = None
    speed: Optional[float] = None
    fragment_interval: Optional[float] = None
    text_split_method: Optional[str] = None
    sample_steps: Optional[int] = None
    aux_ref_ids: list[str] = Field(default_factory=list)
    duration: float
    infer_sec: float
    synth_wall_sec: float
    semantic_tokens: int
    vram_peak_mb: int
    ratio_vs_ref: list[float]
    asr_text: Optional[str] = None
    cer: Optional[float] = None
    cer_kana: Optional[float] = None
    spk_sim: Optional[float] = None
    score: Optional[float] = None
    segments: Optional[list[Segment]] = None
    work_id: Optional[str] = None
    compute: Optional[str] = Field(None, description="在哪台设备上合成的（设备 id）")
    review: Optional[Review] = None


class AudioPage(BaseModel):
    total: int
    items: list[AudioItem]


class CandidatesResult(BaseModel):
    engine: str
    ref_id: str
    ref_text: str
    elapsed_sec: float
    candidates: list[AudioItem] = Field(..., description="按 score 从高到低")


class CompareResult(BaseModel):
    text: str
    ref_id: str
    ref_text: str
    base_seed: int
    elapsed_sec: float
    results: list[CandidatesResult]


# ---------------------------------------------------------------- 服务状态、模型、参考

class WorkerStatus(BaseModel):
    running: bool
    pid: Optional[int]
    busy: int
    idle_sec: int


class Health(BaseModel):
    status: str
    time: str
    training_job: Optional[str] = Field(None, description="正在训练的任务 id；不为空时推理接口返回 503")
    workers: dict[str, WorkerStatus]
    version: str = Field(..., description="正在运行的代码版本：在 tag 上是 0.1.0；不在 tag 上是 0.1.0-dev+<短 sha>"
                                          "（基于最近的 tag，没有 tag 时是 0.0.0）")
    commit: str = Field(..., description="正在运行的代码的短 sha")


class SystemVersion(BaseModel):
    version: str = Field(..., description="同 /health 的 version（服务启动时的代码）")
    commit: str
    dirty: bool = Field(..., description="工作区里有没提交的改动（不算未跟踪文件）；为 true 时 /system/update 返回 409")
    branch: str


class UpdateRequest(BaseModel):
    tag: str = Field(..., pattern=r"^v\d+\.\d+\.\d+$", description="要更新到的版本 tag，如 v0.1.1")


class UpdateResult(BaseModel):
    from_: str = Field(..., alias="from", description="更新前的版本（同 /health 的 version）")
    to: str = Field(..., description="更新后的版本（不带 v）。服务重启后 /health 的 version 等于它")


WeightSource = Literal["july", "user", "pretrained"]


class WeightFile(BaseModel):
    name: str
    source: WeightSource = Field(..., description="july=历史导入权重；user=通过 /train 训练；pretrained=底模")
    epoch: Optional[int] = None
    job_id: Optional[str] = None
    id: Optional[str] = Field(None, description="权重 id（引擎:文件名），GET/PATCH/DELETE /weights/{id} 用")
    display_name: Optional[str] = Field(None, description="用户起的显示名，没起就是 null")
    hidden: bool = Field(False, description="是否被隐藏；隐藏的权重默认不出现在 /models 里（当前默认的除外）")


class EngineWeights(BaseModel):
    gpt: list[WeightFile]
    sovits: list[WeightFile]
    default_gpt: str
    default_sovits: str


class CharacterSynthParams(BaseModel):
    """Per-character default synthesis parameter overrides."""
    model_config = ConfigDict(extra="forbid")

    speed: Optional[float] = Field(None, ge=0.5, le=2.0)
    fragment_interval: Optional[float] = Field(None, ge=0.01, le=2.0)
    top_k: Optional[int] = Field(None, ge=1, le=100)
    top_p: Optional[float] = Field(None, gt=0.0, le=1.0)
    temperature: Optional[float] = Field(None, gt=0.0, le=2.0)
    repetition_penalty: Optional[float] = Field(None, ge=1.0, le=2.5)
    text_split_method: Optional[SplitMethod] = None
    sample_steps: Optional[int] = Field(None, ge=1, le=128)
    text_lang: Optional[TextLang] = None


class CharacterPrefs(BaseModel):
    """Character-level synthesis preferences and default reference settings."""
    slug: str
    default_ref: Optional[str] = None
    synth_params: CharacterSynthParams = Field(default_factory=CharacterSynthParams)
    last_ref_id: Optional[str] = None
    aux_ref_ids: list[str] = Field(default_factory=list)


class CharacterPrefsPatch(BaseModel):
    """Partial update payload for PATCH /characters/{slug}/prefs."""
    default_ref: Optional[str] = Field(None, description="Explicit default reference ID; null or empty string restores automatic default")
    synth_params: Optional[CharacterSynthParams] = Field(None, description="Synthesis parameter overrides to merge; null clears overrides")
    reset_synth_params: bool = Field(False, description="Clear stored synth_params before applying synth_params if also given")
    last_ref_id: Optional[str] = Field(None, description="Last used primary reference ID for this character; null clears it")
    aux_ref_ids: Optional[list[str]] = Field(None, description="Last used auxiliary reference IDs for this character")


class CharacterInfo(BaseModel):
    slug: str
    source: Literal["builtin", "user"]
    dataset_id: Optional[str]
    default_ref: Optional[str] = None
    prefs: Optional[CharacterPrefs] = None
    engines: dict[str, EngineWeights] = Field(..., description="引擎 -> 可用权重（底模 + 历史导入权重 + 训练出的权重）和默认组合")


class TrainDefaults(BaseModel):
    """Recommended training hyperparameters for an engine."""
    sovits_epochs: int
    sovits_save_every: int
    sovits_batch: int
    gpt_epochs: int
    gpt_save_every: int
    gpt_batch: int
    lora: bool
    lora_rank: Optional[LoraRank] = None
    lora_ranks: list[int] = Field(default_factory=list)


class EngineInfo(BaseModel):
    id: str = Field(..., description="引擎 id，即 /tts 等接口的 engine 参数")
    version: str = Field(..., description="GPT-SoVITS 里的版本名")
    code: Literal["fork", "v5"] = Field(..., description="fork=固定上游提交加补丁；v5=GPT-SoVITS 上游 V5")
    code_path: str
    zero_shot: bool = Field(..., description="能否用底模 + 参考音频零样本推理")
    trainable: bool = Field(..., description="能否通过 /train 训练")
    train_method: str
    pretrained_gpt: str = Field(..., description="GPT 底模文件名")
    pretrained_sovits: str = Field(..., description="SoVITS 底模文件名")
    has_july_weights: bool = Field(..., description="是否有历史导入的内置角色权重（source=july，仅 v2pro）")
    supports_aux_refs: bool = Field(False, description="Whether this engine supports multi-reference aux_ref_ids blending")
    train_defaults: Optional[TrainDefaults] = Field(None, description="Recommended training defaults for trainable engines")


class ModelsResponse(BaseModel):
    characters: list[CharacterInfo]
    engines: list[EngineInfo] = Field([], description="所有引擎：能否零样本、能否训练、用哪套代码")


class DeletedCharacter(BaseModel):
    slug: str
    runs: list[str] = Field(..., description="删掉的训练 run（weights/<engine>/<run> 和 training/<run>）")
    audio_deleted: int = Field(..., description="删掉的、不在作品里的生成音频条数")


class Ref(BaseModel):
    id: str
    label: str = Field(..., description="显示名：角色的默认参考是「默认参考音频 · 3.2 秒」，其余是文本前几个字 + 时长"
                                        "（「夜になっても、お母… · 6.1 秒」），没有文本时「参考音频 · 1.0 秒」")
    audio_url: str = Field(..., description="参考原始音频，相对路径 /refs/<id>/audio，可加 ?format=mp3")
    character: str
    text: str
    lang: str
    duration: float
    tags: list[str]


class RefList(BaseModel):
    count: int
    refs: list[Ref]


class TagsBody(BaseModel):
    tags: list[str] = Field(..., description="情绪标签，自由字符串，整体替换")


class WorkerNames(BaseModel):
    workers: list[str]


# ---------------------------------------------------------------- 数据集和训练

class DatasetInfo(BaseModel):
    id: str = Field(..., description="如 SAMPLE_DS.sp0001")
    name: str
    path: str
    character: Optional[str] = Field(None, description="对应的内置角色 slug")
    lines: int
    total_minutes: float


class DatasetList(BaseModel):
    datasets: list[DatasetInfo]


class DatasetPreview(BaseModel):
    id: str
    lines_total: int
    lines_kept: int
    minutes_kept: float
    excluded_short: int
    excluded_long: int
    excluded_interjection: int
    interjection_examples: list[str]


class TrainEpochs(BaseModel):
    """训练轮数和保存间隔：训练请求和 POST /plan（kind=train）共用，估时（训练量、训后评测的组合数）按它算。"""
    sovits_epochs: int = Field(8, ge=1)
    sovits_save_every: int = Field(4, ge=1)
    gpt_epochs: int = Field(15, ge=1)
    gpt_save_every: int = Field(5, ge=1)

    @model_validator(mode="after")
    def _saves_something(self):
        if self.sovits_epochs < self.sovits_save_every or self.gpt_epochs < self.gpt_save_every:
            raise ValueError("轮数不能小于保存间隔（否则训完没有导出的权重）")
        return self


class TrainRequest(ComputeChoice, TrainEpochs):
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{1,40}$", description="角色 slug（英文小写、数字、连字符）")
    dataset_id: str
    min_sec: float = 1.0
    max_sec: float = 12.0
    exclude_interjections: bool = True
    max_lines: Optional[int] = Field(None, ge=10, description="过滤后最多用多少句（均匀抽取），不填用全部")
    engine: Engine = Field("v2pro", description="v1/v2/v2pro/v2proplus 全量微调 SoVITS；v3/v4/v5turbo/v5dev 训 SoVITS LoRA，"
                                                "显存占用大，sovits_batch 建议 2–4")
    sovits_batch: int = Field(8, ge=1)
    gpt_batch: int = Field(8, ge=1)
    lora_rank: Optional[LoraRank] = Field(None, description="LoRA rank for LoRA engines (16/32/64/128); defaults to 32")
    ref_id: Optional[str] = Field(None, description="训后自动评测用的参考音频 id（GET /refs）；不填用这个角色的默认参考。"
                                                    "新角色可以填它训练列表里的句子（id 为「slug-文件名」，训练完才建索引）")


class SentenceEval(BaseModel):
    index: int = Field(..., description="评测集里第几句（从 0 开始）")
    text: str
    n: int
    cer_median: float
    cer_max: float
    spk_sim_median: float
    spk_sim_min: float
    score_median: float
    score_min: float


class ComboEval(BaseModel):
    gpt: str
    sovits: str
    n: int
    cer_median: float = Field(..., description="日语用读音 CER（cer_kana）")
    spk_sim_median: float
    score_median: float
    cer_mean: Optional[float] = Field(None, description="均值；这一轮之前的评测记录没有")
    cer_max: Optional[float] = Field(None, description="最差的一条（CER 最大）")
    spk_sim_mean: Optional[float] = None
    spk_sim_min: Optional[float] = Field(None, description="最差的一条（相似度最小）")
    score_mean: Optional[float] = None
    score_min: Optional[float] = None
    per_sentence: Optional[list[SentenceEval]] = Field(None, description="逐句统计（只在评测任务的结果里有，/weights 里没有）")


class ModelVersion(BaseModel):
    id: str = Field(..., description="版本 id，生成、候选、对比、作品、盲听等接口的 version 参数用它。格式「引擎/GPT/SoVITS」，"
                                     "底模那一半写 pretrained；当作不透明字符串用，文件名看 gpt、sovits")
    label: str = Field(..., description="显示名：「11 月训练」（同月多个加序号，不是今年的带年份）、「我设的默认」、「底模（零样本）」")
    engine: str
    gpt: str = Field(..., description="GPT 权重文件名")
    sovits: str = Field(..., description="SoVITS 权重文件名")
    score: Optional[float] = Field(None, description="评测分（score 中位数，先看默认评测集）；没评测过 null")
    score_set: Optional[EvalSetName] = Field(None, description="score 是哪个评测集上的（不同集的分数不能放在一起比）")
    recommended: bool = Field(..., description="推荐的那个（只有一个）：手动设了默认的；没设时在这个角色有成绩的最优先的"
                                               "评测集里排名最高的；都没评测过时最新的。没有版本时列表为空")
    source_run: Optional[str] = Field(None, description="来自哪次训练（run 名）；历史导入权重和底模为 null")
    created: str = Field(..., description="训练时间（label 按它）：run 名里的启动时间（没有时 run 的 created）；历史导入权重保留原训练月份；"
                                          "我设的默认跟它 GPT 的 run；底模零样本是角色登记的时间。不是进资料库的时间")


class ComboScore(ComboEval):
    eval_set: EvalSetName = Field(..., description="用的评测集；不同集的分数不能放在一起比")
    character: str = Field(..., description="评测时用的角色（决定默认参考）")
    ref_id: str
    job_id: str = Field(..., description="做这次评测的任务（train 或 eval）")
    evaluated_at: str


class TrainFilter(BaseModel):
    min_sec: float
    max_sec: float
    exclude_interjections: bool
    max_lines: Optional[int] = None


WeightSort = Literal["created_at", "score", "epoch", "engine", "character", "size", "name", "type", "source"]


class WeightEntry(BaseModel):
    id: str = Field(..., description="引擎:文件名，例如 v2pro:alice-e15.ckpt")
    name: str = Field(..., description="文件名，/tts 等接口的 gpt / sovits 参数用它")
    engine: str
    type: Literal["gpt", "sovits"]
    character: Optional[str] = Field(None, description="属于哪个角色；底模为 null（所有角色共用）")
    source: WeightSource
    epoch: Optional[int] = None
    step: Optional[int] = Field(None, description="SoVITS 文件名里的训练步数；GPT 没有")
    job_id: Optional[str] = Field(None, description="训练任务 id（只有 user 来源有）")
    run_name: Optional[str] = None
    dataset_id: Optional[str] = Field(None, description="训练数据集；历史导入权重使用对应内置角色的数据集")
    filter: Optional[TrainFilter] = Field(None, description="训练数据的过滤条件（这一轮之后训练的才有记录）")
    lines_used: Optional[int] = None
    minutes_used: Optional[float] = None
    created_at: str = Field(..., description="文件修改时间")
    size_bytes: int
    path: str = Field(..., description="控制端上的绝对路径（资料库里的权重是 weights/<角色>/<引擎>/<id>/gpt.ckpt 或 sovits.pth）")
    sha256: Optional[str] = Field(None, description="资料库里的权重才有；底模为 null")
    display_name: Optional[str] = None
    note: str = ""
    hidden: bool = False
    default_for: list[str] = Field(..., description="以它为默认的角色（在这个引擎下）；底模可能是多个角色的默认")
    is_default: bool = Field(..., description="default_for 不为空")
    evals: list[ComboScore] = Field(..., description="包含这个权重的、评测过的组合（每个组合在每个评测集上取最新一次），"
                                                     "先按评测集（hard 在前）再按分数从高到低")
    best_eval: Optional[ComboScore] = Field(None, description="请求参数 eval_set 那个评测集里分数最高的（默认 hard）；"
                                                              "sort=score 按它排。没在这个集上评测过时为 null")
    deletable: bool = Field(..., description="只有 user 来源可以删除")


class WeightList(BaseModel):
    total: int
    weights: list[WeightEntry]


class WeightPatch(BaseModel):
    display_name: Optional[str] = Field(None, description="显示名；传空字符串清除")
    note: Optional[str] = None
    hidden: Optional[bool] = None
    is_default: Optional[bool] = Field(None, description="true=设为 character 在这个引擎下默认的 GPT（或 SoVITS）；"
                                                         "false=取消手动设置，回到自动默认")
    character: Optional[str] = Field(None, description="设默认时针对哪个角色；不填用权重所属角色（底模必须填）")


class DefaultChange(BaseModel):
    character: str
    gpt: str
    sovits: str


class DeletedWeight(BaseModel):
    id: str
    path: str
    was_default_for: list[str] = Field(..., description="删之前以它为默认的角色")
    new_defaults: list[DefaultChange] = Field(..., description="这些角色现在的默认组合（自动换成评测分最高的另一组）")


class RunImportRequest(BaseModel):
    path: str = Field(..., description="控制端上的目录或 .tar，里面有另一台机器（Colab VM 等）的资料库（含 library.json 的那层）"
                                       "和可选的 data/tts.db（训练任务记录）、data/jobs/*.log，比如 Colab 作业的 results.tar")
    runs: Optional[list[str]] = Field(None, description="只导入这些训练 run（run_name）；不填导入全部")
    into_builtin: bool = Field(False, description="用内置角色数据集训练的角色（如 Colab 扫参的 alice-v2proplus-s16g20）"
                                                  "并到内置角色名下（如 alice），与历史导入权重归在同一个角色下；"
                                                  "run 名、权重文件名不变。已按原名导入过的 run 照样跳过")


class RunImportResult(BaseModel):
    imported: list[dict] = Field(..., description="导入的 run：character、run、engine、gpt、sovits、"
                                                  "evals（评测记录条数）、missing（来源里没有、没导入的权重）、"
                                                  "source_character（并到内置角色时的原角色名）")
    skipped: list[dict] = Field(..., description="跳过的 run 和原因（资料库里已有、缺权重文件、角色数据集不一致）")
    evals: int = Field(..., description="新增的评测记录条数")
    jobs: list[str] = Field(..., description="导入的训练任务记录")
    filled: list[str] = Field([], description="补上结果的原任务（Colab 作业写库失败、训练 run 被跳过或写库时取消后，"
                                              "用结果 tar 重试）")


class EvalRequest(ComputeChoice):
    engine: Optional[Engine] = Field(None, description="不给 version 时必填")
    gpt: Optional[str] = Field(None, description="GPT 权重文件名；不给 version 时必填")
    sovits: Optional[str] = Field(None, description="SoVITS 权重文件名；不给 version 时必填")
    version: Optional[str] = Field(None, description="模型版本 id（GET /characters/{id}/versions 的 id）：评测这个版本绑定的权重"
                                                     "（同生成的 version；绑定的权重已经没有了时 409 version_unavailable）。"
                                                     "给了它就忽略 engine / gpt / sovits，并且要给 character")
    character: Optional[str] = Field(None, description="用哪个角色的默认参考；不填用 GPT 权重所属角色（底模必须填）；"
                                                       "给 version 时必填，是版本所属的角色")
    ref_id: Optional[str] = Field(None, description="指定参考；不填用角色的默认参考")
    eval_set: EvalSetName = Field("hard", description="评测集，见 GET /eval-sets")
    candidates: Optional[int] = Field(None, ge=1, le=8, description="每句候选数；不填用这个集的默认（mini 3、hard 3）")

    @model_validator(mode="after")
    def _version(self):
        """给了 version：engine、gpt、sovits 换成它拆出来的（忽略请求里的）；没给：三个都要有。"""
        if self.version is not None:
            if self.character is None:
                raise ValueError("给 version 时要给 character（版本所属的角色）")
            self.engine, self.gpt, self.sovits = parse_version(self.version)
        elif None in (self.engine, self.gpt, self.sovits):
            raise ValueError("不给 version 时要给 engine、gpt、sovits")
        return self


class EvalSentence(BaseModel):
    text: str
    tests: list[str] = Field(..., description="这句考什么")


class EvalSet(BaseModel):
    name: EvalSetName
    description: str
    default: bool = Field(..., description="评测和盲听不指定时用它")
    candidates: int = Field(..., description="手动评测的默认每句候选数（训练后自动评测按时间预算另算，见 README）")
    sentences: list[EvalSentence]


class EvalSetList(BaseModel):
    sets: list[EvalSet]


class TrainEval(BaseModel):
    job_id: str
    engine: str
    character: str
    ref_id: str
    eval_set: EvalSetName = Field("mini", description="用的评测集（这一轮之前的结果都是 mini）")
    sentences: list[str]
    candidates_per_sentence: int
    combos: list[ComboEval] = Field(..., description="按 score_median 从高到低")
    best: ComboEval


class TrainResult(BaseModel):
    slug: str
    engine: str
    run_name: str
    lines_used: int
    minutes_used: float
    gpt_weights: list[str]
    sovits_weights: list[str]
    eval: Optional[TrainEval] = None


# ---------------------------------------------------------------- 扫参（Colab）

class SweepTrain(TrainEpochs):
    """扫参的一组训练参数。"""
    engine: Optional[Engine] = Field(None, description="只用于这个引擎（比如 v4 的 batch 要小）；不填用于每个引擎")
    sovits_batch: int = Field(8, ge=1)
    gpt_batch: int = Field(8, ge=1)
    lora_rank: Optional[LoraRank] = Field(None, description="LoRA rank for LoRA engines (16/32/64/128); defaults to 32")


class SweepRequest(ComputeChoice):
    """角色 × 引擎 × 训练参数，在同一台云端 VM 上依次训练、训后评测。每组训完就把它前 keep 个组合的权重带回资料库，
    VM 中途被回收时已带回的组不重跑，剩下的换一台接着跑；资料库里已有同样参数的 run 的组跳过（重复提交同一批）。"""
    characters: list[str] = Field(..., min_length=1, description="资料库里有数据集的角色（内置角色等）")
    engines: list[Engine] = Field(..., min_length=1, description="云端能训的引擎（GET /compute 里云端卡的 engines）")
    trains: list[SweepTrain] = Field(..., min_length=1, description="几组轮数、保存间隔、batch")
    min_sec: float = 1.0
    max_sec: float = 12.0
    exclude_interjections: bool = True
    max_lines: Optional[int] = Field(None, ge=10, description="每组最多用多少句（均匀抽取），不填用全部；扫参 A 用 800")
    ref_ids: dict[str, str] = Field({}, description="角色 → 训后评测用的参考（同一角色各组用同一条，分数才能比）；"
                                                    "不填用角色的默认参考")
    keep: int = Field(3, ge=1, description="每组只带回训后评测前 keep 个组合用到的权重")
    samples: int = Field(2, ge=0, description="每组给训后评测前 samples 个组合各生成几句样本（盲听素材，hard 集里的 5 句，"
                                              "同一参考、同一 seed），放进资料库 runs/<run>/samples/；0 不生成")
    into_builtin: bool = Field(True, description="训出的角色（alice-v2proplus-s16g20 这种）并到内置角色名下，"
                                                 "同 POST /runs/import 的 into_builtin")

    @model_validator(mode="after")
    def _samples_kept(self):
        if self.samples > self.keep:
            raise ValueError("samples 不能大于 keep（样本只给带回的组合生成）")
        return self


class SweepGroup(TrainEpochs):
    slug: str = Field(..., description="这一组训练的角色名：<角色>-<引擎>-s<SoVITS 轮数>g<GPT 轮数>-<其余参数的 6 位哈希>"
                                       "（保存间隔、batch、数据集、过滤条件不同时也不同）")
    character: str = Field(..., description="请求里的角色")
    engine: str
    sovits_batch: int
    gpt_batch: int
    lora_rank: Optional[LoraRank] = None
    status: Literal["pending", "skipped", "succeeded", "failed"] = Field(
        ..., description="pending：还没跑（或 VM 被回收时正在跑）；skipped：资料库里已有同样参数的 run，不再训练")
    run_name: Optional[str] = None
    job_id: Optional[str] = Field(None, description="这一组的训练任务记录（GET /train/{job_id}/eval 看训后评测）")
    best: Optional[ComboEval] = None
    kept: list[str] = Field([], description="带回资料库的权重")
    samples: Optional[str] = Field(None, description="资料库里的样本目录（runs/<run>/samples，items.json 是清单）")
    error: Optional[str] = None


class SweepResult(BaseModel):
    groups: list[SweepGroup]
    vms: int = Field(0, description="用了几台 VM（中途被回收、换一台接着跑时大于 1）")
    confirm_required: bool = Field(False, description="执行时剩下的组要花的超过了确认过的还没花的（前几台实际花得比估的多、"
                                                      "或余额变了要多分一台），没开下一台就停了：已完成的组留在资料库里，"
                                                      "重新提交同一批再确认（已完成的组跳过）")


class SweepPlan(BaseModel):
    plan: Optional["Plan"] = Field(..., description="整批（只算要跑的组）的推荐设备、预计耗时、单元、要不要先问；"
                                                 "组都在资料库里（全部 skipped，提交时不开 VM、不花钱）时 null")
    groups: list[SweepGroup] = Field(..., description="展开的组：pending 是要跑的，skipped 是资料库里已有的")


# ---------------------------------------------------------------- 作品

class WorkLineIn(GenParams):
    id: Optional[str] = Field(None, description="已有行的 id；PATCH 时带上 id 的行会保留候选和选择")
    character: str = "demo"
    text: str
    lang: str = "ja"
    pause_after: float = Field(0.5, ge=0, description="这一行后面停顿几秒")


class WorkLine(WorkLineIn):
    id: str
    candidates: list[AudioItem] = []
    picked: Optional[str] = Field(None, description="选中的候选 audio id")


class RenderTiming(BaseModel):
    line_id: str
    character: str
    text: str
    start: float
    end: float


class RenderResult(BaseModel):
    work_id: str
    rendered_at: str
    duration: float
    wav_url: str
    mp3_url: str
    srt_url: str
    lrc_url: str
    lines: list[RenderTiming]


class Work(BaseModel):
    id: str
    title: str
    created: str
    updated: str
    lines: list[WorkLine]
    render: Optional[RenderResult] = None


class WorkSummary(BaseModel):
    id: str
    title: str
    created: str
    updated: str
    n_lines: int
    n_picked: int
    rendered_at: Optional[str]


class WorkList(BaseModel):
    works: list[WorkSummary]


class WorkCreate(BaseModel):
    title: str
    lines: list[WorkLineIn] = []


class WorkPatch(BaseModel):
    title: Optional[str] = None
    lines: Optional[list[WorkLineIn]] = Field(None, description="整体替换行列表；带 id 的行保留候选和选择，不带 id 的是新行")


class GenerateRequest(ComputeChoice):
    line_ids: Optional[list[str]] = Field(None, description="只生成这几行；不填生成所有还没有候选的行")
    n: int = Field(3, ge=1, le=16, description="每行候选数")


class RegenerateRequest(ComputeChoice):
    n: int = Field(3, ge=1, le=16)


class PickRequest(BaseModel):
    audio_id: str


class AttachRequest(BaseModel):
    audio_id: str = Field(..., description="已有的生成音频（比如候选接口里挑中的那条）")
    pick: bool = Field(True, description="挂上去的同时选中它")


class AddLineRequest(BaseModel):
    line: Optional[WorkLineIn] = Field(None, description="新行的内容；不填时按 audio_id 那条音频的文本、角色、引擎和参数生成")
    audio_id: Optional[str] = Field(None, description="作为这一行的候选并直接选中的已有音频")
    position: Optional[int] = Field(None, ge=0, description="插到第几行（从 0 开始），不填追加到末尾")


class LineGenResult(BaseModel):
    line_id: str
    picked: str
    best_score: float
    candidates: list[str]


class GenerateResult(BaseModel):
    work_id: str
    lines: list[LineGenResult]


# ---------------------------------------------------------------- 盲听 A/B

BlindChoice = Literal["left", "right", "tie"]


class BlindSide(BaseModel):
    engine: Engine = "v2pro"
    gpt: Optional[str] = Field(None, description="GPT 权重文件名；不填用角色在这个引擎下的默认")
    sovits: Optional[str] = Field(None, description="SoVITS 权重文件名；不填用角色在这个引擎下的默认")
    version: Optional[str] = Field(None, description=VERSION_DESC)

    _version = model_validator(mode="after")(apply_version)


class BlindCombo(BaseModel):
    engine: str
    gpt: str
    sovits: str


class BlindCreate(ComputeChoice):
    character: str = Field(..., description="角色 slug；两边都用这个角色的参考音频")
    a: BlindSide = Field(..., description="组合 A")
    b: BlindSide = Field(..., description="组合 B（可以是不同引擎）")
    eval_set: EvalSetName = Field("hard", description="用哪个评测集的句子，见 GET /eval-sets")
    repeats: int = Field(1, ge=1, le=5, description="每句生成几次；每次是单独一道题（A、B 用同一个新 seed）")
    ref_id: Optional[str] = Field(None, description="参考音频；不填用角色的默认参考")
    title: Optional[str] = Field(None, max_length=100,
                                 description="显示名；不填自动生成「角色 · 评测集 · 创建时间」，不含模型信息")


class BlindGenerateRequest(ComputeChoice):
    """POST /blind/{id}/generate（补生成）的请求体，可以不带（自动选设备）。"""


class BlindQuestion(BaseModel):
    """一道题：同一句话的左右两段音频。不含任何模型信息。"""
    id: str = Field(..., description="题目 id，如 q03")
    index: int = Field(..., description="第几题（从 0 开始）")
    text: str
    tests: list[str] = Field(..., description="这句考什么（来自评测集）")
    ready: bool = Field(..., description="两段音频都生成好了才能听和投票")
    left_url: Optional[str] = Field(None, description="左边的音频，相对路径；可加 ?format=mp3")
    right_url: Optional[str] = None
    vote: Optional[BlindChoice] = Field(None, description="left / right / tie（差不多）；没投为 null")
    note: str = ""
    voted_at: Optional[str] = None


BlindStatus = Literal["generating", "ready", "failed"]


class BlindSummary(BaseModel):
    id: str
    title: str
    created: str
    character: str
    eval_set: EvalSetName
    repeats: int
    ref_id: str
    a: Optional[BlindCombo] = Field(..., description="组合 A；揭晓前（revealed=false）为 null，揭晓后是实际用的权重")
    b: Optional[BlindCombo] = Field(..., description="组合 B；揭晓前为 null")
    status: BlindStatus = Field(..., description="generating=生成任务排队或进行中；ready=全部生成完；"
                                                 "failed=生成任务失败、被取消或服务重启中断（已生成的题照样能投，"
                                                 "POST /blind/{id}/generate 补齐剩下的）")
    job_id: str = Field(..., description="最近一次生成任务，进度看 GET /jobs/{job_id}")
    n_questions: int
    n_ready: int
    n_voted: int
    revealed: bool = Field(..., description="全部投完或调用过 reveal 后为 true，之后可以 GET /blind/{id}/result")


class BlindTest(BlindSummary):
    questions: list[BlindQuestion]


class BlindList(BaseModel):
    total: int
    tests: list[BlindSummary]


class BlindVote(BaseModel):
    choice: BlindChoice = Field(..., description="left=左边好；right=右边好；tie=差不多")
    note: str = Field("", max_length=200, description="可选的短备注")


class BlindComboResult(BaseModel):
    label: Literal["A", "B"]
    combo: BlindCombo
    wins: int
    losses: int
    ties: int
    win_rate: Optional[float] = Field(None, description="(胜 + 0.5 × 平) / 已投题数；一题没投为 null")
    auto_score_mean: Optional[float] = Field(None, description="这一边全部音频的自动打分（同 /tts/candidates 的 score）均值，"
                                                               "用来和人耳结果对照")


class BlindRevealQuestion(BaseModel):
    id: str
    index: int
    text: str
    tests: list[str]
    vote: Optional[BlindChoice]
    note: str
    left: Optional[Literal["A", "B"]] = Field(None, description="左边是哪个组合；没生成好为 null")
    winner: Optional[Literal["A", "B", "tie"]] = Field(None, description="按投票换算；没投为 null")
    a_audio_url: Optional[str] = None
    b_audio_url: Optional[str] = None
    a_score: Optional[float] = Field(None, description="自动打分")
    b_score: Optional[float] = None


class BlindResult(BaseModel):
    id: str
    title: str
    character: str
    eval_set: EvalSetName
    n_questions: int
    n_voted: int
    revealed_at: str
    a: BlindComboResult
    b: BlindComboResult
    questions: list[BlindRevealQuestion]


# ---------------------------------------------------------------- 任务

class JobItemDone(BaseModel):
    audio_id: str
    audio_url: str
    score: Optional[float]
    seed: int
    line_id: Optional[str] = Field(None, description="作品生成时是哪一行")
    done: int = Field(..., description="这个任务里已完成的候选数")
    total: int


class JobCompute(BaseModel):
    device: str = Field(..., description="设备 id")
    name: str = Field(..., description="设备显示名，如 studio-host、gpu-worker、云端 L4")
    type: Literal["local", "lan", "cloud"]
    estimated_sec: Optional[int] = Field(None, description="提交时的预计耗时（秒）")
    units: Optional[float] = Field(None, description="云端：预计单元")
    reason_code: str = Field(..., description="为什么选它：见 Plan.reason_code；picked = 提交时手动指定")
    reason: str


class Job(BaseModel):
    id: str
    kind: JobKind
    target: str = Field(..., description="训练时是角色 slug，作品任务是 work id")
    status: JobStatus
    progress: float = Field(..., description="0–1")
    stage: str
    created: str
    started: Optional[str] = None
    finished: Optional[str] = None
    error: Optional[str] = None
    log_tail: list[str] = []
    train_result: Optional[TrainResult] = None
    generate_result: Optional[GenerateResult] = None
    render_result: Optional[RenderResult] = None
    candidates_result: Optional[CandidatesResult] = None
    compare_result: Optional[CompareResult] = None
    eval_result: Optional[TrainEval] = Field(None, description="手动评测任务（POST /weights/eval）的结果，combos 只有一项")
    latest: Optional[JobItemDone] = Field(None, description="候选、对比、作品生成任务：刚完成的那条候选")
    completed: list[JobItemDone] = Field([], description="这个任务里已完成的全部候选，按完成顺序")
    compute: Optional[JobCompute] = Field(None, description="派到了哪台设备（生成、评测、盲听出题、训练）")
    bench_result: Optional[dict] = Field(None, description="测速任务（POST /bench）的结果，格式同 GET /bench 的每一项")
    sweep_result: Optional[SweepResult] = Field(None, description="扫参任务（POST /train/sweep）每一组的状态和结果")


class JobList(BaseModel):
    jobs: list[Job]
    total: Optional[int] = Field(None, description="符合过滤条件的任务总数（分页前）")


# ---------------------------------------------------------------- review 统计

class StatsGroup(BaseModel):
    key: str
    n: int
    avg_rating: Optional[float] = Field(None, description="只算打了 1–5 分的")
    good_ratio: float = Field(..., description="good 或 ≥4 分的占比")
    bad_ratio: float = Field(..., description="bad 或 ≤2 分的占比")
    avg_score: Optional[float] = Field(None, description="自动打分（候选接口的 score）的平均")
    tag_ratios: dict[str, float] = Field(..., description="问题标签 -> 占比")


class StatsDimension(BaseModel):
    dimension: str
    groups: list[StatsGroup]


class ReviewStats(BaseModel):
    overall: StatsGroup
    dimensions: list[StatsDimension]


# ---------------------------------------------------------------- 资料库

class MachineRef(BaseModel):
    machine_id: str
    name: str


class LibraryCounts(BaseModel):
    characters: int
    refs: int = Field(..., description="资料库里的参考（上传的、打过标签的、默认参考）；数据集派生的候选不算")
    datasets: int
    weights: int = Field(..., description="权重文件数（GPT 和 SoVITS 各算一个）")
    evals: int = Field(..., description="评测次数（一次评测任务一个文件）")
    blind: int
    works: int
    runs: int
    bench: int


class LibraryScan(BaseModel):
    files: int = Field(..., description="记录文件数")
    parsed: int = Field(..., description="这次重新读的（新增或改过的）")
    unchanged: int = Field(..., description="没变、直接用本地索引的")
    removed: int = Field(..., description="上次有、这次没了的")
    seconds: float


class LibraryInfo(BaseModel):
    selected: bool = Field(..., description="false：未选资料库，用 POST /library 选；error 里可能有原因（配置的路径打不开）")
    error: Optional[str] = None
    path: Optional[str] = None
    kind: Optional[Literal["drive", "local"]] = Field(None, description="drive：在 Google Drive 电脑版同步的文件夹里")
    id: Optional[str] = Field(None, description="资料库 id（library.json）")
    owner: Optional[MachineRef] = Field(None, description="管理这个资料库的控制端")
    owned: bool = Field(False, description="是不是本机管理；false 时只能读，写入 409（code=library_not_owned），"
                                           "POST /library/claim 改为本机管理")
    this_machine: MachineRef
    counts: Optional[LibraryCounts] = None
    conflicts: int = Field(0, description="Drive 同步冲突副本（「xxx (1).json」）的文件数，这些文件不当记录读")
    conflict_files: list[str] = Field([], description="冲突副本的相对路径（最多 100 个）")
    broken_files: list[str] = Field([], description="读不出 json 的记录文件（相对路径），不当记录读")
    scanned_at: Optional[str] = None
    scan: Optional[LibraryScan] = Field(None, description="上次扫描的统计")


class LibrarySelect(BaseModel):
    path: str = Field(..., description="控制端上的文件夹：已有资料库就打开；空文件夹或不存在就在那里新建（本机管理）")


# ---------------------------------------------------------------- 算力和调度

SpeedSource = Literal["measured", "reference", "untested"]
DeviceType = Literal["local", "lan", "cloud"]
DeviceStatus = Literal["idle", "busy", "training", "offline", "paused", "unavailable", "waking"]


class EngineSpeed(BaseModel):
    sec_per_item: Optional[float] = Field(None, description="每句秒数（hard 难句）；scorer 是每条打分秒数，train 是训练每句秒数")
    source: SpeedSource = Field(..., description="measured 已实测；reference 参考值；untested 未测")


class Hardware(BaseModel):
    cpu: Optional[str] = None
    cores: Optional[int] = None
    ram_gb: Optional[float] = None
    accel: Optional[str] = Field(None, description="cuda / mps / cpu")
    gpu: Optional[str] = None
    os: Optional[str] = None
    torch: Optional[str] = None


class ComputeDevice(BaseModel):
    id: str = Field(..., description="local（本机）、lan-xxxxxx（局域网设备）、colab-<卡型>（云端 GPU）")
    name: str
    type: DeviceType
    status: DeviceStatus = Field(..., description="idle 空闲；busy 忙；training 正在训练；offline 离线（配了唤醒的睡着时也是 "
                                                  "offline，wakeable=true）；paused 已暂停；waking 唤醒中；"
                                                  "unavailable 暂时不能用（云端：没有账号能开、没装组件，原因在 status_reason）")
    status_reason: Optional[str] = None
    activity: Optional[str] = Field(None, description="忙的时候在做什么：正在生成 / 正在评测 / 正在训练")
    address: Optional[str] = Field(None, description="局域网设备的地址（只在添加、编辑时显示）")
    machine: Optional[str] = Field(None, description="机器名（测速结果按它存）")
    hw: Optional[Hardware] = None
    engines: list[str] = Field(..., description="能跑的引擎（本机和局域网设备是装好的引擎）")
    loaded: list[str] = Field([], description="已经加载的引擎和 scorer（加载过的不用再等约 15 秒准备）")
    speeds: dict[str, EngineSpeed] = Field(..., description="引擎 -> 速度；另有 scorer、train")
    gpu: Optional[str] = Field(None, description="云端：卡型 T4 / L4 / A100 / G4 / H100")
    rate: Optional[float] = Field(None, description="云端：每小时单元")
    premium: bool = Field(False, description="云端：高费率卡，开之前总要确认")
    accounts: Optional[int] = Field(None, description="云端：能开这张卡的账号数")
    hours: Optional[float] = Field(None, description="云端：按账号池余额大约还能用几小时")
    wake: Optional["WakeConfig"] = Field(None, description="局域网设备：唤醒（Wake-on-LAN），没配是 null")
    wakeable: bool = Field(False, description="局域网设备：离线但配了唤醒、可以唤醒（派任务时 core 先唤醒它）；"
                                              "唤醒超时后一段时间内为 false")
    last_seen: Optional[str] = Field(None, description="局域网设备最近一次心跳的时间（用安装命令自动连回的设备每 30 秒报一次，"
                                                       "超过 90 秒没来算离线）；手动填地址配对的、本机、云端是 null")
    caps: list[str] = Field(default_factory=list, description="Reported worker capabilities (e.g. aux_refs, fragment_interval)")
    warm: int = Field(0, description="Number of warm idle cloud VMs kept ready for reuse")
    disk_free_bytes: Optional[int] = Field(None, description="Available disk space in bytes on this device")
    disk_total_bytes: Optional[int] = Field(None, description="Total disk capacity in bytes on this device")
    cache_bytes: Optional[int] = Field(None, description="Cached blob directory size in bytes (BLOB_DIR)")


class WakeConfig(BaseModel):
    mac: str = Field(..., pattern=r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$", description="有线网卡的 MAC，如 aa:bb:cc:dd:ee:ff")
    broadcast: Optional[str] = Field(None, description="魔术包发到的广播地址，如 192.168.1.255；null = 255.255.255.255")


class AutoSettings(BaseModel):
    mode: Literal["auto", "fixed"] = Field("auto", description="auto 按推荐；fixed 固定一台（device）")
    device: Optional[str] = None
    ask_over: float = Field(2.0, ge=0, description="一次花费超过这么多单元先问；0 = 每次都问")
    ask_month: float = Field(500.0, ge=0, description="本月合计超过这么多单元先问；0 = 每次都问")


class AutoPatch(BaseModel):
    mode: Optional[Literal["auto", "fixed"]] = None
    device: Optional[str] = Field(None, description="mode=fixed 时固定的设备 id")
    ask_over: Optional[float] = Field(None, ge=0)
    ask_month: Optional[float] = Field(None, ge=0)


class ComputeList(BaseModel):
    devices: list[ComputeDevice]
    auto: AutoSettings
    engines: list[str] = Field(..., description="主控装好的引擎（设备 × 引擎表的行）")
    month_used: float = Field(..., description="账号池本月合计已用单元（本机账本记的）")


class DeviceAdd(BaseModel):
    name: Optional[str] = Field(None, description="别名；不给时用它报的机器名（同一台已经在列表里时保留原来的名字）")
    address: str = Field(..., description="worker 终端里显示的连接地址，如 http://gpu-worker.local:9880")
    pair_code: str = Field(..., description="worker 终端里显示的配对码 XXX-XXX（10 分钟有效、用一次作废；"
                                            "过期在那台机器上运行 python -m ttsapi.pair 重新生成）")
    wake: Optional[WakeConfig] = Field(None, description="唤醒（可选）")


class DevicePatch(BaseModel):
    name: Optional[str] = Field(None, description="改名。本机也可以改（空字符串回到机器名）")
    address: Optional[str] = None
    pair_code: Optional[str] = Field(None, description="重新配对（那台 worker 重装、换了令牌时）：用新配对码换令牌，"
                                                       "同时给 address 时按新地址配对")
    paused: Optional[bool] = Field(None, description="暂停使用（本机、局域网设备、云端卡都可以）")
    wake: Optional[WakeConfig] = Field(None, description="局域网设备的唤醒：给了就设，显式给 null 去掉，不给不动")


Network = Literal["lan", "tailscale"]


class InviteCreate(BaseModel):
    network: Network = Field("lan", description="lan 家里的局域网（命令里是主控的 mDNS 名）；tailscale 不在家里的电脑"
                                                "（MagicDNS 名，没有时 Tailscale IP）")


class InviteCommands(BaseModel):
    unix: str = Field(..., description="macOS / Linux 终端里跑的一行命令：curl -fsSL http://studio-host.local/install | sh -s -- --pair 7Q4-K9M"
                                       "（主控 80 端口绑不上时带 core 端口）")
    windows: Optional[str] = Field(None, description="Windows（还没做，null）")


class ComputeInvite(BaseModel):
    id: str
    code: str = Field(..., description="配对码 XXX-XXX：10 分钟有效、只核销一次（自动连回或手动填）；再生成一个时没核销的旧码马上作废")
    network: Network
    host: str = Field(..., description="命令里主控的名字")
    expires_at: str = Field(..., description="过期时间，带时区（ISO 8601 带 offset）")
    expires_in: int = Field(..., description="还剩几秒（向上取整）")
    commands: InviteCommands


class InviteStatus(BaseModel):
    id: str
    state: Literal["waiting", "connected", "expired"] = Field(..., description="waiting 等那台电脑装好、连回来（连回来后到它的服务"
                                                                               "起来、报第一次心跳前也是 waiting）；connected 连上了；"
                                                                               "expired 过期、被重新生成作废、配对码输错太多次、"
                                                                               "取消了或主控重启过（查不到的都是 expired）")
    expires_at: Optional[str] = Field(None, description="过期时间，带时区；查不到的邀请是 null")
    expires_in: int = Field(..., description="还剩几秒能用，向上取整：没核销、没过期时至少 1（手动配对进行中也是）；"
                                             "只有核销了、过期了是 0")
    redeemed: bool = Field(..., description="配对码已核销：那台电脑已经用它连回来，或者手动填配上了。"
                                            "waiting + redeemed 是在等它装好、服务起来报第一次心跳（「正在安装」），不会过期")
    device: Optional[ComputeDevice] = Field(None, description="connected 时连上的设备（名字是它报的机器名，可以 PATCH 改）")


class InviteManual(BaseModel):
    address: str = Field(..., description="那台电脑终端里显示的连接地址，如 http://gpu-worker.local:9880")


class DevicePair(BaseModel):
    """局域网设备连回主控（安装脚本里的 python -m ttsapi.pair join 发）。"""
    code: str = Field(..., description="配对码（POST /compute/invites 生成的）")
    address: str = Field(..., description="主控连这台用的地址：http://<这台连主控时走的那块网卡的 IPv4>:<端口>"
                                         "（主控是 Tailscale 地址时是这台的 Tailscale IP）")
    machine: str = Field(..., description="机器名（设备的默认名字）")
    machine_id: str = Field(..., description="设备上 core 的本机 id：同一台重装、重新配对时更新原来那一项，不另加")
    hardware: Hardware
    engines: list[str] = Field(..., description="装好的引擎")


class DevicePaired(BaseModel):
    device_id: str
    token: str = Field(..., description="长期令牌：主控调设备时带它，设备报心跳也带它（Authorization: Bearer）")
    heartbeat_sec: int = Field(..., description="多少秒报一次心跳")


class DeviceHeartbeat(BaseModel):
    address: str = Field(..., description="这台设备现在的地址（变了主控跟着改）")


TaskKind = Literal["gen", "eval", "train"]


class PlanPart(BaseModel):
    engine: Engine
    items: int = Field(..., ge=0, description="这个引擎的句数")


class PlanRequest(TrainEpochs):
    """训练（kind=train）时轮数和保存间隔同 POST /train，估时和提交时一样含轮数和训后逐组评测。
    引擎三选一：engine（单个引擎 × items）、engines（每个引擎各 items 句，可重复：盲听两边同引擎时给两次）、
    parts（每个引擎各自的句数）。多个引擎时按各引擎的速度分别算再累加。多引擎只用于生成类（kind=gen：生成、候选、
    对比、盲听出题）；评测、训练每次提交只有一个引擎，只收 engine（一起预览会少算几次开机准备）。"""
    kind: TaskKind = Field(..., description="gen：生成、候选、对比、作品、盲听出题；eval：评测；train：训练")
    engine: Optional[Engine] = None
    engines: Optional[list[Engine]] = Field(None, min_length=1, description="混合引擎（对比、盲听）：每个引擎各 items 句")
    parts: Optional[list[PlanPart]] = Field(None, min_length=1, description="混合引擎、各自句数不同时用")
    items: int = Field(1, ge=0, description="句数（生成：条数；评测：句数 × 每句候选数）")
    lines: int = Field(0, ge=0, description="训练：训练数据句数")
    compute: Optional[str] = Field(None, description="想用的设备；给了时 chosen 是它，confirm 按它算")
    character: Optional[str] = Field(None, description="Optional character slug for cloud upload size estimation")
    version: Optional[str] = Field(None, description="Optional model version id for cloud upload size estimation")
    gpt: Optional[str] = Field(None, description="Optional GPT weight filename for cloud upload size estimation")
    sovits: Optional[str] = Field(None, description="Optional SoVITS weight filename for cloud upload size estimation")
    ref_id: Optional[str] = Field(None, description="Optional primary reference ID for cloud upload size estimation")
    aux_ref_ids: list[str] = Field(default_factory=list, description="Optional auxiliary reference IDs to check LAN worker capability and cloud upload size")
    fragment_interval: Optional[float] = Field(None, ge=0.01, le=2.0, description="Optional fragment interval to check LAN worker capability")
    cloud_capable: bool = Field(True, description="Whether cloud GPUs are allowed for this preview (false for sync/works/blind)")

    @model_validator(mode="after")
    def _one_engine_form(self):
        if sum(x is not None for x in (self.engine, self.engines, self.parts)) != 1:
            raise ValueError("engine、engines、parts 给且只给一个")
        if self.kind != "gen" and self.engine is None:
            raise ValueError("多引擎估时只用于生成类（kind=gen）；评测、训练每次提交一个引擎，只收 engine")
        return self

    def task_parts(self) -> list:
        """[(引擎, 句数)]"""
        if self.parts is not None:
            return [(p.engine, p.items) for p in self.parts]
        return [(e, self.items) for e in (self.engines or [self.engine])]


class PlanCandidate(BaseModel):
    device: str
    name: str
    type: DeviceType
    gpu: Optional[str] = None
    seconds: Optional[int] = Field(None, description="预计耗时（排队 + 准备 + 句数 × 每句秒数）；算不出时 null（本机没测过、参考表也没有时照样可用）")
    units: Optional[float] = Field(None, description="云端：预计单元 max(0.25, 小时 × 每小时单元)")
    speed_source: Optional[SpeedSource] = Field(None, description="估时用的速度来源（几个里最差的；有一项没有速度时 untested）")
    available: bool
    reason_code: Optional[str] = Field(None, description="不能用的原因代码：offline、paused、training、unavailable、"
                                                         "kind_unsupported、cloud_only（扫参只在云端跑）、no_rate（云端卡没有报价）、"
                                                         "insufficient_balance（账号池合计余额不够这次的单元）、"
                                                         "cloud_inputs_missing（训练要上传到云端的输入本机缺）、engine_missing、"
                                                         "not_enough_memory、untested（局域网设备没测过也没有参考值；本机不挡）、"
                                                         "over_max_minutes（云端：预计超过设置里的「最长用多久」）")
    reason: Optional[str] = None
    idle: bool
    premium: bool = False
    wake: bool = Field(False, description="局域网设备睡着、配了唤醒：派任务前 core 先唤醒（任务阶段显示「唤醒 X 中」）；"
                                          "seconds 已含唤醒约 30 秒")
    vms: Optional[int] = Field(None, description="扫参在云端：按账号余额分几台 VM 跑（seconds、units 已含每台的开机准备）")
    prep_sec: Optional[int] = Field(None, description="云端：开始干活前准备环境的秒数（开卡、装环境、上传输入；接手做完先别关的 VM 时"
                                                      "只是清目录、核对环境；扫参是第一台的），已含在 seconds 里。"
                                                      "本机、局域网设备，和估不出时间的卡是 null")


class Plan(BaseModel):
    recommended: Optional[str] = Field(None, description="推荐的设备 id；没有能用的设备时 null")
    reason_code: str = Field(..., description="train_needs_gpu（规则 1）、local_fast_enough（2）、lan_faster（3）、"
                                              "cloud_halves（4）、fastest（5）、local_untested（本机估不出时间，照样用本机）、only_cloud、pinned（已固定）、"
                                              "no_device")
    reason: str
    chosen: Optional[str] = Field(None, description="请求里的 compute，没给时同 recommended")
    candidates: list[PlanCandidate]
    confirm: list[str] = Field(..., description="用 chosen 开始前要不要先问：over_task（一次超过 ask_over）、"
                                                "over_month（本月合计超过 ask_month）、premium_gpu；空 = 不用问")
    month_used: float
    ask_over: float
    ask_month: float


class BenchList(BaseModel):
    results: list[dict] = Field(..., description="资料库 compute/bench/ 里的测速结果，新的在前。每项："
                                                "machine、alias、at、bench_version、quick、hw、engines（引擎 -> device、threads、"
                                                "sec_per_item、rtf、load_sec、peak_gb、rough、tried[]）、scorer、path（资料库里的相对路径）")


class BenchRequest(BaseModel):
    device: str = Field("local", description="在哪台设备上测（local 或局域网设备 id）")
    engines: Optional[list[Engine]] = Field(None, description="只测这些引擎（「测这一个」）；不填测这台设备装好的全部")
    quick: bool = Field(False, description="快速测速：常用模型，约 2 分钟")


# ---------------------------------------------------------------- 云端 GPU（账号池）

GpuPref = Literal["auto", "T4", "L4", "A100", "G4"]


class CloudAccount(BaseModel):
    id: str = Field(..., description="账号 id（邮箱的 SHA-256；本机 colab CLI 原有的登录是 default）")
    label: str = Field(..., description="显示名，默认是邮箱")
    email: Optional[str] = Field(None, description="登录时核对出的邮箱；default 账号没有")
    balance: Optional[float] = Field(None, description="剩余单元；查不到时 null（原因在 error）")
    month_used_estimate: float = Field(..., description="本月已用（估计）：本机账本按自然月累计的，Colab 不提供本月累计")
    gpus: list[str] = Field(..., description="能开的卡（T4 / L4 / A100 / G4 / H100；开失败的卡暂缓 10 分钟时不在里面）")
    disabled: bool = Field(..., description="已停用：不参与调度，授权和账本保留；POST /cloud/accounts/{id}/enable 或重新登录恢复")
    error: Optional[str] = None


class CloudGpu(BaseModel):
    gpu: str = Field(..., description="T4 / L4 / A100 / G4 / H100（设备 id 是 colab-<gpu>）")
    label: str = Field(..., description="显示名：G4 是 RTX PRO 6000")
    account_count: int = Field(..., description="能开这张卡的账号数")
    units_per_hour: Optional[float] = Field(None, description="每小时单元；H100 没有报价是 null")
    hours_left: Optional[float] = Field(None, description="按这些账号的余额合计大约还能用几小时")
    available: bool
    reason: Optional[str] = Field(None, description="不能用的原因：没有可用账号、费率未测、没有装云端 GPU 组件、已暂停")
    premium: bool = Field(False, description="高费率卡，开之前总要确认")
    speeds: dict[str, EngineSpeed] = Field(..., description="引擎 -> 速度（同 GET /compute 里这张卡的 speeds）")


class CloudUsageMonth(BaseModel):
    month: str = Field(..., description="2026-11")
    by_account: dict[str, float] = Field(..., description="账号 id -> 这个月的单元（本机账本，估计值）")


class CloudSettings(BaseModel):
    gpu_pref: GpuPref = Field("auto", description="开哪张卡：auto 按推荐（训练、云端更快时用 L4）；给了卡型就用它代替 L4")
    max_minutes: Optional[int] = Field(None, ge=30, le=1440, description="最长用多久（分钟，一台 VM 的时限）；"
                                                                        "null = 自动（预计时间的 2 倍，30 分钟到 24 小时）。"
                                                                        "预计超过它的云端卡不可用（over_max_minutes）")
    keep_minutes: int = Field(0, ge=0, le=120, description="做完先别关：VM 留这么多分钟，同型号的下一个任务直接接手"
                                                           "（不用重新开卡、装环境），没人接手就关；0 = 做完马上关")


class CloudSettingsPatch(BaseModel):
    gpu_pref: Optional[GpuPref] = None
    max_minutes: Optional[int] = Field(None, ge=30, le=1440, description="显式给 null = 自动")
    keep_minutes: Optional[int] = Field(None, ge=0, le=120)


class CloudInfo(BaseModel):
    installed: bool = Field(..., description="本机装了云端 GPU 组件（colab CLI）；没装时 accounts 为空，"
                                             "第一次 POST /cloud/accounts/login 会先装")
    accounts: list[CloudAccount]
    gpus: list[CloudGpu]
    total_balance: float = Field(..., description="启用的账号的剩余合计（查不到的不算）")
    month_used_estimate: float = Field(..., description="本月已用合计（估计，含停用、移除了的账号），提醒线按它算")
    usage_monthly: list[CloudUsageMonth] = Field(..., description="每月用量图：按月、按账号（本机账本，估计值）")
    account_labels: dict[str, str] = Field(..., description="账本里所有账号（含移除了的）的显示名：账号 id -> 显示名，"
                                                            "每月用量图的图例用")
    settings: CloudSettings


class CloudLoginRequest(BaseModel):
    label: Optional[str] = Field(None, description="显示名；不给时用邮箱（重新登录已有账号时沿用原来的）")


class CloudLoginStart(BaseModel):
    login_id: str
    auth_url: str = Field(..., description="在浏览器里打开，同意后 Google 的页面会显示授权码")
    needs_code: bool = Field(..., description="true：把页面上的授权码 POST /cloud/accounts/login/{login_id} 交回来"
                                              "（colab CLI 0.8.0 只有这一种流程）")


class CloudLoginCode(BaseModel):
    code: str = Field(..., min_length=1)


class CloudLoginStatus(BaseModel):
    state: Literal["waiting", "done", "failed"] = Field(..., description="waiting：等授权码或正在核对；"
                                                                          "done：账号已加进池；failed：见 error")
    account: Optional[CloudAccount] = None
    added: bool = Field(False, description="这次登录是否把这个账号新加进池子（重新登录已有账号或未完成时为 false）")
    error: Optional[str] = None


# ---------------------------------------------------------------- 版本

class StudioVersion(BaseModel):
    current: str = Field(..., description="core 所在的 tts-studio 仓库现在的版本（同 /health 的 version）；"
                                          "Studio 自己的版本以 app 包里的为准")
    latest: Optional[str] = Field(None, description="仓库 origin 上最新的 v*.*.* tag（不带 v）；查不到时 null")
    notes_url: Optional[str] = Field(None, description="这个版本的 GitHub Release 页面")
    error: Optional[str] = Field(None, description="查最新版本失败的原因")


class CoreVersion(BaseModel):
    current: str = Field(..., description="同 /health 的 version")


class SystemVersions(BaseModel):
    studio: StudioVersion
    core: CoreVersion
    checked_at: str = Field(..., description="查 origin 的时间（结果缓存 10 分钟，?refresh=true 重新查）")


class SystemInfo(BaseModel):
    web_url: str = Field(..., description="本机在局域网里可访问的 Web 地址：http://<主机名>.local:<端口>/ui/（Studio 的「复制链接」用）")
    host: str = Field(..., description="本机的 mDNS 名（<主机名>.local）")
    port: int = Field(..., description="core 实际监听的端口")
    lan_reachable: bool = Field(..., description="core 监听在局域网地址上（TTSAPI_HOST 不是 127.0.0.1 / localhost）；"
                                                 "false 时别的设备打不开 web_url")
    web_available: bool = Field(..., description="core 实际挂了 /ui/（Web 界面）；false 时 web_url 打开是 404，"
                                                 "Studio 隐藏「复制链接」")
    machine: str = Field(..., description="机器名")


# ---------------------------------------------------------------- Storage overview

class DiskUsage(BaseModel):
    path: str = Field(..., description="Filesystem path queried for disk usage")
    total_bytes: int
    used_bytes: int
    free_bytes: int


class StorageCategory(BaseModel):
    key: str
    label: str
    bytes: int = Field(..., description="Logical size in bytes (sum of st_size)")
    local_bytes: int = Field(..., description="On-disk allocated size in bytes (POSIX st_blocks * 512; st_size on Windows)")
    files: int = Field(..., description="Number of regular files counted")


class LibraryStorage(BaseModel):
    selected: bool
    kind: Optional[Literal["drive", "local"]] = None
    path: Optional[str] = None
    total_bytes: int = 0
    local_bytes: int = 0
    categories: list[StorageCategory] = Field(default_factory=list)


class LocalStorage(BaseModel):
    machine: str
    total_bytes: int = 0
    categories: list[StorageCategory] = Field(default_factory=list)


class StorageOverview(BaseModel):
    measured_at: str = Field(..., description="ISO-8601 timestamp when storage usage was measured")
    disk: DiskUsage
    library: LibraryStorage
    local: LocalStorage


ErrorResponse.model_rebuild()
SweepPlan.model_rebuild()
ComputeDevice.model_rebuild()
InviteStatus.model_rebuild()
