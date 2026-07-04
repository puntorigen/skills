#!/usr/bin/env bash
# video-to-splat step 5 - preview the splat in a local Aholo viewer.
#
# Copies the chosen splat into the viewer's public/ dir, writes a tiny scene.json
# telling the app which file + type to load, then starts Vite. Open the printed
# URL in Chrome/Edge (WebGPU). Everything is served from localhost - nothing is
# uploaded.
#
# Usage:
#   preview.sh <splat-or-project> [--port N] [--no-open]
#
#   <splat-or-project>  a .sog/.spz/.ply file, or a project dir/name (prefers
#                       splat.sog, then splat.ply)
#   --port N            Vite port (default 5173)
#   --no-open           don't auto-open the browser
#
# Env: VIDEO_TO_SPLAT_HOME (default ~/.video-to-splat)
set -euo pipefail

VTS_HOME="${VIDEO_TO_SPLAT_HOME:-$HOME/.video-to-splat}"
VIEWER_DIR="$VTS_HOME/viewer"

PORT=5173
OPEN=1
INPUT_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --no-open) OPEN=0; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    -*) echo "preview: unknown option $1" >&2; exit 1 ;;
    *)  if [[ -z "$INPUT_ARG" ]]; then INPUT_ARG="$1"; shift;
        else echo "preview: unexpected arg $1" >&2; exit 1; fi ;;
  esac
done

[[ -z "$INPUT_ARG" ]] && { echo "preview: need a splat file or a project dir/name" >&2; exit 1; }
[[ -d "$VIEWER_DIR" ]] || { echo "preview: viewer not set up at $VIEWER_DIR (run setup_env.sh)" >&2; exit 1; }

# resolve the splat file
resolve() {
  local a="$1"
  if [[ -f "$a" ]]; then echo "$a"; return; fi
  for base in "$a" "$VTS_HOME/projects/$a"; do
    for f in splat.sog splat.spz splat.ply; do
      [[ -f "$base/$f" ]] && { echo "$base/$f"; return; }
    done
  done
  return 1
}
SPLAT="$(resolve "$INPUT_ARG")" || { echo "preview: no splat found for '$INPUT_ARG'" >&2; exit 1; }
SPLAT="$(cd "$(dirname "$SPLAT")" && pwd)/$(basename "$SPLAT")"

ext="${SPLAT##*.}"
case "$ext" in
  sog) TYPE="sog" ;;
  spz) TYPE="spz" ;;
  ply) TYPE="ply" ;;
  *) echo "preview: unsupported extension .$ext (use sog/spz/ply)" >&2; exit 1 ;;
esac

mkdir -p "$VIEWER_DIR/public"
# clear stale scenes so the app always loads the current one
rm -f "$VIEWER_DIR/public/scene."{sog,spz,ply} 2>/dev/null || true
cp "$SPLAT" "$VIEWER_DIR/public/scene.$ext"

# starting camera: use a real capture pose from the COLMAP model when available
# (an indoor splat looks black/noisy from an arbitrary outside viewpoint)
CAMERA_JSON=""
PROJ_DIR="$(dirname "$SPLAT")"
PYBIN="$VTS_HOME/.venv/bin/python"
if [[ -d "$PROJ_DIR/sparse/0" && -x "$PYBIN" ]]; then
  CAMERA_JSON="$("$PYBIN" - "$PROJ_DIR/sparse/0" <<'PY' 2>/dev/null || true
import json, sys
import numpy as np
import pycolmap
rec = pycolmap.Reconstruction(sys.argv[1])
images = sorted(rec.images.values(), key=lambda im: im.name)
im = images[len(images) // 2]          # mid-tour view: well inside the scene
cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
R_wc = np.asarray(cfw.rotation.matrix()).T
pos = np.asarray(im.projection_center(), dtype=float)
fwd = R_wc[:, 2]                       # camera +Z (look direction) in world
print(json.dumps({"position": [round(float(v), 5) for v in pos],
                  "forward": [round(float(v), 5) for v in fwd]}))
PY
)"
fi

if [[ -n "$CAMERA_JSON" ]]; then
  cat > "$VIEWER_DIR/public/scene.json" <<JSON
{ "file": "/scene.$ext", "type": "$TYPE", "camera": $CAMERA_JSON }
JSON
  echo "[preview] camera: starting at a mid-tour capture pose" >&2
else
  cat > "$VIEWER_DIR/public/scene.json" <<JSON
{ "file": "/scene.$ext", "type": "$TYPE" }
JSON
fi

echo "[preview] splat : $SPLAT ($(du -h "$SPLAT" | cut -f1))" >&2
echo "[preview] viewer: $VIEWER_DIR  (type: $TYPE)" >&2
echo "[preview] starting Vite on http://localhost:$PORT  - open in Chrome/Edge (WebGPU)." >&2
echo "[preview] press Ctrl-C to stop." >&2

cd "$VIEWER_DIR"
args=(vite --port "$PORT")
[[ "$OPEN" -eq 1 ]] && args+=(--open)
exec npx "${args[@]}"
