import json
import os
import socket
import sys
from pathlib import Path

# 路径可用环境变量覆盖，见 Core README。
IS_WINDOWS = os.name == "nt"


def _env_path(name: str, default) -> Path:
    v = os.environ.get(name)
    return Path(v) if v else Path(default)


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else default


ROOT = Path(__file__).resolve().parents[1]
# 本机的运行数据（生成历史、评分、任务的数据库，生成的音频，任务日志，资料库的索引缓存……）。
# 角色、参考、数据集、权重、评测、盲听、作品都在资料库里（ttsapi/library）
DATA = _env_path("TTSAPI_DATA_DIR", ROOT / "data")
AUDIO_DIR = DATA / "audio"
JOBS_DIR = DATA / "jobs"
DB_PATH = DATA / "tts.db"
# 从数据集 .list 建的参考候选（3–10 秒的句子）的缓存，按角色和 .list 的修改时间失效
REFS_INDEX = DATA / "refs_index.json"
DATASET_CACHE = DATA / "dataset_durations.json"
# 资料库：POST /library 选的路径存在 CORE_CONFIG 里；没选过时用 TTSAPI_LIBRARY
LIBRARY = os.environ.get("TTSAPI_LIBRARY")
CORE_CONFIG = DATA / "core.json"
# 资料库扫描结果的缓存（每个库一个 sqlite，删了能重建）
LIBRARY_INDEX_DIR = DATA / "library-index"
# 本机身份：id 存在 MACHINE_FILE（第一次用时生成），名字给资料库的「由 X 管理」用
MACHINE_FILE = DATA / "machine.json"
MACHINE_NAME = os.environ.get("TTSAPI_MACHINE_NAME") or socket.gethostname().split(".")[0]

# 角色：control（主控：资料库、调度、全部接口）或 worker（只执行主控派来的生成、评测、测速，不碰资料库，只接受带令牌的请求）。
# python -m ttsapi.gateway --role worker，或 TTSAPI_ROLE=worker
ROLE = sys.argv[sys.argv.index("--role") + 1] if "--role" in sys.argv else os.environ.get("TTSAPI_ROLE", "control")
# worker 的令牌：TTSAPI_WORKER_TOKEN；不设时用配对码配对时生成，存在 DEVICE_FILE（0600；安装脚本装的每个版本各一份，
# 重装时新版本在旁边配对，不动正在跑的那份）。等配对的配对码存在它旁边的 pair.json
WORKER_TOKEN = os.environ.get("TTSAPI_WORKER_TOKEN")
DEVICE_FILE = _env_path("TTSAPI_DEVICE_FILE", DATA / "device.json")
# worker 上按 sha256 缓存的权重、参考音频；总量上限（GB），超了按最近使用淘汰
BLOB_DIR = DATA / "blobs"
BLOB_MAX_GB = _env_int("TTSAPI_BLOB_MAX_GB", 10)
# 主控的算力注册（局域网设备、自动选择的设置）和「设备 × 引擎」的实测速度
COMPUTE_FILE = DATA / "compute.json"
SPEEDS_FILE = DATA / "speeds.json"
# 测速选出来的最佳设备和线程数（{"v4": {"device": "mps", "threads": 6}, "scorer": {...}}），worker 启动时读，优先于下面的环境变量
TUNING_FILE = DATA / "tuning.json"
# Colab 账号池的目录（usage.sqlite、每账号的 CLI 状态、colab-venv）
COLAB_ROOT = _env_path("TTSAPI_COLAB_ROOT", Path.home() / ".config" / "tts-core")
LOG_DIR = ROOT / "logs"
RUNTIME = ROOT / "runtime"
# 训练的中间产物和导出的权重都放在 tts-api 下，不写进 GPT-SoVITS 的 logs 和 *_weights* 目录。
# WEIGHTS_DIR 只是训练导出的暂存处：训练结束后权重搬进资料库
TRAIN_DIR = _env_path("TTSAPI_TRAIN_DIR", ROOT / "training")
WEIGHTS_DIR = _env_path("TTSAPI_WEIGHTS_DIR", ROOT / "weights")

OLD_REPO = _env_path("TTSAPI_GSV_ROOT", Path.home() / "GPT-SoVITS")
V5_REPO = _env_path("TTSAPI_GSV_V5_ROOT", Path.home() / "GPT-SoVITS-v5")
_VENV_PY = Path(".venv", "Scripts", "python.exe") if IS_WINDOWS else Path(".venv", "bin", "python")
OLD_PYTHON = _env_path("TTSAPI_PYTHON", OLD_REPO / _VENV_PY)
# 上游 V5 多依赖 resampy；这个 venv 通过 .pth 只读复用老 venv 的包，自己只多装了 resampy
V5_PYTHON = _env_path("TTSAPI_V5_PYTHON", V5_REPO / _VENV_PY)
# 保留已导入的 source=july 权重的训练日期，避免用复制后的文件时间排序。
JULY_TRAINED = "2026-07-01T00:00:00+09:00"


def _ffmpeg_bin():
    """ffmpeg 共享库目录（Windows 上 GPT-SoVITS 老目录自带的 ffmpeg-shared，要加进 DLL 搜索路径）。
    TTSAPI_FFMPEG_BIN 可指定；找不到时为 None（Linux 用系统的 ffmpeg / libav，不需要）。"""
    if v := os.environ.get("TTSAPI_FFMPEG_BIN"):
        return Path(v)
    dll = next((OLD_REPO / "runtime" / "ffmpeg-shared").rglob("avcodec-62.dll"), None) if IS_WINDOWS else None
    return dll.parent if dll else None


FFMPEG_BIN = _ffmpeg_bin()
ASR_MODEL = _env_path("TTSAPI_ASR_MODEL", OLD_REPO / "tools" / "asr" / "models" / "faster-whisper-large-v3-turbo")
DATASET_ROOT = _env_path("TTSAPI_DATASET_ROOT", OLD_REPO / "datasets")

# 推理 / 打分 worker 的本机端口整体平移（同一台机器上另起一份 core 时用，比如 worker 角色的实例设 100）
WORKER_PORT_SHIFT = _env_int("TTSAPI_WORKER_PORT_SHIFT", 0)
# 监听地址默认是所有网卡；用 TTSAPI_HOST 绑定到指定地址。
HOST = os.environ.get("TTSAPI_HOST", "0.0.0.0")
PORT = _env_int("TTSAPI_PORT", 9880)
ALLOWED_NETS = ["127.0.0.0/8", "::1/128", "100.64.0.0/10", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"]
# 主控另外在这个端口只提供 GET /install（局域网设备的一行安装命令不带端口）；绑不上（被占、没权限）时命令里带 core 端口。
# 0 = 不开。macOS 上普通用户能绑 0.0.0.0 的 80；Linux 上可按需配置端口
INSTALL_PORT = _env_int("TTSAPI_INSTALL_PORT", 80)
# Web 界面（web/ 构建出来的 dist），主控在 /ui/ 提供；目录不在就不挂（GET /system/info 的 web_available=false）
WEB_DIR = _env_path("TTSAPI_WEB_DIR", ROOT.parent / "web" / "dist")


def tuning(worker: str) -> dict:
    """测速写回的这个 worker 的最佳设置 {"device", "threads"}；没测过是 {}。"""
    if not TUNING_FILE.exists():
        return {}
    return json.loads(TUNING_FILE.read_text(encoding="utf-8")).get(worker, {})


def device_setting(worker: str) -> str:
    """worker（引擎名或 scorer）用什么设备：测速的临时 worker 用 TTSAPI_BENCH_DEVICE（只由 ttsapi.bench 给它设）；
    其余按测速写回的（TUNING_FILE）优先，其次 TTSAPI_DEVICE_<WORKER>（比如 TTSAPI_DEVICE_V4=cpu）、TTSAPI_DEVICE，
    都没有是 auto（有 CUDA 用 cuda，Apple Silicon 用 mps，否则 cpu）。
    auto 在 worker 里解析（ttsapi/device.py），gateway 不 import torch。"""
    return (os.environ.get("TTSAPI_BENCH_DEVICE") or tuning(worker).get("device")
            or os.environ.get(f"TTSAPI_DEVICE_{worker.upper()}") or os.environ.get("TTSAPI_DEVICE") or "auto").lower()


# CPU 上 torch 的线程数，0 = torch 默认。Apple M6 上 torch 默认只开 2 个线程（按最快那一档核心数）
TORCH_THREADS = _env_int("TTSAPI_TORCH_THREADS", 0)


def torch_threads(worker: str) -> int:
    """这个 worker 的 CPU 线程数：测速写回的优先，其次 TTSAPI_TORCH_THREADS。
    测速的临时 worker（设了 TTSAPI_BENCH_DEVICE）不看 tuning.json：用 TTSAPI_BENCH_THREADS，没给（GPU 配置）用 TTSAPI_TORCH_THREADS，
    和写回「GPU、不设线程」后正常 worker 的线程数一样。"""
    if os.environ.get("TTSAPI_BENCH_DEVICE"):
        return _env_int("TTSAPI_BENCH_THREADS", TORCH_THREADS)
    return tuning(worker).get("threads") or TORCH_THREADS
# MPS 上用不用半精度（CUDA 上一直用）：TTSAPI_MPS_HALF=1 开。M6 上 v4 每句快约 20%，说话人相似度略低
MPS_HALF = os.environ.get("TTSAPI_MPS_HALF") == "1"
# 打分用的 faster-whisper（ctranslate2）只能跑 CUDA 或 CPU：CPU 上的计算精度和线程数（0 = ctranslate2 默认 4 线程）。
# Apple Silicon 上 float32 走 Accelerate，比 int8 快（每条约 2.7 对 4.9 秒），但进程多占约 2.2GB
ASR_CPU_COMPUTE_TYPE = os.environ.get("TTSAPI_ASR_COMPUTE_TYPE", "int8")
# 测速的临时打分器按 TTSAPI_BENCH_THREADS 试各档线程，优先于测速写回的
ASR_CPU_THREADS = _env_int("TTSAPI_BENCH_THREADS", 0) or tuning("scorer").get("threads") or _env_int("TTSAPI_ASR_THREADS", 0)

REF_MIN_SEC, REF_MAX_SEC = 3.0, 10.0

# 引擎 -> worker 和训练配置。两套代码：固定上游提交加补丁后运行 v1–v4、v2Pro、v2ProPlus，独立的上游目录运行 V5。
# 每个引擎一个 worker 进程（同一个 engine_worker 模块）：v3/v4/V5 各自带声码器，混在一个进程里换权重时声码器不会跟着换。
# pretrained_gpt / pretrained_sovits：底模，所有角色都能用它零样本推理。
# 训练：s2_script 是 SoVITS 训练脚本（v1/v2/v2Pro 系全量微调，v3/v4/V5 训 LoRA），s1_config 是 GPT 训练配置，
# sv 表示预处理要提取说话人向量（v2Pro 系）。GPT 一律用老目录的 s1_train（见 training.py）。
_OLD_PRE = OLD_REPO / "GPT_SoVITS" / "pretrained_models"
_V5_PRE = V5_REPO / "GPT_SoVITS" / "pretrained_models"
_OLD = dict(repo=OLD_REPO, python=OLD_PYTHON, code="fork", s1_config="GPT_SoVITS/configs/s1longer-v2.yaml", sv=False,
            s2_config="GPT_SoVITS/configs/s2.json", s2_script="GPT_SoVITS/s2_train.py", pretrained_s2D=None,
            supports_aux_refs=True)
_V5 = dict(_OLD, repo=V5_REPO, python=V5_PYTHON, code="v5", s2_script="GPT_SoVITS/s2_train_v3_lora.py",
           pretrained_gpt=_V5_PRE / "s1v3.ckpt", supports_aux_refs=False)
ENGINES = {
    "v1": dict(
        _OLD, port=9894, version="v1", s1_config="GPT_SoVITS/configs/s1longer.yaml",
        pretrained_gpt=_OLD_PRE / "s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt",
        pretrained_sovits=_OLD_PRE / "s2G488k.pth", pretrained_s2D=_OLD_PRE / "s2D488k.pth",
    ),
    "v2": dict(
        _OLD, port=9895, version="v2",
        pretrained_gpt=_OLD_PRE / "gsv-v2final-pretrained" / "s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt",
        pretrained_sovits=_OLD_PRE / "gsv-v2final-pretrained" / "s2G2333k.pth",
        pretrained_s2D=_OLD_PRE / "gsv-v2final-pretrained" / "s2D2333k.pth",
    ),
    "v2pro": dict(
        _OLD, port=9891, version="v2Pro", sv=True, s2_config="GPT_SoVITS/configs/s2v2Pro.json",
        pretrained_gpt=_OLD_PRE / "s1v3.ckpt",
        pretrained_sovits=_OLD_PRE / "v2Pro" / "s2Gv2Pro.pth", pretrained_s2D=_OLD_PRE / "v2Pro" / "s2Dv2Pro.pth",
    ),
    "v2proplus": dict(
        _OLD, port=9896, version="v2ProPlus", sv=True, s2_config="GPT_SoVITS/configs/s2v2ProPlus.json",
        pretrained_gpt=_OLD_PRE / "s1v3.ckpt",
        pretrained_sovits=_OLD_PRE / "v2Pro" / "s2Gv2ProPlus.pth",
        pretrained_s2D=_OLD_PRE / "v2Pro" / "s2Dv2ProPlus.pth",
    ),
    "v3": dict(
        _OLD, port=9897, version="v3", s2_script="GPT_SoVITS/s2_train_v3_lora.py", supports_aux_refs=False,
        pretrained_gpt=_OLD_PRE / "s1v3.ckpt", pretrained_sovits=_OLD_PRE / "s2Gv3.pth",
    ),
    "v4": dict(
        _OLD, port=9898, version="v4", s2_script="GPT_SoVITS/s2_train_v3_lora.py", supports_aux_refs=False,
        pretrained_gpt=_OLD_PRE / "s1v3.ckpt", pretrained_sovits=_OLD_PRE / "gsv-v4-pretrained" / "s2Gv4.pth",
    ),
    "v5turbo": dict(_V5, port=9892, version="v5turbo", pretrained_sovits=_V5_PRE / "gsv-v5-pretrained" / "s2Gv5turbo.pth"),
    "v5dev": dict(_V5, port=9893, version="v5dev", pretrained_sovits=_V5_PRE / "gsv-v5-pretrained" / "s2Gv5dev.pth"),
}
for _cfg in ENGINES.values():
    _cfg["port"] += WORKER_PORT_SHIFT

# Per-engine training defaults exposed in GET /models and used by clients.
LORA_RANKS = (16, 32, 64, 128)
_TRAIN_FULL_DEFAULTS = dict(
    sovits_epochs=8, sovits_save_every=4, sovits_batch=8,
    gpt_epochs=15, gpt_save_every=5, gpt_batch=8,
    lora=False, lora_rank=None, lora_ranks=[],
)
_TRAIN_LORA_DEFAULTS = dict(
    sovits_epochs=2, sovits_save_every=1, sovits_batch=4,
    gpt_epochs=15, gpt_save_every=5, gpt_batch=8,
    lora=True, lora_rank=32, lora_ranks=list(LORA_RANKS),
)
TRAIN_DEFAULTS = {
    e: dict(_TRAIN_LORA_DEFAULTS if "lora" in cfg["s2_script"] else _TRAIN_FULL_DEFAULTS)
    for e, cfg in ENGINES.items()
}


def installed_engines() -> list:
    """本机装好的引擎（底模和 Python 都在）。引擎列表都从这里来，不写死。"""
    return [e for e, c in ENGINES.items()
            if c["pretrained_gpt"].exists() and c["pretrained_sovits"].exists() and c["python"].exists()]


# 云端 VM 能跑的引擎：install_linux.sh 按底模清单装引擎，清单里有的（base 是公共部分）才能在 Colab 上跑。
# 算力列表里云端卡的 engines、调度的「能不能跑」、派发前的检查都用它
CLOUD_ENGINES = sorted({line.split()[0] for line in (ROOT / "scripts" / "pretrained.sha256").read_text(
    encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")} - {"base"})


# 保留 fork 和 v5 作为 API 兼容标识。
CODE_DESC = {"fork": f"{OLD_REPO}（GPT-SoVITS 固定上游提交加补丁）", "v5": f"{V5_REPO}（上游 V5）"}
SCORER = dict(repo=OLD_REPO, python=OLD_PYTHON, port=9890 + WORKER_PORT_SHIFT)

# worker 空闲多久后退出、释放显存（分钟）；0 表示常驻
IDLE_UNLOAD_MIN = _env_int("TTSAPI_IDLE_UNLOAD_MIN", 20)
# 启动新 worker 前要求的空闲显存（MB），不够就先关掉最久没用的空闲 worker。实测每个 worker 约占 2.1–2.3GB。
# 没有 NVIDIA 显卡时（macOS 的统一内存、纯 CPU）比的是系统可用内存，阈值可用 TTSAPI_WORKER_MEM_MB 调整
WORKER_VRAM_MB = _env_int("TTSAPI_WORKER_MEM_MB", 2600)
# 每个 engine worker 里最多缓存几套 GPT / SoVITS 权重
MODEL_CACHE_SIZE = 6
# 生成音频的清理规则。作品用到的、打过 review 的音频不清理
AUDIO_KEEP_DAYS = 7
AUDIO_KEEP_MAX = 3000

# 评测集：评测（训练后自动评测、POST /weights/eval）和盲听都按名字选。分数只在同一个集里比较。
# 保留 mini 的历史评测集标识；默认用 hard。
# tests 是这句考什么，GET /eval-sets 原样返回。
EVAL_SETS = {
    "mini": dict(description="3 句日常短句，用于对照历史评测", sentences=[
        dict(text="今日はいい天気だね。少し散歩でもしようか。", tests=["日常短句"]),
        dict(text="ごめん、ちょっと待って。すぐに行くから。", tests=["日常短句"]),
        dict(text="本当にそれでいいの？もう一度だけ考えてみて。", tests=["日常短句", "疑问"]),
    ]),
    "hard": dict(description="12 句难句：长句、难读汉字、数字单位、英文字母、强情绪、反问、口语缩约、拟声拟态", sentences=[
        dict(text="あの日、君が雨の中で傘も差さずに待っていてくれたことを、私は今でも、ふとした瞬間に思い出してしまうんだ。",
             tests=["长句", "多个从句", "句中停顿和换气"]),
        dict(text="もし明日までに資料が届かなかったら、会議は延期して、来週の水曜日、全員がそろってから改めて話し合いましょう。",
             tests=["长句", "条件从句", "并列和列举"]),
        dict(text="昨日の夕方、境内で大人しく待っていた子猫が、七夕の短冊を咥えて走っていったの。",
             tests=["难读汉字（境内 けいだい、大人しく おとなしく、七夕 たなばた、咥えて くわえて）"]),
        dict(text="こんな人気のない場所で一日中待ってたなんて、風邪を引いても知らないからね。",
             tests=["多音读法（人気 ひとけ、一日中 いちにちじゅう）", "口语缩约（待ってた）"]),
        dict(text="集合は三月二十一日の午前九時四十五分、場所は駅から1.5キロ先の第3倉庫よ。",
             tests=["日期", "时刻", "小数和单位", "阿拉伯数字和汉字数字混排"]),
        dict(text="このPCのWi-Fi、さっきアップデートしたらSSIDが消えちゃったんだけど、どうすればいい？",
             tests=["英文字母（PC、Wi-Fi、SSID）", "外来语", "口语缩约（〜ちゃった）", "疑问"]),
        dict(text="ふざけないで！　何度言えばわかるの！？　あんたのせいで、全部台無しになったじゃない！",
             tests=["强烈愤怒", "感叹", "连续感叹号"]),
        dict(text="ごめんね……本当に、ごめんね……私、もう、どうしたらいいのか、わからないよ……",
             tests=["哭腔", "断续停顿（……）"]),
        dict(text="しーっ……静かに。誰かに聞かれたら、二人とも終わりなんだから……ね？",
             tests=["耳语式", "拟声（しーっ）", "轻声确认的上扬"]),
        dict(text="まさか、本気でそんなことができるとでも思ってるの？　できるわけないでしょう？",
             tests=["反问", "疑问语调"]),
        dict(text="やべっ、宿題忘れちゃった。まあいいじゃん、どうせ先生も見てないし、なんとかなるっしょ。",
             tests=["口语缩约（〜ちゃう、〜じゃん、〜っしょ）", "促音"]),
        dict(text="雨がザーザー降る中、胸はドキドキ、足はガクガク、頭の中はぐるぐる回ってたんだから。",
             tests=["拟声拟态词", "长音", "叠词节奏"]),
    ]),
}
EVAL_DEFAULT_SET = "hard"
# 手动评测（POST /weights/eval 不指定 candidates 时）的每句候选数
EVAL_CANDIDATES = {"mini": 3, "hard": 3}
# 训练后自动评测要评每个 GPT × SoVITS 组合（默认 3 × 2 = 6 组），每句候选数按时间预算算：
# 预算 ÷（组合数 × 句数 × 这个引擎每条候选的耗时），取 1–3。每条耗时（秒，hard 句子、合成和打分重叠后）：
# v2pro / v2proplus 实测约 3.4；其他引擎按历史 /tts 每字耗时相对 v2pro 的倍数估的
TRAIN_EVAL_BUDGET_SEC = 540
EVAL_SEC_PER_ITEM = {"v1": 3.5, "v2": 3.5, "v2pro": 3.5, "v2proplus": 3.5, "v3": 22, "v4": 6, "v5turbo": 10, "v5dev": 40}
# 作品渲染：统一采样率和每行的响度目标（LUFS），整轨峰值不超过 -1 dBFS
RENDER_SR = 48000
RENDER_LUFS = -18.0
