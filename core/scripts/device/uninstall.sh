#!/bin/sh
# 卸载局域网设备（scripts/device/install.sh 装的）：停掉并删掉开机自启的服务，删掉 ~/.tts-core（代码、运行环境、底模、
# 令牌、缓存的权重）。主控那边在 Studio「算力」里把这台设备移除。不动：uv 的下载缓存（~/.cache/uv 或
# ~/Library/Caches/uv，要清用 uv cache clean）、装之前就有的 uv、linger 设置。
# 用法：sh ~/.tts-core/uninstall.sh（安装时复制过去的；脚本在要删的目录里，整个放在 main 里，先读完再执行）

main() {
  set -eu
  ROOT="$HOME/.tts-core"
  if [ "$(uname -s)" = Darwin ]; then
    launchctl bootout "gui/$(id -u)/com.dreaife.tts-core-worker" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/com.dreaife.tts-core-worker.plist"
  else
    systemctl --user disable --now tts-core-worker 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/tts-core-worker.service"
    systemctl --user daemon-reload
  fi
  rm -rf "$ROOT"
  echo "已卸载：服务停了，$ROOT 删了。主控那边在 Studio「算力」里移除这台设备"
}

main "$@"
