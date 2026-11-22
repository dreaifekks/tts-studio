#!/bin/sh
# TTS Studio：把这台电脑加成主控的局域网设备（执行模式的 core：只跑主控派来的生成、评测、测速，不碰资料库）。
# 主控的 GET /install 发这个脚本，主控地址（下面的 CONTROL，core 端口）和取脚本的地址（INSTALL，代码包也从这里取）
# 已经写好。用法是 Studio「添加算力」里的那一行：
#   curl -fsSL 'http://studio-host.local/install' | sh -s -- --pair 7Q4-K9M
# 都装在 ~/.tts-core。每次安装是 releases/ 下单独的一份（core、GPT-SoVITS 和运行环境、配置、配对），装好、核对过才切过去，
# current 指着正在用的那份；中途哪一步失败，正在跑的旧服务和它的配对都不动（重跑失败 = 什么都没变）：
#   1. uv（已有就用）；Python 由 uv 装在 ~/.tts-core/python
#   2. 从主控取 core 的代码（和主控同一版本），马上带配对码连回主控（python -m ttsapi.pair join）：配对码 10 分钟有效，
#      不等后面几 GB 的下载。已经装过的（同一台）主控先把新令牌记着，旧令牌照样能用，到新服务带新令牌报第一次心跳才换上
#   3. GPT-SoVITS：固定上游提交和主控附带的补丁（已有提交对象优先复用）
#   4. 新的运行环境（按锁文件，包从 uv 的缓存来；Linux 没有 NVIDIA 显卡时装 CPU 版 torch）、底模（旧的那份有的硬链接过来，
#      缺的从 HF 下），核对 sha256（scripts/install_*.sh）
#   5. 切换：停旧服务，确认端口空了，按新的这份注册开机自启并起服务（macOS 是 launchd 用户代理 com.dreaife.tts-core-worker，
#      Linux 是 systemd 用户服务 tts-core-worker，加 linger，没登录也跑），用这次的令牌核对本机端口上答话的是它
#      （python -m ttsapi.pair check）。端口被别的程序占着、新服务没起来：换回旧服务，报错退出。成功后删掉旧的那份。
#      第 2 步连不上主控时起服务前再连一次，还连不上就打印这台的地址和配对码，在 Studio 里手动填（装过的先接着用原来的配对）
# 再跑一次（Studio 里重新生成配对码）：代码和环境更新到主控现在的版本，重新配对。
# 卸载：sh ~/.tts-core/uninstall.sh
# 参数：--pair <配对码>（必填）  --port <这台 core 的端口，默认 9880>  --engines <装哪些引擎的底模，默认 v2pro,v2proplus>
# 整个脚本放在 main 里：curl | sh 时 sh 先读完整个脚本再执行，中间的命令读不到后面的脚本

main() {
  set -eu
  CONTROL='__CONTROL__'
  INSTALL='__INSTALL__'
  ROOT="$HOME/.tts-core"
  PORT=9880
  ENGINES=v2pro,v2proplus
  PAIR=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --pair) PAIR=$2; shift 2 ;;
      --port) PORT=$2; shift 2 ;;
      --engines) ENGINES=$2; shift 2 ;;
      *) echo "不认识的参数：$1" >&2; exit 2 ;;
    esac
  done
  [ -n "$PAIR" ] || { echo "缺配对码：在 Studio「添加算力」里复制完整的命令（带 --pair）" >&2; exit 2; }
  case "$(uname -s)" in
    Darwin) OS=macos ;;
    Linux) OS=linux ;;
    *) echo "只支持 macOS 和 Linux" >&2; exit 1 ;;
  esac
  LABEL=com.dreaife.tts-core-worker
  UNIT=tts-core-worker
  if [ "$OS" = linux ]; then
    SERVICE=$HOME/.config/systemd/user/$UNIT.service
  else
    SERVICE=$HOME/Library/LaunchAgents/$LABEL.plist
  fi
  # 正在用的那份：current 指的；这一版之前装的没有 releases，就在 ~/.tts-core 下面（配对在 data/device.json）
  if [ -L "$ROOT/current" ]; then
    OLD=$(readlink "$ROOT/current")
    OLD_DEVICE=$OLD/device.json
  else
    OLD=$ROOT
    OLD_DEVICE=$ROOT/data/device.json
  fi
  for d in "$ROOT"/releases/*; do  # 上次中途被打断留下的（服务定义还指着的不删：上次在切换中途被强行杀掉）
    [ ! -e "$d" ] || [ "$d" = "$OLD" ] || grep -qF "$d/" "$SERVICE" 2>/dev/null || rm -rf "$d"
  done
  REL=$ROOT/releases/$(date +%Y%m%d-%H%M%S)
  GSV=$REL/GPT-SoVITS
  PY=$GSV/.venv/bin/python
  GSV_V5=$REL/GPT-SoVITS-v5
  PY_V5=$GSV_V5/.venv/bin/python
  TMP=$(mktemp -d)
  DONE="" SWITCHING="" rc=0
  trap cleanup EXIT
  trap 'exit 1' INT TERM HUP
  mkdir -p "$ROOT/bin" "$ROOT/data" "$ROOT/logs" "$REL"

  # 1. uv
  export PATH="$ROOT/bin:$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    ts "装 uv"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$ROOT/bin" UV_NO_MODIFY_PATH=1 sh
  fi
  # Python 装在 ~/.tts-core/python；uv python install 默认还往 ~/.local/bin 放 python3.10 的链接，改到 ~/.tts-core/bin
  export UV_PYTHON_INSTALL_DIR="$ROOT/python" UV_PYTHON_BIN_DIR="$ROOT/bin" UV_PYTHON_PREFERENCE=only-managed

  # 2. core 的代码
  ts "从主控取代码（${INSTALL}）"
  curl -fsSL "$INSTALL/install/code.tar" | tar -x -C "$TMP"
  . "$TMP/install.env"
  git init -q "$REL/core"
  git -C "$REL/core" fetch -q --tags "$TMP/$CORE_BUNDLE" HEAD
  git -C "$REL/core" checkout -q -B core "$CORE_COMMIT"
  ts "core $VERSION"

  # 配置：每行 KEY=VALUE，原样作为服务的环境变量
  cat > "$REL/tts-core.env" <<EOF
# 安装脚本生成。tts-core 执行模式，这一份的配对和令牌在 device.json
TTSAPI_ROLE=worker
TTSAPI_HOST=0.0.0.0
TTSAPI_PORT=$PORT
TTSAPI_WORKER_PORT_SHIFT=100
TTSAPI_GSV_ROOT=$GSV
TTSAPI_GSV_V5_ROOT=$GSV_V5
TTSAPI_V5_PYTHON=$PY_V5
TTSAPI_DATA_DIR=$ROOT/data
TTSAPI_DEVICE_FILE=$REL/device.json
TTSAPI_TRAIN_DIR=$ROOT/training
TTSAPI_WEIGHTS_DIR=$ROOT/weights
TTSAPI_DATASET_ROOT=$ROOT/datasets
PYTHONPATH=$REL/core
PYTHONUNBUFFERED=1
PYTHONIOENCODING=utf-8
EOF

  # 连回主控：运行环境还没装，用 uv 临时起一个只有 httpx、psutil 的 Python。令牌写进这一份的 device.json，旧服务看不到
  ts "连回主控"
  pair_join uv run -q --no-project --python 3.10 --with httpx --with psutil python
  [ $rc -ne 3 ] || echo "先接着装，起服务前再连一次"

  # 3. GPT-SoVITS：复用旧安装的 Git 对象，或从公开上游浅取固定提交。
  . "$REL/core/scripts/gptsovits.sh"
  if [ -e "$OLD/GPT-SoVITS/.git" ]; then
    git clone -q --no-checkout "$OLD/GPT-SoVITS" "$GSV"
  else
    git init -q "$GSV"
  fi
  gsv_fetch_base "$GSV" "$GSV_BASE"
  gsv_checkout "$GSV" "$GSV_BASE" "$GSV_BRANCH" "$TMP/$GSV_PATCH" "${GSV_PATCH_SHA256:?}"
  # 底模和用到时才下的模型（中文的 G2PW，约 600MB）：旧的那份有的硬链接过来（不占地方、不重下）；install_*.sh 核对底模的
  # sha256，对不上的先删掉链接再下，不改旧的那份
  { awk '!/^#/ && $1 != "v5turbo" && $1 != "v5dev" {print $3}' "$REL/core/scripts/pretrained.sha256"
    [ ! -d "$OLD/GPT-SoVITS/GPT_SoVITS/text/G2PWModel" ] || (cd "$OLD/GPT-SoVITS" && find GPT_SoVITS/text/G2PWModel -type f)
  } | while read -r rel; do
    [ -n "$rel" ] && [ -f "$OLD/GPT-SoVITS/$rel" ] || continue
    mkdir -p "$(dirname "$GSV/$rel")"
    ln -f "$OLD/GPT-SoVITS/$rel" "$GSV/$rel"
  done
  ts "GPT-SoVITS $(git -C "$GSV" log --oneline -1)"

  # Reuse V5 repo and V5 pretrained weights from the previous release when V5 engines are requested
  case ",$ENGINES," in
    *,v5turbo,*|*,v5dev,*)
      if [ -d "$OLD/GPT-SoVITS-v5/.git" ]; then
        git clone -q "$OLD/GPT-SoVITS-v5" "$GSV_V5"
      fi
      if [ -d "$OLD/GPT-SoVITS-v5" ]; then
        awk '!/^#/ && ($1 == "v5turbo" || $1 == "v5dev") {print $3}' "$REL/core/scripts/pretrained.sha256" | while read -r rel; do
          [ -n "$rel" ] && [ -f "$OLD/GPT-SoVITS-v5/$rel" ] || continue
          mkdir -p "$(dirname "$GSV_V5/$rel")"
          ln -f "$OLD/GPT-SoVITS-v5/$rel" "$GSV_V5/$rel"
        done
      fi
      ;;
  esac

  # 4. 运行环境和底模
  ts "装运行环境和底模（第一次约 5–10 分钟，要下几个 GB；再装时大多从缓存来）"
  NLTK=""
  [ ! -d "$OLD/GPT-SoVITS/.venv/nltk_data" ] || NLTK=$OLD/GPT-SoVITS/.venv/nltk_data
  GSV_ROOT=$GSV GSV_V5_ROOT=$GSV_V5 ENGINES=$ENGINES PRETRAINED=hf NLTK_DATA_SRC=$NLTK bash "$REL/core/scripts/install_$OS.sh"

  # 第 2 步连不上主控：再连一次；还连不上时 join 把配对码记成这一份的（从现在起 10 分钟），打印手动填用的地址。
  # 装过的先接着用原来的配对（主控那边没换令牌），手动填配上时换成新的
  if [ $rc -eq 3 ]; then
    pair_join "$PY"
    [ $rc -ne 3 ] || [ ! -f "$OLD_DEVICE" ] || cp -p "$OLD_DEVICE" "$REL/device.json"
  fi

  # 5. 切换：失败时 cleanup 换回旧服务
  ts "切到新装的这份"
  [ ! -f "$SERVICE" ] || cp -p "$SERVICE" "$REL/service.prev"
  SWITCHING=1
  stop_service
  port_free || { echo "端口 $PORT 被别的程序占着（停了旧服务还有人在听）：先关掉它，或者换个端口（--port）再装" >&2; exit 1; }
  write_service
  start_service
  verify || { echo "新服务没起来或者答话的不是它，看 $ROOT/logs/gateway.log" >&2; exit 1; }
  SWITCHING="" DONE=1
  ln -sfn "$REL" "$ROOT/current"
  if [ "$OLD" = "$ROOT" ]; then
    rm -rf "$ROOT/core" "$ROOT/GPT-SoVITS" "$ROOT/GPT-SoVITS-v5" "$ROOT/tts-core.env" "$ROOT/data/device.json" "$ROOT/data/pair.json"
  else
    rm -rf "$OLD"
  fi
  rm -f "$REL/service.prev"
  cp "$REL/core/scripts/device/uninstall.sh" "$ROOT/uninstall.sh"
  if [ "$OS" = linux ] && [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != yes ]; then
    loginctl enable-linger "$(id -un)" || echo "没登录时也要跑，需要 linger：sudo loginctl enable-linger $(id -un)" >&2
  fi
  ts "装好了：${REL}（core ${VERSION}，端口 ${PORT}，日志 $ROOT/logs/gateway.log）"
  if [ $rc -eq 0 ]; then
    echo "已经连上主控，回 Studio 继续。卸载：sh $ROOT/uninstall.sh"
  else
    echo "没连上主控：在 Studio 里按上面的连接地址手动填（10 分钟内有效）。卸载：sh $ROOT/uninstall.sh"
  fi
}

ts() { echo "[$(date +%H:%M:%S)] $*"; }

# 带配对码连回主控（python -m ttsapi.pair join，用新装的这份 core）：参数是用哪个 Python。结果在 rc：0 连上了；
# 3 连不上主控（join 已经把配对码记成这一份的、打印了手动填用的地址）；4 主控不认这个配对码，退出
pair_join() {
  set +e
  (set -a; . "$REL/tts-core.env"; set +a; cd "$REL/core" && "$@" -m ttsapi.pair join "$CONTROL" "$PAIR")
  rc=$?
  set -e
  case $rc in
    0|3) ;;
    4) echo "在 Studio 里重新生成配对码，再跑一次新的命令" >&2; exit 1 ;;
    *) exit $rc ;;
  esac
}

stop_service() {
  if [ "$OS" = linux ]; then
    systemctl --user stop "$UNIT" 2>/dev/null || true
  else
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    # bootout 返回时旧进程可能还在退出：等它从 launchd 里消失（最多 30 秒），不然马上 bootstrap 同名的会失败
    i=0
    while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && [ $i -lt 30 ]; do
      i=$((i + 1))
      sleep 1
    done
  fi
}

start_service() {
  if [ "$OS" = linux ]; then
    systemctl --user daemon-reload
    systemctl --user enable -q "$UNIT"
    systemctl --user restart "$UNIT"
  else
    launchctl bootstrap "gui/$(id -u)" "$SERVICE"
  fi
}

# 开机自启的服务指向新装的这份
write_service() {
  mkdir -p "$(dirname "$SERVICE")"
  if [ "$OS" = linux ]; then
    cat > "$SERVICE" <<EOF
[Unit]
Description=TTS Studio 局域网设备（tts-core 执行模式）
Wants=network-online.target
After=network-online.target

[Service]
WorkingDirectory=$REL/core
EnvironmentFile=$REL/tts-core.env
ExecStart=$PY -m ttsapi.gateway
Restart=on-failure
RestartSec=5
StandardOutput=append:$ROOT/logs/gateway.log
StandardError=append:$ROOT/logs/gateway.log

[Install]
WantedBy=default.target
EOF
  else
    "$PY" -I - "$REL" "$ROOT" "$PY" "$LABEL" "$SERVICE" <<'EOF'
import plistlib, sys
rel, root, py, label, plist = sys.argv[1:]
env = dict(line.split("=", 1) for line in open(f"{rel}/tts-core.env", encoding="utf-8").read().splitlines()
           if line and not line.startswith("#"))
env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"  # ffmpeg（Homebrew）、git
with open(plist, "wb") as f:
    plistlib.dump(dict(Label=label, ProgramArguments=[py, "-m", "ttsapi.gateway"], WorkingDirectory=f"{rel}/core",
                       EnvironmentVariables=env, RunAtLoad=True, KeepAlive=dict(SuccessfulExit=False), ThrottleInterval=5,
                       StandardOutPath=f"{root}/logs/gateway.log", StandardErrorPath=f"{root}/logs/gateway.log"), f)
EOF
  fi
}

# 停了旧服务以后本机端口上没人在听（curl 连不上，退出码 7；不走代理）；旧服务退出要一会儿，最多等 10 秒
port_free() {
  i=0
  while :; do
    set +e
    curl --noproxy '*' -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/"
    c=$?
    set -e
    [ $c -ne 7 ] || return 0
    i=$((i + 1))
    [ $i -lt 10 ] || return 1
    sleep 1
  done
}

# 新服务起来了、答话的是它：python -m ttsapi.pair check（带这一份的令牌查本机端口，机器 id 对得上）。
# 退出码 0 是它；5 还连不上，接着等（最多 60 秒）；别的是端口上答话的不是它
verify() {
  i=0
  while :; do
    set +e
    (set -a; . "$REL/tts-core.env"; set +a; cd "$REL/core" && "$PY" -m ttsapi.pair check)
    c=$?
    set -e
    [ $c -ne 0 ] || return 0
    [ $c -eq 5 ] || return 1
    i=$((i + 1))
    [ $i -lt 60 ] || { echo "服务 60 秒还没起来" >&2; return 1; }
    sleep 1
  done
}

# 退出时：切换到一半（失败、被打断）就换回旧服务（没装过的：去掉新服务）；没装成就删掉新的这份。换回去的时候不理会
# 再来的 Ctrl-C、SSH 断开的 TERM，免得换到一半
cleanup() {
  trap '' INT TERM HUP
  set +e
  if [ -n "$SWITCHING" ]; then
    echo "换回原来的服务" >&2
    stop_service
    if [ -f "$REL/service.prev" ]; then
      cp -p "$REL/service.prev" "$SERVICE"
      start_service
    else
      [ "$OS" != linux ] || systemctl --user disable -q "$UNIT" 2>/dev/null
      rm -f "$SERVICE"
      [ "$OS" != linux ] || systemctl --user daemon-reload
    fi
  fi
  [ -n "$DONE" ] || rm -rf "$REL"
  rm -rf "$TMP"
}

main "$@"
