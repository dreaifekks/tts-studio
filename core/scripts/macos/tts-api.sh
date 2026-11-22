#!/usr/bin/env bash
# macOS 上起停 tts-api（对应 Windows 的 start.ps1 / stop.ps1）。gateway 由 launchd 用户代理托管：
# - 不常驻：plist 生成在 runtime/launchd/，不放进 ~/Library/LaunchAgents，重启或登出后不会自己起来
# - gateway 崩溃时 launchd 5 秒后拉起；POST /system/update 之后的主动重启（退出码 75）也由它拉起；stop 之后不再拉起
# - 推理 worker 跟随 gateway 退出；正在跑的训练进程不受影响，下次启动后 gateway 自动接管（要停训练先 POST /jobs/{id}/cancel）
#
# 用法：scripts/macos/tts-api.sh start|stop|restart|status|logs
# 配置：TTSAPI_ENV_FILE（默认 ~/tts-api-data/tts-api.env），每行 KEY=VALUE（可用 ~），原样作为 gateway 的环境变量；
#      模板 scripts/macos/tts-api.env.example。Python 用 $TTSAPI_PYTHON，默认 $TTSAPI_GSV_ROOT/.venv/bin/python。
set -euo pipefail
API=$(cd "$(dirname "$0")/../.." && pwd)
LABEL=com.dreaife.tts-api
DOMAIN=gui/$(id -u)
ENV_FILE=${TTSAPI_ENV_FILE:-$HOME/tts-api-data/tts-api.env}
PLIST=$API/runtime/launchd/$LABEL.plist
LOG=$API/logs/gateway.log

conf() {  # conf KEY：配置文件里的值（~ 展开）
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 | sed "s|^~|$HOME|"
}

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

url() {
  local h p
  h=$(conf TTSAPI_HOST)
  p=$(conf TTSAPI_PORT)
  if [ -z "$h" ] || [ "$h" = 0.0.0.0 ]; then h=127.0.0.1; fi
  echo "http://$h:${p:-9880}"
}

start() {
  [ -f "$ENV_FILE" ] || { echo "没有配置文件 ${ENV_FILE}（模板：scripts/macos/tts-api.env.example）" >&2; exit 1; }
  if loaded; then echo "已经在运行（launchctl print ${DOMAIN}/${LABEL}）"; return; fi
  local py=${TTSAPI_PYTHON:-$(conf TTSAPI_PYTHON)}
  py=${py:-$(conf TTSAPI_GSV_ROOT)/.venv/bin/python}
  [ -x "$py" ] || { echo "找不到 Python：${py}" >&2; exit 1; }
  mkdir -p "$API/logs" "$API/runtime/launchd"
  if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt $((20 * 1024 * 1024)) ]; then mv "$LOG" "$LOG.old"; fi
  "$py" -I - "$ENV_FILE" "$PLIST" "$API" "$py" "$LABEL" "$LOG" <<'EOF'
import os, plistlib, sys
env_file, plist, api, py, label, log = sys.argv[1:]
env = {}
for line in open(env_file, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = os.path.expanduser(v.strip())
# launchd 的 PATH 很短：ffmpeg（Homebrew）、git（/system/update）要能找到
env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
env.update(PYTHONPATH=api, PYTHONPYCACHEPREFIX=f"{api}/runtime/pycache", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
plistlib.dump(dict(
    Label=label, ProgramArguments=[py, "-m", "ttsapi.gateway"], WorkingDirectory=api, EnvironmentVariables=env,
    RunAtLoad=True, KeepAlive=dict(SuccessfulExit=False), ThrottleInterval=5,
    StandardOutPath=log, StandardErrorPath=log,
), open(plist, "wb"))
EOF
  echo "$(date '+%F %T') tts-api.sh: start" >> "$API/logs/supervisor.log"
  launchctl bootstrap "$DOMAIN" "$PLIST"
  local u; u=$(url)
  for _ in $(seq 60); do
    if curl -sf -m 2 "$u/health" >/dev/null; then echo "已启动：${u}（日志 ${LOG}）"; return; fi
    sleep 1
  done
  echo "60 秒内 ${u}/health 没有响应，看日志：${LOG}" >&2
  exit 1
}

stop() {
  if ! loaded; then echo "没有在运行"; return; fi
  echo "$(date '+%F %T') tts-api.sh: stop" >> "$API/logs/supervisor.log"
  launchctl bootout "$DOMAIN/$LABEL" || true
  # bootout 返回时服务可能还在卸载；worker 每 2 秒查一次 gateway 还在不在。最多等 15 秒
  for _ in $(seq 15); do
    if ! loaded && ! pgrep -f "ttsapi\.(gateway|engine_worker|scorer_worker)" >/dev/null; then echo "已停止"; return; fi
    sleep 1
  done
  echo "15 秒后还没停干净：$(pgrep -fl 'ttsapi\.(gateway|engine_worker|scorer_worker)' | cut -c1-100)" >&2
  exit 1
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status)
    if loaded; then
      launchctl print "$DOMAIN/$LABEL" | grep -E "^\s*(state|pid|last exit code) =" || true
      curl -sf -m 3 "$(url)/health" && echo || echo "/health 没有响应"
    else
      echo "没有在运行"
    fi ;;
  logs) tail -n 50 -f "$LOG" ;;
  *) echo "用法：$0 start|stop|restart|status|logs" >&2; exit 2 ;;
esac
