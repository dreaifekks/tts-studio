# Shared GPT-SoVITS checkout for the Core installers. Source with sh or bash.
GSV_UPSTREAM=https://github.com/RVC-Boss/GPT-SoVITS.git

gsv_fetch_base() {
  git -C "$1" cat-file -e "$2^{commit}" 2>/dev/null ||
    git -C "$1" fetch -q --depth 1 "$GSV_UPSTREAM" "$2"
}

gsv_checkout() {
  local repo="$1" base="$2" branch="$3" patch="$4" expected_sha="$5"
  local actual_sha
  if [ -n "$patch" ] && [ -n "$expected_sha" ]; then
    if command -v sha256sum >/dev/null 2>&1; then
      actual_sha=$(sha256sum < "$patch")
    else
      actual_sha=$(shasum -a 256 < "$patch")
    fi
    if [ "${actual_sha%% *}" != "$expected_sha" ]; then
      echo "GPT-SoVITS patch SHA-256 mismatch: $patch" >&2
      return 1
    fi
  fi
  git -C "$repo" checkout -q -f -B "$branch" "$base"
  if [ -n "$patch" ]; then
    git -C "$repo" apply --index --whitespace=nowarn "$patch"
    GIT_AUTHOR_NAME="TTS Studio" GIT_AUTHOR_EMAIL="noreply@tts-studio.local" \
      GIT_COMMITTER_NAME="TTS Studio" GIT_COMMITTER_EMAIL="noreply@tts-studio.local" \
      git -C "$repo" -c commit.gpgSign=false commit -q -m "Apply TTS Studio patch"
  fi
}

gsv_ensure_installed() (
  local repo patch="$2" stage backup=""
  local base=bf81cdb14a38b674b6e9996dabc97340bc9978d2
  mkdir -p "$1"
  repo=$(cd "$1" && pwd -P)
  if [ "$(git -C "$repo" rev-parse --show-prefix 2>/dev/null || echo invalid)" = "" ] &&
     [ "$(git -C "$repo" rev-parse --is-inside-work-tree 2>/dev/null)" = "true" ]; then
    exit 0
  fi
  # Build the patched tree before touching the target. read-tree protects any
  # pre-existing files (such as a venv or weights) in a non-Git directory.
  stage=$(mktemp -d "$(dirname "$repo")/.tts-gsv.XXXXXX")
  trap 'rm -rf "$stage"' EXIT
  git init -q "$stage"
  git -C "$stage" remote add origin "$GSV_UPSTREAM"
  gsv_fetch_base "$stage" "$base"
  gsv_checkout "$stage" "$base" tts-studio "$patch" ""
  git -C "$stage" read-tree --empty
  git --git-dir="$stage/.git" --work-tree="$repo" read-tree -m -u HEAD
  if [ -e "$repo/.git" ] || [ -L "$repo/.git" ]; then
    backup=$(mktemp -d "$(dirname "$repo")/.tts-git-backup.XXXXXX")
    mv "$repo/.git" "$backup/.git"
  fi
  if ! mv "$stage/.git" "$repo/.git"; then
    [ -z "$backup" ] || mv "$backup/.git" "$repo/.git"
    exit 1
  fi
  if [ -n "$backup" ]; then
    echo "Preserved invalid Git metadata at $backup/.git" >&2
  fi
)
