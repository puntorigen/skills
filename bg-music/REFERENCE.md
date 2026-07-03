# Background Music - Reference

Model choices, parameters, prompt patterns, licensing, and troubleshooting for the
bg-music skill (ACE-Step 1.5).

## Why ACE-Step 1.5

- **License: MIT** (code and weights) - the most permissive top-tier open music
  model, safe for commercial reel/ad use.
- **Training data provenance:** licensed tracks + royalty-free/public-domain +
  synthetic (MIDI-to-audio). The model card documents this explicitly, which
  reduces copyright risk versus models trained on scraped music.
- **Apple Silicon native:** ships an **MLX backend** (M1-M4). The DiT runs via MLX
  (`use_mlx_dit=True`) and the planner LM via `backend="mlx"`. Runs in <4 GB VRAM;
  the default tier fits comfortably in 16 GB unified memory.
- **Fit for reels:** 10-600 s clips, instrumental mode, metadata control (BPM,
  key, time signature), 50+ languages (for vocals), 1000+ styles. Quality
  benchmarked "between Suno v4.5 and v5" by the authors.
- **Anonymous downloads:** weights pull from Hugging Face without a token.

### Rejected alternatives

| Model | Why not |
|-------|---------|
| **MusicGen (Meta)** | Weights are **CC-BY-NC** - non-commercial. Disqualifies commercial reels. |
| **Stable Audio Open / Small** | Stability Community License (commercial only under a revenue cap), **max ~11 s** clips, gated HF download, and the card says it is better at SFX than music. |
| **DiffRhythm / YuE / InspireMusic / AudioLDM2** | Research-grade, heavier, unclear weight licensing, weak macOS packaging. |
| **Cloud APIs (Suno, Udio, ElevenLabs Music)** | Not local - violate the offline requirement. |

## Model zoo (which to run on 16 GB)

ACE-Step 1.5 splits into a **DiT** (audio decoder) + a **planner LM** (expands your
prompt into a blueprint). Defaults chosen for a 16 GB Mac:

| Component | Default | Alternatives | Notes |
|-----------|---------|--------------|-------|
| DiT | `acestep-v15-turbo` (2B, 8-step) | `acestep-v15-sft`/`base` (50-step, higher quality, slower); `acestep-v15-xl-*` (4B, ~9 GB, needs >=20 GB or offload) | Turbo is fastest and fits 16 GB comfortably. Pass `--config`. |
| LM | `acestep-5Hz-lm-0.6B` | `acestep-5Hz-lm-1.7B` (better planning, more RAM), `-4B` (needs 16 GB+ just for the LM) | 0.6B is the safe default at 16 GB. Pass `--lm-model`. |

XL (4B) DiT gives higher fidelity but is not recommended on 16 GB (needs offload).
Stick with `turbo` + `0.6B` unless the user has more memory.

## GenerationParams cheatsheet

`generate_music.py` maps its flags onto the ACE-Step `GenerationParams` (see the
checkout's `docs/en/INFERENCE.md`):

| Flag | Param | Meaning |
|------|-------|---------|
| `--prompt` | `caption` | Free-text description (<=512 chars). The main control. |
| (default) | `lyrics="[Instrumental]"`, `instrumental=True` | No vocals. `--vocals` flips this. |
| `--duration` | `duration` | Seconds, 10-600. |
| `--bpm` | `bpm` | 30-300; omit for LM auto-detect. |
| `--keyscale` | `keyscale` | e.g. `"A minor"`; empty = auto. |
| `--seed` | `seed` / `config.seeds` | Reproducibility. |
| `--steps` | `inference_steps` | 8 for turbo. Higher only helps non-turbo DiTs. |
| `--count` | `config.batch_size` | Variations per run. |
| (fixed) | `thinking=True` | LM expands the prompt into metadata/structure for better results. |
| (fixed) | `guidance_scale=1.0` | Turbo bakes guidance into distillation; value is auto-corrected anyway. |

Output audio is 48 kHz stereo; the script encodes to MP3 via
`ffmpeg -c:a libmp3lame -q:a <quality>`.

## Prompt patterns

Specific, comma-separated prompts win. Formula: **genre + mood + 2-3 instruments +
tempo feel + "instrumental"/"loopable"**.

| Use case | Prompt |
|----------|--------|
| Lo-fi study/vlog | `warm lo-fi hip hop, mellow Rhodes piano, soft vinyl crackle, laid-back drums, loopable, instrumental` |
| Corporate/demo | `clean corporate ambient, gentle synth pads, light plucks, optimistic, minimal, instrumental` |
| Cinematic intro | `cinematic inspiring orchestral, soft strings, piano, subtle percussion building to a warm swell, instrumental` |
| Product hype reel | `driving synthwave, analog bass, retro arpeggios, punchy drums, night-drive energy, instrumental` |
| Calm explainer | `soft acoustic folk, fingerpicked nylon guitar, light shaker, warm and hopeful, instrumental` |
| Tech/AI reel | `modern electronic, glassy synths, tight beat, futuristic and clean, instrumental` |

Tips: name a tempo (`80 BPM` or "slow/mid/fast"), keep it instrumental for beds,
add "loopable" for seamless loops, and generate `--count 2` to compare.

## Mixing under narration

`mix_voiceover.sh` uses ffmpeg `sidechaincompress`: the narration is the sidechain
key, so the music compresses (ducks) while the voice speaks and recovers in gaps.
Music is looped (`-stream_loop -1`) to cover the narration and faded in/out.

- `--music-gain` (dB): overall bed level. Start at `-8`; go lower for a subtler bed.
- `--duck` (ratio): how hard the music drops under speech. `8` is a strong, clear
  duck; `4` is gentler.
- `--fade` (s): music fade in/out.

For a reel you usually want the music noticeably under the voice: keep
`--music-gain` around -8 to -12 and `--duck` 6-10.

## Storage & privacy

- Data root: `~/.bg-music/` (`ACE-Step-1.5/` checkout + `.venv`, `checkpoints/`,
  `out/`). Override with `BG_MUSIC_HOME`. Nothing is written into the repo.
- Weights cache under `~/.bg-music/ACE-Step-1.5/checkpoints` (~10 GB). Anonymous
  download.
- Prompts and audio are local only. Never upload them.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `uv not found` | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, then re-run setup. |
| `ffmpeg not found` | `brew install ffmpeg`. |
| `import mlx.core` fails on Mac | Re-run `setup_env.sh`; it repairs mlx/mlx-lm. Needs a recent macOS. |
| First generation is slow / stalls | It is downloading ~10 GB of weights; subsequent runs are fast. |
| Out of memory on 16 GB | The 2B model peaks near the MPS ceiling. `generate_music.py` sets `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0` by default so allocations spill into swap instead of OOM-ing, and clears caches between takes. If it still fails, keep the defaults (`acestep-v15-turbo` + `acestep-5Hz-lm-0.6B`), avoid XL/larger LMs, close other apps, and use `--count 1` (one process per take is the most reliable on 16 GB). |
| Vocals sneaking in | Keep default instrumental (don't pass `--vocals`); add "instrumental, no vocals" to the prompt. |
| Track shorter than the reel | Increase `--duration`; for mixing, the music auto-loops to cover the voice. |
| Non-Mac / CPU only | Works via PyTorch (`pt` backend) but slow; a CUDA machine is much faster. |

## Sources

- ACE-Step 1.5: https://github.com/ace-step/ACE-Step-1.5
- Model card (MIT, data provenance): https://huggingface.co/ACE-Step/Ace-Step1.5
- Inference API docs: `docs/en/INFERENCE.md` in the checkout; reference example
  `run_generate_test.py`.
