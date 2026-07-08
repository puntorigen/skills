#!/usr/bin/env bash
# Setup for brand-logo-kit: a self-contained Python venv with the Google GenAI
# SDK + Pillow/NumPy so the scripts in this skill's scripts/ dir can generate
# brand logos and on-brand assets via Gemini image models.
#
# Unlike most skills in this repo, brand-logo-kit calls a CLOUD image API
# (Google Gemini, or OpenRouter's Gemini/Nano-Banana endpoints). It bundles NO
# API key: resolve_key.py discovers one from your environment or another
# installed skill and caches it OUTSIDE the repo (see below).
#
# The venv AND the cached key live OUTSIDE the repo at ~/.brand-logo-kit/ so
# they never get committed and can be reused across projects. Internet is used
# to fetch the SDK on first run and for every image generation call.
#
# Idempotent: re-running skips work that's already done. Prints the venv python
# on the last stdout line so callers can capture it.
#
# Usage:  setup_env.sh [--update]
#   --update   refresh the Python dependencies to their latest releases
# Env:
#   BRAND_LOGO_KIT_HOME     data root (default ~/.brand-logo-kit)
#   BRAND_LOGO_KIT_PYTHON   python version for the venv when using uv (default 3.11)
set -euo pipefail

UPDATE=0
[[ "${1:-}" == "--update" ]] && UPDATE=1

HOME_DIR="${BRAND_LOGO_KIT_HOME:-$HOME/.brand-logo-kit}"
VENV="$HOME_DIR/.venv"
PYTHON_VERSION="${BRAND_LOGO_KIT_PYTHON:-3.11}"

mkdir -p "$HOME_DIR"

# --- create the venv -----------------------------------------------------------
# Prefer uv (fast, matches the rest of this repo); fall back to the stdlib venv
# so the skill also works on machines without uv.
if [[ ! -x "$VENV/bin/python" || "$UPDATE" -eq 1 ]]; then
  if command -v uv >/dev/null 2>&1; then
    echo "[setup] creating venv with uv at $VENV (python $PYTHON_VERSION)" >&2
    uv venv --python "$PYTHON_VERSION" "$VENV" >&2
  elif command -v python3 >/dev/null 2>&1; then
    echo "[setup] creating venv with python3 -m venv at $VENV" >&2
    python3 -m venv "$VENV" >&2
  else
    echo "[setup] ERROR: neither 'uv' nor 'python3' found on PATH." >&2
    echo "[setup]        install Python 3.9+ (brew install python) or uv" >&2
    echo "[setup]        (curl -LsSf https://astral.sh/uv/install.sh | sh)." >&2
    exit 1
  fi
fi

PY="$VENV/bin/python"

# --- install / refresh deps ----------------------------------------------------
# google-genai talks to the Gemini API; requests handles the OpenRouter path;
# Pillow + numpy do the transparent-background cutout and palette extraction.
DEPS=(google-genai Pillow numpy requests)
echo "[setup] installing ${DEPS[*]} (first run downloads them from PyPI)" >&2
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" --upgrade "${DEPS[@]}" >&2
else
  "$PY" -m pip install --upgrade pip >&2
  "$PY" -m pip install --upgrade "${DEPS[@]}" >&2
fi

# --- sanity check --------------------------------------------------------------
if "$PY" -c "import google.genai, PIL, numpy, requests" >/dev/null 2>&1; then
  echo "[setup] imports OK ($("$PY" -c 'import PIL; print("Pillow", PIL.__version__)'))" >&2
else
  echo "[setup] WARNING: an import failed - the scripts may not run." >&2
  echo "[setup]          check the pip output above." >&2
  exit 1
fi

echo "[setup] done." >&2
echo "[setup]   data root : $HOME_DIR" >&2
echo "[setup]   python    : $PY" >&2
echo "[setup]   key cache : $HOME_DIR/config.json (created on first use; git-ignored)" >&2
echo "[setup]   next      : run scripts/resolve_key.py to discover + cache an API key" >&2
# stdout: the venv python to drive the scripts with
echo "$PY"
