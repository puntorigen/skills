#!/usr/bin/env bash
# object-to-3d - preview an object splat in a local Three.js orbit viewer.
#
# Copies the chosen splat into the viewer's public/ dir, writes a tiny scene.json
# telling the app which file + type to load, then starts Vite. Open the printed
# URL in any modern browser (WebGL2). Everything is served from localhost.
#
# Two preview modes:
#   (default) Splat Preview - the raw Gaussian-splat scan (honest capture data,
#             may show holes on unseen faces like the underside).
#   --print   Print Preview - the repaired, watertight mesh (mesh/object.glb from
#             splat_to_mesh.py): what actually gets 3D printed.
#
# Usage:
#   preview.sh <splat-or-project> [--file NAME] [--port N] [--no-open]
#   preview.sh <project> --print [--file object.glb] [--port N] [--no-open]
#
#   <splat-or-project>  a .ply/.sog/.spz file, or a project dir/name
#                       (default preference: cleaned.ply, then splat.sog, splat.ply)
#   --print             show the repaired print mesh (mesh/object.glb) instead
#   --file NAME         load a specific file from the project (e.g. cleaned.ply,
#                       splat.ply, or object.glb with --print). May be a path.
#   --port N            Vite port (default 5173)
#   --no-open           don't auto-open the browser
#
# Env: VIDEO_TO_SPLAT_HOME (default ~/.video-to-splat)
set -euo pipefail

VTS_HOME="${VIDEO_TO_SPLAT_HOME:-$HOME/.video-to-splat}"
VIEWER_DIR="$VTS_HOME/viewer-object"

PORT=5173
OPEN=1
FILE=""
INPUT_ARG=""
PRINT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file) FILE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --print) PRINT=1; shift ;;
    --no-open) OPEN=0; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    -*) echo "preview: unknown option $1" >&2; exit 1 ;;
    *)  if [[ -z "$INPUT_ARG" ]]; then INPUT_ARG="$1"; shift;
        else echo "preview: unexpected arg $1" >&2; exit 1; fi ;;
  esac
done

[[ -z "$INPUT_ARG" ]] && { echo "preview: need a splat file or a project dir/name" >&2; exit 1; }
[[ -d "$VIEWER_DIR" ]] || { echo "preview: orbit viewer not set up at $VIEWER_DIR (run setup_env.sh)" >&2; exit 1; }

# resolve the project base dir for a name/dir argument
resolve_base() {
  local a="$1"
  if [[ -d "$a" ]]; then echo "$a"; return 0; fi
  [[ -d "$VTS_HOME/projects/$a" ]] && { echo "$VTS_HOME/projects/$a"; return 0; }
  return 1
}

# ---- Print Preview: serve the repaired watertight mesh (GLB) --------------
if [[ "$PRINT" -eq 1 ]]; then
  MESH=""
  if [[ -f "$INPUT_ARG" && "${INPUT_ARG##*.}" == "glb" ]]; then
    MESH="$INPUT_ARG"
  else
    BASE="$(resolve_base "$INPUT_ARG")" || { echo "preview: no project found for '$INPUT_ARG'" >&2; exit 1; }
    if [[ -n "$FILE" ]]; then
      if [[ -f "$FILE" ]]; then MESH="$FILE";
      elif [[ -f "$BASE/$FILE" ]]; then MESH="$BASE/$FILE";
      elif [[ -f "$BASE/mesh/$FILE" ]]; then MESH="$BASE/mesh/$FILE";
      else echo "preview: --file '$FILE' not found (looked at '$FILE', '$BASE/$FILE', '$BASE/mesh/$FILE')" >&2; exit 1; fi
    else
      for f in mesh/object.glb object.glb; do
        [[ -f "$BASE/$f" ]] && { MESH="$BASE/$f"; break; }
      done
      [[ -z "$MESH" ]] && { echo "preview: no print mesh (mesh/object.glb) in $BASE - run splat_to_mesh.py first" >&2; exit 1; }
    fi
  fi
  MESH="$(cd "$(dirname "$MESH")" && pwd)/$(basename "$MESH")"

  mkdir -p "$VIEWER_DIR/public"
  rm -f "$VIEWER_DIR/public/scene."{sog,spz,ply,splat,ksplat,glb} 2>/dev/null || true
  cp "$MESH" "$VIEWER_DIR/public/scene.glb"
  cat > "$VIEWER_DIR/public/scene.json" <<JSON
{ "file": "/scene.glb", "type": "glb", "mode": "print" }
JSON

  echo "[preview] print mesh: $MESH ($(du -h "$MESH" | cut -f1))" >&2
  echo "[preview] viewer: $VIEWER_DIR  (Print Preview - repaired watertight mesh)" >&2
  echo "[preview] starting Vite on http://localhost:$PORT  - open in any modern browser." >&2
  echo "[preview] press Ctrl-C to stop." >&2
  cd "$VIEWER_DIR"
  args=(vite --port "$PORT")
  [[ "$OPEN" -eq 1 ]] && args+=(--open)
  exec npx "${args[@]}"
fi

# resolve the splat file to serve
SPLAT=""
if [[ -f "$INPUT_ARG" ]]; then
  SPLAT="$INPUT_ARG"
else
  BASE="$(resolve_base "$INPUT_ARG")" || { echo "preview: no project found for '$INPUT_ARG'" >&2; exit 1; }
  if [[ -n "$FILE" ]]; then
    if [[ -f "$FILE" ]]; then SPLAT="$FILE";
    elif [[ -f "$BASE/$FILE" ]]; then SPLAT="$BASE/$FILE";
    else echo "preview: --file '$FILE' not found (looked at '$FILE' and '$BASE/$FILE')" >&2; exit 1; fi
  else
    for f in cleaned.ply splat.ply splat.splat splat.spz splat.sog; do
      [[ -f "$BASE/$f" ]] && { SPLAT="$BASE/$f"; break; }
    done
    [[ -z "$SPLAT" ]] && { echo "preview: no splat (cleaned.ply/splat.sog/splat.ply) in $BASE" >&2; exit 1; }
  fi
fi
SPLAT="$(cd "$(dirname "$SPLAT")" && pwd)/$(basename "$SPLAT")"

ext="${SPLAT##*.}"
case "$ext" in
  sog) TYPE="sog" ;;
  spz) TYPE="spz" ;;
  ply) TYPE="ply" ;;
  splat) TYPE="splat" ;;
  *) echo "preview: unsupported extension .$ext (use sog/spz/ply/splat)" >&2; exit 1 ;;
esac

mkdir -p "$VIEWER_DIR/public"
# clear stale scenes so the app always loads the current one
rm -f "$VIEWER_DIR/public/scene."{sog,spz,ply,splat,ksplat,glb} 2>/dev/null || true
cp "$SPLAT" "$VIEWER_DIR/public/scene.$ext"

# Three.js viewer reads ply / splat / spz / ksplat — convert .sog on the fly.
if [[ "$TYPE" == "sog" ]]; then
  echo "[preview] converting scene.sog -> scene.ply (Three.js viewer)" >&2
  ST_VERSION="${SPLAT_TRANSFORM_VERSION:-0.11.0}"
  npx -y "@playcanvas/splat-transform@${ST_VERSION}" \
    "$VIEWER_DIR/public/scene.sog" "$VIEWER_DIR/public/scene.ply" >&2
  rm -f "$VIEWER_DIR/public/scene.sog"
  ext=ply
  TYPE=ply
fi

cat > "$VIEWER_DIR/public/scene.json" <<JSON
{ "file": "/scene.$ext", "type": "$TYPE" }
JSON

echo "[preview] splat : $SPLAT ($(du -h "$SPLAT" | cut -f1))" >&2
echo "[preview] viewer: $VIEWER_DIR  (type: $TYPE, Three.js orbit)" >&2
echo "[preview] starting Vite on http://localhost:$PORT  - open in any modern browser." >&2
echo "[preview] press Ctrl-C to stop." >&2

cd "$VIEWER_DIR"
args=(vite --port "$PORT")
[[ "$OPEN" -eq 1 ]] && args+=(--open)
exec npx "${args[@]}"
