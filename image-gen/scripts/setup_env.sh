#!/usr/bin/env bash
# Setup for image-gen: create an isolated venv and install mflux (native MLX).
#
# mflux runs the image models on the Apple GPU via MLX, so this skill needs an
# Apple Silicon Mac (a Metal GPU). Model weights download from Hugging Face on
# first generation into the shared HF cache (~/.cache/huggingface) - never into
# the repo or this skill's data root.
#
# Idempotent: re-running recreates the venv and upgrades mflux. Prints the venv
# python path on the last stdout line.
#
# Usage:  setup_env.sh
# Env:
#   IMAGE_GEN_HOME     data root for outputs (default ~/.image-gen)
#   IMAGE_GEN_PYTHON   python version for the venv (default 3.11)
set -euo pipefail

IMG_HOME="${IMAGE_GEN_HOME:-$HOME/.image-gen}"
VENV="$IMG_HOME/.venv"
PYTHON_VERSION="${IMAGE_GEN_PYTHON:-3.11}"

mkdir -p "$IMG_HOME/out"

# --- platform check ------------------------------------------------------------
OS="$(uname -s)"; ARCH="$(uname -m)"
if [[ "$OS" != "Darwin" || "$ARCH" != "arm64" ]]; then
  echo "[setup] WARNING: image-gen uses mflux (native MLX), which requires an" >&2
  echo "[setup]          Apple Silicon Mac with a Metal GPU. Detected: $OS/$ARCH." >&2
  echo "[setup]          Generation will not work on this platform." >&2
fi

# --- required tools ------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "[setup] 'uv' not found on PATH." >&2
  echo "[setup]   install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

# --- create venv + install mflux ----------------------------------------------
echo "[setup] creating venv at $VENV (python $PYTHON_VERSION)" >&2
uv venv --python "$PYTHON_VERSION" "$VENV" >&2

PY="$VENV/bin/python"
echo "[setup] installing mflux (first run downloads dependencies)" >&2
uv pip install --python "$PY" --upgrade mflux >&2

# --- verify the GPU-backed import actually works ------------------------------
# Importing mflux pulls in mlx.nn, which compiles Metal kernels; that only works
# in a real (non-headless, non-sandboxed) GUI session with GPU access.
if "$PY" -c "import mflux; import mlx.core as mx; mx.eval(mx.array([1.0]) + 1); print('ok')" >/dev/null 2>&1; then
  echo "[setup] mflux + MLX GPU import OK" >&2
else
  echo "[setup] WARNING: mflux/MLX GPU check did not pass here." >&2
  echo "[setup]          This is expected inside a sandbox or headless session;" >&2
  echo "[setup]          it will work when run from a normal desktop session." >&2
fi

echo "[setup] done." >&2
echo "[setup]   data root : $IMG_HOME" >&2
echo "[setup]   python    : $PY" >&2
echo "[setup]   out dir   : $IMG_HOME/out" >&2
echo "[setup] Model weights download to the HF cache (~/.cache/huggingface) on" >&2
echo "[setup] first generation: ~5-6 GB for Z-Image-Turbo 4-bit (one time)." >&2
# stdout: the venv python to drive the scripts with
echo "$PY"
