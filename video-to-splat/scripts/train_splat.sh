#!/usr/bin/env bash
# video-to-splat step 3 - train a Gaussian splat from the COLMAP model with Brush.
#
# Brush (github.com/ArthurBrussee/brush, Apache-2.0) trains 3DGS natively on the
# Apple GPU via WebGPU/Metal - no CUDA, no python ML stack. We run its prebuilt
# binary headlessly: given a source path (the project dir) it trains and exports
# a standard 3DGS .ply. That .ply is already loadable by the Aholo viewer; step 4
# compresses it to .sog for the web.
#
# Usage:
#   train_splat.sh <project-dir-or-name> [--steps N] [--sh-degree D]
#                  [--max-resolution R] [--max-splats N] [--with-viewer]
#                  [-- <extra brush flags>]
#
# Options:
#   --steps N           training iterations (default 30000; try 2000 for a smoke test)
#   --sh-degree D       spherical-harmonics degree 0-4 (default 2; lower = smaller file)
#   --max-resolution R  cap training image long side (default 1600)
#   --max-splats N      hard cap on splat count (optional)
#   --with-viewer       open Brush's live GUI instead of headless (won't auto-exit)
#   --                  pass any remaining args straight to brush_app
#
# Env:
#   VIDEO_TO_SPLAT_HOME   data root (default ~/.video-to-splat)
#   BRUSH_BIN             override path to the brush_app binary
set -euo pipefail

VTS_HOME="${VIDEO_TO_SPLAT_HOME:-$HOME/.video-to-splat}"
BRUSH_BIN="${BRUSH_BIN:-$VTS_HOME/brush/brush_app}"

STEPS=30000
SH_DEGREE=2
MAX_RES=1600
MAX_SPLATS=""
WITH_VIEWER=0
PROJECT_ARG=""
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --steps)          STEPS="$2"; shift 2 ;;
    --sh-degree)      SH_DEGREE="$2"; shift 2 ;;
    --max-resolution) MAX_RES="$2"; shift 2 ;;
    --max-splats)     MAX_SPLATS="$2"; shift 2 ;;
    --with-viewer)    WITH_VIEWER=1; shift ;;
    --)               shift; EXTRA=("$@"); break ;;
    -h|--help)        sed -n '2,30p' "$0"; exit 0 ;;
    -*)               echo "train_splat: unknown option $1" >&2; exit 1 ;;
    *)                if [[ -z "$PROJECT_ARG" ]]; then PROJECT_ARG="$1"; shift;
                      else echo "train_splat: unexpected arg $1" >&2; exit 1; fi ;;
  esac
done

[[ -z "$PROJECT_ARG" ]] && { echo "train_splat: need a project dir or name" >&2; exit 1; }
[[ -x "$BRUSH_BIN" ]] || { echo "train_splat: brush binary not found at $BRUSH_BIN (run setup_env.sh)" >&2; exit 1; }

# resolve the project dir (accept a name, the dir, or the images dir)
if [[ -d "$PROJECT_ARG/sparse" || -d "$PROJECT_ARG/images" ]]; then
  PROJECT="$PROJECT_ARG"
elif [[ -d "$VTS_HOME/projects/$PROJECT_ARG" ]]; then
  PROJECT="$VTS_HOME/projects/$PROJECT_ARG"
else
  echo "train_splat: no project found for '$PROJECT_ARG'" >&2; exit 1
fi
PROJECT="$(cd "$PROJECT" && pwd)"

if [[ ! -d "$PROJECT/sparse/0" ]]; then
  echo "train_splat: no COLMAP model at $PROJECT/sparse/0 - run run_colmap.py first" >&2
  exit 1
fi

PLY_OUT="$PROJECT/splat.ply"
rm -f "$PLY_OUT"

# Brush scans its source dir RECURSIVELY and trains on the first
# cameras.bin/images.bin/points3d.bin it finds anywhere. run_colmap.py keeps
# every disconnected sub-model (sparse/0, sparse/1, ...), so feeding the raw
# project dir would mix files from different sub-models = garbage training.
# Stage a clean view holding only sparse/0.
SRC="$PROJECT"
STAGE=""
if compgen -G "$PROJECT/sparse/[1-9]*" > /dev/null; then
  STAGE="$(mktemp -d "${TMPDIR:-/tmp}/brush-src.XXXXXX")"
  ln -s "$PROJECT/images" "$STAGE/images"
  mkdir -p "$STAGE/sparse"
  cp -R "$PROJECT/sparse/0" "$STAGE/sparse/0"
  SRC="$STAGE"
  echo "[train] staging     : $STAGE (sparse/0 only; project has extra sub-models)" >&2
  trap '[[ -n "$STAGE" ]] && rm -rf "$STAGE"' EXIT
fi

echo "[train] project    : $PROJECT" >&2
echo "[train] steps       : $STEPS   sh-degree: $SH_DEGREE   max-res: $MAX_RES" >&2
echo "[train] brush       : $BRUSH_BIN" >&2
echo "[train] export      : $PLY_OUT" >&2
echo "[train] NOTE: training is the slow stage (~minutes at 2k steps, hours at 30k on M-series)." >&2

CMD=("$BRUSH_BIN" "$SRC"
     --total-steps "$STEPS"
     --sh-degree "$SH_DEGREE"
     --max-resolution "$MAX_RES"
     --export-path "$PROJECT/"
     --export-name "splat.ply"
     --export-every "$STEPS")
[[ -n "$MAX_SPLATS" ]] && CMD+=(--max-splats "$MAX_SPLATS")
# --with-viewer is a plain flag (present = open the live GUI, which won't auto-exit)
[[ "$WITH_VIEWER" -eq 1 ]] && CMD+=(--with-viewer)
[[ ${#EXTRA[@]} -gt 0 ]] && CMD+=("${EXTRA[@]}")

echo "[train] running: ${CMD[*]}" >&2
"${CMD[@]}"

# Brush writes export_<iter>.ply / splat.ply into --export-path. Resolve the result.
if [[ ! -f "$PLY_OUT" ]]; then
  newest="$(/usr/bin/find "$PROJECT" -maxdepth 2 -name '*.ply' -type f -print0 2>/dev/null \
            | xargs -0 ls -t 2>/dev/null | head -n1 || true)"
  if [[ -n "$newest" && -f "$newest" ]]; then
    cp "$newest" "$PLY_OUT"
  fi
fi

if [[ ! -f "$PLY_OUT" ]]; then
  echo "[train] ERROR: no exported .ply found under $PROJECT. Check the Brush output above." >&2
  exit 1
fi

echo "[train] done. splat: $PLY_OUT ($(du -h "$PLY_OUT" | cut -f1))" >&2
echo "[train] next: convert_splat.sh $PROJECT" >&2
# stdout: the trained ply path
echo "$PLY_OUT"
