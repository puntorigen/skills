#!/usr/bin/env bash
# Setup for sound-effects: create an isolated venv, install mlx-audiogen (the MLX
# Apple Silicon runtime for Stable Audio Open Small), and pre-download the
# PUBLIC pre-converted MLX weights.
#
#   - Apple Silicon (Darwin/arm64) -> MLX backend (native, fast)
#   - everything else              -> unsupported (MLX requires Metal); the
#                                     script still installs but warns.
#
# NO HUGGING FACE ACCOUNT OR TOKEN IS NEEDED. The weights we fetch
# (jasonvassallo/mlx-stable-audio, a public re-hosted MLX conversion) are NOT
# gated and download anonymously. Only the advanced self-conversion path
# (converting Stability's original gated weights yourself) requires an account.
#
# Idempotent: re-running reuses the venv and existing weights.
#
# Usage:  setup_env.sh [--upgrade] [--no-weights]
#   --upgrade      pip-upgrade mlx-audiogen in the existing venv
#   --no-weights   skip the weight pre-download (they auto-download on first use)
# Env:
#   SOUND_EFFECTS_HOME   data root (default ~/.sound-effects)
#   SFX_WEIGHTS_REPO     public MLX weights repo (default jasonvassallo/mlx-stable-audio)
#   SFX_WEIGHTS_DIR      where to store weights (default $SOUND_EFFECTS_HOME/weights/mlx-stable-audio)
set -euo pipefail

UPGRADE=0
FETCH_WEIGHTS=1
for arg in "$@"; do
  case "$arg" in
    --upgrade)    UPGRADE=1 ;;
    --no-weights) FETCH_WEIGHTS=0 ;;
    *) echo "[setup] unknown arg: $arg" >&2; exit 2 ;;
  esac
done

SFX_HOME="${SOUND_EFFECTS_HOME:-$HOME/.sound-effects}"
VENV="$SFX_HOME/.venv"
WEIGHTS_REPO="${SFX_WEIGHTS_REPO:-jasonvassallo/mlx-stable-audio}"
WEIGHTS_DIR="${SFX_WEIGHTS_DIR:-$SFX_HOME/weights/mlx-stable-audio}"

mkdir -p "$SFX_HOME/out"

# --- required tools ------------------------------------------------------------
for tool in uv ffmpeg; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "[setup] '$tool' not found on PATH." >&2
    case "$tool" in
      ffmpeg) echo "[setup]   install: brew install ffmpeg" >&2 ;;
      uv)     echo "[setup]   install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2 ;;
    esac
    exit 1
  fi
done

# --- platform check ------------------------------------------------------------
OS="$(uname -s)"; ARCH="$(uname -m)"
if [[ "$OS" == "Darwin" && "$ARCH" == "arm64" ]]; then
  echo "[setup] platform: Apple Silicon (MLX)" >&2
else
  echo "[setup] WARNING: mlx-audiogen requires Apple Silicon (Metal GPU)." >&2
  echo "[setup]   This platform ($OS/$ARCH) is unsupported for generation." >&2
fi

# --- create venv + install -----------------------------------------------------
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "[setup] creating venv at $VENV (Python 3.11)" >&2
  uv venv --python 3.11 "$VENV" >&2
fi
PY="$VENV/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "[setup] expected venv python not found at $PY" >&2
  exit 1
fi

if [[ "$UPGRADE" -eq 1 ]]; then
  echo "[setup] upgrading mlx-audiogen" >&2
  uv pip install --python "$PY" --upgrade mlx-audiogen >&2
else
  echo "[setup] installing mlx-audiogen (first run downloads dependencies)" >&2
  uv pip install --python "$PY" mlx-audiogen >&2
fi

# --- verify --------------------------------------------------------------------
if ! "$PY" -c "import mlx_audiogen" 2>/dev/null; then
  echo "[setup] WARNING: 'import mlx_audiogen' failed; generation may not work." >&2
fi
CLI="$VENV/bin/mlx-audiogen"
[[ -x "$CLI" ]] || echo "[setup] WARNING: mlx-audiogen console script missing at $CLI" >&2

# --- pre-download the PUBLIC pre-converted MLX weights (no HF account needed) ---
# We fetch anonymously (token=False). This both proves the ungated path works and
# makes generation fully offline afterwards. If it fails (e.g. offline), we warn
# but do not fail setup: the weights auto-download on first generation instead.
if [[ "$FETCH_WEIGHTS" -eq 1 ]]; then
  if [[ -f "$WEIGHTS_DIR/config.json" ]]; then
    echo "[setup] weights already present at $WEIGHTS_DIR (skipping download)" >&2
  else
    echo "[setup] downloading public MLX weights '$WEIGHTS_REPO' (anonymous, ~1-2GB)..." >&2
    if SFX_WEIGHTS_REPO="$WEIGHTS_REPO" SFX_WEIGHTS_DIR="$WEIGHTS_DIR" "$PY" - <<'PY' >&2
import os, sys
try:
    from huggingface_hub import snapshot_download
except Exception as e:  # noqa: BLE001
    sys.stderr.write(f"[setup]   huggingface_hub unavailable: {e}\n")
    sys.exit(3)
repo = os.environ["SFX_WEIGHTS_REPO"]
dest = os.environ["SFX_WEIGHTS_DIR"]
os.makedirs(dest, exist_ok=True)
try:
    # token=False -> force an anonymous request; no account/login required for a
    # public (ungated) repo. Raises if the repo is gated/private.
    snapshot_download(repo_id=repo, local_dir=dest, token=False)
    print(f"[setup]   weights ready at {dest}")
except Exception as e:  # noqa: BLE001
    sys.stderr.write(f"[setup]   weight pre-download failed: {e}\n")
    sys.exit(3)
PY
    then
      :
    else
      echo "[setup] WARNING: could not pre-download weights now." >&2
      echo "[setup]   Not fatal - they will auto-download on the first generation." >&2
      echo "[setup]   (The repo is public; retry when you have network.)" >&2
    fi
  fi
fi

echo "[setup] done." >&2
echo "[setup]   data root : $SFX_HOME" >&2
echo "[setup]   python    : $PY" >&2
echo "[setup]   cli       : $CLI" >&2
echo "[setup]   weights   : $WEIGHTS_DIR" >&2
echo "[setup]   ffmpeg    : $(command -v ffmpeg)" >&2
echo "[setup] No Hugging Face account or token is required - the weights are public." >&2
# stdout: the venv python path
echo "$PY"
