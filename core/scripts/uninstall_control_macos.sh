#!/usr/bin/env bash
# Uninstalls the local macOS TTS Studio control core installed by install_control_macos.sh.
# Defaults ROOT to the directory containing this script when it resides inside an
# installation root; uses a kernel flock shared with the installer and verifies
# plist ownership (and launchd liveness even when plist is absent) before deleting files.

main() {
  set -euo pipefail

  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  if [ -n "${TTS_CORE_ROOT:-}" ]; then
    ROOT="$TTS_CORE_ROOT"
  elif [ -d "$SCRIPT_DIR/releases" ] || [ -L "$SCRIPT_DIR/current" ] || [ -f "$SCRIPT_DIR/tts-core.env" ]; then
    ROOT="$SCRIPT_DIR"
  else
    ROOT="$HOME/Library/Application Support/TTSStudio/core"
  fi

  PURGE=0
  FORCE=0

  while [ $# -gt 0 ]; do
    case "$1" in
      --root)
        [ $# -ge 2 ] || { echo "Missing value for --root" >&2; exit 2; }
        ROOT="$2"
        shift 2
        ;;
      --purge)
        PURGE=1
        shift
        ;;
      --force)
        FORCE=1
        shift
        ;;
      *)
        echo "Unknown argument: $1" >&2
        exit 2
        ;;
    esac
  done

  LABEL="com.dreaife.tts-core"
  LAUNCH_AGENTS_DIR="${TTS_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
  SERVICE="$LAUNCH_AGENTS_DIR/$LABEL.plist"
  LOCK_FILE="$LAUNCH_AGENTS_DIR/.${LABEL}.lock"
  LAUNCHCTL="${TTS_LAUNCHCTL_BIN:-launchctl}"
  HELPER_PY="$(command -v python3 || echo /usr/bin/python3)"
  LOCK_HELD=0

  trap release_lock EXIT
  trap 'exit 1' INT TERM HUP

  acquire_lock

  if [ -f "$SERVICE" ]; then
    if [ "$FORCE" -eq 0 ]; then
      check_plist_ownership "$HELPER_PY" "$SERVICE" "$ROOT" || exit 1
    fi
    stop_launchd_service || exit 1
    rm -f "$SERVICE"
  else
    # Even when the plist file is absent, check whether launchd still has the service loaded.
    if [ "${TTS_SKIP_LAUNCHCTL:-0}" != "1" ] && [ "$(uname -s)" = "Darwin" ]; then
      if "$LAUNCHCTL" print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
        if [ "$FORCE" -eq 0 ]; then
          echo "Service $LABEL is still loaded in launchd, but $SERVICE is missing so ownership cannot be verified. Preserving files; pass --force to override." >&2
          exit 1
        fi
        stop_launchd_service || exit 1
      fi
    fi
  fi

  if [ "$PURGE" -eq 1 ]; then
    rm -rf "$ROOT"
    echo "Uninstalled control core and purged $ROOT."
  else
    rm -rf "$ROOT/releases" "$ROOT/current" "$ROOT/bin" "$ROOT/python" "$ROOT/uninstall.sh" "$ROOT/.rollback.json"
    echo "Uninstalled control core runtime from $ROOT (preserved data and colab)."
  fi
}

stop_launchd_service() {
  if [ "${TTS_SKIP_LAUNCHCTL:-0}" = "1" ] || [ "$(uname -s)" != "Darwin" ]; then
    return 0
  fi
  "$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  local i=0
  while "$LAUNCHCTL" print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && [ "$i" -lt 30 ]; do
    i=$((i + 1))
    sleep 1
  done
  if "$LAUNCHCTL" print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "Control service did not stop within 30 seconds; aborting file removal." >&2
    return 1
  fi
  return 0
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
  trap '' INT TERM HUP
  set +e
  if [ "${LOCK_HELD:-0}" -eq 1 ]; then
    exec 9>&-
    LOCK_HELD=0
  fi
}

check_plist_ownership() {
  local py_bin="$1"
  local plist_path="$2"
  local root="$3"
  "$py_bin" -I - "$plist_path" "$root" <<'PYEOF'
import os
import plistlib
import sys

plist_path, root = sys.argv[1:]
try:
    with open(plist_path, "rb") as fh:
        doc = plistlib.load(fh)
except Exception as exc:
    sys.stderr.write(f"Refusing to stop service: unable to parse {plist_path}: {exc}. Pass --force to override.\n")
    raise SystemExit(1)

if not isinstance(doc, dict):
    sys.stderr.write(f"Refusing to stop service: invalid plist structure in {plist_path}. Pass --force to override.\n")
    raise SystemExit(1)

root_real = os.path.realpath(os.path.expanduser(root))
candidates = []
wd = doc.get("WorkingDirectory")
if isinstance(wd, str) and wd.strip():
    candidates.append(wd.strip())
env = doc.get("EnvironmentVariables")
if isinstance(env, dict):
    pypath = env.get("PYTHONPATH")
    if isinstance(pypath, str) and pypath.strip():
        candidates.extend(part for part in pypath.split(":") if part.strip())


def is_under(path: str, base: str) -> bool:
    real = os.path.realpath(os.path.expanduser(path))
    return real == base or real.startswith(base + os.sep)


if any(is_under(c, root_real) for c in candidates):
    raise SystemExit(0)

owner = candidates[0] if candidates else "(unknown)"
sys.stderr.write(
    f"Refusing to stop service: {plist_path} belongs to {owner}, not {root_real}. Pass --force to override.\n"
)
raise SystemExit(1)
PYEOF
}

main "$@"
