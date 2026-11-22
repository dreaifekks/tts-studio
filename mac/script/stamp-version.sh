#!/usr/bin/env bash
set -euo pipefail

cd "$SRCROOT"
REPO_ROOT="$(cd "$SRCROOT/.." && pwd)"
VERSION="$(./script/version.sh)"
REVISION="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
GSV_PATCH_REL="patches/gptsovits-bf81cdb.patch"
GSV_PATCH="$REPO_ROOT/core/$GSV_PATCH_REL"
if [ ! -f "$GSV_PATCH" ]; then
  echo "Missing $GSV_PATCH_REL in core; build from a complete checkout." >&2
  exit 1
fi
GSV_PATCH_SHA256="$(shasum -a 256 "$GSV_PATCH" | cut -d ' ' -f 1)"

plist="$TARGET_BUILD_DIR/$INFOPLIST_PATH"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$plist"
/usr/libexec/PlistBuddy -c "Set :TTSStudioRevision $REVISION" "$plist"

RES="$TARGET_BUILD_DIR/$UNLOCALIZED_RESOURCES_FOLDER_PATH"
BUNDLE_OUT="$RES/core-bundle"
mkdir -p "$RES"

cp script/update.sh "$RES/update.sh"
cp script/update.command "$RES/update.command"
cp "$REPO_ROOT/core/scripts/install_control_macos.sh" "$RES/install_control_macos.sh"
cp "$REPO_ROOT/core/scripts/uninstall_control_macos.sh" "$RES/uninstall_control_macos.sh"
chmod +x "$RES/install_control_macos.sh" "$RES/uninstall_control_macos.sh"

# Build a single-commit git bundle for HEAD:core in a temporary bare repo so
# repository refs stay untouched and version.py's git describe matches VERSION.
TMP_BUILD="$(mktemp -d "${TMPDIR:-/tmp}/tts-studio-core-bundle.XXXXXX")"
trap 'rm -rf "$TMP_BUILD"' EXIT
TMP_BARE="$TMP_BUILD/git"
STAGED_BUNDLE="$TMP_BUILD/core-bundle"
mkdir "$STAGED_BUNDLE"

HEAD_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
CORE_TREE="$(git -C "$REPO_ROOT" rev-parse "$HEAD_COMMIT:core")"
BUNDLED_PATCH_SHA256="$(git -C "$REPO_ROOT" cat-file blob "$CORE_TREE:$GSV_PATCH_REL" | shasum -a 256 | cut -d ' ' -f 1)"
if [ "$GSV_PATCH_SHA256" != "$BUNDLED_PATCH_SHA256" ]; then
  echo "The working copy of $GSV_PATCH_REL differs from the patch in HEAD:core." >&2
  exit 1
fi
OBJECTS_DIR="$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir)/objects"

git init -q --bare -b core "$TMP_BARE"
printf '%s\n' "$OBJECTS_DIR" > "$TMP_BARE/objects/info/alternates"
CORE_COMMIT="$(
  git -C "$TMP_BARE" \
    -c user.name=tts-core \
    -c user.email=tts-core@localhost \
    commit-tree "$CORE_TREE" \
    -m "tts-core $VERSION (tts-studio $HEAD_COMMIT)"
)"
git -C "$TMP_BARE" update-ref refs/heads/core "$CORE_COMMIT"
git -C "$TMP_BARE" tag "v$VERSION" "$CORE_COMMIT"
git -C "$TMP_BARE" bundle create -q "$STAGED_BUNDLE/core-head.bundle" HEAD --tags

HAS_WEB_DIST=0
if [ -f "$REPO_ROOT/web/dist/index.html" ]; then
  tar -czf "$STAGED_BUNDLE/web-dist.tar.gz" -C "$REPO_ROOT/web/dist" .
  HAS_WEB_DIST=1
fi

cat > "$STAGED_BUNDLE/bundle.env" <<EOF
VERSION=$VERSION
CORE_COMMIT=$CORE_COMMIT
GSV_BASE=bf81cdb14a38b674b6e9996dabc97340bc9978d2
GSV_BRANCH=tts-studio
GSV_PATCH_REL=$GSV_PATCH_REL
GSV_PATCH_SHA256=$GSV_PATCH_SHA256
HAS_WEB_DIST=$HAS_WEB_DIST
EOF

# Rebuild the entire payload so incremental builds cannot ship obsolete resources.
rm -rf "$BUNDLE_OUT"
mv "$STAGED_BUNDLE" "$BUNDLE_OUT"
