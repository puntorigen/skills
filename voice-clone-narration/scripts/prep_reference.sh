#!/usr/bin/env bash
# Prep a reference clip for voice cloning: convert any audio/video input into a
# clean mono 24 kHz 16-bit WAV in the voice library, with optional trimming and
# a quality sanity-check.
#
# Usage:  prep_reference.sh <voice-name> <input-audio> [--start SS] [--duration SS]
#   --start SS      seconds to skip from the start (before decoding)
#   --duration SS   length in seconds to keep
#
# Writes:  ~/.voice-clone-narration/voices/<voice-name>.wav
# Prints the output wav path on the last stdout line.
#
# Env:  VOICE_CLONE_HOME  data root (default ~/.voice-clone-narration)
set -euo pipefail

NAME="${1:-}"
INPUT="${2:-}"
if [[ -z "$NAME" || -z "$INPUT" ]]; then
  echo "usage: prep_reference.sh <voice-name> <input-audio> [--start SS] [--duration SS]" >&2
  exit 2
fi
shift 2

START=""
DURATION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --start) START="${2:-}"; shift 2 ;;
    --duration) DURATION="${2:-}"; shift 2 ;;
    *) echo "[prep] unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -f "$INPUT" ]] || { echo "[prep] input not found: $INPUT" >&2; exit 2; }
command -v ffmpeg >/dev/null 2>&1 || { echo "[prep] ffmpeg not on PATH" >&2; exit 1; }

VC_HOME="${VOICE_CLONE_HOME:-$HOME/.voice-clone-narration}"
VOICES="$VC_HOME/voices"
mkdir -p "$VOICES"

SAFE_NAME="$(printf '%s' "$NAME" | tr -c 'A-Za-z0-9._-' '-')"
OUT="$VOICES/$SAFE_NAME.wav"

# Build ffmpeg args: -ss before -i is fast/accurate enough for trimming a clip.
args=(-y)
[[ -n "$START" ]] && args+=(-ss "$START")
args+=(-i "$INPUT")
[[ -n "$DURATION" ]] && args+=(-t "$DURATION")
# mono, 24 kHz, signed 16-bit PCM
args+=(-ac 1 -ar 24000 -sample_fmt s16 "$OUT")

echo "[prep] converting -> $OUT" >&2
ffmpeg "${args[@]}" >/dev/null 2>&1 || { echo "[prep] ffmpeg failed" >&2; exit 1; }

# --- sanity checks -------------------------------------------------------------
DUR="$(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "$OUT" 2>/dev/null || echo 0)"
DUR_INT="${DUR%.*}"; DUR_INT="${DUR_INT:-0}"

if (( DUR_INT < 4 )); then
  echo "[prep] WARNING: clip is ${DUR}s - very short. Cloning is best with ~7-15s of clean speech." >&2
elif (( DUR_INT > 25 )); then
  echo "[prep] NOTE: clip is ${DUR}s - only the first ~10s is used for cloning. Consider trimming with --start/--duration." >&2
fi

# Loudness check: mean_volume from ffmpeg volumedetect.
MEANV="$(ffmpeg -hide_banner -i "$OUT" -af volumedetect -f null /dev/null 2>&1 \
  | sed -n 's/.*mean_volume: \(-*[0-9.]*\) dB/\1/p' | head -1)"
if [[ -n "$MEANV" ]]; then
  # mean below -35 dB is likely too quiet / far from mic
  if awk "BEGIN{exit !($MEANV < -35)}"; then
    echo "[prep] WARNING: reference is quiet (mean ${MEANV} dB). Use a closer, clearer recording for best results." >&2
  fi
fi

echo "[prep] ready: $SAFE_NAME (${DUR}s, mean ${MEANV:-?} dB)" >&2
echo "$OUT"
