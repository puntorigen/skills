#!/usr/bin/env bash
# Setup for voice-clone-narration: create a dedicated venv and install the TTS
# backend that fits this machine.
#
#   - Apple Silicon (Darwin/arm64) -> mlx-audio (fast, and required for voice design)
#   - everything else              -> chatterbox-tts (PyTorch: CUDA/CPU)
#
# Idempotent: re-running only installs what's missing. Prints the selected
# backend on the last stdout line.
#
# Usage:  setup_env.sh [--force] [--backend mlx|torch]
# Env:
#   VOICE_CLONE_HOME   data root (default ~/.voice-clone-narration)
#   VC_BACKEND         force backend: mlx | torch (same as --backend)
set -euo pipefail

FORCE=0
BACKEND_OVERRIDE="${VC_BACKEND:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --backend) BACKEND_OVERRIDE="${2:-}"; shift 2 ;;
    *) echo "[setup] unknown arg: $1" >&2; exit 2 ;;
  esac
done

VC_HOME="${VOICE_CLONE_HOME:-$HOME/.voice-clone-narration}"
VENV="$VC_HOME/venv"
PY_BIN="$VENV/bin/python"

mkdir -p "$VC_HOME/voices" "$VC_HOME/out"

# --- ffmpeg is required for mp3 encoding ---------------------------------------
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[setup] ffmpeg not found on PATH. Install it first:" >&2
  echo "[setup]   macOS:  brew install ffmpeg" >&2
  echo "[setup]   Debian: sudo apt install ffmpeg" >&2
  exit 1
fi

# --- decide backend ------------------------------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"
if [[ -n "$BACKEND_OVERRIDE" ]]; then
  BACKEND="$BACKEND_OVERRIDE"
elif [[ "$OS" == "Darwin" && "$ARCH" == "arm64" ]]; then
  BACKEND="mlx"
else
  BACKEND="torch"
fi
if [[ "$BACKEND" != "mlx" && "$BACKEND" != "torch" ]]; then
  echo "[setup] invalid backend: $BACKEND (expected mlx|torch)" >&2
  exit 2
fi

# --- create the venv (uv preferred, python3.11 fallback) ----------------------
if [[ ! -x "$PY_BIN" ]]; then
  echo "[setup] creating venv at $VENV" >&2
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV" --python 3.11 >&2
  elif command -v python3.11 >/dev/null 2>&1; then
    python3.11 -m venv "$VENV" >&2
  elif command -v python3 >/dev/null 2>&1; then
    echo "[setup] python3.11 not found; falling back to $(python3 --version 2>&1)" >&2
    python3 -m venv "$VENV" >&2
  else
    echo "[setup] no python found (need uv or python3.11)" >&2
    exit 1
  fi
fi

# pip runner that works whether or not uv is present
pipi() {
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY_BIN" "$@" >&2
  else
    "$PY_BIN" -m pip install "$@" >&2
  fi
}

# ensure pip exists when not using uv
if ! command -v uv >/dev/null 2>&1; then
  "$PY_BIN" -m pip --version >/dev/null 2>&1 || "$PY_BIN" -m ensurepip --upgrade >&2
fi

# --- probe whether the backend is already importable --------------------------
have_backend() {
  if [[ "$BACKEND" == "mlx" ]]; then
    "$PY_BIN" - <<'PY' 2>/dev/null
import importlib.util as u, sys
sys.exit(0 if (u.find_spec("mlx_audio") and u.find_spec("soundfile")) else 1)
PY
  else
    "$PY_BIN" - <<'PY' 2>/dev/null
import importlib.util as u, sys
sys.exit(0 if (u.find_spec("chatterbox") and u.find_spec("soundfile")) else 1)
PY
  fi
}

if [[ "$FORCE" -eq 0 ]] && have_backend; then
  echo "[setup] backend '$BACKEND' already installed - skipping (use --force to reinstall)" >&2
else
  echo "[setup] installing backend '$BACKEND' into $VENV (first run downloads packages)" >&2
  if [[ "$BACKEND" == "mlx" ]]; then
    pipi -U mlx-audio soundfile numpy
  else
    # chatterbox-tts pulls torch/torchaudio; soundfile for wav I/O
    pipi -U chatterbox-tts soundfile numpy
  fi
fi

# --- pin a compatible transformers (mlx backend) ------------------------------
# mlx-audio pulls the newest transformers, but the Qwen3-TTS VoiceDesign path
# imports mlx-lm 0.31.3, which calls the pre-5.10 AutoTokenizer.register()
# signature; transformers >=5.10 raises "'str' object has no attribute
# '__module__'" and voice DESIGN fails (Chatterbox cloning is unaffected). Pin
# to a compatible range. Self-healing + idempotent: only acts when out of range.
if [[ "$BACKEND" == "mlx" ]]; then
  if ! "$PY_BIN" - <<'PY' 2>/dev/null
import sys
try:
    import transformers as t
    v = tuple(int(x) for x in t.__version__.split(".")[:2])
    sys.exit(0 if (5, 5) <= v < (5, 10) else 1)
except Exception:
    sys.exit(1)
PY
  then
    echo "[setup] pinning transformers to >=5.5,<5.10 (mlx-lm VoiceDesign compatibility)" >&2
    pipi 'transformers>=5.5,<5.10'
  fi
fi

echo "[setup] done." >&2
echo "[setup]   data root : $VC_HOME" >&2
echo "[setup]   python    : $PY_BIN" >&2
echo "[setup]   ffmpeg    : $(command -v ffmpeg)" >&2
echo "[setup] Model weights (~2.6GB clone, +3.5GB design) download on first use." >&2
# stdout: the selected backend, for callers that want to branch
echo "$BACKEND"
