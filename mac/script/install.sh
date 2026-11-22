#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$ROOT_DIR/../web"
if [[ "${TTS_WEB_BUILT:-0}" != "1" && -f "$WEB_DIR/package.json" ]] && command -v npm >/dev/null 2>&1; then
  (cd "$WEB_DIR" && { [[ -d node_modules ]] || npm ci; } && npm run build)
fi
cd "$ROOT_DIR"
DERIVED_DATA="${TTS_DERIVED_DATA:-$HOME/Library/Caches/TTSStudio/Install.noindex}"
xcodebuild -project TTSStudio.xcodeproj -scheme TTSStudio -configuration Release -derivedDataPath "$DERIVED_DATA" -destination 'platform=macOS' build
echo '[3/4] 安装到 /Applications/TTSStudio.app'
APP_BUNDLE="$DERIVED_DATA/Build/Products/Release/TTSStudio.app"
STAGING="$(mktemp -d /Applications/.TTSStudio-install.XXXXXX)"
trap 'rm -rf "$STAGING"' EXIT
ditto "$APP_BUNDLE" "$STAGING/TTSStudio.app"
codesign --verify --deep --strict "$STAGING/TTSStudio.app"
rm -rf /Applications/TTSStudio.app
mv "$STAGING/TTSStudio.app" /Applications/TTSStudio.app
echo "已安装 /Applications/TTSStudio.app（$(./script/version.sh)）。正在运行的旧版本未退出，请手动重新打开。"
