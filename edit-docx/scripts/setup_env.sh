#!/usr/bin/env bash
# Setup for edit-docx: a self-contained Python venv with python-docx so the
# CLI at scripts/docx_tool.py can inspect and edit .docx files locally.
#
# This skill is pure Python (no models, no GPU) and works on any OS with
# Python 3.9+. The only dependency is python-docx (which pulls in lxml).
#
# The venv lives OUTSIDE the repo at ~/.edit-docx/.venv so it never gets
# committed and can be reused across projects. Internet is used only on first
# run to fetch python-docx from PyPI; your documents never leave the machine.
#
# Idempotent: re-running skips work that's already done. Prints the venv python
# on the last stdout line so callers can capture it.
#
# Usage:  setup_env.sh [--update]
#   --update   refresh python-docx to the latest release
# Env:
#   EDIT_DOCX_HOME     data root (default ~/.edit-docx)
#   EDIT_DOCX_PYTHON   python version for the venv when using uv (default 3.11)
set -euo pipefail

UPDATE=0
[[ "${1:-}" == "--update" ]] && UPDATE=1

HOME_DIR="${EDIT_DOCX_HOME:-$HOME/.edit-docx}"
VENV="$HOME_DIR/.venv"
PYTHON_VERSION="${EDIT_DOCX_PYTHON:-3.11}"

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

# --- install / refresh python-docx --------------------------------------------
echo "[setup] installing python-docx (first run downloads it + lxml)" >&2
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" --upgrade python-docx >&2
else
  "$PY" -m pip install --upgrade pip >&2
  "$PY" -m pip install --upgrade python-docx >&2
fi

# --- sanity check --------------------------------------------------------------
if "$PY" -c "import docx" >/dev/null 2>&1; then
  echo "[setup] python-docx import OK ($("$PY" -c 'import docx; print("python-docx", docx.__version__)'))" >&2
else
  echo "[setup] WARNING: 'import docx' failed - docx_tool.py will not run." >&2
  echo "[setup]          check the pip output above." >&2
  exit 1
fi

echo "[setup] done." >&2
echo "[setup]   data root : $HOME_DIR" >&2
echo "[setup]   python    : $PY" >&2
# stdout: the venv python to drive docx_tool.py with
echo "$PY"
