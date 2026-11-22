#!/usr/bin/env bash
# Atomic installer and updater for the local macOS TTS Studio control core.
# Installs each release into "$ROOT/releases/<timestamp>/" and atomically
# switches "$ROOT/current" and the launchd user agent after strict health
# verification. Uses a kernel flock for cross-process mutual exclusion and
# preserves existing plist/env configuration across takeovers and upgrades.

main() {
  set -euo pipefail

  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  BUNDLE_DIR="$SCRIPT_DIR/core-bundle"
  ROOT="${TTS_CORE_ROOT:-$HOME/Library/Application Support/TTSStudio/core}"
  PORT=""
  PORT_EXPLICIT=0
  ENGINES="v2pro,v2proplus"
  SKIP_HEAVY=0
  ALLOW_UPSTREAM_GSV=0

  while [ $# -gt 0 ]; do
    case "$1" in
      --bundle)
        [ $# -ge 2 ] || { echo "Missing value for --bundle" >&2; exit 2; }
        BUNDLE_DIR="$2"
        shift 2
        ;;
      --root)
        [ $# -ge 2 ] || { echo "Missing value for --root" >&2; exit 2; }
        ROOT="$2"
        shift 2
        ;;
      --port)
        [ $# -ge 2 ] || { echo "Missing value for --port" >&2; exit 2; }
        PORT="$2"
        PORT_EXPLICIT=1
        shift 2
        ;;
      --engines)
        [ $# -ge 2 ] || { echo "Missing value for --engines" >&2; exit 2; }
        ENGINES="$2"
        shift 2
        ;;
      --skip-heavy)
        SKIP_HEAVY=1
        shift
        ;;
      --allow-upstream-gsv)
        ALLOW_UPSTREAM_GSV=1
        shift
        ;;
      *)
        echo "Unknown argument: $1" >&2
        exit 2
        ;;
    esac
  done

  if [ "$PORT_EXPLICIT" -eq 1 ]; then
    case "$PORT" in
      ''|*[!0-9]*)
        echo "Invalid port: $PORT" >&2
        exit 2
        ;;
    esac
    if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
      echo "Port out of range (1-65535): $PORT" >&2
      exit 2
    fi
  fi

  LABEL="com.dreaife.tts-core"
  LAUNCH_AGENTS_DIR="${TTS_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
  SERVICE="$LAUNCH_AGENTS_DIR/$LABEL.plist"
  LOCK_FILE="$LAUNCH_AGENTS_DIR/.${LABEL}.lock"
  LAUNCHCTL="${TTS_LAUNCHCTL_BIN:-launchctl}"
  ENV_FILE="$ROOT/tts-core.env"
  ROLLBACK_FILE="$ROOT/.rollback.json"

  HELPER_PY="$(command -v python3 || echo /usr/bin/python3)"
  TMP="$(mktemp -d)"
  REL=""
  RECOVERED_REL=""
  OLD=""
  DONE=""
  SWITCHING=""
  ENV_EXISTED=0
  LOCK_HELD=0

  trap cleanup EXIT
  trap 'exit 1' INT TERM HUP

  acquire_lock

  # If a previous interrupted run left an incomplete rollback marker, recover first.
  if [ -f "$ROLLBACK_FILE" ]; then
    recover_pending_rollback
  fi

  # Step 1: Check local macOS environment, existing plist integrity, and prerequisites.
  progress 1 6 0.05 "检查本机环境"
  export PATH="/opt/homebrew/bin:$ROOT/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

  if [ "$(uname -s)" != "Darwin" ]; then
    echo "Unsupported OS: this installer requires macOS." >&2
    exit 1
  fi
  if [ "$(uname -m)" != "arm64" ]; then
    echo "Unsupported architecture ($(uname -m)): Apple Silicon (arm64) is required." >&2
    exit 1
  fi

  if ! command -v git >/dev/null 2>&1 || { [ "$(command -v git)" = "/usr/bin/git" ] && ! xcode-select -p >/dev/null 2>&1; }; then
    echo "Missing git / Xcode Command Line Tools. Run 'xcode-select --install' in Terminal and retry." >&2
    exit 1
  fi

  if [ ! -x /opt/homebrew/bin/ffmpeg ] && [ ! -x /usr/local/bin/ffmpeg ] && ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Missing ffmpeg. Run 'brew install ffmpeg' in Terminal and retry." >&2
    exit 1
  fi

  BUNDLE_ENV="$BUNDLE_DIR/bundle.env"
  [ -f "$BUNDLE_ENV" ] || BUNDLE_ENV="$BUNDLE_DIR/code.env"
  CORE_BUNDLE="$BUNDLE_DIR/core-head.bundle"
  [ -f "$CORE_BUNDLE" ] || CORE_BUNDLE="$BUNDLE_DIR/core.bundle"
  if [ ! -f "$BUNDLE_ENV" ] || [ ! -f "$CORE_BUNDLE" ]; then
    echo "Missing installer bundle files in: $BUNDLE_DIR" >&2
    exit 1
  fi

  # Guard against $ROOT/current being a regular directory or file instead of a symlink.
  if [ -e "$ROOT/current" ] && [ ! -L "$ROOT/current" ]; then
    echo "Refusing to overwrite non-symlink path: $ROOT/current" >&2
    exit 1
  fi

  # Validate existing service plist and extract legacy paths before making any changes.
  OLD_PLIST_WD=""
  OLD_PLIST_WEB_DIR=""
  OLD_PLIST_GSV_ROOT=""
  OLD_PLIST_GSV_V5_ROOT=""
  if [ -f "$SERVICE" ]; then
    PLIST_META_FILE="$TMP/plist_meta.env"
    inspect_existing_plist "$HELPER_PY" "$SERVICE" "$PLIST_META_FILE" || {
      echo "Existing service plist is unreadable or invalid: $SERVICE" >&2
      exit 1
    }
    # shellcheck disable=SC1090
    . "$PLIST_META_FILE"
  fi

  if [ -L "$ROOT/current" ]; then
    OLD="$(readlink "$ROOT/current")"
  fi

  # Remove leftover directories from previously interrupted installs (never touching $OLD or active plist target).
  if [ -d "$ROOT/releases" ]; then
    for d in "$ROOT"/releases/*; do
      [ -e "$d" ] || continue
      if [ -n "$OLD" ] && [ "$d" = "$OLD" ]; then
        continue
      fi
      if [ -f "$SERVICE" ] && grep -qF "$d/" "$SERVICE" 2>/dev/null; then
        continue
      fi
      rm -rf "$d"
    done
  fi

  STAMP="$(date +%Y%m%d-%H%M%S)"
  REL="$ROOT/releases/$STAMP"
  if [ -e "$REL" ] || { [ -n "$RECOVERED_REL" ] && [ "$REL" = "$RECOVERED_REL" ]; }; then
    REL="$ROOT/releases/$STAMP-$$"
    suffix=1
    while [ -e "$REL" ] || { [ -n "$RECOVERED_REL" ] && [ "$REL" = "$RECOVERED_REL" ]; }; do
      REL="$ROOT/releases/$STAMP-$$-$suffix"
      suffix=$((suffix + 1))
    done
  fi

  GSV="$REL/GPT-SoVITS"
  PY="$GSV/.venv/bin/python"
  GSV_V5_COMMIT="${GSV_V5_COMMIT:-6d51ffceafbf240145619af4c92408a6e54b7924}"
  GSV_V5="$REL/GPT-SoVITS-v5"
  PY_V5="$GSV_V5/.venv/bin/python"

  mkdir -p "$ROOT/bin" "$ROOT/data" "$ROOT/logs" "$ROOT/colab" "$ROOT/datasets" "$ROOT/training" "$ROOT/weights" "$ROOT/releases" "$REL"

  if [ "$SKIP_HEAVY" -eq 0 ] && ! command -v uv >/dev/null 2>&1; then
    ts "Installing uv into $ROOT/bin"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$ROOT/bin" UV_NO_MODIFY_PATH=1 sh
  fi
  export UV_PYTHON_INSTALL_DIR="$ROOT/python"
  export UV_PYTHON_BIN_DIR="$ROOT/bin"
  export UV_PYTHON_PREFERENCE=only-managed

  # Step 2: Unpack control core source bundle and web UI bundle (preserving old web UI when omitted).
  progress 2 6 0.15 "解包主控代码"
  VERSION=""
  CORE_VERSION=""
  CORE_COMMIT=""
  GSV_BASE="bf81cdb14a38b674b6e9996dabc97340bc9978d2"
  GSV_BRANCH="tts-studio"
  GSV_PATCH_REL="patches/gptsovits-bf81cdb.patch"
  GSV_PATCH_SHA256=""
  HAS_WEB_DIST="0"
  # shellcheck disable=SC1090
  . "$BUNDLE_ENV"
  CORE_VERSION="${VERSION:-$CORE_VERSION}"
  if [ "$BUNDLE_ENV" = "$BUNDLE_DIR/code.env" ] && [ -z "$GSV_PATCH_SHA256" ]; then
    GSV_BRANCH=tts-studio
  fi

  if [ -z "$CORE_VERSION" ] || [ -z "$CORE_COMMIT" ]; then
    echo "Invalid bundle manifest: VERSION and CORE_COMMIT are required." >&2
    exit 1
  fi

  git init -q "$REL/core"
  git -C "$REL/core" fetch -q --tags "$CORE_BUNDLE" HEAD
  git -C "$REL/core" checkout -q -B core "$CORE_COMMIT"
  if git -C "$REL/core" remote get-url origin >/dev/null 2>&1; then
    git -C "$REL/core" remote set-url origin https://github.com/dreaifekks/tts-studio.git
  else
    git -C "$REL/core" remote add origin https://github.com/dreaifekks/tts-studio.git
  fi
  ts "Unpacked core $CORE_VERSION ($CORE_COMMIT)"

  if [ -f "$BUNDLE_DIR/web-dist.tar.gz" ]; then
    mkdir -p "$REL/web/dist"
    tar -xzf "$BUNDLE_DIR/web-dist.tar.gz" -C "$REL/web/dist"
    ts "Unpacked web UI to $REL/web/dist"
  else
    PREV_WEB_SRC=""
    if [ -n "$OLD" ] && [ -f "$OLD/web/dist/index.html" ]; then
      PREV_WEB_SRC="$OLD/web/dist"
    elif [ -n "$OLD_PLIST_WEB_DIR" ] && [ -f "$OLD_PLIST_WEB_DIR/index.html" ]; then
      PREV_WEB_SRC="$OLD_PLIST_WEB_DIR"
    elif [ -n "$OLD_PLIST_WD" ] && [ -f "$OLD_PLIST_WD/../web/dist/index.html" ]; then
      PREV_WEB_SRC="$OLD_PLIST_WD/../web/dist"
    fi
    if [ -n "$PREV_WEB_SRC" ]; then
      mkdir -p "$REL/web"
      cp -R "$PREV_WEB_SRC" "$REL/web/dist"
      ts "Preserved existing web UI from $PREV_WEB_SRC"
    fi
  fi

  # Step 3: Prepare GPT-SoVITS repository and hardlink reusable models.
  progress 3 6 0.30 "准备语音引擎"
  GSV_PATCH="$REL/core/${GSV_PATCH_REL:-patches/gptsovits-bf81cdb.patch}"
  if [ ! -f "$GSV_PATCH" ]; then
    if [ "$ALLOW_UPSTREAM_GSV" -ne 1 ]; then
      echo "Missing gptsovits-bf81cdb.patch ($GSV_PATCH). Pass --allow-upstream-gsv to use the upstream base." >&2
      exit 1
    fi
    GSV_PATCH=""
  fi

  GSV_SEED_DIR=""
  if [ -n "$OLD" ] && [ -e "$OLD/GPT-SoVITS/.git" ]; then
    GSV_SEED_DIR="$OLD/GPT-SoVITS"
  elif [ -n "$OLD_PLIST_GSV_ROOT" ] && [ -e "$OLD_PLIST_GSV_ROOT/.git" ]; then
    GSV_SEED_DIR="$OLD_PLIST_GSV_ROOT"
  fi
  if [ -n "$GSV_SEED_DIR" ]; then
    git clone -q --no-checkout "$GSV_SEED_DIR" "$GSV"
  else
    git init -q "$GSV"
  fi

  if [ "$SKIP_HEAVY" -eq 0 ]; then
    . "$REL/core/scripts/gptsovits.sh"
    gsv_fetch_base "$GSV" "$GSV_BASE"
  fi
  if git -C "$GSV" cat-file -e "$GSV_BASE^{commit}" 2>/dev/null; then
    if [ "$SKIP_HEAVY" -eq 1 ]; then
      . "$REL/core/scripts/gptsovits.sh"
    fi
    gsv_checkout "$GSV" "$GSV_BASE" "$GSV_BRANCH" "$GSV_PATCH" "$GSV_PATCH_SHA256"
    ts "GPT-SoVITS ready: $(git -C "$GSV" log --oneline -1)"
  else
    ts "Skipped remote GPT-SoVITS fetch (--skip-heavy)"
  fi

  if [ -n "$GSV_SEED_DIR" ] && [ -d "$GSV_SEED_DIR" ]; then
    {
      if [ -f "$REL/core/scripts/pretrained.sha256" ]; then
        awk '!/^#/ && NF >= 3 && $1 != "v5turbo" && $1 != "v5dev" {print $3}' "$REL/core/scripts/pretrained.sha256"
      fi
      if [ -d "$GSV_SEED_DIR/GPT_SoVITS/text/G2PWModel" ]; then
        (cd "$GSV_SEED_DIR" && find GPT_SoVITS/text/G2PWModel -type f)
      fi
    } | while IFS= read -r rel_path; do
      [ -n "$rel_path" ] && [ -f "$GSV_SEED_DIR/$rel_path" ] || continue
      mkdir -p "$(dirname "$GSV/$rel_path")"
      ln -f "$GSV_SEED_DIR/$rel_path" "$GSV/$rel_path" 2>/dev/null || cp -p "$GSV_SEED_DIR/$rel_path" "$GSV/$rel_path"
    done
  fi

  if wants_v5; then
    GSV_V5_SEED_DIR=""
    if [ -n "$OLD" ] && [ -d "$OLD/GPT-SoVITS-v5/.git" ]; then
      GSV_V5_SEED_DIR="$OLD/GPT-SoVITS-v5"
      git clone -q --no-checkout "$OLD/GPT-SoVITS-v5" "$GSV_V5"
    elif [ -n "$OLD_PLIST_GSV_V5_ROOT" ] && { [ -d "$OLD_PLIST_GSV_V5_ROOT/.git" ] || [ -f "$OLD_PLIST_GSV_V5_ROOT/.git" ]; }; then
      GSV_V5_SEED_DIR="$OLD_PLIST_GSV_V5_ROOT"
      git clone -q --no-checkout "$OLD_PLIST_GSV_V5_ROOT" "$GSV_V5"
    else
      git init -q "$GSV_V5"
    fi
    if [ "$SKIP_HEAVY" -eq 0 ]; then
      git -C "$GSV_V5" cat-file -e "${GSV_V5_COMMIT}^{commit}" 2>/dev/null || \
        git -C "$GSV_V5" fetch -q --depth 1 https://github.com/RVC-Boss/GPT-SoVITS.git "$GSV_V5_COMMIT"
      git -C "$GSV_V5" checkout -q -f "$GSV_V5_COMMIT"
      ts "GPT-SoVITS V5 ready: $(git -C "$GSV_V5" log --oneline -1)"
    else
      if git -C "$GSV_V5" cat-file -e "${GSV_V5_COMMIT}^{commit}" 2>/dev/null; then
        git -C "$GSV_V5" checkout -q -f "$GSV_V5_COMMIT"
      fi
      ts "Skipped remote GPT-SoVITS V5 fetch (--skip-heavy)"
    fi
    if [ -n "$GSV_V5_SEED_DIR" ] && [ -d "$GSV_V5_SEED_DIR" ] && [ -f "$REL/core/scripts/pretrained.sha256" ]; then
      awk '!/^#/ && NF >= 3 && ($1 == "v5turbo" || $1 == "v5dev") {print $3}' "$REL/core/scripts/pretrained.sha256" | \
        while IFS= read -r rel_path; do
          [ -n "$rel_path" ] && [ -f "$GSV_V5_SEED_DIR/$rel_path" ] || continue
          mkdir -p "$(dirname "$GSV_V5/$rel_path")"
          ln -f "$GSV_V5_SEED_DIR/$rel_path" "$GSV_V5/$rel_path" 2>/dev/null || cp -p "$GSV_V5_SEED_DIR/$rel_path" "$GSV_V5/$rel_path"
        done
    fi
  fi

  # Step 4: Install Python runtime environment, .pth files, and NLTK data.
  progress 4 6 0.50 "安装运行环境"
  if [ "$SKIP_HEAVY" -eq 0 ]; then
    NLTK_SRC=""
    if [ -n "$OLD" ] && [ -d "$OLD/GPT-SoVITS/.venv/nltk_data" ]; then
      NLTK_SRC="$OLD/GPT-SoVITS/.venv/nltk_data"
    fi
    if wants_v5; then
      GSV_ROOT="$GSV" GSV_V5_ROOT="$GSV_V5" GSV_V5_COMMIT="$GSV_V5_COMMIT" ENGINES="$ENGINES" \
        STEPS=venv,pth,nltk NLTK_DATA_SRC="$NLTK_SRC" bash "$REL/core/scripts/install_macos.sh"
    else
      GSV_ROOT="$GSV" ENGINES="$ENGINES" STEPS=venv,pth,nltk NLTK_DATA_SRC="$NLTK_SRC" bash "$REL/core/scripts/install_macos.sh"
    fi
  else
    ts "Skipped Python venv installation (--skip-heavy)"
  fi

  # Step 5: Download and verify pretrained base models for selected engines.
  progress 5 6 0.78 "下载并校验基础模型"
  if [ "$SKIP_HEAVY" -eq 0 ]; then
    if wants_v5; then
      GSV_ROOT="$GSV" GSV_V5_ROOT="$GSV_V5" GSV_V5_COMMIT="$GSV_V5_COMMIT" STEPS=pretrained ENGINES="$ENGINES" \
        PRETRAINED=hf bash "$REL/core/scripts/install_macos.sh"
    else
      GSV_ROOT="$GSV" STEPS=pretrained ENGINES="$ENGINES" PRETRAINED=hf bash "$REL/core/scripts/install_macos.sh"
    fi
  else
    ts "Skipped pretrained model download (--skip-heavy)"
  fi

  # Step 6: Merge configuration, register launchd agent, verify health, and commit atomically.
  progress 6 6 0.92 "启动本机主控服务"
  if [ -x "$PY" ]; then
    HELPER_PY="$PY"
  fi

  DEFAULT_MACHINE_NAME="$(scutil --get ComputerName 2>/dev/null || hostname -s)"

  if [ -f "$ENV_FILE" ]; then
    ENV_EXISTED=1
    cp -p "$ENV_FILE" "$REL/tts-core.env.prev"
  fi
  if [ -f "$SERVICE" ]; then
    cp -p "$SERVICE" "$REL/service.prev"
  fi

  # Enter the uninterruptible switch phase: write rollback marker and announce @@SWITCHING.
  echo "@@SWITCHING"
  write_rollback_marker "$HELPER_PY" "$ROLLBACK_FILE" "$REL" "$OLD" "$SERVICE" "$ENV_FILE" "$ENV_EXISTED"
  SWITCHING=1

  MERGE_META="$TMP/merged_endpoint.env"
  V5_ENV_ROOT=""
  V5_ENV_PY=""
  if wants_v5; then
    V5_ENV_ROOT="$GSV_V5"
    V5_ENV_PY="$PY_V5"
  fi
  merge_env_file "$HELPER_PY" "$ENV_FILE" "$SERVICE" "$ROOT" "$REL" "$GSV" "$PY" "$PORT" "$PORT_EXPLICIT" "$DEFAULT_MACHINE_NAME" "$MERGE_META" "$V5_ENV_ROOT" "$V5_ENV_PY"
  LISTEN_HOST="0.0.0.0"
  # shellcheck disable=SC1090
  . "$MERGE_META"

  case "${LISTEN_HOST:-}" in
    ""|"0.0.0.0"|"::"|"[::]") PROBE_HOST="127.0.0.1" ;;
    *) PROBE_HOST="$LISTEN_HOST" ;;
  esac

  stop_service
  if [ "$SKIP_HEAVY" -eq 0 ]; then
    port_free "$PROBE_HOST" "$PORT" || { echo "Port $PORT on $PROBE_HOST is already in use after stopping the previous service." >&2; exit 1; }
  fi
  write_service "$HELPER_PY" "$ENV_FILE" "$SERVICE" "$ROOT" "$REL" "$PY" "$LABEL"
  start_service
  verify_health "$HELPER_PY" "$PROBE_HOST" "$PORT" "$CORE_COMMIT" "$CORE_VERSION" || {
    echo "Control service failed health check; inspect $ROOT/logs/gateway.log" >&2
    exit 1
  }

  # Install uninstall.sh atomically before committing $ROOT/current.
  UNINSTALL_SRC=""
  if [ -f "$REL/core/scripts/uninstall_control_macos.sh" ]; then
    UNINSTALL_SRC="$REL/core/scripts/uninstall_control_macos.sh"
  elif [ -f "$SCRIPT_DIR/uninstall_control_macos.sh" ]; then
    UNINSTALL_SRC="$SCRIPT_DIR/uninstall_control_macos.sh"
  fi
  if [ -n "$UNINSTALL_SRC" ]; then
    cp "$UNINSTALL_SRC" "$ROOT/.uninstall.sh.tmp.$$"
    chmod +x "$ROOT/.uninstall.sh.tmp.$$"
    mv -f "$ROOT/.uninstall.sh.tmp.$$" "$ROOT/uninstall.sh"
  fi

  # Atomically switch $ROOT/current via os.symlink + os.replace.
  atomic_switch_current "$HELPER_PY" "$ROOT" "$REL"

  # Commit point reached: disarm rollback and clear rollback marker.
  SWITCHING=""
  DONE=1
  rm -f "$ROLLBACK_FILE" "$REL/service.prev" "$REL/tts-core.env.prev"

  # Remove only the previous release recorded for this run (and only if no longer referenced).
  if [ -n "$OLD" ] && [ "$OLD" != "$REL" ]; then
    case "$OLD" in
      "$ROOT/releases/"*)
        if [ ! -f "$SERVICE" ] || ! grep -qF "$OLD/" "$SERVICE" 2>/dev/null; then
          rm -rf "$OLD"
        fi
        ;;
    esac
  fi

  ts "Control core installed: $REL (version $CORE_VERSION, port $PORT)"
  echo "@@DONE $CORE_VERSION $PORT"
}

progress() {
  echo "@@PROGRESS $1 $2 $3 $4"
}

ts() {
  echo "[$(date +%H:%M:%S)] $*"
}

wants_v5() {
  case ",$ENGINES," in
    *,v5turbo,*|*,v5dev,*) return 0 ;;
    *) return 1 ;;
  esac
}

acquire_lock() {
  mkdir -p "$LAUNCH_AGENTS_DIR"
  if [ -d "$LOCK_FILE" ]; then
    rm -rf "$LOCK_FILE" 2>/dev/null || true
  fi
  exec 9>>"$LOCK_FILE"
  if ! "$HELPER_PY" -I -c '
import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
' 2>/dev/null; then
    local holder=""
    holder="$(cat "$LOCK_FILE" 2>/dev/null || true)"
    exec 9>&-
    if [ -n "$holder" ]; then
      echo "已有另一安装或卸载正在进行 (PID $holder)" >&2
    else
      echo "已有另一安装或卸载正在进行" >&2
    fi
    exit 1
  fi
  "$HELPER_PY" -I -c '
import os, sys
os.ftruncate(9, 0)
os.lseek(9, 0, os.SEEK_SET)
os.write(9, (sys.argv[1] + "\n").encode())
' "$$" 2>/dev/null || true
  LOCK_HELD=1
}

release_lock() {
  if [ "${LOCK_HELD:-0}" -eq 1 ]; then
    exec 9>&-
    LOCK_HELD=0
  fi
}

recover_pending_rollback() {
  ts "Found pending rollback marker ($ROLLBACK_FILE); restoring previous service before continuing" >&2
  local rec_env="$TMP/rollback_recover.env"
  if ! "$HELPER_PY" -I - "$ROLLBACK_FILE" "$rec_env" <<'PYEOF'
import json
import shlex
import sys

marker, out_path = sys.argv[1:]
with open(marker, encoding="utf-8") as fh:
    doc = json.load(fh)
if not isinstance(doc, dict):
    raise SystemExit("invalid rollback marker")

with open(out_path, "w", encoding="utf-8") as out:
    for key, env_key in [
        ("release", "RB_RELEASE"),
        ("previous_current", "RB_PREV_CURRENT"),
        ("service_path", "RB_SERVICE_PATH"),
        ("service_prev", "RB_SERVICE_PREV"),
        ("env_path", "RB_ENV_PATH"),
        ("env_prev", "RB_ENV_PREV"),
    ]:
        val = str(doc.get(key) or "")
        out.write(f"{env_key}={shlex.quote(val)}\n")
    out.write(f"RB_ENV_EXISTED={1 if doc.get('env_existed') else 0}\n")
    out.write(f"RB_SERVICE_EXISTED={1 if doc.get('service_existed') else 0}\n")
PYEOF
  then
    echo "Pending rollback marker $ROLLBACK_FILE is unreadable; inspect backups before retrying." >&2
    exit 1
  fi

  local RB_RELEASE="" RB_PREV_CURRENT="" RB_SERVICE_PATH="" RB_SERVICE_PREV="" RB_ENV_PATH="" RB_ENV_PREV="" RB_ENV_EXISTED=0 RB_SERVICE_EXISTED=0
  # shellcheck disable=SC1090
  . "$rec_env"

  stop_service || true

  if [ -n "$RB_PREV_CURRENT" ] && [ -e "$RB_PREV_CURRENT" ]; then
    atomic_switch_current "$HELPER_PY" "$ROOT" "$RB_PREV_CURRENT"
  fi

  if [ -n "$RB_ENV_PREV" ] && [ -f "$RB_ENV_PREV" ] && [ -n "$RB_ENV_PATH" ]; then
    cp -p "$RB_ENV_PREV" "$RB_ENV_PATH"
  elif [ "$RB_ENV_EXISTED" -eq 0 ] && [ -n "$RB_ENV_PATH" ]; then
    rm -f "$RB_ENV_PATH"
  elif [ "$RB_ENV_EXISTED" -eq 1 ]; then
    echo "Pending rollback missing env backup ($RB_ENV_PREV); aborting to protect configuration." >&2
    exit 1
  fi

  if [ "$RB_SERVICE_EXISTED" -eq 1 ]; then
    if [ -z "$RB_SERVICE_PREV" ] || [ ! -f "$RB_SERVICE_PREV" ]; then
      echo "Pending rollback missing service backup ($RB_SERVICE_PREV); aborting to protect service." >&2
      exit 1
    fi
    cp -p "$RB_SERVICE_PREV" "$RB_SERVICE_PATH"
    if ! start_service; then
      echo "Failed to restart previous service during pending rollback recovery; keeping $ROLLBACK_FILE and backups." >&2
      exit 1
    fi
  elif [ -n "$RB_SERVICE_PATH" ] && [ -f "$RB_SERVICE_PATH" ]; then
    rm -f "$RB_SERVICE_PATH"
  fi

  rm -f "$ROLLBACK_FILE"
  RECOVERED_REL="$RB_RELEASE"
  if [ -n "$RB_RELEASE" ] && [ "$RB_RELEASE" != "$RB_PREV_CURRENT" ]; then
    case "$RB_RELEASE" in
      "$ROOT/releases/"*) rm -rf "$RB_RELEASE" ;;
    esac
  fi
  ts "Pending rollback recovered successfully" >&2
}

inspect_existing_plist() {
  local py_bin="$1"
  local plist_path="$2"
  local out_file="$3"
  "$py_bin" -I - "$plist_path" "$out_file" <<'PYEOF'
import os
import plistlib
import shlex
import sys


def strip_outer_quotes(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


plist_path, out_file = sys.argv[1:]
with open(plist_path, "rb") as fh:
    doc = plistlib.load(fh)
if not isinstance(doc, dict):
    raise SystemExit("plist root must be a dictionary")
env = doc.get("EnvironmentVariables")
if env is not None and not isinstance(env, dict):
    raise SystemExit("EnvironmentVariables must be a dictionary")
env = env or {}

wd = doc.get("WorkingDirectory")
wd_str = os.path.expanduser(wd.strip()) if isinstance(wd, str) and wd.strip() else ""
web_raw = env.get("TTSAPI_WEB_DIR")
web_str = os.path.expanduser(strip_outer_quotes(str(web_raw))) if web_raw is not None and str(web_raw).strip() else ""
gsv_raw = env.get("TTSAPI_GSV_ROOT")
gsv_str = os.path.expanduser(strip_outer_quotes(str(gsv_raw))) if gsv_raw is not None and str(gsv_raw).strip() else ""
gsv_v5_raw = env.get("TTSAPI_GSV_V5_ROOT")
gsv_v5_str = os.path.expanduser(strip_outer_quotes(str(gsv_v5_raw))) if gsv_v5_raw is not None and str(gsv_v5_raw).strip() else ""

with open(out_file, "w", encoding="utf-8") as out:
    out.write(f"OLD_PLIST_WD={shlex.quote(wd_str)}\n")
    out.write(f"OLD_PLIST_WEB_DIR={shlex.quote(web_str)}\n")
    out.write(f"OLD_PLIST_GSV_ROOT={shlex.quote(gsv_str)}\n")
    out.write(f"OLD_PLIST_GSV_V5_ROOT={shlex.quote(gsv_v5_str)}\n")
PYEOF
}

write_rollback_marker() {
  local py_bin="$1"
  local marker="$2"
  local rel="$3"
  local old="$4"
  local service="$5"
  local env_file="$6"
  local env_existed="$7"
  "$py_bin" -I - "$marker" "$rel" "$old" "$service" "$env_file" "$env_existed" "$$" <<'PYEOF'
import json
import os
import sys
from datetime import datetime, timezone

marker, rel, old, service, env_file, env_existed, pid = sys.argv[1:]
service_prev = os.path.join(rel, "service.prev")
payload = {
    "release": rel,
    "previous_current": old,
    "service_path": service,
    "service_prev": service_prev,
    "service_existed": os.path.isfile(service_prev),
    "env_path": env_file,
    "env_prev": os.path.join(rel, "tts-core.env.prev"),
    "env_existed": env_existed == "1",
    "pid": int(pid),
    "started_at": datetime.now(timezone.utc).isoformat(),
}
tmp = f"{marker}.tmp.{os.getpid()}"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2)
os.replace(tmp, marker)
PYEOF
}

atomic_switch_current() {
  local py_bin="$1"
  local root="$2"
  local rel="$3"
  "$py_bin" -I - "$root" "$rel" <<'PYEOF'
import os
import sys

root, rel = sys.argv[1:]
tmp_link = os.path.join(root, f".current.tmp.{os.getpid()}")
if os.path.lexists(tmp_link):
    os.unlink(tmp_link)
os.symlink(rel, tmp_link)
os.replace(tmp_link, os.path.join(root, "current"))
PYEOF
}

merge_env_file() {
  local py_bin="$1"
  local env_file="$2"
  local plist_path="$3"
  local root="$4"
  local rel="$5"
  local gsv="$6"
  local py_target="$7"
  local cli_port="$8"
  local port_explicit="$9"
  local default_machine="${10}"
  local out_meta="${11}"
  local gsv_v5="${12:-}"
  local py_v5="${13:-}"

  "$py_bin" -I - "$env_file" "$plist_path" "$root" "$rel" "$gsv" "$py_target" "$cli_port" "$port_explicit" "$default_machine" "$out_meta" "$gsv_v5" "$py_v5" <<'PYEOF'
import os
import plistlib
import shlex
import sys

(
    env_file,
    plist_path,
    root,
    rel,
    gsv,
    py_target,
    cli_port,
    port_explicit,
    default_machine,
    out_meta,
    gsv_v5,
    py_v5,
) = sys.argv[1:]

RUNTIME_KEYS = {
    "PATH",
    "PYTHONPATH",
    "PYTHONPYCACHEPREFIX",
    "PYTHONUNBUFFERED",
    "PYTHONIOENCODING",
    "HOME",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "PWD",
    "_",
    "SHLVL",
}


def strip_outer_quotes(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


plist_env = {}
old_wd = ""
if os.path.isfile(plist_path):
    with open(plist_path, "rb") as fh:
        doc = plistlib.load(fh)
    wd_val = doc.get("WorkingDirectory")
    if isinstance(wd_val, str) and wd_val.strip():
        old_wd = os.path.expanduser(wd_val.strip())
    raw_env = doc.get("EnvironmentVariables") or {}
    for k, v in raw_env.items():
        if isinstance(k, str) and v is not None:
            plist_env[k.strip()] = strip_outer_quotes(str(v))

existing_lines = []
file_env = {}
if os.path.isfile(env_file):
    with open(env_file, encoding="utf-8") as fh:
        existing_lines = fh.read().splitlines()
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        file_env[k.strip()] = strip_outer_quotes(v)


def inherit(key: str, default: str) -> str:
    if key in file_env and file_env[key] != "":
        return file_env[key]
    if key in plist_env and plist_env[key] != "":
        return plist_env[key]
    return default


# If an existing legacy plist omitted TTSAPI_DATA_DIR, check core's implicit default (<old_wd>/data).
default_data_dir = os.path.join(root, "data")
if old_wd:
    legacy_data = os.path.join(old_wd, "data")
    if os.path.isfile(os.path.join(legacy_data, "core.json")) or os.path.isfile(os.path.join(legacy_data, "tts.db")):
        default_data_dir = legacy_data

resolved_port = cli_port if port_explicit == "1" else inherit("TTSAPI_PORT", "9880")
resolved_host = inherit("TTSAPI_HOST", "0.0.0.0")
releases_prefix = os.path.join(root, "releases") + os.sep
rel_web_dist = os.path.join(rel, "web", "dist")
has_rel_web = os.path.isfile(os.path.join(rel_web_dist, "index.html"))
prev_web_dir = inherit("TTSAPI_WEB_DIR", "")

desired = {
    "TTSAPI_ROLE": "control",
    "TTSAPI_HOST": resolved_host,
    "TTSAPI_PORT": resolved_port,
    "TTSAPI_WORKER_PORT_SHIFT": inherit("TTSAPI_WORKER_PORT_SHIFT", "100"),
    "TTSAPI_MACHINE_NAME": inherit("TTSAPI_MACHINE_NAME", default_machine),
    "TTSAPI_GSV_ROOT": gsv,
    "TTSAPI_PYTHON": py_target,
    "TTSAPI_DATA_DIR": inherit("TTSAPI_DATA_DIR", default_data_dir),
    "TTSAPI_COLAB_ROOT": inherit("TTSAPI_COLAB_ROOT", os.path.join(root, "colab")),
    "TTSAPI_DATASET_ROOT": inherit("TTSAPI_DATASET_ROOT", os.path.join(root, "datasets")),
    "TTSAPI_TRAIN_DIR": inherit("TTSAPI_TRAIN_DIR", os.path.join(root, "training")),
    "TTSAPI_WEIGHTS_DIR": inherit("TTSAPI_WEIGHTS_DIR", os.path.join(root, "weights")),
}
if gsv_v5:
    desired["TTSAPI_GSV_V5_ROOT"] = gsv_v5
if py_v5:
    desired["TTSAPI_V5_PYTHON"] = py_v5

if has_rel_web:
    desired["TTSAPI_WEB_DIR"] = rel_web_dist
elif (
    prev_web_dir
    and not prev_web_dir.startswith(releases_prefix)
    and os.path.isfile(os.path.join(os.path.expanduser(prev_web_dir), "index.html"))
):
    desired["TTSAPI_WEB_DIR"] = prev_web_dir

# Carry over custom TTSAPI_* keys (including TTSAPI_LIBRARY and tuning knobs) from old plist if not already in env file.
for k, v in plist_env.items():
    if k in RUNTIME_KEYS or k.startswith(("LC_", "XPC_", "__CF")):
        continue
    if k == "TTSAPI_WEB_DIR":
        continue
    if k in ("TTSAPI_GSV_V5_ROOT", "TTSAPI_V5_PYTHON") and not gsv_v5 and v.startswith(releases_prefix):
        continue
    if k.startswith("TTSAPI_") and k not in desired and k not in file_env:
        desired[k] = v

out_lines = []
seen = set()
for line in existing_lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        out_lines.append(line)
        continue
    key, raw_val = line.split("=", 1)
    key = key.strip()
    if key == "TTSAPI_WEB_DIR" and "TTSAPI_WEB_DIR" not in desired:
        if strip_outer_quotes(raw_val).startswith(releases_prefix):
            continue
        out_lines.append(f"{key}={strip_outer_quotes(raw_val)}")
        seen.add(key)
        continue
    if key in ("TTSAPI_GSV_V5_ROOT", "TTSAPI_V5_PYTHON") and key not in desired:
        if strip_outer_quotes(raw_val).startswith(releases_prefix):
            continue
        out_lines.append(f"{key}={strip_outer_quotes(raw_val)}")
        seen.add(key)
        continue
    if key in desired:
        if key not in seen:
            out_lines.append(f"{key}={desired[key]}")
            seen.add(key)
    else:
        if key not in seen:
            out_lines.append(f"{key}={strip_outer_quotes(raw_val)}")
            seen.add(key)

if not existing_lines:
    out_lines.append("# Managed by TTS Studio local control installer")

for key, value in desired.items():
    if key not in seen:
        out_lines.append(f"{key}={value}")
        seen.add(key)

tmp_file = f"{env_file}.tmp.{os.getpid()}"
with open(tmp_file, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out_lines) + "\n")
os.replace(tmp_file, env_file)

with open(out_meta, "w", encoding="utf-8") as out:
    out.write(f"PORT={shlex.quote(resolved_port)}\n")
    out.write(f"LISTEN_HOST={shlex.quote(resolved_host)}\n")
PYEOF
}

write_service() {
  local py_bin="$1"
  local env_file="$2"
  local plist_path="$3"
  local root="$4"
  local rel="$5"
  local py_target="$6"
  local label="$7"

  mkdir -p "$(dirname "$plist_path")"
  "$py_bin" -I - "$env_file" "$plist_path" "$root" "$rel" "$py_target" "$label" <<'PYEOF'
import os
import plistlib
import sys

env_file, plist_path, root, rel, py_target, label = sys.argv[1:]


def strip_outer_quotes(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


env = {}
with open(env_file, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = os.path.expanduser(strip_outer_quotes(v))

home = os.path.expanduser("~")
env["PATH"] = f"/opt/homebrew/bin:{root}/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
env["PYTHONPATH"] = os.path.join(rel, "core")
env["PYTHONPYCACHEPREFIX"] = os.path.join(rel, "runtime", "pycache")
env["PYTHONUNBUFFERED"] = "1"
env["PYTHONIOENCODING"] = "utf-8"

payload = {
    "Label": label,
    "ProgramArguments": [py_target, "-m", "ttsapi.gateway"],
    "WorkingDirectory": os.path.join(rel, "core"),
    "EnvironmentVariables": env,
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 5,
    "StandardOutPath": os.path.join(root, "logs", "gateway.log"),
    "StandardErrorPath": os.path.join(root, "logs", "gateway.log"),
}
tmp_path = f"{plist_path}.tmp.{os.getpid()}"
with open(tmp_path, "wb") as fh:
    plistlib.dump(payload, fh)
os.replace(tmp_path, plist_path)
PYEOF
}

stop_service() {
  if [ "$SKIP_HEAVY" -eq 1 ] && [ -z "${TTS_LAUNCHCTL_BIN:-}" ]; then
    return 0
  fi
  "$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  local i=0
  while "$LAUNCHCTL" print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && [ "$i" -lt 30 ]; do
    i=$((i + 1))
    sleep 1
  done
}

start_service() {
  if [ "$SKIP_HEAVY" -eq 1 ] && [ -z "${TTS_LAUNCHCTL_BIN:-}" ]; then
    return 0
  fi
  "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$SERVICE"
}

port_free() {
  local probe_host="$1"
  local port="$2"
  local i=0
  while :; do
    set +e
    curl --noproxy '*' -s -o /dev/null --max-time 2 "http://${probe_host}:${port}/"
    local c=$?
    set -e
    [ "$c" -ne 7 ] || return 0
    i=$((i + 1))
    [ "$i" -lt 10 ] || return 1
    sleep 1
  done
}

verify_health() {
  local py_bin="$1"
  local probe_host="$2"
  local port="$3"
  local expected_commit="$4"
  local expected_version="$5"

  if [ -n "${TTS_HEALTH_CHECK_CMD:-}" ]; then
    PROBE_HOST="$probe_host" PORT="$port" REL="$REL" ROOT="$ROOT" SERVICE="$SERVICE" EXPECTED_COMMIT="$expected_commit" EXPECTED_VERSION="$expected_version" \
      bash -c "$TTS_HEALTH_CHECK_CMD"
    return $?
  fi

  local health_url="${TTS_HEALTH_URL:-http://${probe_host}:${port}/health}"
  if [ "$SKIP_HEAVY" -eq 1 ] && [ -z "${TTS_HEALTH_URL:-}" ]; then
    return 0
  fi

  local attempts=60
  if [ -n "${TTS_HEALTH_ATTEMPTS:-}" ]; then
    attempts="$TTS_HEALTH_ATTEMPTS"
  fi

  local i=0
  while [ "$i" -lt "$attempts" ]; do
    local body=""
    if body="$(curl --noproxy '*' -sf --max-time 2 "$health_url" 2>/dev/null)"; then
      if HEALTH_BODY="$body" "$py_bin" -I - "$expected_commit" "$expected_version" <<'PYEOF'
import json
import os
import sys

expected_commit, expected_version = sys.argv[1:]
try:
    data = json.loads(os.environ.get("HEALTH_BODY", ""))
except Exception:
    raise SystemExit(1)
if not isinstance(data, dict) or data.get("status") != "ok":
    raise SystemExit(1)
version = str(data.get("version") or "").strip()
if not expected_version or version != expected_version:
    raise SystemExit(1)
commit = str(data.get("commit") or "").strip()
if len(commit) < 7 or not expected_commit.startswith(commit):
    raise SystemExit(1)
PYEOF
      then
        return 0
      fi
    fi
    i=$((i + 1))
    if [ "$i" -lt "$attempts" ]; then
      sleep 1
    fi
  done
  return 1
}

cleanup() {
  trap '' INT TERM HUP
  set +e
  local keep_rel_for_rollback=0
  if [ -n "${SWITCHING:-}" ]; then
    ts "Rolling back to previous control service" >&2
    local rollback_ok=1
    stop_service
    if [ -n "${OLD:-}" ] && [ -L "$ROOT/current" ]; then
      local cur_target=""
      cur_target="$(readlink "$ROOT/current" 2>/dev/null || true)"
      if [ "$cur_target" != "$OLD" ]; then
        atomic_switch_current "${HELPER_PY:-/usr/bin/python3}" "$ROOT" "$OLD" || rollback_ok=0
      fi
    fi
    if [ -n "${REL:-}" ] && [ -f "$REL/tts-core.env.prev" ]; then
      cp -p "$REL/tts-core.env.prev" "$ENV_FILE" || rollback_ok=0
    elif [ "${ENV_EXISTED:-0}" -eq 0 ]; then
      rm -f "$ENV_FILE"
    fi
    if [ -n "${REL:-}" ] && [ -f "$REL/service.prev" ]; then
      cp -p "$REL/service.prev" "$SERVICE" || rollback_ok=0
      start_service || rollback_ok=0
    else
      rm -f "$SERVICE"
    fi
    if [ "$rollback_ok" -eq 1 ]; then
      rm -f "$ROLLBACK_FILE"
    else
      # Preserve .rollback.json and the backup files inside $REL for recovery on the next run.
      keep_rel_for_rollback=1
      ts "Rollback restart failed; preserving $ROLLBACK_FILE and backups in $REL" >&2
    fi
  fi
  rm -f "$ROOT/.current.tmp.$$" "$ROOT/.uninstall.sh.tmp.$$" 2>/dev/null
  if [ -z "${DONE:-}" ] && [ -n "${REL:-}" ] && [ "$keep_rel_for_rollback" -eq 0 ]; then
    rm -rf "$REL"
  fi
  if [ -n "${TMP:-}" ]; then
    rm -rf "$TMP"
  fi
  release_lock
}

main "$@"
