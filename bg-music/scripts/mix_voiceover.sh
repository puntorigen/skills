#!/usr/bin/env bash
# Mix a narration voiceover over background music with automatic ducking.
#
# The music is lowered whenever the voice is speaking (sidechain compression) and
# rises back in the gaps. Music is looped to cover the voice, with fade in/out.
#
# Usage:
#   mix_voiceover.sh --voice narration.mp3 --music bed.mp3 --out reel.mp3
#                    [--music-gain -8] [--duck 8] [--fade 2] [--mp3-quality 2]
#
#   --voice         narration audio (stays at full level)  [required]
#   --music         background music (gets ducked + looped) [required]
#   --out           output mixed mp3                         [required]
#   --music-gain    baseline music level in dB (default -8; lower = quieter bed)
#   --duck          ducking strength / compressor ratio (default 8; higher = more)
#   --fade          music fade in/out seconds (default 2)
#   --mp3-quality   ffmpeg libmp3lame -q:a (default 2)
#
# Prints the output path on the last stdout line.
set -euo pipefail

VOICE="" ; MUSIC="" ; OUT=""
GAIN="-8" ; DUCK="8" ; FADE="2" ; QUALITY="2"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --voice) VOICE="${2:-}"; shift 2 ;;
    --music) MUSIC="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --music-gain) GAIN="${2:-}"; shift 2 ;;
    --duck) DUCK="${2:-}"; shift 2 ;;
    --fade) FADE="${2:-}"; shift 2 ;;
    --mp3-quality) QUALITY="${2:-}"; shift 2 ;;
    *) echo "[mix] unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$VOICE" || -z "$MUSIC" || -z "$OUT" ]]; then
  echo "usage: mix_voiceover.sh --voice <narration> --music <bed> --out <mixed.mp3> [opts]" >&2
  exit 2
fi
[[ -f "$VOICE" ]] || { echo "[mix] voice not found: $VOICE" >&2; exit 2; }
[[ -f "$MUSIC" ]] || { echo "[mix] music not found: $MUSIC" >&2; exit 2; }
command -v ffmpeg  >/dev/null 2>&1 || { echo "[mix] ffmpeg not on PATH" >&2; exit 1; }
command -v ffprobe >/dev/null 2>&1 || { echo "[mix] ffprobe not on PATH" >&2; exit 1; }

# Total length follows the narration.
DUR="$(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "$VOICE" 2>/dev/null || echo 0)"
[[ -z "$DUR" || "$DUR" == "N/A" ]] && DUR=0
# Fade-out start = end - fade (floored at 0), computed in awk for float safety.
ST_OUT="$(awk -v d="$DUR" -v f="$FADE" 'BEGIN{v=d-f; if(v<0)v=0; printf "%.3f", v}')"

mkdir -p "$(dirname "$(cd "$(dirname "$OUT")" 2>/dev/null && pwd || echo .)/$(basename "$OUT")")" 2>/dev/null || true
mkdir -p "$(dirname "$OUT")"

echo "[mix] voice=${VOICE##*/} music=${MUSIC##*/} dur=${DUR}s gain=${GAIN}dB duck=${DUCK} fade=${FADE}s" >&2

# Filtergraph:
#  - music: to stereo/44.1k, apply baseline gain + fade in/out
#  - voice: split into a sidechain key and the mix copy
#  - sidechaincompress: duck the music using the voice as the key
#  - amix: combine ducked music + clean voice (no auto-normalize)
FILTER="[1:a]aformat=sample_rates=44100:channel_layouts=stereo,volume=${GAIN}dB,afade=t=in:st=0:d=${FADE},afade=t=out:st=${ST_OUT}:d=${FADE}[m];
[0:a]aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[vkey][vmix];
[m][vkey]sidechaincompress=threshold=0.03:ratio=${DUCK}:attack=20:release=300[mduck];
[vmix][mduck]amix=inputs=2:duration=first:normalize=0[out]"

ffmpeg -y -loglevel error -i "$VOICE" -stream_loop -1 -i "$MUSIC" \
  -filter_complex "$FILTER" -map "[out]" \
  -c:a libmp3lame -q:a "$QUALITY" "$OUT" >&2 || { echo "[mix] ffmpeg failed" >&2; exit 1; }

echo "[mix] wrote $OUT" >&2
echo "$OUT"
