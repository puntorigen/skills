#!/usr/bin/env bash
# Setup for talking-head: clone JoyVASA, build a macOS-adapted venv, patch a few
# CUDA-hardcoded defaults, and download the model checkpoints.
#
# JoyVASA = audio-driven facial-motion diffusion + LivePortrait renderer. It runs
# on the Apple GPU via MLX/MPS (torch), so this skill needs an Apple Silicon Mac.
# The upstream repo only ships CUDA/Linux instructions; this script installs a
# curated dependency set that works natively on Apple Silicon and applies small
# source patches so the models load on MPS/CPU instead of CUDA.
#
# Everything lives under ~/.talking-head/ (checkout, venv, weights) - never in the
# repo. Weights (~5 GB) download anonymously from Hugging Face on first setup.
#
# Idempotent: re-running re-syncs deps, re-applies patches (no-op if present), and
# skips already-downloaded weights. Prints the venv python on the last stdout line.
#
# Usage:  setup_env.sh [--update]
#   --update   git pull the JoyVASA checkout before syncing
# Env:
#   TALKING_HEAD_HOME   data root (default ~/.talking-head)
set -euo pipefail

UPDATE=0
[[ "${1:-}" == "--update" ]] && UPDATE=1

TH_HOME="${TALKING_HEAD_HOME:-$HOME/.talking-head}"
REPO_DIR="$TH_HOME/JoyVASA"
REPO_URL="https://github.com/jdh-algo/JoyVASA.git"
VENV="$TH_HOME/.venv"
PW="$REPO_DIR/pretrained_weights"
PY="$VENV/bin/python"
PYTHON_VERSION="${TALKING_HEAD_PYTHON:-3.10}"

mkdir -p "$TH_HOME/out"

# --- platform check ------------------------------------------------------------
OS="$(uname -s)"; ARCH="$(uname -m)"
if [[ "$OS" != "Darwin" || "$ARCH" != "arm64" ]]; then
  echo "[setup] WARNING: talking-head targets Apple Silicon (MPS). Detected $OS/$ARCH." >&2
  echo "[setup]          It may not run without an Apple GPU." >&2
fi

# --- required tools ------------------------------------------------------------
for tool in git ffmpeg uv; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "[setup] '$tool' not found on PATH." >&2
    case "$tool" in
      ffmpeg) echo "[setup]   install: brew install ffmpeg" >&2 ;;
      uv)     echo "[setup]   install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2 ;;
      git)    echo "[setup]   install: xcode-select --install" >&2 ;;
    esac
    exit 1
  fi
done

# --- clone / update the JoyVASA checkout ---------------------------------------
if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "[setup] cloning JoyVASA into $REPO_DIR" >&2
  git clone --depth 1 "$REPO_URL" "$REPO_DIR" >&2
elif [[ "$UPDATE" -eq 1 ]]; then
  echo "[setup] updating JoyVASA checkout" >&2
  git -C "$REPO_DIR" pull --ff-only >&2 || echo "[setup] (pull skipped/failed; keeping current checkout)" >&2
else
  echo "[setup] JoyVASA already present (use --update to pull)" >&2
fi

# --- create venv + install a macOS-native dependency set -----------------------
# Notes:
#  - torch>=2.8 is required: earlier versions raise "Conv3D is not supported on
#    MPS" instead of falling back. 2.8 runs Conv3D on MPS and falls back
#    grid_sampler_3d to CPU automatically.
#  - onnxruntime (not -gpu); numpy pinned <2 for numba/opencv ABI compatibility.
#  - The heavy CUDA-only extras in JoyVASA's requirements.txt (xformers,
#    bitsandbytes, decord, onnxruntime-gpu, tensorrt, audio-separator, mediapipe)
#    are NOT imported by the human image+audio path and are omitted.
echo "[setup] creating venv at $VENV (python $PYTHON_VERSION)" >&2
uv venv --python "$PYTHON_VERSION" "$VENV" >&2

echo "[setup] installing dependencies (first run downloads packages)" >&2
uv pip install --python "$PY" \
  torch==2.8.0 torchvision==0.23.0 torchaudio==2.11.0 \
  "numpy==1.26.4" scipy==1.13.1 scikit-image==0.24.0 \
  opencv-python==4.10.0.84 imageio==2.34.2 imageio-ffmpeg==0.5.1 moviepy==1.0.3 \
  librosa==0.10.2.post1 \
  transformers==4.39.2 \
  onnx==1.16.1 onnxruntime==1.18.0 \
  omegaconf==2.3.0 pyyaml==6.0.1 tyro==0.8.5 rich==13.7.1 tqdm==4.66.4 \
  einops==0.8.0 pykalman==0.9.7 ffmpeg-python==0.2.0 matplotlib==3.9.0 \
  "huggingface_hub[cli,hf_xet]" >&2

if [[ ! -x "$PY" ]]; then
  echo "[setup] expected venv python not found at $PY" >&2
  exit 1
fi

# --- patch CUDA-hardcoded defaults so models load on MPS/CPU -------------------
# JoyVASA hardcodes device='cuda' in a few constructor defaults; load_model() then
# calls .to(device) with the real (mps) device, so switching the *defaults* to
# 'cpu' is sufficient and safe. Idempotent: replaces only the exact 'cuda' tokens.
echo "[setup] applying macOS/MPS source patches (idempotent)" >&2
"$PY" - "$REPO_DIR" <<'PYEOF' >&2
import sys, io, os
repo = sys.argv[1]
patches = {
    "src/modules/common.py": [
        ("def enc_dec_mask(T, S, frame_width=2, expansion=0, device='cuda'):",
         "def enc_dec_mask(T, S, frame_width=2, expansion=0, device='cpu'):"),
    ],
    "src/modules/dit_talking_head.py": [
        ('    def __init__(self, device=\'cuda\', target="sample", architecture="decoder",',
         '    def __init__(self, device=\'cpu\', target="sample", architecture="decoder",'),
        ("    def __init__(self, device='cuda', motion_feat_dim=76, ",
         "    def __init__(self, device='cpu', motion_feat_dim=76, "),
    ],
}
for rel, subs in patches.items():
    p = os.path.join(repo, rel)
    with io.open(p, encoding="utf-8") as f:
        txt = f.read()
    orig = txt
    for old, new in subs:
        if old in txt:
            txt = txt.replace(old, new)
    if txt != orig:
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"[setup]   patched {rel}")
    else:
        print(f"[setup]   {rel} already patched / no change")
PYEOF

# --- download checkpoints into the pretrained_weights layout -------------------
# Anonymous HF downloads. hf_transfer is intentionally disabled (hf_xet handles
# speed) so an inherited HF_HUB_ENABLE_HF_TRANSFER=1 does not break setup.
export HF_HUB_ENABLE_HF_TRANSFER=0
HF="$VENV/bin/hf"
mkdir -p "$PW"

dl() {  # repo  local_dir  extra-args...
  local repo="$1"; local dir="$2"; shift 2
  if [[ -d "$dir" && -n "$(ls -A "$dir" 2>/dev/null | grep -v '^\.cache$' || true)" ]]; then
    echo "[setup]   $repo already downloaded" >&2
  else
    echo "[setup]   downloading $repo" >&2
    "$HF" download "$repo" --local-dir "$dir" "$@" >&2
  fi
}

echo "[setup] downloading model weights (~5 GB on first run)" >&2
dl jdh-algo/JoyVASA "$PW/JoyVASA" --exclude "README.md" ".gitattributes"
# Chinese-HuBERT audio encoder. The motion generator expects this exact folder
# name (with the colon) on non-Windows systems.
dl TencentGameMate/chinese-hubert-base "$PW/TencentGameMate:chinese-hubert-base" \
   --include "config.json" "preprocessor_config.json" "pytorch_model.bin"
# LivePortrait renderer + InsightFace face detectors (humans only).
dl KlingTeam/LivePortrait "$PW" --include "liveportrait/*" "insightface/*"

# --- smoke test ----------------------------------------------------------------
if "$PY" -c "import torch, transformers, cv2, librosa, onnxruntime, tyro, moviepy; import mlx.core" >/dev/null 2>&1; then
  :
fi
if "$PY" -c "import torch; assert torch.backends.mps.is_available()" >/dev/null 2>&1; then
  echo "[setup] torch MPS available: yes" >&2
else
  echo "[setup] torch MPS available: NO (generation will be CPU-only / slow)" >&2
fi

echo "[setup] done." >&2
echo "[setup]   data root : $TH_HOME" >&2
echo "[setup]   checkout  : $REPO_DIR" >&2
echo "[setup]   python    : $PY" >&2
echo "[setup]   weights   : $PW" >&2
# stdout: the venv python to drive animate.py with
echo "$PY"
