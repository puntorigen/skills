#!/usr/bin/env bash
# Setup for bg-music: clone ACE-Step 1.5 and sync its isolated environment.
#
#   - Apple Silicon (Darwin/arm64) -> MLX backend (native, fast)
#   - everything else              -> PyTorch backend (CUDA/CPU)
#
# Idempotent: re-running skips the clone and just re-syncs. Prints the selected
# backend on the last stdout line. Model weights (~10GB) download on first
# generation, not here.
#
# Usage:  setup_env.sh [--update]
#   --update   git pull the ACE-Step checkout before syncing
# Env:
#   BG_MUSIC_HOME   data root (default ~/.bg-music)
set -euo pipefail

UPDATE=0
[[ "${1:-}" == "--update" ]] && UPDATE=1

BG_HOME="${BG_MUSIC_HOME:-$HOME/.bg-music}"
ACE_DIR="$BG_HOME/ACE-Step-1.5"
ACE_REPO="https://github.com/ace-step/ACE-Step-1.5.git"

mkdir -p "$BG_HOME/out"

# --- required tools ------------------------------------------------------------
for tool in git ffmpeg uv; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "[setup] '$tool' not found on PATH." >&2
    case "$tool" in
      ffmpeg) echo "[setup]   install: brew install ffmpeg" >&2 ;;
      uv)     echo "[setup]   install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2 ;;
      git)    echo "[setup]   install: xcode-select --install (macOS) or apt install git" >&2 ;;
    esac
    exit 1
  fi
done

# --- backend selection ---------------------------------------------------------
OS="$(uname -s)"; ARCH="$(uname -m)"
if [[ "$OS" == "Darwin" && "$ARCH" == "arm64" ]]; then
  BACKEND="mlx"
else
  BACKEND="pt"
fi

# --- clone / update the ACE-Step checkout --------------------------------------
if [[ ! -d "$ACE_DIR/.git" ]]; then
  echo "[setup] cloning ACE-Step 1.5 into $ACE_DIR" >&2
  git clone --depth 1 "$ACE_REPO" "$ACE_DIR" >&2
elif [[ "$UPDATE" -eq 1 ]]; then
  echo "[setup] updating ACE-Step 1.5 checkout" >&2
  git -C "$ACE_DIR" pull --ff-only >&2 || echo "[setup] (pull skipped/failed; keeping current checkout)" >&2
else
  echo "[setup] ACE-Step 1.5 already present (use --update to pull)" >&2
fi

# --- sync the environment (creates $ACE_DIR/.venv) -----------------------------
echo "[setup] syncing environment with uv (first run downloads dependencies)" >&2
( cd "$ACE_DIR" && uv sync >&2 )

PY="$ACE_DIR/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "[setup] expected venv python not found at $PY" >&2
  exit 1
fi

# --- Apple Silicon: make sure MLX actually imports (repo's own fix) ------------
if [[ "$BACKEND" == "mlx" ]]; then
  if ! "$PY" -c "import mlx.core" 2>/dev/null || ! "$PY" -c "from mlx_lm.utils import load" 2>/dev/null; then
    echo "[setup] repairing MLX packages (one-time)" >&2
    ( cd "$ACE_DIR" && uv pip install --upgrade \
        mlx mlx-lm 'transformers>=4.51.0,<4.58.0' 'vector-quantize-pytorch>=1.27.15,<1.28.0' >&2 ) || \
      echo "[setup] (MLX repair failed; generation will try PyTorch fallback)" >&2
  fi
fi

echo "[setup] done." >&2
echo "[setup]   data root : $BG_HOME" >&2
echo "[setup]   ace-step  : $ACE_DIR" >&2
echo "[setup]   python    : $PY" >&2
echo "[setup]   ffmpeg    : $(command -v ffmpeg)" >&2
echo "[setup] Model weights (~10GB) download to $ACE_DIR/checkpoints on first generation." >&2
# stdout: the selected backend
echo "$BACKEND"
