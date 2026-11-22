#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$ROOT_DIR/.." && pwd)"
WEB_DIR="$REPO_ROOT/web"
DIST_DIR="${DIST_DIR:-$REPO_ROOT/dist}"
APP_NAME="TTSStudio"
ARCH_NAME="${ARCH_NAME:-$(uname -m)}"
APP_VERSION="${APP_VERSION:-$(bash "$ROOT_DIR/script/version.sh")}"
DMG_PATH="${DMG_PATH:-$DIST_DIR/${APP_NAME}-${APP_VERSION}-macos-${ARCH_NAME}.dmg}"
DERIVED_DATA="${TTS_DERIVED_DATA:-$HOME/Library/Caches/TTSStudio/Package.noindex}"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
NOTARIZE="${NOTARIZE:-0}"
SIGN_DMG="${SIGN_DMG:-1}"
NOTARY_TIMEOUT="${NOTARY_TIMEOUT:-30m}"

log() { printf '[package] %s\n' "$*"; }
fail() { printf '[package] error: %s\n' "$*" >&2; exit 1; }

decode_base64() {
  if /usr/bin/base64 --help 2>&1 | /usr/bin/grep -q -- '--decode'; then
    /usr/bin/base64 --decode
  else
    /usr/bin/base64 -D
  fi
}

[[ "$(uname -s)" == "Darwin" ]] || fail "macOS packaging must run on Darwin"

if [[ "$NOTARIZE" == "1" && ( -z "$CODESIGN_IDENTITY" || "$CODESIGN_IDENTITY" == "-" ) ]]; then
  fail "NOTARIZE=1 requires a Developer ID signing identity in CODESIGN_IDENTITY"
fi

# Build Web UI so stamp-version.sh embeds web-dist.tar.gz inside TTSStudio.app
if [[ "${TTS_WEB_BUILT:-0}" != "1" && -f "$WEB_DIR/package.json" ]] && command -v npm >/dev/null 2>&1; then
  log "building Web UI"
  (cd "$WEB_DIR" && { [[ -d node_modules ]] || npm ci; } && npm run build)
fi

# Respect active Xcode SDK when building on runners with an older SDK than project.pbxproj
SDK_VER="$(xcrun --sdk macosx --show-sdk-version)"
DEPLOY_TARGET="${MACOSX_DEPLOYMENT_TARGET:-}"
if [[ -z "$DEPLOY_TARGET" ]]; then
  if python3 -c 'import sys; a=tuple(map(int, sys.argv[1].split("."))); b=(27,0); sys.exit(0 if a < b else 1)' "$SDK_VER"; then
    DEPLOY_TARGET="$SDK_VER"
  fi
fi

XCODE_ARGS=(
  -project "$ROOT_DIR/TTSStudio.xcodeproj"
  -scheme "$APP_NAME"
  -configuration Release
  -derivedDataPath "$DERIVED_DATA"
  -destination "platform=macOS,arch=${ARCH_NAME}"
  ARCHS="$ARCH_NAME"
  ONLY_ACTIVE_ARCH=YES
  CODE_SIGN_IDENTITY="-"
)
if [[ -n "$DEPLOY_TARGET" ]]; then
  log "using MACOSX_DEPLOYMENT_TARGET=$DEPLOY_TARGET (active SDK $SDK_VER)"
  XCODE_ARGS+=("MACOSX_DEPLOYMENT_TARGET=$DEPLOY_TARGET")
fi

log "building $APP_NAME $APP_VERSION ($ARCH_NAME)"
xcodebuild "${XCODE_ARGS[@]}" build

BUILT_APP="$DERIVED_DATA/Build/Products/Release/$APP_NAME.app"
[[ -d "$BUILT_APP" ]] || fail "built app not found: $BUILT_APP"

STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tts-studio-dmg.XXXXXX")"
NOTARY_KEY_PATH=""
cleanup() {
  rm -rf "$STAGE_DIR"
  if [[ -n "$NOTARY_KEY_PATH" ]]; then rm -f "$NOTARY_KEY_PATH"; fi
}
trap cleanup EXIT

APP_BUNDLE="$STAGE_DIR/$APP_NAME.app"
/usr/bin/ditto "$BUILT_APP" "$APP_BUNDLE"
/usr/bin/xattr -cr "$APP_BUNDLE" 2>/dev/null || true

if [[ -n "$CODESIGN_IDENTITY" && "$CODESIGN_IDENTITY" != "-" ]]; then
  log "signing app bundle with Developer ID (Hardened Runtime)"
  /usr/bin/codesign --force --deep --options runtime --timestamp \
    --entitlements "$ROOT_DIR/App/TTSStudio.entitlements" \
    --sign "$CODESIGN_IDENTITY" \
    "$APP_BUNDLE"
else
  log "signing app bundle ad-hoc"
  /usr/bin/codesign --force --deep \
    --entitlements "$ROOT_DIR/App/TTSStudio.entitlements" \
    --sign - \
    "$APP_BUNDLE"
fi
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP_BUNDLE"

log "creating dmg: $DMG_PATH"
mkdir -p "$DIST_DIR"
rm -f "$DMG_PATH" "${DMG_PATH}.sha256"
ln -s /Applications "$STAGE_DIR/Applications"

/usr/bin/hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$STAGE_DIR" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

if [[ -n "$CODESIGN_IDENTITY" && "$CODESIGN_IDENTITY" != "-" && "$SIGN_DMG" == "1" ]]; then
  log "signing dmg"
  /usr/bin/codesign --force --timestamp --sign "$CODESIGN_IDENTITY" "$DMG_PATH"
  /usr/bin/codesign --verify --verbose=4 "$DMG_PATH"
fi

if [[ "$NOTARIZE" == "1" ]]; then
  notary_args=(submit "$DMG_PATH" --wait --timeout "$NOTARY_TIMEOUT")
  if [[ -n "${NOTARYTOOL_KEYCHAIN_PROFILE:-}" ]]; then
    notary_args+=(--keychain-profile "$NOTARYTOOL_KEYCHAIN_PROFILE")
  elif [[ -n "${APPLE_API_KEY_P8_BASE64:-}" ]]; then
    [[ -n "${APPLE_API_KEY_ID:-}" ]] || fail "APPLE_API_KEY_ID is required"
    [[ -n "${APPLE_API_ISSUER_ID:-}" ]] || fail "APPLE_API_ISSUER_ID is required"
    NOTARY_KEY_PATH="$(mktemp "${TMPDIR:-/tmp}/notary-key.XXXXXX.p8")"
    printf '%s' "$APPLE_API_KEY_P8_BASE64" | decode_base64 >"$NOTARY_KEY_PATH"
    chmod 600 "$NOTARY_KEY_PATH"
    notary_args+=(--key "$NOTARY_KEY_PATH" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER_ID")
  else
    fail "NOTARIZE=1 requires APPLE_API_KEY_P8_BASE64 or NOTARYTOOL_KEYCHAIN_PROFILE"
  fi

  log "submitting dmg for notarization"
  xcrun notarytool "${notary_args[@]}"
  log "stapling notarization ticket"
  xcrun stapler staple "$DMG_PATH"
  /usr/sbin/spctl --assess --type open --context context:primary-signature -vv "$DMG_PATH"
fi

(cd "$DIST_DIR" && shasum -a 256 "$(basename "$DMG_PATH")" > "$(basename "$DMG_PATH").sha256")
log "created $DMG_PATH ($(du -h "$DMG_PATH" | cut -f1))"
