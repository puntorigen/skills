#!/usr/bin/env bash
# video-to-splat step 4 - compress the trained .ply into web-friendly .sog (and
# optionally .spz) with PlayCanvas splat-transform (MIT, run via npx).
#
# The Aholo viewer loads PLY, SPZ, SOG, SPLAT, KSPLAT and LCC. A raw 3DGS .ply is
# large (tens-hundreds of MB); .sog is the compact, streaming-friendly format
# Aholo (and PlayCanvas) recommend - typically ~10-20x smaller with little visible
# loss. We keep the .ply too (lossless master).
#
# Usage:
#   convert_splat.sh <ply-or-project> [--spz] [--out PATH.sog] [--cpu]
#
# Options:
#   --spz        also emit an .spz alongside the .sog
#   --out PATH   output .sog path (default: <project>/splat.sog)
#   --cpu        force CPU compression (slower; use if GPU/WebGPU is unavailable)
#
# Env: VIDEO_TO_SPLAT_HOME (default ~/.video-to-splat)
set -euo pipefail

VTS_HOME="${VIDEO_TO_SPLAT_HOME:-$HOME/.video-to-splat}"
ST_VERSION="${SPLAT_TRANSFORM_VERSION:-2.7.1}"

WANT_SPZ=0
OUT=""
FORCE_CPU=0
INPUT_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --spz) WANT_SPZ=1; shift ;;
    --out) OUT="$2"; shift 2 ;;
    --cpu) FORCE_CPU=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "convert_splat: unknown option $1" >&2; exit 1 ;;
    *)  if [[ -z "$INPUT_ARG" ]]; then INPUT_ARG="$1"; shift;
        else echo "convert_splat: unexpected arg $1" >&2; exit 1; fi ;;
  esac
done

[[ -z "$INPUT_ARG" ]] && { echo "convert_splat: need a .ply path or a project dir/name" >&2; exit 1; }
command -v npx >/dev/null 2>&1 || { echo "convert_splat: npx not found (brew install node)" >&2; exit 1; }

# resolve the input .ply
if [[ -f "$INPUT_ARG" ]]; then
  PLY="$INPUT_ARG"
elif [[ -f "$INPUT_ARG/splat.ply" ]]; then
  PLY="$INPUT_ARG/splat.ply"
elif [[ -f "$VTS_HOME/projects/$INPUT_ARG/splat.ply" ]]; then
  PLY="$VTS_HOME/projects/$INPUT_ARG/splat.ply"
else
  echo "convert_splat: no .ply found for '$INPUT_ARG'" >&2; exit 1
fi
PLY="$(cd "$(dirname "$PLY")" && pwd)/$(basename "$PLY")"

[[ -z "$OUT" ]] && OUT="$(dirname "$PLY")/splat.sog"
mkdir -p "$(dirname "$OUT")"

echo "[convert] input : $PLY ($(du -h "$PLY" | cut -f1))" >&2
echo "[convert] output: $OUT" >&2

run_transform() {
  # $@ are args after the package spec
  npx -y "@playcanvas/splat-transform@${ST_VERSION}" "$@"
}

# -w: overwrite - re-converting after retraining is the normal workflow
sog_args=(-w)
[[ "$FORCE_CPU" -eq 1 ]] && sog_args+=(-g cpu)
sog_args+=("$PLY" "$OUT")

echo "[convert] building .sog ..." >&2
if ! run_transform "${sog_args[@]}"; then
  if [[ "$FORCE_CPU" -eq 0 ]]; then
    echo "[convert] GPU compression failed; retrying on CPU (-g cpu) ..." >&2
    run_transform -w -g cpu "$PLY" "$OUT"
  else
    echo "[convert] ERROR: splat-transform failed." >&2
    exit 1
  fi
fi

[[ -f "$OUT" ]] || { echo "[convert] ERROR: expected $OUT was not produced" >&2; exit 1; }
echo "[convert] wrote $OUT ($(du -h "$OUT" | cut -f1))" >&2

if [[ "$WANT_SPZ" -eq 1 ]]; then
  SPZ="${OUT%.sog}.spz"
  echo "[convert] building .spz ..." >&2
  run_transform -w "$PLY" "$SPZ" && echo "[convert] wrote $SPZ ($(du -h "$SPZ" | cut -f1))" >&2 || \
    echo "[convert] WARNING: .spz conversion failed (continuing; .sog is the primary output)" >&2
fi

echo "[convert] next: preview.sh $OUT" >&2
# stdout: the .sog path
echo "$OUT"
