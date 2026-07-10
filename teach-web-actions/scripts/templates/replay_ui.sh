#!/usr/bin/env bash
# UI replay for a generated web-action skill (self-contained).
#
# Injects the embedded HAR's session cookies into a skill-local Chrome profile,
# runs a variant module (recorded UI steps with your substituted inputs),
# records video, and converts it to an mp4 proof.
#
# Usage:  replay_ui.sh [<variant.js>]
#   <variant.js> defaults to <skill>/variant.js, falling back to the shipped
#   scripts/variant.js scaffold. It exports:
#     module.exports = async (page, params) => { ... }
# Env:
#   PW_CHANNEL   chrome (default) | chromium | msedge | "" (bundled chromium)
#   PW_HEADLESS  1 to run headless
#   PW_PARAMS    JSON object passed to the variant as `params`
# Prints the path to proof.mp4 on stdout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HAR_PATH="$SKILL_DIR/data/session.har"
PW_CHANNEL="${PW_CHANNEL-chrome}"

VARIANT="${1:-}"
if [[ -z "$VARIANT" ]]; then
  if [[ -f "$SKILL_DIR/variant.js" ]]; then
    VARIANT="$SKILL_DIR/variant.js"
  else
    VARIANT="$SCRIPT_DIR/variant.js"
  fi
fi
[[ -f "$VARIANT" ]] || { echo "variant not found: $VARIANT" >&2; exit 2; }
[[ -f "$HAR_PATH" ]] || { echo "no embedded HAR at $HAR_PATH" >&2; exit 2; }

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
RUN_DIR="$SKILL_DIR/runs/$TS"
VIDEO_DIR="$RUN_DIR/video"
mkdir -p "$VIDEO_DIR"

# extract the embedded session cookies for injection (values never printed)
COOKIES="$RUN_DIR/cookies.json"
python3 "$SCRIPT_DIR/har_auth.py" "$HAR_PATH" --out "$COOKIES" >&2 || true

echo "[replay] running variant -> $RUN_DIR" >&2
PW_CHANNEL="$PW_CHANNEL" node "$SCRIPT_DIR/run_variant.js" "$VARIANT" "$VIDEO_DIR" "$COOKIES" >&2

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
  list="$RUN_DIR/_concat.txt"; : > "$list"
  for w in "${webms[@]}"; do printf "file '%s'\n" "$w" >> "$list"; done
  ffmpeg -y -f concat -safe 0 -i "$list" -movflags +faststart -pix_fmt yuv420p "$PROOF" >&2
  rm -f "$list"
fi

echo "[replay] proof written" >&2
echo "$PROOF"
