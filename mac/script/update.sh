#!/usr/bin/env bash
# Called by update.command in Terminal, or directly from a shell. Never stops an app.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TAG="${1:?usage: update.sh v0.x.y}"
[[ "$TAG" =~ ^v0\.[0-9]+\.[0-9]+$ ]] || { echo '版本 tag 必须为 v0.x.y' >&2; exit 2; }
REPO_URL="https://github.com/dreaifekks/tts-studio.git"
SOURCE="$HOME/Library/Application Support/TTSStudio/release-src"
CACHE="$HOME/Library/Caches/TTSStudio"
mkdir -p "$(dirname "$SOURCE")" "$CACHE"
LOCK="$CACHE/update.lock"
mkdir "$LOCK" || { echo '已有 Mac 更新在进行' >&2; exit 1; }
trap 'rmdir "$LOCK"' EXIT
echo '[1/4] 获取 GitHub 发布源码'
if [[ ! -d "$SOURCE/.git" ]]; then
    git clone "$REPO_URL" "$SOURCE"
fi
cd "$SOURCE"
git fetch "$REPO_URL" refs/heads/master:refs/remotes/origin/master --tags
git rev-parse --verify "refs/tags/$TAG^{commit}" >/dev/null
git merge-base --is-ancestor "$TAG" origin/master
git checkout --detach "$TAG"
echo "[2/4] 构建 Release $TAG"
TTS_WEB_BUILT=0
if [[ -f web/package.json ]] && command -v npm >/dev/null 2>&1; then
    (cd web && { [[ -d node_modules ]] || npm ci; } && npm run build)
    TTS_WEB_BUILT=1
fi
if [[ -f mac/script/install.sh ]]; then
    INSTALL_SCRIPT=./mac/script/install.sh
elif [[ -f script/install.sh ]]; then
    INSTALL_SCRIPT=./script/install.sh
else
    echo "这个版本没有安装脚本：$TAG" >&2
    exit 1
fi
TTS_WEB_BUILT="$TTS_WEB_BUILT" TTS_DERIVED_DATA="$CACHE/Release.noindex" /bin/bash "$INSTALL_SCRIPT"
echo "[4/4] 更新完成：$TAG"
echo '请退出旧版，再从 /Applications/TTSStudio.app 打开新版。'
