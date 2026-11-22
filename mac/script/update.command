#!/bin/bash
# Prebuilt resource: the app writes only the selected tag as data.
set -uo pipefail
TAG_FILE="$HOME/Library/Caches/TTSStudio/update-tag"
CACHE="$HOME/Library/Caches/TTSStudio"
mkdir -p "$CACHE"
exec > >(tee "$CACHE/update-terminal.log") 2>&1
TAG="$(cat "$TAG_FILE")" || exit 1
rm "$TAG_FILE" || exit 1
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "TTS Studio 终端更新：$TAG"
echo "发布源码及构建使用终端的用户目录：$HOME"
if /bin/bash "$SCRIPT_DIR/update.sh" "$TAG"; then
  echo '更新已安装。请退出当前 TTS Studio，再按回车打开 /Applications/TTSStudio.app。'
  read -r _
  open /Applications/TTSStudio.app
else
  STATUS=$?
  echo "更新失败（退出码 $STATUS），原 app 未退出。日志：$CACHE/update-terminal.log"
  exit "$STATUS"
fi
