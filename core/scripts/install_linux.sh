#!/usr/bin/env bash
# 在 Linux（Colab VM、x86_64 + NVIDIA 的机器，或没有显卡的局域网设备）上给 tts-api 装好 GPT-SoVITS 的运行环境。装完后 tts-api 只要设 TTSAPI_GSV_ROOT=<GSV_ROOT> 就能起（不需要 sitecustomize 之类的桩）。
#
# 用法：GSV_ROOT=/content/GPTSoVits scripts/install_linux.sh
#   GSV_ROOT       GPT-SoVITS 目录（必填）；缺少 Git 工作树时取固定上游提交并应用 Core 补丁
#   VENV           默认 $GSV_ROOT/.venv（和 tts-api 的默认 TTSAPI_PYTHON 一致）
#   PRETRAINED     hf（默认，从 HF 下载）| skip | 一个目录（布局同 GSV_ROOT，例如本地底模缓存）
#                  都按 scripts/pretrained.sha256 校验，不一致就失败
#   ENGINES        要装底模的引擎，逗号分隔，默认 v2pro（manifest 里有 v2pro、v2proplus、v4；base 总是装）
#   NLTK_DATA_SRC  nltk 数据目录（可复用已有安装的 .venv/nltk_data）；不给就用 nltk.download 下载
#   STEPS          只跑哪几步，默认 venv,pth,nltk,pretrained
#   TORCH          cu128（有 nvidia-smi 时的默认）| cpu（没有 NVIDIA 显卡时的默认，局域网设备常见；装出来小 4GB 左右）
#
# 依赖：uv、curl、git。Python 3.10 由 uv 装。
# 锁文件 requirements/linux-cu128.lock.txt 固定了 Linux 上的包版本（生成命令见它的开头），
# linux-cpu.lock.txt 是同一份依赖配 CPU 版 torch。
# 装的时候要 --extra-index-url <torch 的源> + --index-strategy unsafe-best-match：torch 在 PyTorch 的源、torchcodec 在 PyPI。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
API=$(dirname "$HERE")
: "${GSV_ROOT:?GSV_ROOT=GPT-SoVITS 代码目录}"
# shellcheck source=gptsovits.sh
. "$HERE/gptsovits.sh"
gsv_ensure_installed "$GSV_ROOT" "$API/patches/gptsovits-bf81cdb.patch"
VENV=${VENV:-$GSV_ROOT/.venv}
PY=$VENV/bin/python
GSV_V5_COMMIT=${GSV_V5_COMMIT:-6d51ffceafbf240145619af4c92408a6e54b7924}
GSV_V5_ROOT=${GSV_V5_ROOT:-$(dirname "$GSV_ROOT")/GPT-SoVITS-v5}
V5_VENV=${V5_VENV:-$GSV_V5_ROOT/.venv}
V5_PY=$V5_VENV/bin/python
PRETRAINED=${PRETRAINED:-hf}
ENGINES=${ENGINES:-v2pro}
STEPS=${STEPS:-venv,pth,nltk,pretrained}
TORCH=${TORCH:-$(command -v nvidia-smi >/dev/null && echo cu128 || echo cpu)}
LOCK=$API/requirements/linux-$TORCH.lock.txt
IDX=(--extra-index-url "https://download.pytorch.org/whl/$TORCH" --index-strategy unsafe-best-match)
ts() { echo "[$(date +%H:%M:%S)] $*"; }
has() { [[ ",$STEPS," == *",$1,"* ]]; }
wants_v5() { [[ ",$ENGINES," == *",v5turbo,"* || ",$ENGINES," == *",v5dev,"* ]]; }
sha() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum < "$1" | cut -d' ' -f1
  else
    shasum -a 256 < "$1" | cut -d' ' -f1
  fi
}

ensure_v5_repo() {
  [ "${GSV_V5_SKIP_CLONE:-0}" = "1" ] && { mkdir -p "$GSV_V5_ROOT"; return 0; }
  if [ -d "$GSV_V5_ROOT/.git" ]; then
    if [ "$(git -C "$GSV_V5_ROOT" rev-parse HEAD 2>/dev/null || true)" != "$GSV_V5_COMMIT" ]; then
      git -C "$GSV_V5_ROOT" cat-file -e "${GSV_V5_COMMIT}^{commit}" 2>/dev/null || \
        git -C "$GSV_V5_ROOT" fetch -q --depth 1 https://github.com/RVC-Boss/GPT-SoVITS.git "$GSV_V5_COMMIT"
      git -C "$GSV_V5_ROOT" checkout -q -f "$GSV_V5_COMMIT"
    fi
  else
    ts "v5 repo start: $GSV_V5_ROOT ($GSV_V5_COMMIT)"
    mkdir -p "$GSV_V5_ROOT"
    git init -q "$GSV_V5_ROOT"
    git -C "$GSV_V5_ROOT" remote add origin https://github.com/RVC-Boss/GPT-SoVITS.git
    git -C "$GSV_V5_ROOT" fetch -q --depth 1 origin "$GSV_V5_COMMIT"
    git -C "$GSV_V5_ROOT" checkout -q -f "$GSV_V5_COMMIT"
    ts "v5 repo end"
  fi
}

if has venv; then
  ts "venv start: ${VENV}（torch ${TORCH}）"
  uv python install 3.10
  [ -x "$PY" ] || uv venv -q -p 3.10 "$VENV"
  # 先装 torch（大，单独一步好看耗时），再装全部
  grep -E '^(torch|torchaudio|torchcodec|nvidia-[a-z0-9-]+|triton)==' "$LOCK" > "$VENV/req-torch.txt"
  uv pip install -q -p "$PY" -r "$VENV/req-torch.txt" "${IDX[@]}"
  uv pip install -q -p "$PY" -r "$LOCK" "${IDX[@]}"
  ts "venv end"
  if wants_v5; then
    ensure_v5_repo
    ts "v5 venv start: $V5_VENV"
    [ -x "$V5_PY" ] || uv venv -q -p 3.10 "$V5_VENV"
    uv pip install -q -p "$V5_PY" --no-deps resampy
    ts "v5 venv end"
  fi
fi

SITE=$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
if has pth; then
  # 两个 .pth：上游训练脚本靠它们 import text、tools、BigVGAN 等
  printf '%s\n' "$GSV_ROOT" "$GSV_ROOT/GPT_SoVITS/BigVGAN" "$GSV_ROOT/tools" "$GSV_ROOT/tools/asr" "$GSV_ROOT/GPT_SoVITS" \
    > "$SITE/users.pth"
  echo "$GSV_ROOT" > "$SITE/gpt_sovits_project_root.pth"
  ts "pth written: $SITE/{users,gpt_sovits_project_root}.pth"
  if wants_v5 && [ -x "$V5_PY" ]; then
    V5_SITE=$("$V5_PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    printf '%s\n' "$GSV_V5_ROOT" "$GSV_V5_ROOT/GPT_SoVITS/BigVGAN" "$GSV_V5_ROOT/tools" "$GSV_V5_ROOT/tools/asr" "$GSV_V5_ROOT/GPT_SoVITS" \
      > "$V5_SITE/users.pth"
    echo "$GSV_V5_ROOT" > "$V5_SITE/gpt_sovits_project_root.pth"
    echo "$SITE" > "$V5_SITE/zz_main_venv.pth"
    ts "v5 pth written: $V5_SITE/{users,gpt_sovits_project_root,zz_main_venv}.pth"
  fi
fi

if has nltk; then
  # g2p_en（日语句子里的英文字母）要 cmudict、averaged_perceptron_tagger(_eng)；nltk 会在 sys.prefix/nltk_data 里找
  if [ -n "${NLTK_DATA_SRC:-}" ]; then
    mkdir -p "$VENV/nltk_data" && cp -r "$NLTK_DATA_SRC"/. "$VENV/nltk_data/"
  else
    "$PY" -c "import nltk; [nltk.download(p, download_dir='$VENV/nltk_data', quiet=True) for p in
              ('cmudict', 'averaged_perceptron_tagger', 'averaged_perceptron_tagger_eng')]"
  fi
  if wants_v5 && [ -d "$V5_VENV" ] && [ -d "$VENV/nltk_data" ]; then
    ln -sfn "$VENV/nltk_data" "$V5_VENV/nltk_data"
  fi
  ts "nltk_data: $(ls "$VENV/nltk_data" | tr '\n' ' ')"
fi

if has pretrained && [ "$PRETRAINED" != skip ]; then
  ts "pretrained start ($PRETRAINED; base,$ENGINES)"
  if wants_v5; then
    ensure_v5_repo
  fi
  bad=()
  while read -r eng sum rel url; do
    [[ -z "$eng" || "$eng" == \#* ]] && continue
    [[ "$eng" == base || ",$ENGINES," == *",$eng,"* ]] || continue
    if [[ "$eng" == v5turbo || "$eng" == v5dev ]]; then
      dst="$GSV_V5_ROOT/$rel"
    else
      dst="$GSV_ROOT/$rel"
    fi
    if [ ! -f "$dst" ] || [ "$(sha "$dst")" != "$sum" ]; then
      mkdir -p "$(dirname "$dst")"
      rm -f "$dst"  # 可能是硬链接到上一次安装的（scripts/device/install.sh）：换成新文件，不改那边的
      if [ "$PRETRAINED" = hf ]; then
        curl -sSfL --retry 3 -o "$dst" "$url"
      else
        cp "$PRETRAINED/$rel" "$dst"
      fi
      [ "$(sha "$dst")" = "$sum" ] || bad+=("$rel")
    fi
  done < "$HERE/pretrained.sha256"
  if [ ${#bad[@]} -gt 0 ]; then
    echo "SHA-256 与底模清单不一致：${bad[*]}" >&2
    exit 1
  fi
  if wants_v5; then
    while read -r eng sum rel url; do
      [[ "$eng" == base && "$rel" == GPT_SoVITS/pretrained_models/* ]] || continue
      src="$GSV_ROOT/$rel"
      dst="$GSV_V5_ROOT/$rel"
      if [ -f "$src" ]; then
        mkdir -p "$(dirname "$dst")"
        ln -f "$src" "$dst" 2>/dev/null || ln -sfn "$src" "$dst"
      fi
    done < "$HERE/pretrained.sha256"
  fi
  ts "pretrained end"
fi

if [ "${SKIP_CHECK:-0}" != "1" ]; then
  ts "check"
  LD_LIBRARY_PATH=$(ls -d "$SITE"/nvidia/*/lib 2>/dev/null | paste -sd:)${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH} "$PY" -c "
import sys, torch, ctranslate2, pyopenjtalk
print(sys.version.split()[0], torch.__version__, 'cuda', torch.cuda.is_available(), 'ct2 cuda', ctranslate2.get_cuda_device_count())
print(pyopenjtalk.g2p('今日はいい天気', kana=True))"
  if wants_v5 && [ -x "$V5_PY" ]; then
    LD_LIBRARY_PATH=$(ls -d "$SITE"/nvidia/*/lib 2>/dev/null | paste -sd:)${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH} "$V5_PY" -c "
import resampy, torch
print('v5', torch.__version__, 'resampy', resampy.__version__)"
  fi
fi
echo "完成。起 tts-api：TTSAPI_GSV_ROOT=$GSV_ROOT$(wants_v5 && echo " TTSAPI_GSV_V5_ROOT=$GSV_V5_ROOT TTSAPI_V5_PYTHON=$V5_PY") $PY -m ttsapi.gateway（在 tts-api 根目录下）"
