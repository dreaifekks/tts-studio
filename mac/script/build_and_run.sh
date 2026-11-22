#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
pkill -x TTSStudio >/dev/null 2>&1 || true
xcodebuild -project TTSStudio.xcodeproj -scheme TTSStudio -configuration Debug -derivedDataPath "$HOME/Library/Caches/TTSStudio/Debug.noindex" build
APP_BUNDLE="$HOME/Library/Caches/TTSStudio/Debug.noindex/Build/Products/Debug/TTSStudio.app"
case "${1:-run}" in
  run) open -n "$APP_BUNDLE" ;;
  --verify) open -n "$APP_BUNDLE"; sleep 1; pgrep -x TTSStudio >/dev/null ;;
  --debug) lldb -- "$APP_BUNDLE/Contents/MacOS/TTSStudio" ;;
  --logs) open -n "$APP_BUNDLE"; /usr/bin/log stream --info --style compact --predicate 'process == "TTSStudio"' ;;
  --telemetry) open -n "$APP_BUNDLE"; /usr/bin/log stream --info --style compact --predicate 'subsystem == "com.dreaife.tts-studio"' ;;
  *) echo 'usage: build_and_run.sh [--verify|--debug|--logs|--telemetry]' >&2; exit 2 ;;
esac
