#!/usr/bin/env bash
# Setup for j-space: create an isolated venv and install the two dependencies the
# CLI needs - numpy (matrix math) and sentence-transformers (local embeddings) -
# then pre-download the embedding model so builds/queries run fully offline.
#
# The embedding model is sentence-transformers/all-MiniLM-L6-v2 (384-dim, ~90 MB).
# It runs on Apple MPS when available and on CPU otherwise - this skill works on
# any platform, unlike the repo's MLX-only media skills.
#
# NO Hugging Face account or token is needed: all-MiniLM-L6-v2 is a public,
# ungated model that downloads anonymously.
#
# Idempotent: re-running reuses the venv and the cached model.
#
# IMPORTANT: nothing here touches your projects. Workspace DATA (graphs,
# matrices, checkpoints) always lives under ./.jspace/ in the project you run the
# CLI in - never in this home dir and never in the skill repo.
#
# Usage:  setup_env.sh [--upgrade] [--no-model]
#   --upgrade    pip-upgrade numpy + sentence-transformers in the existing venv
#   --no-model   skip the model pre-download (it downloads lazily on first build)
# Env:
#   JSPACE_HOME   data root for the venv + model cache (default ~/.j-space)
set -euo pipefail

UPGRADE=0
FETCH_MODEL=1
for arg in "$@"; do
  case "$arg" in
    --upgrade)  UPGRADE=1 ;;
    --no-model) FETCH_MODEL=0 ;;
    *) echo "[setup] unknown arg: $arg" >&2; exit 2 ;;
  esac
done

JSPACE_HOME="${JSPACE_HOME:-$HOME/.j-space}"
VENV="$JSPACE_HOME/.venv"
MODELS_DIR="$JSPACE_HOME/models"
MODEL_ID="sentence-transformers/all-MiniLM-L6-v2"

mkdir -p "$MODELS_DIR"

# --- required tools ------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "[setup] 'uv' not found on PATH." >&2
  echo "[setup]   install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
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
  echo "[setup] upgrading numpy + sentence-transformers" >&2
  uv pip install --python "$PY" --upgrade numpy sentence-transformers >&2
else
  echo "[setup] installing numpy + sentence-transformers (first run pulls torch; a few minutes)" >&2
  uv pip install --python "$PY" numpy sentence-transformers >&2
fi

# --- verify --------------------------------------------------------------------
if ! "$PY" -c "import numpy, sentence_transformers" 2>/dev/null; then
  echo "[setup] WARNING: importing numpy / sentence_transformers failed; the CLI may not work." >&2
fi

# --- pre-download the PUBLIC embedding model (no HF account needed) -------------
# We cache into $JSPACE_HOME/models so builds/queries are fully offline afterward.
# If it fails (e.g. offline) we warn but do not fail setup: the model downloads
# lazily on the first build instead.
if [[ "$FETCH_MODEL" -eq 1 ]]; then
  echo "[setup] pre-downloading embedding model '$MODEL_ID' (anonymous, ~90MB)..." >&2
  if JSPACE_MODELS_DIR="$MODELS_DIR" JSPACE_MODEL_ID="$MODEL_ID" "$PY" - <<'PY' >&2
import os, sys
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
cache = os.environ["JSPACE_MODELS_DIR"]
model_id = os.environ["JSPACE_MODEL_ID"]
try:
    from sentence_transformers import SentenceTransformer
    # cache_folder keeps the model under the data root (not the global HF cache).
    m = SentenceTransformer(model_id, cache_folder=cache)
    _ = m.encode(["warmup"])  # force a full load so a broken download fails now
    print(f"[setup]   model ready in {cache}")
except Exception as e:  # noqa: BLE001
    sys.stderr.write(f"[setup]   model pre-download failed: {e}\n")
    sys.exit(3)
PY
  then
    :
  else
    echo "[setup] WARNING: could not pre-download the model now." >&2
    echo "[setup]   Not fatal - it will download on the first 'build'." >&2
    echo "[setup]   (The model is public; retry when you have network.)" >&2
  fi
fi

echo "[setup] done." >&2
echo "[setup]   data root : $JSPACE_HOME" >&2
echo "[setup]   python    : $PY" >&2
echo "[setup]   model     : $MODEL_ID (cache: $MODELS_DIR)" >&2
echo "[setup] No Hugging Face account or token is required - the model is public." >&2
echo "[setup] Workspace data lives in ./.jspace/ in your project, never here." >&2
# stdout: the venv python path (callers capture this)
echo "$PY"
