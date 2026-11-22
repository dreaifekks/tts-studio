#!/usr/bin/env bash
set -euo pipefail

GSV_ROOT="${TTSAPI_GSV_ROOT:-/opt/GPT-SoVITS}"
GSV_V5_ROOT="${TTSAPI_GSV_V5_ROOT:-/opt/GPT-SoVITS-v5}"
VENV="${GSV_ROOT}/.venv"
PY="${VENV}/bin/python"
MODELS_DIR="${TTSAPI_MODELS_DIR:-/models}"

mkdir -p \
  "${TTSAPI_DATA_DIR:-/data}" \
  "${TTSAPI_LIBRARY:-/library}" \
  "${MODELS_DIR}/pretrained" \
  "${MODELS_DIR}/v5-pretrained" \
  "${MODELS_DIR}/hf" \
  "${MODELS_DIR}/asr"

# Persist pretrained models inside the mounted /models volume
link_pretrained_dir() {
  local target_dir="$1"
  local persist_dir="$2"
  if [ -d "$(dirname "$target_dir")" ]; then
    if [ ! -L "$target_dir" ]; then
      if [ -d "$target_dir" ]; then
        cp -rn "$target_dir/." "$persist_dir/" 2>/dev/null || true
        rm -rf "$target_dir"
      fi
      ln -s "$persist_dir" "$target_dir"
    fi
  fi
}

link_pretrained_dir "${GSV_ROOT}/GPT_SoVITS/pretrained_models" "${MODELS_DIR}/pretrained"
if [ -d "${GSV_V5_ROOT}/GPT_SoVITS" ]; then
  link_pretrained_dir "${GSV_V5_ROOT}/GPT_SoVITS/pretrained_models" "${MODELS_DIR}/v5-pretrained"
fi

export HF_HOME="${HF_HOME:-${MODELS_DIR}/hf}"
export TTSAPI_ASR_MODEL="${TTSAPI_ASR_MODEL:-${MODELS_DIR}/asr/faster-whisper-large-v3-turbo}"

if [ "${TTSAPI_AUTO_DOWNLOAD_MODELS:-1}" = "1" ]; then
  ENGINES="${TTSAPI_ENGINES:-v2pro,v2proplus}"
  echo "[entrypoint] Ensuring pretrained models for engines: base,${ENGINES}"
  GSV_ROOT="${GSV_ROOT}" \
  GSV_V5_ROOT="${GSV_V5_ROOT}" \
  ENGINES="${ENGINES}" \
  PRETRAINED="hf" \
  STEPS="pretrained" \
  SKIP_CHECK="1" \
  GSV_V5_SKIP_CLONE="1" \
    bash /app/core/scripts/install_linux.sh
fi

SITE="$("${PY}" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
NV_LIBS="$(ls -d "${SITE}"/nvidia/*/lib 2>/dev/null | paste -sd: || true)"
if [ -n "${NV_LIBS}" ]; then
  export LD_LIBRARY_PATH="${NV_LIBS}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi

cd /app/core
exec "${PY}" -m ttsapi.gateway "$@"
