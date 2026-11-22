# TTS Studio 的 Colab 算力

控制端负责选账号、准备输入和收回结果。云端作业只在 VM 本地读写。运行记录在控制端的 `~/.config/tts-core/usage.sqlite`。

## 安装

在 core/ 目录运行 `python -m ttsapi.compute.colab.install`。安装器用 uv 和对应平台的 `requirements/colab-{linux,macos}.lock.txt` 在 `~/.config/tts-core/colab-venv` 建立 CLI 环境。发布 core 时要带模块、两份锁和仓库的 `colab/vm_{lib,setup,run,pack}.py`。可用 `--root` 指定安装位置。

core 和引擎继续使用 Python 3.10，google-colab-cli 0.8.0 单独使用 Python 3.12。CLI 包元数据要求 Python ≥3.12、Pydantic ≥2.12.5、filelock ≥3.29.2，原引擎锁分别为 Python 3.10、Pydantic 2.10.6、filelock 3.29.0。两边通过子进程通信。

## 控制端接口

```python
from ttsapi.compute.colab import ColabPool, WaitingForGPU, run_job

pool = ColabPool()  # 可传 root=...，python=... 指定 CLI Python
async with pool.lifespan():  # 挂在 control 角色上
    ...  # 由调度器接收和执行任务
```

| 方法 | 返回与约定 |
|---|---|
| `accounts(*, refresh=True)` | `list[Account]`。字段为 `id, label, balance, month_used, gpus, recorded_month_used, error`。余额查询失败时为 `None`，附原因。设置页可强制刷新。 |
| `rates()` | `dict[str, float]`，单位为单元/小时。T4 1.15、L4 1.94、A100 6.73、G4（RTX PRO 6000）10.05。 |
| `gpu_types()` | `list[GpuType]`。字段为 `gpu, label, account_count, hours, reason`。可用小时为符合卡型的账号余额之和除以费率。H100 费率未知，展示但不可派发。 |
| `start(gpu, max_minutes=None, *, task_id, approved=False, estimated_minutes=None)` | 成功返回 `ColabRun`（有做完先别关的同型号 VM 时接手它，见下文 `keep_idle`）。推荐传调度器算出的 `estimated_minutes`，可同时指定最长运行时间（设置里的「最长用多久」）。只有 `max_minutes` 时按其一半估算花费。 |
| `run_script(gpu, script, args=(), *, task_id, estimated_minutes, approved=False)` | 一次性本地 Python 脚本，返回 CLI 标准输出。使用 `colab run --keep --timeout 0 -- SCRIPT ARGS`，由账号池释放并记账。CLI 会抽取脚本参数中的 `--env`，因此提前拒绝这个参数。 |
| `reap()`、`lifespan(interval=60)` | core 可自行周期调用 reap，或使用异步 lifespan。reap 关某台前在这台的锁里按现在的记录再判一次（可能刚被接手、时限延长）。后者启动后台检查，每 30 分钟刷新账号，并在正常关闭时释放本进程发起的 VM。崩溃后由下一次运行的主控回收。 |
| `login(email, label=None, *, approved=False)` | 交互式终端入口，用户完成 CLI 授权后通过 Google OpenID Connect UserInfo 核对身份，再登记并试开有资格的已知费率卡。高费率卡需要显式批准。首次使用 default 时记录 UserInfo 邮箱哈希。已启用的具名账号可用同一邮箱重新登录，成功后更新自己的授权，沿用原标签（可显式改名），跳过新账号的试卡。再次添加 default 对应邮箱仍会被拒绝。授权结果暂存在登录子进程内存，邮箱核对成功才写入目标条目，返回账号 id。 |
| `register(email, label=None)`、`disable(account)`、`enable(account)`、`remove(account)` | 登记已有授权，或从调度候选移除。账号 id 为规范化邮箱的 SHA-256。停用保留凭据和账本，`enable` 恢复。`remove` 先停用（释放留着的 VM），再删 core 给这个账号写的凭据（`worker.py forget` → `keyring_backend.forget`：macOS 钥匙串 `google-colab-cli` / `oauth_token/<id>`，Linux `keyring/<id>` 和 Secret Service 里的文件密钥）和 `colab/<id>/`，账本行保留（`accounts.removed=1`，不再列出，`month_used()`、`labels()` 照算），还没等到余额扣款的按「运行时长 × 费率」记估值，之后不再回补结算；VM 还在跑任务时 ValueError；`default` 只停用。删凭据在账号锁里（`_allocate` 登记前也在这把锁里复核账号还启用、没移除）；删凭据失败（只有条目本来没有才跳过）时 CliError，账号停用、没标移除，可重试。重新登录（`register`、后台登录）就回来。 |
| `adopt(gpu, *, task_id, estimated_minutes, max_minutes=None, approved=False)` | 只接手不新开：有能接手的同型号留机就返回它，否则 None（扫参按热启动报价的那一台先用它，接不上就按新开卡重算）。 |
| `begin_login(label=None)`、`submit_code(login_id, code)`、`login_status(login_id)` | Studio 用的后台登录（`POST /cloud/accounts/login`）。`worker.py login` 子进程跑 CLI 0.8.0 的粘贴授权码流程（`colab_cli/auth.py` 的 `_run_remote_flow`，不用本机回环），HOME 是临时的 `colab/login-<id>/`。`begin_login` 等到 stderr 上的授权链接就返回 `Login`；交了授权码后子进程用 UserInfo 拿到邮箱，stdout 回一行 `{"email", "account"}`，主控核对（和 default 是同一个邮箱时 abort）后回 `commit`，子进程才把内存里的授权写进这个账号的凭据条目，主控登记账号（label 默认邮箱，已有账号沿用原名并恢复启用）。新账号随后试开有报价的非高费率卡；`lifespan` 收尾时先禁止再试（`closing`），等正在开的那台进入释放，再收最后一轮。`LOGIN_TIMEOUT`（15 分钟）没完成就结束子进程。 |
| `accounts(*, refresh=True, include_disabled=False)`、`account(id, *, refresh=True)`、`usage_monthly()`、`idle_vms()` | 账号（`Account` 另有 `email`、`disabled`；本机 CLI 没登录时不列 default）、单个账号（没有时 KeyError）、按月按账号的已结算单元、做完先别关的 VM 数 `{卡型: 台数}`。 |

Google 的 `GET /tun/m/ccu-info` 实测提供余额、当前每小时消耗、活动台数、`eligibleGpus` 和 `eligibleTpus`。服务端未提供本月累计值，`month_used` 为 `None`。`recorded_month_used` 是这台主控按本地自然月累计的估计值，设置页应标为「本月已用（估计）」。充值或月度发放使余额上升时，该区间消耗未计入；晚扣尾段按费率和时长分摊；共用 default 凭据的其他客户端也可能贡献消耗。

每个账号的 CLI 子进程以 `colab/<id>/` 为 HOME，`--config` 指向其中的 `sessions.json`。history、CLI 日志和设置也落在这个私有 HOME。默认账号只读使用原系统登录条目。新增 Linux 账号的凭据加密保存在 `keyring/<id>`（0600），每账号的文件密钥由 Secret Service 保管。macOS 新账号使用 Keychain 的独立条目，存在本机用户的登录钥匙串里：Security 框架按 HOME 找默认钥匙串，`cli.py` 第一次用到某个 HOME 时用 `security default-keychain -d user -s` 在其中设好（写 `Library/Preferences/com.apple.security.plist`；没设时存凭据会弹「Keychain Not Found」，读也读不到。keyring 25 的 macOS 后端不理会指定的钥匙串，所以不在后端里改）。Linux 钥匙串锁定时需要用户处理。普通调度关闭 CLI 标准输入，授权流程只通过显式登录发起。

### 调度与释放

按设计第 3 节在调度器计算预计分钟数。账号池按 `max(0.25, 预计分钟数 × 费率 / 60)` 排除余额不足者。默认时限为预计时间的 2 倍，限制在 30 分钟到 24 小时。按余额降序申请，明确的配额或容量拒绝会尝试下一账号。开机超时或分配响应丢失时，保留原 UUID 给 reap 核对，同时立即尝试下一候选账号。失败卡型暂缓 600 秒，其他卡型仍可用。权限卡为 A100、G4 和 H100。可申请卡型随余额刷新，快于七天，无需为了刷新资格而额外租卡。

无候选抛出 `WaitingForGPU`，字段是 `gpu` 和 `retry_after=600`。线 A 将任务转为「等待云端 GPU」，到时重新调用 start。一个账号可同时运行多台 VM。每次开机使用独立 UUID 和作业锁。释放前按 endpoint 查询服务端列表，仍在列表里的 VM 才执行 unassign。CLI 丢失本地 sessions 记录时，worker 用账本中该作业的 endpoint 从服务端重建，再继续当前操作。

开机结果不确定时保留账本行。首次 recover 至少等 10 秒，reap 按原 notebook UUID 重查。开机满约 10 分钟后，只有一次成功的空查询才能删除记录。查询失败会留待下一轮。endpoint 尚未确认的旧记录允许同一任务换卡或重试。若旧 VM 稍后出现，reap 只按旧 UUID 释放它。控制端应持续运行 lifespan。

账本使用每账号的余额采样。一次余额下降只入账一次，按照该采样区间各 VM 的「费率 × 运行秒数」分摊。关机时先入账，默认维护周期下约 5–6 分钟后再读余额并更新同一行。若余额晚扣，分摊包含延迟计费窗口里的已关 VM。没有服务端逐 VM 账单，同时运行任务的单项费用是分摊值；其他客户端消费、充值也可能影响余额。作业完成释放并记入即时费用后清理 UUID 锁文件，晚扣仍按账号锁结算。旧账本首次打开时自动升级。

### VM 与作业

训练、评测任务由 `ttsapi/compute/colab_jobs.py` 的 `run_colab` 放到 Colab 上跑（gateway 在任务协程里调用）：

```python
summary = await run_colab(run, "train", req, gpu="L4", estimated_minutes=eta, approved=False,
                          pool=pool, catalog=catalog, refs=refs)            # 评测：kind="eval"，加 character、ref_id
```

1. **输入**（`export_inputs`、`bundle_code`、`job_spec`）：从控制端资料库导出这次作业用到的子集作为「输入资料库」——用到的角色
   （训练的角色、同一数据集的内置角色、评测用的角色、参考所属的角色）、它们的数据集记录和 tar、训练出来的角色的 train.list、
   资料库里登记的默认参考和指定参考、要评测的权重（底模不带，VM 从 HF 下）。`library.json` 新写一份（新 id），其余是资料库
   里的原文件，不复制。代码是 tts-core 当前已提交的 HEAD 里 tts-studio 的 `core/`（单独做成一个提交，带和主控版本号同名的 tag）的 Git bundle，以及 `patches/gptsovits-bf81cdb.patch` 和它的 SHA-256。job.json 只有一个任务，带控制端的任务 id。
2. **开机**：`pool.start`；抛 `WaitingForGPU` 时任务转为「等待云端 GPU」，`retry_after` 秒后再试。
3. **跑**：`run_job` 上传输入，依次后台跑 `colab/vm_setup.py`、`colab/vm_run.py`，最后不论成败跑 `colab/vm_pack.py` 打包并下载。
   在 `with vm:` 里跑（`colab_jobs.run_and_fetch`）：结果下载失败时 `vm.keep_for_retry()` 留机，每 120 秒重新进 `with vm:` 调
   `vm.download(*vm.pending_result)`，最多 5 次（在 30 分钟保留期内）；都失败或任务被取消时 `vm.stop()`。其余情况退出 `with` 即释放。
4. **写回**：结果 tar 用 `ttsapi.importer.import_runs` 写进控制端资料库；VM 上的任务记录填回 `train_result` / `eval_result`，
   VM 的任务日志接到这个任务的日志后面。作业目录 `DATA/colab/<任务 id>/`：`job/` 是 VM 的时间线和各脚本日志；
   `results.tar` 导入后删掉，导入失败时留着，可以 `POST /runs/import` 重试。

`run_job(vm, spec, inputs, output, stage=None)` 接受已展开的 job 字典、映射「VM 输入目录下的相对路径 → 本地文件」、输出 tar 路径，
`stage(step)` 在 upload / setup / run / download 前调用（在任务线程里）。输入上传到 `/content/tts-inputs-<随机 id>/`，job.json 的
`inputs` 指向它。不用 Drive、rclone。返回最后的 `RunState`；VM 没了（gone）时不下载。

`ColabRun` 的字段为 `name, account_id, endpoint, deadline`。默认在正常或异常退出上下文时释放 VM；下载失败会保留原本地文件。需要留机重试时，在当前 `with` 内捕获下载错误并显式调用 `vm.keep_for_retry()`，之后可以处理或继续抛出原异常。仅在仍有待取结果时跳过这次退出的自动释放。重试时重新进入 `with vm:` 并调用 `vm.download(*vm.pending_result)`；如需再次保留，需再次显式请求。放弃时直接调用 `vm.stop()`。以下方法均为同步阻塞调用，在 FastAPI 异步路由中应交给任务线程运行。

- `upload(local, remote)` 按 70 MiB 分片，VM 流式拼回，校验 SHA-256 后原子替换目标文件，再清除分片。重跑会先核对已有目标的哈希。拼接时限按实际上传字节数估算，按 100 MiB/s 给三倍余量并另加 60 秒，最低 300 秒，受 VM 剩余时限约束。
- `download(remote, local, *, timeout=180)` 在 VM 以 70 MiB 切片并生成 SHA-256 清单。逐片下载、校验，单片最多尝试三次。整文件校验成功后才原子替换本地文件。timeout 针对单次传输；上传、下载和 poll 共用五分钟保活。目标父目录应已存在。
- `execute(python_code, *, timeout=90)` 通过 `colab exec --timeout 0` 执行，另设本地时限，并检查内核执行确认。遇到 CLI 错误最多尝试三次，间隔五秒；传入的代码应可重复执行。
- `run_background(cmd, done_marker, *, heartbeat=None)` 在 VM 后台执行 shell 命令。每次新命令使用独立标记路径，同一启动请求重发幂等。可传已在 VM 上的输入文件路径作为下载心跳。
- `poll()` 返回字符串枚举 `RunState` 的 `running/done/failed/gone`。调度器约每 30 秒调用，或用 `wait()`。每五分钟 exec 保活，exec 失败只重试。连续 15 分钟标记及心跳都下载失败，且服务端成功列举并确认 endpoint 不在时，才返回 gone。时限到达时会主动拆机并抛出 `TimeoutError`。
- `keep_for_retry(max_minutes=30)` 从请求保留时开始计时。保留后要么重试，要么调用 `stop()`；默认 30 分钟后由下一次 `reap()` 自动释放并结算，VM 原时限更早时按原时限释放。保持 `pool.lifespan()` 运行，或约每分钟调用 `reap()`，以执行保活和到期回收。
- `stop()` 随时释放 VM，包括下载未完成或已请求保留的情况，并核对释放结果、结算。
- `keep_idle(minutes)`：做完先别关（设置的 `keep_minutes`）。结果已取回时把账本这一行标成空闲（`idle=1`，`retry_deadline` 为 minutes 分钟后，不超过 VM 原时限），退出 `with` 时不释放；之后由 `reap()` 每 5 分钟保活、到点释放结算（和 `keep_for_retry` 同一套）。快到时限或已关时不留，返回 False。之后同型号的 `start()`（不含 `run_script` 和加账号时的试卡）先接手空闲的 VM（`_adopt`：当场查余额够这次、账号没停用、开机 24 小时的硬期限前剩下的时间够这次预计的（`ADOPTABLE`，查和接手的原子更新都按它）；同一行账本换成新任务 id、记 `adopted` 接手时间，时限从现在起算、不超过开机后 24 小时；`ColabRun.stop` 带自己的任务 id，已被接手的 VM 旧句柄释放不了），返回的 `ColabRun.reused` 为 true；`run_job` 和扫参上传前先清掉上次的 `/content/job`、`/content/library`、`/content/tts-api`、输入和结果（`job.RESET`），`vm_setup.py` 会核对 GPT-SoVITS 基点、补丁和本地分支，匹配时复用已提交的补丁，`.venv` 和底模不重装。

VM 上的布局和过程（`colab/vm_lib.py` 开头有完整说明）：

- `vm_setup.py`：将 Core bundle 解到 `/content/tts-api`，从公开上游浅取基点并在 `/content/GPTSoVits` 应用补丁；输入资料库移到 `/content/library`，数据集 tar 留在资料库里，
  VM 的 gateway 第一次用到时按正式逻辑（`ttsapi.datasets.local_root`：核对 sha256、解到 `datasets/.unpacked/<库 id>-<哈希>/`）解压，
  `vm_pack.py` 打结果时不带 tar；`scripts/install_linux.sh` 装环境、从 HF 下 job 用到的引擎的底模、nltk 数据。
  Colab 上能跑的引擎是 `scripts/pretrained.sha256` 里有底模的，别的 `run_colab` 直接报错。
- `vm_run.py`：起 gateway（`TTSAPI_LIBRARY=/content/library`），`POST /library/claim` 认领资料库，按 job.json 的任务调
  `POST /train` 或 `POST /weights/eval`（`compute=local`）并等它结束。结束后把 VM 上的任务 id 换成控制端的任务 id（tts.db、
  任务日志、资料库里 run 的 job_id 和评测文件），删掉输入带来的权重文件，写 `summary.json`。有任务失败时退出码非零。
- `vm_pack.py`：结果 tar，布局就是 importer 认的来源：

```
library/        VM 的资料库（library.json、characters/、refs/、datasets/*.json、weights/、runs/、evals/）
data/tts.db     VM gateway 的任务记录（任务 id 已换成控制端的）
data/jobs/      任务日志 <任务 id>.log
job/            job.json、timeline.jsonl、gateway.out、各脚本日志、summary.json、venv-freeze-colab.txt
```

本机模拟：`tests/colab_sim`（假 VM 在本机目录执行这三个脚本，假 gateway 用 tts-core 的资料库代码写记录）。

## 验证

- core 解释器运行 `python -m pytest -q ttsapi/compute/colab/test_colab.py tests/unit/test_colab_jobs.py`，VM 脚本的本机模拟 `python -m pytest -q tests/colab_sim`（占 127.0.0.1:9880）。
- 锁定 CLI 解释器运行 `python -m unittest ttsapi.compute.colab.test_keyring`。这组测试使用临时目录和假系统钥匙串。
- 人工授权的 T4 冒烟入口为 `python -m ttsapi.compute.colab.smoke STATE_DIR CLI_PYTHON`。使用独立空账本；Linux 上可用 systemd 用户单元设置 6 分钟硬期限，ExecStopPost 用同一入口加 `--reap`。VM 上生成 150 MiB 随机文件，检查 3 片下载、两端 SHA-256、已释放 endpoint 的重复 release、服务端列表和账本。关机满 5 分钟后用同一账本运行 --reap，核对晚扣结果；一次 T4 冒烟预算 0.3 单元。
