#!/usr/bin/env bash
# Setup for pdf-documents: a local Python venv with Docling (structured PDF
# reading) and fpdf2 (professional PDF writing), so the scripts in this skill's
# scripts/ dir can read and create PDFs entirely on-device.
#
# The venv lives OUTSIDE the repo at ~/.pdf-documents/.venv so it never gets
# committed. Docling also downloads its layout/table AI models on FIRST READ
# (~1-2 GB) into the HuggingFace cache (~/.cache/huggingface), also outside the
# repo. Internet is used only for that first fetch; your PDFs never leave the
# machine.
#
# On macOS this also installs the optional `docling[ocrmac]` extra so --ocr can
# use the native Vision OCR engine.
#
# Idempotent: re-running skips work that's already done. Prints the venv python
# on the last stdout line so callers can capture it.
#
# Usage:  setup_env.sh [--update]
#   --update   refresh docling + fpdf2 to the latest releases
# Env:
#   PDF_DOCS_HOME     data root (default ~/.pdf-documents)
#   PDF_DOCS_PYTHON   python version for the venv when using uv (default 3.11)
set -euo pipefail

UPDATE=0
[[ "${1:-}" == "--update" ]] && UPDATE=1

HOME_DIR="${PDF_DOCS_HOME:-$HOME/.pdf-documents}"
VENV="$HOME_DIR/.venv"
PYTHON_VERSION="${PDF_DOCS_PYTHON:-3.11}"

mkdir -p "$HOME_DIR"

OS="$(uname -s)"

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
# docling pulls torch + layout/table models' loaders (large first install);
# fpdf2 is small. On macOS add the ocrmac extra for native Vision OCR.
DOCLING_SPEC="docling"
if [[ "$OS" == "Darwin" ]]; then
  DOCLING_SPEC="docling[ocrmac]"
fi

echo "[setup] installing $DOCLING_SPEC + fpdf2 (first run pulls torch etc.; can be >1 GB)" >&2
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" --upgrade "$DOCLING_SPEC" fpdf2 >&2
else
  "$PY" -m pip install --upgrade pip >&2
  "$PY" -m pip install --upgrade "$DOCLING_SPEC" fpdf2 >&2
fi

# --- sanity check --------------------------------------------------------------
if "$PY" -c "import docling, fpdf" >/dev/null 2>&1; then
  echo "[setup] docling + fpdf2 import OK ($("$PY" -c 'import fpdf; print("fpdf2", fpdf.__version__)'))" >&2
else
  echo "[setup] WARNING: 'import docling/fpdf' failed - the scripts will not run." >&2
  echo "[setup]          check the pip output above." >&2
  exit 1
fi

echo "[setup] done." >&2
echo "[setup]   data root : $HOME_DIR" >&2
echo "[setup]   python    : $PY" >&2
echo "[setup]   note      : Docling downloads ~1-2 GB of models on the FIRST read_pdf/inspect_pdf run." >&2
# stdout: the venv python to drive the scripts with
echo "$PY"
