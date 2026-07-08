# Brand Logo Kit — Reference

Detailed reference for the `brand-logo-kit` skill: providers, key discovery,
prompt construction, palette output, and troubleshooting.

All commands assume the handles from `SKILL.md`:

```bash
PY="$HOME/.brand-logo-kit/.venv/bin/python"
SC="<skill dir>/scripts"
```

## Where state lives

| Thing | Location | Committed? |
|-------|----------|-----------|
| venv | `~/.brand-logo-kit/.venv` | No (outside repo) |
| cached key + model prefs | `~/.brand-logo-kit/config.json` | No (outside repo; also git-ignored) |
| style presets | `scripts/styles.json` | Yes (part of the skill) |
| generated assets | wherever you point `-o` (e.g. `out/`) | git-ignores media by default |

Override the data root with `BRAND_LOGO_KIT_HOME`.

## API key discovery + caching

`scripts/keylib.py` is the single source of truth; `resolve_key.py` and
`generate.py` both import it.

### Resolution order

1. **Cached** — `~/.brand-logo-kit/config.json` (`api_key` + `provider`).
2. **Environment variables**
   - Google: `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENAI_API_KEY`, `GOOGLE_AI_API_KEY`
   - OpenRouter: `OPENROUTER_API_KEY`, `OPEN_ROUTER_API_KEY`
3. **Other skills** — every `*/config.json` under `~/.cursor/skills`,
   `~/.claude/skills`, `~/.config/skills`, reading the fields
   `gemini_api_key`, `google_api_key`, `google_genai_api_key`, `openrouter_api_key`,
   `api_key`. This is how it re-uses an existing `asset-generator` key.
4. **Local fallback** — if no key is found and `allow_local` is on (default), the
   on-device `image-gen` skill is used (`provider="local"`, empty key). It is only
   chosen automatically when `local_available()` is true: the `image-gen` script is
   found, its venv exists, and the host is Apple Silicon.

The first non-empty hit is **cached** (`api_key`, `provider`, `key_source`) so
later runs skip the search.

### Provider detection

Inferred from the key prefix (authoritative):

| Prefix | Provider |
|--------|----------|
| `sk-or-` | `openrouter` |
| `AIza` | `google` |

If ambiguous, the source's hint (env var group or config field) is used, defaulting
to `google`.

### Managing the cached key

```bash
"$PY" "$SC/resolve_key.py"                 # discover + cache, print masked status
"$PY" "$SC/resolve_key.py" --show          # show cached config (key masked)
"$PY" "$SC/resolve_key.py" --set KEY       # cache a key manually
"$PY" "$SC/resolve_key.py" --set KEY --provider openrouter
"$PY" "$SC/resolve_key.py" --clear         # forget the cached key
```

Nothing sensitive is bundled with the skill — the key is only written locally,
outside the repo, after the first run.

## Providers + models

| Provider | Package / API | Default model | Notes |
|----------|---------------|---------------|-------|
| `google` | `google-genai` | `gemini-3-pro-image-preview` | Native Google AI Studio; supports interleaved reference images and `{imageN}` placeholders |
| `openrouter` | OpenAI-compatible REST (`/api/v1/chat/completions`) | `google/gemini-3-pro-image` (Nano Banana Pro) | `modalities: ["image","text"]` + `image_config`; references sent as base64 data URLs |
| `local` | shells out to the `image-gen` skill (mflux / MLX) | `flux2-klein-4b` | On-device, no key, Apple Silicon; **text-to-image only** (no reference); weaker at text |

Override per provider with `--model`, or set `google_model` / `openrouter_model` /
`local_model` in `~/.brand-logo-kit/config.json`.

### Local provider details

`generate_local()` invokes the sibling `image-gen` skill:
`<image-gen venv python> image-gen/scripts/generate_image.py --model <m> --prompt ... --width W --height H --count N --out tmp`.

- **Model**: `flux2-klein-4b` (default — cleaner for graphic/logo work) or
  `z-image-turbo` (photoreal; gets a logo-cleanup negative prompt).
- **Sizing**: the `--aspect-ratio` (and `--resolution` scale) is mapped to
  pixel `W×H` near ~1 MP (see `AR_BASE` / `RES_SCALE` in `generate.py`), then
  snapped to /16 by image-gen.
- **Prompt boost**: a flat-vector logo cue string (`LOCAL_LOGO_BOOST`) is appended
  so a diffusion model produces clean, centered, high-contrast marks.
- **Transparency**: same chroma-key cutout as the cloud path — the model is asked
  for a flat chroma background, which is then keyed out and trimmed.
- **Consistency**: no reference image; reuse the exact palette hexes and style
  wording (and optionally an `image-gen` LoRA via a custom prompt) across a set.
- **Disable / force**: `--provider local` forces it; pass a key (or
  `--provider google`) to use the cloud path even when local is available.

Other OpenRouter Gemini image slugs you can pass to `--model`:

- `google/gemini-3-pro-image` — Nano Banana Pro (best quality; default)
- `google/gemini-3.1-flash-image` — Nano Banana 2 (fast, cheaper)
- `google/gemini-2.5-flash-image` — Nano Banana (original)

### Request shapes

**Google** (`google-genai`):

```python
client.models.generate_content(
    model="gemini-3-pro-image-preview",
    contents=[pil_image, "prompt..."],          # image before text for edits
    config=types.GenerateContentConfig(
        response_modalities=["TEXT", "IMAGE"],
        image_config=types.ImageConfig(aspect_ratio="1:1", image_size="2K"),
    ),
)
```

**OpenRouter** (`POST https://openrouter.ai/api/v1/chat/completions`):

```json
{
  "model": "google/gemini-3-pro-image",
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "prompt..."},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
  ]}],
  "modalities": ["image", "text"],
  "image_config": {"aspect_ratio": "1:1", "image_size": "2K"}
}
```

Generated images come back at `choices[0].message.images[i].image_url.url` as a
`data:image/png;base64,...` URL.

## Prompt construction

`generate.py` wraps the user prompt with the selected style preset's brand guidance
from `styles.json`:

```
<user prompt>. <aesthetic>. <qualities...>. <framing>. [<transparent bg instruction>]. <constraints>.
```

- `--raw-prompt` sends the prompt verbatim (still adds the transparent-bg instruction
  when `--transparent` is set).
- For transparent output, a chroma color (magenta by default, avoided if it clashes
  with the prompt/reference colors) is requested, then removed with a two-pass
  chroma-key + hue matcher and a 1px edge erosion; output is trimmed to content.

## styles.json

Each preset defines: `name`, `description`, `aesthetic`, `qualities[]`,
`default_framing`, `default_constraints[]`, `recommended_aspect_ratio`,
`recommended_transparent`. Add or edit presets freely — new keys are picked up
automatically and show in `--list-styles`.

## brand.json (extract_palette.py)

```json
{
  "name": "Northwind",
  "source_image": "out/logo.png",
  "palette": [{"hex": "#0B2A4A", "rgb": [11,42,74], "weight": 0.54, "luminance": 0.12}],
  "roles": {"primary": "#0B2A4A", "secondary": "#2E8BC0", "accent": "#2E8BC0",
             "ink": "#0B2A4A", "paper": "#EAF2F8"},
  "prompt_snippet": "Use the brand palette #0B2A4A, #2E8BC0 (primary ..., accent ...). Keep the same visual style as the logo."
}
```

Options: `--colors N` (palette size), `--keep-bg` (include white/transparent pixels),
`--name` (embed the brand name).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "No … API key found, and no local fallback available" | Set a key (`resolve_key.py --set KEY`), or set up `image-gen` for the local fallback |
| Wrong provider chosen | `--provider google|openrouter|local`, or `resolve_key.py --clear` then re-run |
| OpenRouter 402 / insufficient credits | Add credits, switch to a Google key, or use `--provider local` |
| No image returned (only text) | Reword the prompt; for OpenRouter use a Gemini/`*-image` slug |
| Transparent background has a color fringe | The remover already erodes 1px; if a color clashes, mention a different dominant color, or drop `--transparent` and keep a white bg |
| `google.genai` import error | Re-run `scripts/setup_env.sh` |
| Reference not applied | Cloud: ensure the path exists and include `{image1}`. Local ignores references (text-to-image) |
| Local: "image-gen not found / not set up" | `bash ../image-gen/scripts/setup_env.sh` (Apple Silicon) |
| Local render slow / big download | First FLUX.2 Klein run fetches ~8 GB once; later runs are fast |

## Cloud-first, local-capable

The rest of `puntorigen/skills` is local-first (no cloud, no keys). Gemini's image
family is currently the best for clean vector-like marks, in-image text, and
reference-based consistency, so brand-logo-kit prefers that API when a key is
available. But it degrades gracefully: with **no key** it renders on-device through
the local `image-gen` skill (FLUX.2 Klein), so it still works fully offline for
symbol marks. It stays faithful to the repo's philosophy where it can — **no key is
committed**, all state lives outside the repo, it re-uses a key you already have,
and it can run entirely locally.
