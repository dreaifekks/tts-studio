# TTS Studio Core

TTS Studio Core 是基于 GPT-SoVITS 的语音合成、训练和评测服务。Mac 客户端、Web 界面和脚本通过同一套 HTTP API 操作。主控启动后可在 [本机 API 文档](http://127.0.0.1:9880/docs) 浏览和试用接口，仓库也提供 [OpenAPI 快照](../api/openapi.json)。

## 架构

| 角色 | 职责 |
| --- | --- |
| **主控（control）** | 管理资料库、合成历史和任务队列，通过本机、局域网设备或 Colab 调度计算。默认运行模式。 |
| **局域网设备（worker）** | 按主控请求执行合成、评测和测速。配对后以令牌认证，输入文件按 SHA-256 缓存，产物交还主控。 |
| **Colab VM** | 运行临时 Core 实例，完成训练、评测、候选生成、对比或扫参。主控负责账号、输入、结果写回和 VM 回收。 |

网关使用 FastAPI。每个引擎独立启动一个推理进程，评分器也是独立进程，按需加载并在空闲时释放。训练任务用单独的进程运行。

## 快速安装

macOS 使用 Apple Silicon 和 Xcode 命令行工具。安装 `git`、`curl`、`uv` 和 FFmpeg，并把它们加入 `PATH`。macOS 可用 `xcode-select --install` 和 `brew install uv ffmpeg` 准备工具。Linux 有 NVIDIA CUDA 环境时默认装 CUDA 12.8 的 PyTorch 锁，无 NVIDIA 时默认装 CPU 锁，也可显式设 `TORCH=cpu`。

从 [TTS Studio 仓库](https://github.com/dreaifekks/tts-studio) 的 `core/` 目录执行对应命令。首次安装建议为 GPT-SoVITS 选择一个新目录。

```sh
# macOS
GSV_ROOT="$HOME/GPT-SoVITS" ENGINES=v2pro,v2proplus bash scripts/install_macos.sh

# Linux
GSV_ROOT="$HOME/GPT-SoVITS" ENGINES=v2pro,v2proplus bash scripts/install_linux.sh
```

脚本浅取 [GPT-SoVITS 上游固定提交](https://github.com/RVC-Boss/GPT-SoVITS/commit/bf81cdb14a38b674b6e9996dabc97340bc9978d2)，应用 [Core 补丁](patches/gptsovits-bf81cdb.patch)，把结果提交在本地 `tts-studio` 分支。已有 Git 工作树会沿用，使用已有引擎目录时请确认该目录包含补丁。脚本通过 uv 建立 Python 3.10 环境，安装锁定依赖、NLTK 数据，并按 [底模清单](scripts/pretrained.sha256) 校验下载文件。设置 `PRETRAINED=/path/to/cache` 可使用同样目录布局的模型缓存。需要 V5 时把 `v5turbo` 或 `v5dev` 加入 `ENGINES`，脚本会准备独立的上游 V5 目录。

前台启动主控。首次运行绑定本机地址，并选择空目录创建资料库，或使用已有资料库路径。

```sh
TTSAPI_HOST=127.0.0.1 TTSAPI_INSTALL_PORT=0 \
  TTSAPI_GSV_ROOT="$HOME/GPT-SoVITS" \
  "$HOME/GPT-SoVITS/.venv/bin/python" -m ttsapi.gateway
```

在另一个终端检查服务并选择资料库。把下面的路径换成实际的绝对路径。

```sh
curl -fsS http://127.0.0.1:9880/health
curl -fsS -X POST http://127.0.0.1:9880/library \
  -H 'Content-Type: application/json' \
  -d '{"path":"/absolute/path/to/tts-library"}'
```

macOS 也可复制 [环境变量模板](scripts/macos/tts-api.env.example) 到 `~/tts-api-data/tts-api.env`，再用 `bash scripts/macos/tts-api.sh start` 启动，`stop` 停止。Mac 应用随包安装主控时使用 `scripts/install_control_macos.sh` 和应用内的 Core bundle。其 `bundle.env` 记录 `VERSION`、`CORE_COMMIT`、`GSV_BASE`、`GSV_BRANCH`、`GSV_PATCH_REL`、`GSV_PATCH_SHA256` 和 `HAS_WEB_DIST`，补丁随 Core 源码解包。

## 资料库

资料库可以是本地目录，也可以放在由桌面客户端同步的目录。多个主控可打开同一个库，当前 owner 负责写入。迁移后可从 Studio 或 `POST /library/claim` 切换 owner。更改资料库路径、同步文件后，可分别用 `POST /library`、`POST /library/rescan` 让主控重新读取。

```text
library.json                               格式版本、资料库 ID、owner
characters/<slug>.json                     角色、默认参考和各引擎的默认权重
refs/<slug>/<ref_id>.json + 音频             参考文本、语言、时长和情绪标签
datasets/<name>.json + <name>.tar           数据集登记和训练素材
weights/<slug>/<engine>/<weight_id>/        meta.json 加 gpt.ckpt 或 sovits.pth
runs/<run>/run.json + 训练产物               训练参数、训练清单、产出和样音
evals/<yyyy-mm>/<job_id>.json                权重组合评测
blind/<test_id>.json                        盲听题目、投票与结果
works/<work_id>.json + works/<work_id>/      作品、选中的音频和渲染文件
compute/bench/<machine>/<date>.json         算力测速
```

数据集 tar 的目录结构为 `<name>/characters/<folder>/`，每个角色目录有 `.list` 和 `audio/`。每行格式为 `音频路径|说话人|语言|文本`，例如 `audio/001.wav|alice|ja|今日はいい天気です。`。登记文件中的 `characters` 将目录名映射到 `{id, list}`，例如 `sp0001_alice` 对应 `SAMPLE_DS.sp0001`。主控和 Colab 都会在首次使用时校验 tar 并解包到本机缓存。角色默认主参考应是可读的 3–10 秒音频。

本机的任务队列、合成历史、生成音频和索引缓存存于 `TTSAPI_DATA_DIR`。迁移主控时，资料库和本机运行数据可分别迁移。已有资料库可继续使用历史角色、权重来源及评测集标识。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `TTSAPI_GSV_ROOT`、`TTSAPI_GSV_V5_ROOT` | 两套引擎目录，默认 `~/GPT-SoVITS`、`~/GPT-SoVITS-v5`。 |
| `TTSAPI_PYTHON`、`TTSAPI_V5_PYTHON` | 对应引擎使用的解释器，默认安装目录下的 `.venv`。 |
| `TTSAPI_DATA_DIR`、`TTSAPI_LIBRARY` | 本机运行数据目录和初始资料库路径。API 选定的新路径会持久化。 |
| `TTSAPI_DATASET_ROOT`、`TTSAPI_TRAIN_DIR`、`TTSAPI_WEIGHTS_DIR` | 解包缓存、训练中间产物与权重导出的本机目录。 |
| `TTSAPI_HOST`、`TTSAPI_PORT` | 网关监听地址与端口，程序默认 `0.0.0.0:9880`。 |
| `TTSAPI_INSTALL_PORT`、`TTSAPI_WEB_DIR` | 专用配对安装端口（默认 80，0 关闭），以及可选的 Web 构建目录（在 `/ui/` 提供）。 |
| `TTSAPI_ROLE`、`TTSAPI_WORKER_TOKEN` | 运行角色和可选的 worker 令牌覆盖。配对安装会写入令牌文件。 |
| `TTSAPI_DEVICE`、`TTSAPI_DEVICE_V4`、`TTSAPI_TORCH_THREADS` | 计算设备、单引擎覆盖和 CPU 线程数。 |
| `TTSAPI_COLAB_ROOT` | Colab CLI 环境、账号状态和使用量账本的目录。 |

网关允许回环、RFC 1918 私网和 Tailscale 所用的 CGNAT 地址段。将网关用于可信网络，本地单机使用时保持回环绑定。

## 多算力与 Colab

在 Studio 的「添加算力」中生成一次性配对码，在目标 macOS 或 Linux 设备执行生成的命令。worker 下载主控的 Core bundle 和带 SHA-256 的 GPT-SoVITS 补丁。它从固定上游提交构建引擎，更新时可以复用旧安装的 Git 对象和底模文件。

用 `POST /plan` 查看可用设备、预计时间和云端费用。支持指定本机、局域网设备或云端设备，也可以使用自动调度。调度会考虑任务类型、引擎、设备状态和速度。可用 `POST /bench` 测速，参考来自资料库首个有可用默认参考的角色。

云端账号可在 Studio 内添加。手动安装独立的 Colab CLI 环境时，在 `core/` 执行 `python -m ttsapi.compute.colab.install`。任务投入 VM 前，主控只上传用到的资料库子集、已提交的 Core bundle 和引擎补丁。VM 校验补丁，热机命中相同的基点和补丁时复用本地提交。主控下载结果并写回原资料库与任务。账号池跟踪余额和使用量，处理 GPU 等待、运行时限及空闲 VM 回收。实现细节见 [Colab 作业与账号池](ttsapi/compute/colab/README.md)。

## API 分组

| 用途 | 主要入口 |
| --- | --- |
| 基础与资料库 | `/health`、`/system/info`、`/system/storage`、`/library` |
| 角色、引擎和参考 | `/models`、`/characters/{slug}/versions`、`/refs`、`/refs/upload` |
| 合成与作品 | `/tts`、`/tts/candidates/jobs`、`/compare/jobs`、`/works`、`/history` |
| 数据集、训练和权重 | `/datasets`、`/train`、`/train/sweep`、`/weights`、`/runs/import` |
| 评测和盲听 | `/eval-sets`、`/weights/eval`、`/blind`、`/reviews` |
| 算力与任务 | `/compute`、`/compute/invites`、`/cloud`、`/plan`、`/bench`、`/jobs`、`/jobs/{job_id}/events` |

异步任务创建后返回任务 ID。可读取 `GET /jobs/{job_id}`，也可订阅该任务的 SSE 事件流。出错时检查响应的 `detail` 与可选的 `code`。

## 开发与验证

在 `core/` 目录用包含 Core 依赖和 pytest 的 Python 运行以下命令。测试使用临时资料库、本地 Git 仓库和模拟 Colab；模拟 gateway 使用回环端口 9880，运行前确保该端口可用。

```sh
python -m pytest tests/unit ttsapi/compute/colab/test_colab.py tests/colab_sim
```

从当前网关路由和模型重新导出 OpenAPI 快照。

```sh
python - <<'PY'
import json
from pathlib import Path
from ttsapi.gateway import app

Path("../api/openapi.json").write_text(
    json.dumps(app.openapi(), ensure_ascii=False, separators=(",", ":")),
    encoding="utf-8",
)
PY
```
