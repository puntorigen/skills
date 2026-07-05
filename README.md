# puntorigen/skills

Local-first agent skills for Cursor and other AI coding agents. Every skill in
this repo runs **on your machine** — no cloud APIs, no API keys. Most are
optimized for **Apple Silicon Macs** (MLX / MPS / Metal).

Install with the [skills CLI](https://skills.sh/):

```bash
npx skills add puntorigen/skills              # install all
npx skills add puntorigen/skills@talking-head # one skill
npx skills add puntorigen/skills -g -y        # global install, skip prompts
```

Browse: [skills.sh/puntorigen/skills](https://skills.sh/puntorigen/skills)

## Available skills

### image-gen

Generate hyper-realistic images from a text prompt at any resolution, locally
with mflux (Z-Image-Turbo or FLUX.2-klein). Optional SeedVR2 upscaling.

```bash
npx skills add puntorigen/skills@image-gen -g -y
```

**Requires:** Apple Silicon Mac, uv, ~6 GB disk for default model.

---

### voice-clone-narration

Clone a voice from a short sample or design one from a description. Generate
expressive MP3 narrations in English or Spanish with Chatterbox / Qwen3-TTS.

```bash
npx skills add puntorigen/skills@voice-clone-narration -g -y
```

**Requires:** Python 3.11, ffmpeg, ~5 GB disk. Voice design is Apple Silicon only.

---

### bg-music

Generate royalty-free instrumental background music from a text brief with
ACE-Step 1.5. Mix under a voiceover with automatic ducking.

```bash
npx skills add puntorigen/skills@bg-music -g -y
```

**Requires:** uv, git, ffmpeg, ~10 GB disk. Best on Apple Silicon.

---

### talking-head

Turn a portrait image + narration audio into a lip-synced talking-head MP4 with
JoyVASA and LivePortrait. Composes with `image-gen` and `voice-clone-narration`.

```bash
npx skills add puntorigen/skills@talking-head -g -y
```

**Requires:** Apple Silicon Mac, uv, git, ffmpeg, ~6 GB disk.

---

### video-to-splat

Convert an mp4 walkthrough into a 3D Gaussian splat (PLY/SOG) and preview it
in a bundled Aholo viewer. Full local photogrammetry pipeline.

```bash
npx skills add puntorigen/skills@video-to-splat -g -y
```

**Requires:** Apple Silicon Mac (macOS 14+), uv, ffmpeg, node, Chrome/Edge 134+.

---

### object-to-3d

Turn an mp4 of an orbited object into a clean, browser-navigable Gaussian splat
**and** a watertight, millimeter-scaled STL/GLB mesh for 3D printing. Extends the
`video-to-splat` pipeline with automatic splat cleanup and printable mesh
extraction - Poisson reconstruction plus base-down orientation, voxel
solidify and a flat print base, with in-browser Splat and Print previews
(open3d + trimesh + scikit-image).

```bash
npx skills add puntorigen/skills@object-to-3d -g -y
```

**Requires:** Apple Silicon Mac (macOS 14+), uv, ffmpeg, node, a WebGL2 browser.

---

### teach-web-actions

Record a real Chrome session (HAR + UI steps), distill it into a reusable
lesson, then replay the action with different parameters or record UI proof.

```bash
npx skills add puntorigen/skills@teach-web-actions -g -y
```

**Requires:** Node.js, Google Chrome, python3, ffmpeg (for mp4 proof).

## End-to-end reel workflow

These skills compose into a fully local content pipeline:

```
image-gen  →  avatar.png
voice-clone-narration  →  narration.mp3
talking-head  →  talking-head.mp4
bg-music  →  reel-audio.mp3 (optional mix)
```

Install the set:

```bash
npx skills add puntorigen/skills \
  -s image-gen,voice-clone-narration,bg-music,talking-head \
  -g -y
```

Each skill stores models and outputs **outside the repo** (under `~/.*` home
dirs). First run of `scripts/setup_env.sh` downloads weights and may take
several minutes.

## Repo layout

```
skills/
├── README.md
├── skills.sh.json          # optional grouping for skills.sh
├── image-gen/
│   ├── SKILL.md            # required — agent instructions + frontmatter
│   ├── REFERENCE.md        # optional deep reference
│   └── scripts/
├── voice-clone-narration/
├── bg-music/
├── talking-head/
├── video-to-splat/
├── object-to-3d/
└── teach-web-actions/
```

## Verify locally before publishing

```bash
# From this repo root:
npx skills add . --list

# Dry-run install one skill:
npx skills add .@talking-head -g -y
```

## Development

Scaffold a new skill:

```bash
npx skills init my-new-skill
```

Each `SKILL.md` needs YAML frontmatter with `name` and `description`. See
[agentskills.io](https://agentskills.io/) and existing skills here for
conventions.

## License

Skill instructions and scripts in this repo are MIT unless noted otherwise.
Bundled model weights retain their upstream licenses (ACE-Step MIT, Chatterbox
MIT, Qwen3-TTS Apache-2.0, Z-Image/FLUX Apache-2.0, etc.) — see each skill's
`REFERENCE.md`.
