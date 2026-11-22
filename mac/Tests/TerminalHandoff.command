#!/bin/bash
set -euo pipefail
mkdir -p "$HOME/Library/Caches/TTSStudio"
printf 'TERMINAL_HANDOFF_OK\nHOME=%s\n' "$HOME" > "$HOME/Library/Caches/TTSStudio/handoff-probe.log"
