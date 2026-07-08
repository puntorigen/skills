# audio-theater - Reference

Deep reference for the local audio-theater orchestrator: how it composes the
sibling skills, the spatial mixer, stems and video handoff, delivery pacing, and
troubleshooting.

## Architecture

audio-theater is pure Python stdlib + ffmpeg. It owns **no models and no venv**;
it shells out to three sibling local skills through their installed venvs and
mixes their audio.

| Concern | Sibling skill | How it's called |
|---------|---------------|-----------------|
| Voices | voice-clone-narration | `_vc_batch.py` run with `~/.voice-clone-narration/venv/bin/python`, model loaded once for all lines |
| SFX | sound-effects | `generate_sfx.py` run with `~/.sound-effects/.venv/bin/python` |
| Music | bg-music | `generate_music.py` run with `~/.bg-music/ACE-Step-1.5/.venv/bin/python` |

Sibling script locations are resolved relative to this skill's install directory,
then a few common roots (`~/.cursor/skills`, `~/.agents/skills`, `./.cursor/skills`,
`./.agents/skills`). Override any of them with a `<SKILL>_DIR` env var (e.g.
`SOUND_EFFECTS_DIR`) and their data homes with `VOICE_CLONE_HOME`, `BG_MUSIC_HOME`,
`SOUND_EFFECTS_HOME`.

## Scripts

| Script | Runs under | Purpose |
|--------|-----------|---------|
| `write_script.py` | python3 | Parse a `Name: line` `.txt` into `script.json` + `story.md`. |
| `setup_cast.py` | python3 | Design (persona) or clone (clip) one voice per character; writes voices back to `script.json`. |
| `generate_voices.py` | python3 | Build the per-line manifest and drive `_vc_batch.py`; pad + concat to `dialogue.wav` + `lines.json`. |
| `_vc_batch.py` | voice-clone venv | Load the Chatterbox model once, synthesize every line clip (conds cached per voice). |
| `generate_sfx.py` | python3 | Generate `ambient`/`oneshot` cues (sound-effects) and `music` cues (bg-music); write files back to `cues.json`. |
| `generate_music.py` | python3 | Thin wrapper over bg-music (importable + CLI). |
| `mix.py` | python3 | Flat center mix: dialogue + cues, sidechain ducking, loudnorm, stems. |
| `mix_spatial.py` | python3 | Virtual-stage stereo mix (pan/distance/movement) + stems. |
| `spatial.py` | (lib) | Constant-power pan + distance (level/low-pass) + movement (`aeval`) helpers. |
| `build_transcript.py` | python3 | Timecoded `transcript.md` + SFX/music table. |
| `export_lipsync.py` | python3 | `lipsync.json` manifest for the talking-head skill. |
| `split_tracks.py` | python3 | Split into `narration.mp3` + `lipsync_mix.mp3` (narration muted). |

## Voice delivery & emotion

This is an audio **drama**: the characters should act, not just read. Chatterbox
(the multilingual default) has no inline emotion markup, so delivery is driven by
two continuous knobs, which `generate_voices.py` resolves per line from a rich
emotion vocabulary:

- **exaggeration** (0-1): emotional intensity. `0.5` neutral, `0.7-0.9` dramatic.
  Capped at `1.0` (higher gets artifact-prone and also speeds delivery up).
- **cfg_weight** (0-1): cadence adherence; lower = slower/more deliberate/more
  emotive, and **less accent transfer** (non-English lines cap cfg at 0.4).

### Stating emotion in the script

Every line can carry a first-class **`emotion`** (preferred) or a list of `tags`,
plus an optional **`intensity`**:

```json
{"speaker": "Tomas", "text": "The latch won't hold!", "emotion": "panicked", "intensity": 0.85}
```

- **`emotion`** — one word from the vocabulary below (wins over `tags`). Free-form
  stage directions resolve too: `whispering`→`whisper`, `angrily`→`angry`,
  `sternly`→`stern`, `scared`→`frightened` (see `EMOTION_ALIASES` + the `-ing/-ly`
  reduction in `generate_voices.py`).
- **`intensity`** (0-1, `0.5` = leave the preset as-is) — dials the same emotion
  hotter or cooler: higher pushes `exaggeration` up and `cfg_weight` down. Also
  settable per character.
- **explicit `exaggeration`/`cfg_weight`** numbers on a line win outright — the
  escape hatch for a stubborn take (the example pins line 4 this way).
- character-level **`tone`** (+ optional `exaggeration`/`cfg_weight`) sets a default
  for lines with no emotion of their own.

### Emotion vocabulary (grouped by register)

| Register | Emotions (exaggeration rises left→right) |
|----------|------------------------------------------|
| Intimate | `hushed` `whisper` `soft` `calm` `gentle` `tender` `warm` `wistful` `reassuring` `cold` |
| Gravity | `tired/weary` `sad` `sorrowful` `solemn` `serious` `grave` `grim` `hopeful` |
| Authority | `firm` `proud` `resolute/determined` `sarcastic` `menacing` `stern` `commanding` `defiant` |
| Fear/distress | `worried` `anxious` `nervous` `trembling` `pleading` `breathless` `frightened/afraid/scared` `desperate` `panicked` `terrified` |
| Heat | `urgent` `intense` `dramatic` `angry` `furious` `shout/shouting/yelling` |
| Bright | `happy/cheerful` `playful` `joyful` `surprised` `awe` `excited/energetic` `triumphant` `laughs` `promo` |

### Expressiveness (dynamic range)

`--expressiveness` (or script/config `expressiveness`) stretches every line's
distance from neutral (0.5), giving the whole piece dynamic range — calm lines
sit calmer, peaks hit harder. Default **1.2 for theater**, 1.0 otherwise. Precedence:
CLI > script field > config > mode default. It never touches explicit numeric
overrides.

### Non-verbal cues (`[gasp]`, `[sigh]`, ...)

You can write breaths and reactions inline in the text. On the **`turbo`** model
(English-only; `--model turbo`) Chatterbox voices them natively — supported tokens:
`[laugh] [sigh] [chuckle] [cough] [gasp] [groan] [sniff] [shush] [clear throat]`.
On the multilingual default the bracket is **stripped** (never read aloud) and its
presence instead nudges `intensity` up, so the script still reads like a play. Use
turbo for punchy English drama where audible gasps/sighs matter; keep multilingual
for non-English or when you need the designed multilingual voices.

## SFX cue quality (one-shots)

Stable Audio Open Small responds best to **short, concrete** prompts. A vague or
overloaded prompt drifts into the wrong texture (a "door … straining in a violent
wind gust" can come out as an animal roar). For a crisp impact:

- Prompt the *sound*, not the scene: `heavy wooden door slams shut, sharp wooden bang`.
- Per-cue quality knobs in `cues.json` are passed through to the sound-effects CLI:
  `steps` (bump to ~20-24), `sampler` (`"rk4"` for a cleaner one-shot), `cfg_scale`
  (higher = follows the prompt more strictly), and `negative_prompt` (steer away
  from the wrong texture, e.g. `"music, animal, roar, voice, drone"`).
- `seed` fixes the take. Generate a few (`sound-effects/generate_sfx.py --count N
  --seed S`) and keep the best — an impact should have a sharp attack and fast
  decay, not a sustained tail.

## Casting voices

`setup_cast.py` gives each character its own voice:

- **Design** (Apple Silicon): `voice-clone-narration/design_voice.py` synthesizes
  a voice from the character `persona` (Qwen3-TTS VoiceDesign), saved as a normal
  reference so all narration runs through the same cloning pipeline. Audition mp3s
  land under `~/.voice-clone-narration/out/`.
- **Clone** (any platform): pass `--clip "Name=path"` (or `--clips-dir` with
  `<character-slug>.<ext>` files); `prep_reference.sh` converts the clip to a clean
  reference. Cloning needs only ffmpeg, so it works off Apple Silicon.

Voices are namespaced `at-<project>-<character>` so they never overwrite your
personal voice library. Re-run with `--force` to regenerate.

## Spatial mix (`mix_spatial.py`)

Places each voice line and one-shot SFX on a virtual stereo stage:

- **pan** in [-1, +1] (L..R), **distance** in [0, 1] (0=close, 1=far). Distance is
  built from level + a low-pass (air absorption) + constant-power pan.
- Positions come from `script.json` `characters[].stage` (seat) and
  `lines[].spatial` (per-line override/movement), and `cues.json` `cues[].spatial`.
  Shapes: static `{pan, distance}`, or movement `{from, to}` / `{path:[{t,pan,distance}]}`.
- With no positions authored it still seats non-narration speakers at gentle
  alternating L/R offsets. Voices are clamped to `±--voice-pan-limit` (0.5) for
  intelligibility; SFX may travel to `±--sfx-pan-limit` (0.95).
- **Narration wins:** the narrator sits closest and one-shot SFX are floored at
  `--sfx-min-distance` (0.12) and ducked under the narrator (`--sfx-duck-db -6`).
- **Music stays fixed** by default (a still wide bed). Only diegetic *scene* music
  moves, and only as one gentle gesture: `{"scene": true, "enter": "left"|"right"|"front"}`,
  `{"scene": true, "exit": ...}`, or one `{from, to}` sweep. **Never zig-zag music.**
- Useful flags: `--crossfeed` (headphone-friendlier hard pans), `--no-duck`,
  `--no-duck-sfx`, `--voice-pan-limit`, `--max-atten-db`.

The spatial mixer is for the standalone listening deliverable. The talking-head
lip-sync feed must stay centered/mono-safe, so `split_tracks.py` uses the flat
`mix.py`.

## Three deliverables when there's music

When a project has `music` cues, both mixers emit three tracks in one pass (shared
gain, so they recombine exactly):

| File | Contents | Use it for |
|------|----------|-----------|
| `final.mp3` | dialogue + SFX + music | the finished listen |
| `final.music.mp3` | music only (ducked/faded as in the mix) | swap/level the score, overlay after a video render |
| `final.nomusic.mp3` | dialogue + SFX, no music | feed the talking-head lip-sync feed, then add music back |

`--stems always` forces stems even without music; `--stems off` writes only
`final.mp3`.

## Video handoff (talking-head)

audio-theater is audio only. To make characters speak on camera:

1. `export_lipsync.py --out $OUT` -> `lipsync.json` (per clip: speaker, voice,
   transcript, duration, file, `ok`).
2. For each on-camera character, generate ONE front-facing, mouth-closed avatar
   with the **image-gen** skill.
3. Per line clip, run the **talking-head** skill:

```bash
"$TH_PY" animate.py --image <avatar-for-speaker>.png \
  --audio $OUT/lines/line-000.wav --crop --out shot-000.mp4
```

talking-head lip-syncs whatever voice is in the clip, so always feed the clean
per-line clip, never the full mix. Longer clips render slower (~24s compute per 1s
of video on an M4) - keep lines short.

### Narration vs on-camera

When one track carries both an off-camera narrator and an on-camera speaker,
feeding the whole thing to a lip-sync model makes it mouth the narration. Split:

```bash
python3 $SCRIPTS/split_tracks.py --out $OUT
# or override detection:
python3 $SCRIPTS/split_tracks.py --out $OUT --narration "Narrador" --onscreen "Doki"
```

- `narration.mp3` - only narration/off-camera lines on the original timeline.
- `lipsync_mix.mp3` - on-camera voices + SFX, narration muted (feed this to
  talking-head), then overlay `narration.mp3` (and `final.music.mp3`) back onto the
  rendered video, e.g.:

```bash
ffmpeg -y -i shot.mp4 -i narration.mp3 \
  -filter_complex "[0:a][1:a]amix=inputs=2:normalize=0:dropout_transition=0,loudnorm=I=-16:TP=-1.5:LRA=11[a]" \
  -map 0:v -map "[a]" -c:v copy -c:a aac -shortest shot_final.mp4
```

## Pacing & fixed-duration targets

To hit a target length (e.g. a 15s reel), **budget the script**, don't stretch the
speech. Rough budget ~**2 spoken words/second** (accounts for the model's pace +
inter-line pauses), so ~25-30 words for 15s. Trim dead air first, then apply only a
gentle `atempo` (<=1.1x) if slightly over. If still too long, cut a line.

## Modes recap

- **theater**: full dramatized mix (+ stems + transcript). Use `mix_spatial.py` for
  an immersive stereo stage.
- **podcast**: two hosts; add `music` cues for intro/bed/outro. Same mixer.
- **lipsync**: stop after `generate_voices.py` + `export_lipsync.py`; the clean
  clips are the deliverable for talking-head.

## Troubleshooting

- **"voice-clone-narration is not set up"** - run its `setup_env.sh`; the venv is
  expected at `~/.voice-clone-narration/venv/bin/python`.
- **SFX/music cues skipped** - the sound-effects / bg-music skill isn't set up. Run
  its `setup_env.sh`, or omit those cues. The mix still renders without them.
- **A character has no voice** - run `setup_cast.py` before `generate_voices.py`.
  Off Apple Silicon you must supply `--clip NAME=path` (design needs Apple Silicon).
- **Voice sounds accented on Spanish lines** - lower `cfg_weight` (already capped at
  0.4 for non-English) or design/clone a native-accent reference.
- **A clip is too long / renders slowly in talking-head** - split that line; keep
  each clip short.
- **Music pumps or drowns the voices** - lower the cue `gain_db` (music base
  `-24..-20`) and keep `duck_db` shallow (`-8`); the mixer's music duck is
  intentionally slow/shallow.
- **Filter graph errors** - the mixers print the exact ffmpeg filtergraph on
  failure; check for a cue with a missing/zero-length file.

## Licensing

Inherited from the sibling skills. The one to watch: **sound-effects** uses Stable
Audio Open Small under the **Stability AI Community License** (free under $1M
annual revenue; not Apache/MIT). voice-clone-narration (Chatterbox/MLX) and
bg-music (ACE-Step) are more permissive. Always disclose AI-generated audio where
required, and obtain consent before cloning a real person's voice.
