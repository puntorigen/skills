#!/usr/bin/env bash
# Phase 3 (UI mode) of generate-web-skills: replay a variant of the recorded UI
# flow, record video, and convert it to mp4 proof.
#
# Usage:  replay_ui.sh <lesson-dir> <variant.js>
#   <variant.js> exports `module.exports = async (page) => { ... }` (see SKILL.md)
# Env:
#   PW_CHANNEL   chrome (default) | chromium | msedge | "" (bundled chromium)
#   PW_HEADLESS  1 to run headless
# Prints the path to proof.mp4 on stdout.
set -euo pipefail

LESSON_DIR="${1:-}"
VARIANT="${2:-}"
if [[ -z "$LESSON_DIR" || -z "$VARIANT" ]]; then
  echo "usage: replay_ui.sh <lesson-dir> <variant.js>" >&2
  exit 2
fi
[[ -d "$LESSON_DIR" ]] || { echo "no such lesson dir: $LESSON_DIR" >&2; exit 2; }
# resolve variant relative to lesson dir if not absolute / not found as given
if [[ ! -f "$VARIANT" ]]; then
  if [[ -f "$LESSON_DIR/$VARIANT" ]]; then
    VARIANT="$LESSON_DIR/$VARIANT"
  else
    echo "variant not found: $VARIANT" >&2; exit 2
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PW_CHANNEL="${PW_CHANNEL-chrome}"

command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg not on PATH" >&2; exit 1; }

if [[ ! -d "$SCRIPT_DIR/node_modules/playwright" ]]; then
  echo "[replay] installing playwright npm package (first run)..." >&2
  (cd "$SCRIPT_DIR" && npm install --no-audit --no-fund >&2)
fi
# Video recording needs Playwright's own bundled ffmpeg (separate from the
# system ffmpeg used for the webm->mp4 step below); install is idempotent.
(cd "$SCRIPT_DIR" && npx --no-install playwright install ffmpeg >&2) || true
if [[ -z "$PW_CHANNEL" ]]; then
  (cd "$SCRIPT_DIR" && npx --no-install playwright install chromium >&2) || true
fi

TS="$(date -u +%Y%m%d-%H%M%S)"
RUN_DIR="$LESSON_DIR/runs/$TS"
VIDEO_DIR="$RUN_DIR/video"
mkdir -p "$VIDEO_DIR"

echo "[replay] running variant -> $RUN_DIR" >&2
PW_CHANNEL="$PW_CHANNEL" node "$SCRIPT_DIR/run_variant.js" "$VARIANT" "$VIDEO_DIR" >&2

# Playwright writes one .webm per page/context into VIDEO_DIR.
shopt -s nullglob
webms=("$VIDEO_DIR"/*.webm)
if (( ${#webms[@]} == 0 )); then
  echo "[replay] no video was produced (did the variant open a page?)" >&2
  exit 1
fi

PROOF="$RUN_DIR/proof.mp4"
if (( ${#webms[@]} == 1 )); then
  ffmpeg -y -i "${webms[0]}" -movflags +faststart -pix_fmt yuv420p "$PROOF" >&2
else
  # concat multiple segments in filename order
  list="$RUN_DIR/_concat.txt"; : > "$list"
  for w in "${webms[@]}"; do printf "file '%s'\n" "$w" >> "$list"; done
  ffmpeg -y -f concat -safe 0 -i "$list" -movflags +faststart -pix_fmt yuv420p "$PROOF" >&2
  rm -f "$list"
fi

echo "[replay] proof written" >&2
echo "$PROOF"
