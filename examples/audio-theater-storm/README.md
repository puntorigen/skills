# Example: "The Keeper of Karsk" — a 3-voice spatial radio drama

A fully local `audio-theater` run: a 55-second storm scene with three
distinct **designed** voices (no reference clips) and four **spatialized**
sound effects, mixed to a stereo stage. Everything here was produced on an
Apple Silicon Mac with no cloud APIs and no Hugging Face account.

## What's in this folder

| File | Committed | What it is |
|------|-----------|------------|
| `script.json` | yes | The story: 3 characters (with stage positions) + 11 lines with delivery tags. |
| `cues.json` | yes | 4 SFX cues (1 ambient bed + 3 one-shots) with `spatial` positions, anchored to the line timings in `lines.json`. |
| `transcript.md` | yes | The generated, timecoded transcript. |
| `lines.json` | (generated) | Per-line clip timings from voice synthesis. |
| `dialogue.wav`, `lines/`, `sfx/`, `final.mp3` | no (git-ignored) | Generated audio. Run the commands below to (re)create them. |

The rendered mix is `final.mp3` (stereo, ~55s). Audio is git-ignored
(`*.mp3`/`*.wav`), so it isn't in the repo — reproduce it locally:

## Reproduce it

```bash
# 0. Install + set up the sibling skills (once):
#    voice-clone-narration (required), sound-effects (SFX). Both are Apple-Silicon.
bash voice-clone-narration/scripts/setup_env.sh
bash sound-effects/scripts/setup_env.sh

OUT=examples/audio-theater-storm
SC=audio-theater/scripts

# 1. Cast: design one voice per character from its `persona` (Qwen3-TTS VoiceDesign)
python3 $SC/setup_cast.py --out $OUT

# 2. Voices: one clip per line -> dialogue.wav + lines.json (exact timings)
python3 $SC/generate_voices.py --script $OUT/script.json --out $OUT

# 3. (author cues.json against the real line timings in lines.json, then:)
#    SFX: ambient bed + one-shots via the sound-effects skill
python3 $SC/generate_sfx.py --cues $OUT/cues.json --out $OUT

# 4. Spatial stereo mix + timecoded transcript
python3 $SC/mix_spatial.py --out $OUT
python3 $SC/build_transcript.py --out $OUT
```

## The spatial stage

Positions are authored in `script.json` (`characters[].stage`) and `cues.json`
(`cues[].spatial`), as `pan` (−1 L … +1 R) and `distance` (0 close … 1 far):

- **Narrator** — center, close (the storyteller).
- **Mara** (keeper) — left (`pan −0.4`).
- **Tomas** (apprentice) — right (`pan +0.45`); on one line he steps toward the
  lamp (a `{from, to}` move).
- **door** one-shot — hard left (`pan −0.55`, close), on Mara's side.
- **thunder** — near-center overhead.
- **foghorn** — far right (`pan +0.3`, `distance 0.85`).

Measured on the rendered `final.mp3`, the panning is audible and correct: at the
door hit the left channel is ~8 dB louder than the right; the distant foghorn
leans right.
