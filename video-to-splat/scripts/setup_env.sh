#!/usr/bin/env bash
# Setup for video-to-splat: everything needed to turn an mp4 into an Aholo-ready
# Gaussian splat, entirely local on Apple Silicon.
#
#   - a uv venv with pycolmap (SfM), opencv/numpy/pillow (frame scoring)
#   - the Brush training binary (prebuilt macOS release, Metal via WebGPU)
#   - a minimal Aholo viewer app (vite + @manycore/aholo-viewer) for preview
#
# All of it lives OUTSIDE the repo under ~/.video-to-splat/. Nothing is ever
# uploaded anywhere. Internet is used only on first run to fetch these tools
# (and pycolmap SIFT / the splat itself never leave the machine).
#
# Idempotent: re-running skips work that's already done. Prints the venv python
# on the last stdout line.
#
# Usage:  setup_env.sh [--update]
#   --update   re-download the Brush binary and refresh npm/venv packages
# Env:
#   VIDEO_TO_SPLAT_HOME     data root (default ~/.video-to-splat)
#   VIDEO_TO_SPLAT_PYTHON   python version for the venv (default 3.11)
#   BRUSH_VERSION           Brush release tag to install (default v0.3.0)
set -euo pipefail

UPDATE=0
[[ "${1:-}" == "--update" ]] && UPDATE=1

VTS_HOME="${VIDEO_TO_SPLAT_HOME:-$HOME/.video-to-splat}"
VENV="$VTS_HOME/.venv"
PYTHON_VERSION="${VIDEO_TO_SPLAT_PYTHON:-3.11}"
BRUSH_VERSION="${BRUSH_VERSION:-v0.3.0}"
BRUSH_DIR="$VTS_HOME/brush"
BRUSH_BIN="$BRUSH_DIR/brush_app"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
VIEWER_SRC="$SKILL_DIR/viewer"
VIEWER_DIR="$VTS_HOME/viewer"

mkdir -p "$VTS_HOME/projects"

# --- platform check ------------------------------------------------------------
OS="$(uname -s)"; ARCH="$(uname -m)"
if [[ "$OS" != "Darwin" || "$ARCH" != "arm64" ]]; then
  echo "[setup] WARNING: this skill targets Apple Silicon (Darwin/arm64)." >&2
  echo "[setup]          Detected $OS/$ARCH. The prebuilt Brush binary and the" >&2
  echo "[setup]          pycolmap wheels installed here are for macOS arm64;" >&2
  echo "[setup]          training/reconstruction will likely not run here." >&2
fi

# --- required tools ------------------------------------------------------------
missing=0
for tool in ffmpeg ffprobe uv node npx; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    missing=1
    echo "[setup] '$tool' not found on PATH." >&2
    case "$tool" in
      ffmpeg|ffprobe) echo "[setup]   install: brew install ffmpeg" >&2 ;;
      uv)             echo "[setup]   install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2 ;;
      node|npx)       echo "[setup]   install: brew install node" >&2 ;;
    esac
  fi
done
[[ "$missing" -eq 1 ]] && exit 1

# --- python venv + pycolmap ----------------------------------------------------
if [[ ! -x "$VENV/bin/python" || "$UPDATE" -eq 1 ]]; then
  echo "[setup] creating venv at $VENV (python $PYTHON_VERSION)" >&2
  uv venv --python "$PYTHON_VERSION" "$VENV" >&2
fi
PY="$VENV/bin/python"
echo "[setup] installing pycolmap + frame-scoring deps (first run downloads wheels)" >&2
uv pip install --python "$PY" --upgrade pycolmap "opencv-python-headless" numpy pillow >&2

if "$PY" -c "import pycolmap; print(pycolmap.__version__)" >/dev/null 2>&1; then
  echo "[setup] pycolmap import OK ($("$PY" -c 'import pycolmap; print(pycolmap.__version__)'))" >&2
else
  echo "[setup] WARNING: 'import pycolmap' failed. On Intel Macs / old macOS there is" >&2
  echo "[setup]          no wheel; reconstruction will not work here." >&2
fi

# --- Brush training binary -----------------------------------------------------
if [[ ! -x "$BRUSH_BIN" || "$UPDATE" -eq 1 ]]; then
  if [[ "$OS" == "Darwin" && "$ARCH" == "arm64" ]]; then
    asset="brush-app-aarch64-apple-darwin.tar.xz"
    url="https://github.com/ArthurBrussee/brush/releases/download/$BRUSH_VERSION/$asset"
    echo "[setup] downloading Brush $BRUSH_VERSION ($asset)" >&2
    mkdir -p "$BRUSH_DIR"
    tmp="$BRUSH_DIR/$asset"
    if curl -fL --retry 2 -o "$tmp" "$url"; then
      # release layout: files at the archive root, incl. the brush_app executable
      tar -xf "$tmp" -C "$BRUSH_DIR"
      rm -f "$tmp"
      found="$(/usr/bin/find "$BRUSH_DIR" -name brush_app -type f | head -n1)"
      if [[ -n "$found" && "$found" != "$BRUSH_BIN" ]]; then
        mv "$found" "$BRUSH_BIN"
      fi
      chmod +x "$BRUSH_BIN" 2>/dev/null || true
      echo "[setup] Brush installed at $BRUSH_BIN" >&2
    else
      echo "[setup] WARNING: could not download Brush. Install it manually from" >&2
      echo "[setup]          https://github.com/ArthurBrussee/brush/releases and place" >&2
      echo "[setup]          the brush_app binary at $BRUSH_BIN" >&2
    fi
  else
    echo "[setup] skipping Brush download (no arm64 macOS binary for $OS/$ARCH)." >&2
  fi
else
  echo "[setup] Brush already present at $BRUSH_BIN (use --update to re-download)" >&2
fi

# --- COLMAP vocab tree (loop detection) -----------------------------------------
# ~9 MB, enables run_colmap.py --loop-detection (helps loopy multi-floor tours).
# Must be the faiss-format tree from the COLMAP release assets - the legacy
# flann trees on demuc.de crash pycolmap >= 3.12.
VOCAB_TREE="$VTS_HOME/vocab_tree_faiss_flickr100K_words32K.bin"
if [[ ! -s "$VOCAB_TREE" || "$UPDATE" -eq 1 ]]; then
  echo "[setup] downloading COLMAP vocab tree (loop detection)" >&2
  curl -fL --retry 2 -o "$VOCAB_TREE" \
    "https://github.com/colmap/colmap/releases/download/3.11.1/vocab_tree_faiss_flickr100K_words32K.bin" >&2 || \
    echo "[setup] WARNING: vocab tree download failed; --loop-detection will need --vocab-tree" >&2
else
  echo "[setup] vocab tree present at $VOCAB_TREE" >&2
fi

# --- viewer app (vite + @manycore/aholo-viewer) --------------------------------
echo "[setup] preparing Aholo viewer app in $VIEWER_DIR" >&2
mkdir -p "$VIEWER_DIR/public"
# copy the template (index.html/index.ts/package.json/tsconfig.json), never node_modules
for f in package.json tsconfig.json index.html index.ts vite.config.ts; do
  [[ -f "$VIEWER_SRC/$f" ]] && cp "$VIEWER_SRC/$f" "$VIEWER_DIR/$f"
done
if [[ ! -d "$VIEWER_DIR/node_modules" || "$UPDATE" -eq 1 ]]; then
  echo "[setup] installing viewer npm deps (first run downloads @manycore/aholo-viewer + vite)" >&2
  ( cd "$VIEWER_DIR" && npm install >&2 ) || \
    echo "[setup] WARNING: npm install failed; you can retry with 'cd $VIEWER_DIR && npm install'" >&2
else
  echo "[setup] viewer node_modules present (use --update to refresh)" >&2
fi

echo "[setup] done." >&2
echo "[setup]   data root : $VTS_HOME" >&2
echo "[setup]   python    : $PY" >&2
echo "[setup]   brush     : $BRUSH_BIN" >&2
echo "[setup]   viewer    : $VIEWER_DIR" >&2
echo "[setup]   projects  : $VTS_HOME/projects" >&2
# stdout: the venv python to drive the python scripts with
echo "$PY"
