# sound-effects - Reference

Deep reference for the local sound-effects skill: the model, the runtime, prompt
recipes, the loop-for-beds trick, licensing, and troubleshooting.

## The model: Stable Audio Open Small

- **What it is:** a latent-diffusion text-to-audio model from Stability AI. Three
  parts: an Oobleck autoencoder (waveform <-> latent), a T5 text encoder, and a
  transformer diffusion (DiT) that denoises in latent space with rectified-flow
  sampling.
- **Output:** variable-length **stereo, 44.1 kHz**, up to about **11 seconds**.
- **Strengths:** Stability explicitly notes it is "better at generating sound
  effects and field recordings than music" - exactly the foley/ambience niche this
  skill targets.
- **Weaknesses:** cannot generate realistic vocals; trained on English prompts;
  not a music model (use **bg-music** / ACE-Step for music).
- Paper: https://arxiv.org/abs/2505.08175

## The runtime: mlx-audiogen (MLX)

- `setup_env.sh` installs [`mlx-audiogen`](https://pypi.org/project/mlx-audiogen/)
  (Apache-2.0) into `~/.sound-effects/.venv`. It runs the model natively on the
  Apple GPU via **MLX** (Metal). There is no CPU/CUDA path here.
- `generate_sfx.py` shells out to the venv's `mlx-audiogen` console script
  (`--model stable_audio`) and then encodes the resulting WAV to MP3 with ffmpeg.
- The base install does **not** pull PyTorch (torch is only needed for the
  optional `[convert]` extra, which this skill does not use).

## Weights, offline use, and the "no Hugging Face account" story

This is the important part. There are two different sets of weights:

| | Repo | Gated? | Used by this skill |
|---|------|--------|--------------------|
| **Public MLX build (default)** | `jasonvassallo/mlx-stable-audio` | **No** - anonymous download | **Yes** |
| Stability's originals | `stabilityai/stable-audio-open-small` | **Yes** (license click-through + token) | Only if you self-convert (advanced) |

`mlx-audiogen` ships pre-converted MLX weights on the maintainer's **public** HF
account, so the default generation path needs **no HF account, no token, no
license click-through**. `setup_env.sh` fetches them anonymously (`token=False`)
into `~/.sound-effects/weights/mlx-stable-audio` so that:

1. setup fails loudly if the ungated path ever breaks, and
2. generation afterwards is fully **offline** (`generate_sfx.py` passes
   `--weights-dir`, so no network call at generation time).

Knobs:

- `SFX_WEIGHTS_DIR` / `--weights-dir` - point at any local weights dir (a mirror,
  a shared drive, or an air-gapped copy). If it has a `config.json`, it's used and
  nothing is downloaded.
- `SFX_WEIGHTS_REPO` - fetch from a different **public** repo at setup.
- `setup_env.sh --no-weights` - skip the pre-download; `generate_sfx.py` then lets
  `mlx-audiogen` auto-download the public weights on first use (still no account).

### Advanced: self-converting Stability's gated originals

Only needed if you specifically want to convert the upstream weights yourself
(e.g. to audit the conversion). This path **is** gated: accept the license on the
[model page](https://huggingface.co/stabilityai/stable-audio-open-small), run
`huggingface-cli login`, then:

```bash
"$PY" -m pip install 'mlx-audiogen[convert]'   # pulls torch
"$SFX_HOME/.venv/bin/mlx-audiogen-convert" \
  --model stabilityai/stable-audio-open-small \
  --output "$SFX_HOME/weights/mlx-stable-audio"
```

Then generation picks up that `--weights-dir` automatically. For everyone else,
the public default already works with no account.

### Why not stable-audio-tools (PyTorch)?

The official `stable-audio-tools` library runs the same model on PyTorch/MPS, but
it is a heavier install and slower on Apple Silicon than the native MLX path.
mlx-audiogen keeps this skill consistent with the repo's other MLX-native skills
(image-gen, bg-music). For non-Apple-Silicon machines, `stable-audio-tools`
(`get_pretrained_model("stabilityai/stable-audio-open-small")` +
`generate_diffusion_cond(..., sampler_type="pingpong", steps=8)`) is the future
fallback; it is not wired up in v1.

## Sampler and steps

- **`euler` (default), 8 steps** is the fast sweet spot for Stable Audio Open
  Small (it is a rectified-flow model - few steps go a long way).
- Bump to **`rk4`, 20-30 steps** for a cleaner, more detailed take when a one-shot
  matters (a hero impact, a signature UI sound).
- `--cfg-scale` (default 6.0): higher = follows the prompt more strictly; lower =
  more variety / less literal. 4-8 is the useful range.

## Prompt cookbook

Name the **source + material + action + space**. One idea per clip.

| Category | Prompt example |
|----------|----------------|
| Impact / foley | `heavy wooden door slamming shut in a stone hallway, close-miked, no music` |
| Footsteps (seq) | `a sequence of several footsteps crunching on dry gravel` |
| Nature bed | `steady heavy rain on a tin roof with distant rolling thunder, no music` |
| Wind | `cold howling wind gusting across an open moor, eerie, no music` |
| Water | `a small stream trickling over rocks, birds in the distance` |
| Fire | `a crackling campfire close up, occasional pops` |
| Mechanical | `an old analog clock ticking in a quiet room` |
| Creature | `a large owl hooting once, then wings flapping as it flies off` |
| UI / abstract | `a short clean digital notification chime, bright, synthetic` |
| Whoosh / transition | `a fast cinematic whoosh passing left to right` |

Tips:

- For **repeated or continuous** actions, describe the whole sequence ("several
  footsteps", "a series of knocks") - a single-event prompt renders one thin hit.
- Add `"no music"` (or `--negative-prompt "music, melody, instruments"`) for pure
  foley; the model can drift musical.
- Name the **space** ("in a small tiled bathroom", "across a wide field") to get
  the right reverb/distance character.

## Loop a short clip into a long ambient bed

The model caps around 11s. For a 40s rain bed, generate a shorter clip and loop
it. ffmpeg (raw loop with short crossfade-free repeat):

```bash
# repeat rain.mp3 to cover 40s, with 1s fade in/out
ffmpeg -y -stream_loop -1 -i rain.mp3 -t 40 \
  -af "afade=t=in:st=0:d=1,afade=t=out:st=39:d=1" rain-40s.mp3
```

The **audio-theater** skill's mixer does this automatically for `ambient` cues
(it stream-loops and trims the clip to the exact cue window with fades and
ducking), so when cueing SFX into a drama you only need a short generated clip.

## Files and layout

```
~/.sound-effects/
├── .venv/            # uv venv with mlx-audiogen
├── weights/
│   └── mlx-stable-audio/   # public pre-converted MLX weights (offline)
└── out/              # generated audio (default output dir)
```

Weights live under the data root (or a shared HF cache if you skip the
pre-download), never in the repo.

## Licensing

- **mlx-audiogen**: Apache-2.0.
- **Stable Audio Open Small weights**: **Stability AI Community License**. Free for
  research and for individuals/organizations with **< $1M USD annual revenue**;
  at or above that threshold you need an Enterprise license from Stability. See
  https://stability.ai/license. This license governs *use of the model* and
  applies no matter where you obtain the weights - including the public MLX
  conversion this skill uses by default. Not having to log in to Hugging Face does
  not change the license terms. Unlike the repo's Apache/MIT models, factor this
  in before commercial use, and always disclose AI-generated audio where required.

## Troubleshooting

- **`mlx-audiogen not found`** - run `setup_env.sh` and invoke `generate_sfx.py`
  with the venv python (`~/.sound-effects/.venv/bin/python`).
- **Weight download failed at setup** - the default repo is *public*, so this is
  almost always a network/proxy issue, not auth. Retry `setup_env.sh`, or copy a
  populated weights dir onto the machine and set `SFX_WEIGHTS_DIR` to it. You do
  **not** need a Hugging Face account for the default weights.
- **401 / gated** - you only hit this on the advanced self-conversion path
  (converting `stabilityai/stable-audio-open-small`). Accept the license at the
  model page and run `huggingface-cli login`, or just use the public default.
- **Output sounds musical / tonal** - add `"no music"` and/or
  `--negative-prompt "music, melody"`, and make the prompt more concrete about the
  physical source.
- **Thin / single-hit result for a continuous action** - reprompt as a sequence
  ("several ...", "a series of ...").
- **Runs on CPU / errors off Apple Silicon** - this skill is Apple-Silicon only;
  MLX requires a Metal GPU.
- **Out of memory** - close other GPU-heavy apps; the model needs ~4-6 GB. Shorter
  `--duration` and fewer `--steps` also help.
