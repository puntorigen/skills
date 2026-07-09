# Brand Logo Kit — Reference

Detailed reference for the `brand-logo-kit` skill: provider resolution (local-first
+ disk guard), key discovery, looks, real-font wordmarks, prompt construction,
palette output, and troubleshooting.

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
| look presets | `scripts/looks.json` | Yes (part of the skill) |
| FLUX.2 Klein weights | `~/.cache/huggingface/hub` (HF cache) | No (shared model cache) |
| generated assets | wherever you point `-o` (e.g. `out/`) | git-ignores media by default |

Override the data root with `BRAND_LOGO_KIT_HOME`.

## Provider resolution (local-first + disk guard)

`scripts/keylib.py` is the single source of truth; `resolve_key.py` and
`generate.py` both import it. This repo is **local-first**, so on-device generation
is preferred over any cloud key.

### Resolution order (default, `prefer=None`)

1. **Local (preferred)** — chosen when `local_usable()` is true and
   `BRAND_LOGO_KIT_PREFER != cloud`. Returns `provider="local"`, empty key.
2. **A cloud key** — the first non-empty hit from:
   - **Cached** `~/.brand-logo-kit/config.json` (`api_key` + `provider`)
   - **Env vars** — Google: `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENAI_API_KEY`,
     `GOOGLE_AI_API_KEY`; OpenRouter: `OPENROUTER_API_KEY`, `OPEN_ROUTER_API_KEY`
   - **Other skills** — every `*/config.json` under `~/.cursor/skills`,
     `~/.claude/skills`, `~/.config/skills` (fields `gemini_api_key`,
     `google_api_key`, `google_genai_api_key`, `openrouter_api_key`, `api_key`).
     This re-uses an existing `asset-generator` key.
3. **Local (last resort)** — if no key is found but `image-gen` is merely installed
   (`local_installed()`), local is used even below the disk bar (`local:image-gen(low-disk)`);
   the run may fail mid-download.

Only a **cloud key** is cached (`api_key`, `provider`, `key_source`) — local is a
policy decision recomputed each run, so a previously cached key is retained and used
automatically whenever local isn't usable. Force a provider with `--provider`, which
bypasses this order (`local` never touches keys; `google`/`openrouter` require one).

### Local usability & the disk guard

`local_usable(model)` gates the automatic local choice:

```
local_installed()  = image-gen script found  AND  its venv python exists  AND  Apple Silicon
local_usable()     = local_installed()  AND  ( weights already downloaded  OR  enough free disk )
```

- **Weights present** — `local_weights_present()` looks for
  `models--black-forest-labs--FLUX.2-klein-4B` (or the z-image repo) with a
  `*.safetensors` file in the HF hub cache (`HF_HUB_CACHE` / `HF_HOME` / `~/.cache/huggingface/hub`).
  If present, no disk is needed and local is usable.
- **Enough disk** — otherwise `free_gb(hf cache)` must be ≥ `min_free_gb(model)`:
  **12 GB** for `flux2-klein-4b`, **7 GB** for `z-image-turbo`. Override with
  `BRAND_LOGO_KIT_MIN_DISK_GB=N`.

Inspect all of this with `resolve_key.py --status`:

```json
{ "model": "flux2-klein-4b", "apple_silicon": true, "installed": true,
  "weights_present": false, "free_gb": 10.1, "min_free_gb": 12.0, "usable": false }
```

### Env knobs

| Var | Effect |
|-----|--------|
| `BRAND_LOGO_KIT_PREFER=cloud` | Try a cloud key **before** local |
| `BRAND_LOGO_KIT_MIN_DISK_GB=N` | Free-GB bar for auto-picking local on a fresh download |
| `BRAND_LOGO_KIT_HOME=DIR` | Move the venv + key cache |
| `IMAGE_GEN_HOME` / `HF_HOME` / `HF_HUB_CACHE` | Locate the image-gen venv / model cache |

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
"$PY" "$SC/resolve_key.py"                 # resolve provider (local-first), print status
"$PY" "$SC/resolve_key.py" --status        # local diagnostics JSON (disk, weights, usability)
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
| `local` (**preferred**) | shells out to the `image-gen` skill (mflux / MLX) | `flux2-klein-4b` | On-device, no key, Apple Silicon; **text-to-image only** (no reference). Set brand text with `wordmark.py`, not the model |
| `google` | `google-genai` | `gemini-3-pro-image-preview` | Native Google AI Studio; supports interleaved reference images and `{imageN}` placeholders |
| `openrouter` | OpenAI-compatible REST (`/api/v1/chat/completions`) | `google/gemini-3-pro-image` (Nano Banana Pro) | `modalities: ["image","text"]` + `image_config`; references sent as base64 data URLs |

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
- **Prompt boost**: `LOCAL_LOGO_BOOST` is intentionally **light** — it asks for a
  polished, professional design with *a single clear recognizable symbol* (the old
  "flat 2D vector" wording produced plain/abstract marks). The visual finish comes
  from `--look`, which defaults to **`modern`** for local.
- **Text**: diffusion renders text with a generic, often-misspelled font — do **not**
  let the model draw the brand name. Generate the symbol here and compose the
  wordmark with `wordmark.py`. `generate.py` prints a tip when a `*-wordmark`/`*-lockup`
  style is run locally.
- **Transparency**: same chroma-key cutout as the cloud path — the model is asked
  for a flat chroma background, which is then keyed out and trimmed.
- **Consistency**: no reference image; reuse the exact palette hexes, identical
  style wording, and the same `--look` across a set.
- **Disable / force**: `--provider local` forces it (even below the disk bar);
  `BRAND_LOGO_KIT_PREFER=cloud` or `--provider google` uses a cloud key instead.

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
from `styles.json`, plus the resolved `--look` modifier from `looks.json`:

```
<user prompt>. <aesthetic>. <qualities...>. <framing>. [<look modifier>]. [<transparent bg instruction>]. <constraints>.
```

- The **local** path additionally appends `LOCAL_LOGO_BOOST` (light quality cue) and,
  for `z-image-turbo`, `LOCAL_NEGATIVE`.
- `--raw-prompt` sends the prompt verbatim (still appends the look modifier and the
  transparent-bg instruction when set).
- For transparent output, a chroma color (magenta by default, avoided if it clashes
  with the prompt/reference colors) is requested, then removed with a two-pass
  chroma-key + hue matcher and a 1px edge erosion; output is trimmed to content.

## styles.json

Each preset defines: `name`, `description`, `aesthetic`, `qualities[]`,
`default_framing`, `default_constraints[]`, `recommended_aspect_ratio`,
`recommended_transparent`. Add or edit presets freely — new keys are picked up
automatically and show in `--list-styles`.

The `logo` preset was tuned to reduce "abstract blob" output: it asks for *a clear
recognizable symbol that represents the concept* and explicitly avoids "vague
abstract swooshes", and it no longer forces flat/single-color so a `--look` can add
gradients or glow.

## looks.json (`--look`)

A **look** is a short finish modifier appended to any style. `looks.json` maps a name
to its phrase; `resolve_look()` handles the `auto`/`none` sentinels:

- `auto` (default) → **`modern`** for `provider == "local"`, **none** for cloud. The
  map lives in `LOOK_AUTO_DEFAULT` in `generate.py`.
- `none` → no modifier.
- otherwise → the phrase from `looks.json` (e.g. `modern`, `minimal`, `geometric`,
  `gradient`, `glow`, `flow`, `line`, `badge`, `3d`, `mesh`, `duotone`, `corporate`).

List them with `--list-looks`. Add/edit entries freely — new keys work immediately.
Keep the same look across a set for cohesion; the chosen look is recorded in the JSON
result (`"look": ...`).

## wordmark.py (real-font wordmarks & lockups)

Diffusion models (and even API models sometimes) mangle text. `wordmark.py` sidesteps
that entirely: it renders the brand name from a genuine installed font, deterministically
and fully offline, and optionally composes it with a symbol into a lockup.

```bash
"$PY" "$SC/wordmark.py" --list-fonts                     # curated fonts on this machine
"$PY" "$SC/wordmark.py" --text "Acme" --font Futura -o wm.png
"$PY" "$SC/wordmark.py" --text "Acme" --mark logo.png --layout horizontal -o lockup.png
```

- **Font resolution** (`resolve_font()`): a direct `.ttf`/`.otf`/`.ttc` path wins;
  otherwise a friendly-name alias or family substring is matched against fonts in the
  macOS + Linux font dirs, preferring names that **start with** the query and the
  **shortest** (base-weight) face. Falls back through a curated list
  (Futura → Avenir Next → Gill Sans → Optima → DIN → Helvetica Neue → Didot →
  Baskerville → Georgia → … → DejaVu Sans). `.ttc` collections take `--font-index`.
- **Rendering** (`render_wordmark()`): draws glyph-by-glyph so `--tracking` (em)
  letter-spacing is exact, on a transparent RGBA canvas, then trims to the alpha bbox.
  `--case upper|lower|title|as-is`, `--size` (px), `--color HEX` or `--brand brand.json`
  + `--color-role ink|primary|secondary|accent`.
- **Lockup** (`compose_lockup()`): the mark is alpha-trimmed and scaled to
  `--mark-scale ×` the text height, placed left (`horizontal`) or above (`vertical`)
  with a `--gap` (em) and centered on the cross axis.
- **Output**: `--bg HEX` (default transparent) + `--padding N` px, saved as PNG; a JSON
  summary (font path, color, dimensions) is printed to stdout.

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
| Unexpectedly using cloud, not local | Check `resolve_key.py --status`: likely `usable:false` (low disk or `image-gen` not set up). Free disk, lower `BRAND_LOGO_KIT_MIN_DISK_GB`, or set up `image-gen` |
| Unexpectedly using local, not a key | Set `BRAND_LOGO_KIT_PREFER=cloud`, or force `--provider google|openrouter` |
| "No usable provider found" | Set up `image-gen` (Apple Silicon) **or** set a key (`resolve_key.py --set KEY`) |
| Wrong provider chosen | `--provider local|google|openrouter`, or `resolve_key.py --clear` then re-run |
| OpenRouter 402 / insufficient credits | Add credits, switch to a Google key, or use `--provider local` |
| No image returned (only text) | Reword the prompt; for OpenRouter use a Gemini/`*-image` slug |
| Logos look generic/abstract | Name a **concrete** symbol; try a different `--look` (e.g. `modern`, `geometric`, `badge`) |
| Wordmark text mushy/misspelled | Don't render text with the model — use `wordmark.py` (real font) |
| `wordmark.py` picked an odd font | Pass `--font` with a family substring or a `.ttf`/`.otf` path; `--list-fonts` to see options |
| Transparent background has a color fringe | The remover already erodes 1px; if a color clashes, mention a different dominant color, or drop `--transparent` and keep a white bg |
| `google.genai` import error | Re-run `scripts/setup_env.sh` |
| Reference not applied | Cloud: ensure the path exists and include `{image1}`. Local ignores references (text-to-image) |
| Local: "image-gen not found / not set up" | `bash ../image-gen/scripts/setup_env.sh` (Apple Silicon) |
| Local render slow / big download | First FLUX.2 Klein run fetches its weights once (~12 GB free needed); later runs are fast |

## Local-first, cloud-capable

`puntorigen/skills` is local-first (no cloud, no keys), and brand-logo-kit follows
that: it **prefers on-device** FLUX.2 Klein and only reaches for a cloud key when
local can't realistically run (no Apple Silicon, `image-gen` not set up, or not
enough disk to download weights). The cloud path (Gemini / Nano Banana Pro) remains
available because it's still the best for reference-based consistency and in-image
text — but text is better handled locally and deterministically by `wordmark.py`.
Either way, **no key is committed**, all state lives outside the repo, and an
existing key is re-used rather than re-entered.
