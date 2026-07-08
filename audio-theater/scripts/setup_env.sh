#!/usr/bin/env bash
# Setup check for audio-theater. This skill has NO models and NO venv of its own:
# it is a pure ffmpeg + stdlib orchestrator that drives three sibling local skills
# through their installed venvs. This script only verifies that ffmpeg and those
# siblings are set up, and prints the exact command to fix whatever is missing.
#
#   required : ffmpeg + voice-clone-narration (voices)
#   optional : bg-music (music cues), sound-effects (SFX cues) - degrade gracefully
#
# Usage:  setup_env.sh
# Env overrides: VOICE_CLONE_HOME, BG_MUSIC_HOME, SOUND_EFFECTS_HOME,
#                and <SKILL>_DIR to point at a sibling skill install.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILLS_ROOT="$(cd "$SKILL_DIR/.." && pwd)"

missing_required=0

echo "[setup] audio-theater is an orchestrator (no venv/models of its own)." >&2

# --- ffmpeg / ffprobe ----------------------------------------------------------
for tool in ffmpeg ffprobe; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "[ok]   $tool: $(command -v "$tool")" >&2
  else
    echo "[MISS] $tool not on PATH -> brew install ffmpeg" >&2
    missing_required=1
  fi
done

# Locate a sibling skill dir (env override, then alongside this skill, then common roots).
find_skill_dir() {
  local skill="$1"
  local envvar
  envvar="$(printf '%s' "$skill" | tr 'a-z-' 'A-Z_')_DIR"
  local override="${!envvar:-}"
  if [[ -n "$override" && -d "$override" ]]; then echo "$override"; return 0; fi
  for root in "$SKILLS_ROOT" "$HOME/.cursor/skills" "$HOME/.agents/skills" "$PWD/.cursor/skills" "$PWD/.agents/skills"; do
    if [[ -d "$root/$skill" ]]; then echo "$root/$skill"; return 0; fi
  done
  return 1
}

check_sibling() {
  local skill="$1" venv_path="$2" required="$3"
  local dir
  dir="$(find_skill_dir "$skill" || true)"
  if [[ -z "$dir" ]]; then
    echo "[MISS] skill '$skill' not found alongside audio-theater." >&2
    echo "         install it in the same skills directory." >&2
    [[ "$required" == "required" ]] && missing_required=1
    return
  fi
  if [[ -x "$venv_path" ]]; then
    echo "[ok]   $skill: set up ($venv_path)" >&2
  else
    echo "[MISS] $skill installed but not set up -> bash \"$dir/scripts/setup_env.sh\"" >&2
    [[ "$required" == "required" ]] && missing_required=1
  fi
}

VC_HOME="${VOICE_CLONE_HOME:-$HOME/.voice-clone-narration}"
BG_HOME="${BG_MUSIC_HOME:-$HOME/.bg-music}"
SFX_HOME="${SOUND_EFFECTS_HOME:-$HOME/.sound-effects}"

# voice-clone-narration uses 'venv' (fallback '.venv').
VC_PY="$VC_HOME/venv/bin/python"; [[ -x "$VC_PY" ]] || VC_PY="$VC_HOME/.venv/bin/python"
check_sibling "voice-clone-narration" "$VC_PY" required
check_sibling "bg-music" "$BG_HOME/ACE-Step-1.5/.venv/bin/python" optional
check_sibling "sound-effects" "$SFX_HOME/.venv/bin/python" optional

echo "[setup] done." >&2
if [[ "$missing_required" -ne 0 ]]; then
  echo "[setup] Required pieces are missing (see [MISS] above). Voices need "\
       "voice-clone-narration + ffmpeg." >&2
  echo "not-ready"
  exit 1
fi
echo "[setup] Ready. (bg-music / sound-effects are optional; SFX/music cues are "\
     "skipped if their skill is absent.)" >&2
echo "ready"
