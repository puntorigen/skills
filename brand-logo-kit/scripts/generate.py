#!/usr/bin/env python3
"""Generate brand assets (logos, marks, on-brand graphics) with Gemini image models.

Works with either provider, auto-selected by the resolved API key:
  - Google AI Studio (google-genai)  -> model gemini-3-pro-image-preview
  - OpenRouter (OpenAI-compatible)    -> model google/gemini-3-pro-image (Nano Banana Pro)

No API key is stored in this skill; it is discovered + cached by keylib.

Usage:
    python3 generate.py "a minimalist fox head mark" --style logo -o fox.png

    # Brand-consistent asset using an existing logo as reference:
    python3 generate.py "a delivery-truck spot illustration matching {image1}" \
        --ref logo.png --style brand-illustration -o truck.png
"""

import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

import keylib

SCRIPT_DIR = Path(__file__).resolve().parent
STYLES_FILE = SCRIPT_DIR / "styles.json"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_RETRIES = 3
RETRY_BASE_DELAY = 8.0

# Aspect ratio -> (width, height) near ~1 MP, for the local (mflux) path.
AR_BASE = {
    "1:1": (1024, 1024), "3:2": (1216, 832), "2:3": (832, 1216),
    "16:9": (1280, 720), "9:16": (720, 1280), "4:3": (1152, 896),
    "3:4": (896, 1152), "4:5": (896, 1120), "5:4": (1120, 896), "21:9": (1536, 640),
}
RES_SCALE = {"1K": 1.0, "2K": 1.5, "4K": 2.0}

# Diffusion-friendly cues that push a local text-to-image model toward clean,
# flat, vector-style logo output (the API models don't need these).
LOCAL_LOGO_BOOST = ("flat 2D vector logo design, crisp clean edges, high contrast, "
                    "centered, minimal, professional, sharp geometric shapes, "
                    "not a photograph, not a 3D render")
LOCAL_NEGATIVE = ("photograph, 3d render, realistic photo, busy background, gradient noise, "
                  "blurry, low quality, jpeg artifacts, watermark, signature, extra text")

BG_CANDIDATES = [
    ("FF00FF", "magenta (#FF00FF)"),
    ("00FF00", "bright green (#00FF00)"),
    ("0000FF", "blue (#0000FF)"),
    ("FF0000", "red (#FF0000)"),
    ("00FFFF", "cyan (#00FFFF)"),
    ("FFFF00", "yellow (#FFFF00)"),
]

COLOR_KEYWORDS = {
    "FF00FF": ["magenta", "pink", "purple", "violet", "fuchsia", "rose", "lavender", "plum"],
    "00FF00": ["green", "lime", "emerald", "mint", "forest", "olive", "leaf", "grass", "plant",
               "tree", "nature"],
    "0000FF": ["blue", "navy", "cobalt", "azure", "ocean", "sea", "water", "sky", "indigo"],
    "FF0000": ["red", "scarlet", "crimson", "ruby", "fire", "flame", "blood", "cherry", "tomato"],
    "00FFFF": ["cyan", "teal", "turquoise", "aqua", "aquamarine"],
    "FFFF00": ["yellow", "gold", "golden", "amber", "lemon", "sunshine", "sunflower"],
}


def load_styles():
    if not STYLES_FILE.exists():
        return {}
    return json.loads(STYLES_FILE.read_text(encoding="utf-8"))


def list_styles():
    styles = load_styles()
    print("Available brand style presets:\n")
    for key, style in styles.items():
        rec = []
        if style.get("recommended_aspect_ratio"):
            rec.append(f"ratio={style['recommended_aspect_ratio']}")
        if style.get("recommended_transparent"):
            rec.append("transparent")
        rec_str = f" [{', '.join(rec)}]" if rec else ""
        print(f"  {key:20s} {style['name']}: {style['description']}{rec_str}")


# ──────────────────────────────────────────────────────────
# Prompt construction (shared across providers)
# ──────────────────────────────────────────────────────────

def pick_bg_color(user_prompt, ref_paths=None):
    """Pick a chroma background color that won't clash with the subject."""
    prompt_lower = user_prompt.lower()
    conflicting = set()
    for hex_code, keywords in COLOR_KEYWORDS.items():
        if any(kw in prompt_lower for kw in keywords):
            conflicting.add(hex_code)

    if ref_paths:
        try:
            from PIL import Image as PILImage
            import numpy as np
        except ImportError:
            pass
        else:
            for rp in ref_paths:
                p = Path(rp)
                if not p.exists():
                    continue
                try:
                    img = PILImage.open(str(p)).convert("RGB").resize((64, 64), PILImage.LANCZOS)
                    pixels = np.array(img).reshape(-1, 3).astype(float)
                    for hex_code, _ in BG_CANDIDATES:
                        target = np.array([int(hex_code[i:i+2], 16) for i in (0, 2, 4)], dtype=float)
                        dists = np.sqrt(np.sum((pixels - target) ** 2, axis=1))
                        if np.min(dists) < 80:
                            conflicting.add(hex_code)
                except Exception:
                    continue

    for hex_code, label in BG_CANDIDATES:
        if hex_code not in conflicting:
            return hex_code, label
    return BG_CANDIDATES[0]


def build_natural_prompt(user_prompt, style_key, styles, transparent=False, bg_color_info=None):
    """Compose a natural-language prompt with the style preset's brand guidance."""
    style_data = styles.get(style_key, {})
    parts = [user_prompt.rstrip(". ")]

    if style_data:
        if style_data.get("aesthetic"):
            parts.append(style_data["aesthetic"])
        if style_data.get("qualities"):
            parts.append(", ".join(style_data["qualities"]))
        if style_data.get("default_framing"):
            parts.append(style_data["default_framing"])

    if transparent and bg_color_info:
        hex_code, label = bg_color_info
        parts.append(f"The background is plain #{hex_code} {label}. "
                     f"No shadows, no gradients, no floor, no reflections — "
                     f"just a single flat #{hex_code} color filling the entire background")

    if style_data.get("default_constraints"):
        parts.append(". ".join(style_data["default_constraints"]))

    return ". ".join(p.rstrip(". ") for p in parts if p) + "."


# ──────────────────────────────────────────────────────────
# Provider: Google (google-genai)
# ──────────────────────────────────────────────────────────

def _interleave(prompt, ref_images):
    parts = re.split(r"(\{image\d+\})", prompt)
    contents = []
    for part in parts:
        m = re.match(r"\{image(\d+)\}", part)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(ref_images):
                if contents and isinstance(contents[-1], str):
                    contents[-1] = contents[-1].rstrip()
                contents.append(ref_images[idx])
            else:
                contents.append(part)
        elif part:
            contents.append(part)
    return contents


def generate_google(key, model, prompt, ref_paths, aspect_ratio, resolution):
    from google import genai
    from google.genai import types
    from PIL import Image as PILImage

    client = genai.Client(api_key=key)

    ref_images = []
    for rp in ref_paths or []:
        p = Path(rp)
        if p.exists():
            ref_images.append(PILImage.open(str(p)))
        else:
            print(f"Warning: reference not found: {rp}", file=sys.stderr)

    if ref_images:
        if re.search(r"\{image\d+\}", prompt):
            contents = _interleave(prompt, ref_images)
        elif len(ref_images) == 1:
            contents = [ref_images[0], prompt]
        else:
            contents = [prompt] + ref_images
    else:
        contents = [prompt]

    image_cfg = {}
    if aspect_ratio:
        image_cfg["aspect_ratio"] = aspect_ratio
    if resolution:
        image_cfg["image_size"] = resolution
    config = types.GenerateContentConfig(
        response_modalities=["TEXT", "IMAGE"],
        image_config=types.ImageConfig(**image_cfg) if image_cfg else None,
    )

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            images, texts = [], []
            for part in resp.candidates[0].content.parts:
                if part.text is not None:
                    texts.append(part.text)
                elif part.inline_data is not None:
                    images.append(PILImage.open(BytesIO(part.inline_data.data)))
            if texts:
                print(f"Model: {' '.join(texts)}", file=sys.stderr)
            return images
        except Exception as e:
            last_error = e
            _handle_retry(e, attempt)
    print(f"Error: all attempts failed. Last error: {last_error}", file=sys.stderr)
    return []


# ──────────────────────────────────────────────────────────
# Provider: OpenRouter (OpenAI-compatible chat completions)
# ──────────────────────────────────────────────────────────

def _data_url(path):
    p = Path(path)
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    ext = p.suffix.lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "webp": "webp", "gif": "gif"}.get(ext, "png")
    return f"data:image/{mime};base64,{data}"


def generate_openrouter(key, model, prompt, ref_paths, aspect_ratio, resolution):
    import requests
    from PIL import Image as PILImage

    if ref_paths:
        content = [{"type": "text", "text": prompt}]
        for rp in ref_paths:
            p = Path(rp)
            if p.exists():
                content.append({"type": "image_url", "image_url": {"url": _data_url(rp)}})
            else:
                print(f"Warning: reference not found: {rp}", file=sys.stderr)
    else:
        content = prompt

    image_config = {}
    if aspect_ratio:
        image_config["aspect_ratio"] = aspect_ratio
    if resolution:
        image_config["image_size"] = resolution

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
    }
    if image_config:
        payload["image_config"] = image_config

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://skills.sh",
        "X-Title": "brand-logo-kit",
    }

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=180)
            if resp.status_code == 429:
                _handle_retry(Exception("429 rate limit"), attempt)
                continue
            if resp.status_code >= 400:
                print(f"OpenRouter error {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
                if resp.status_code in (400, 401, 402, 403, 404):
                    break
                _handle_retry(Exception(resp.text), attempt)
                continue

            data = resp.json()
            msg = data["choices"][0]["message"]
            images = []
            for img in msg.get("images", []) or []:
                url = img.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    b64 = url.split(",", 1)[1]
                    images.append(PILImage.open(BytesIO(base64.b64decode(b64))))
            if not images and msg.get("content"):
                print(f"Model returned no image. Text: {str(msg['content'])[:300]}", file=sys.stderr)
            return images
        except Exception as e:
            last_error = e
            _handle_retry(e, attempt)
    print(f"Error: all attempts failed. Last error: {last_error}", file=sys.stderr)
    return []


# ──────────────────────────────────────────────────────────
# Provider: Local (on-device image-gen skill, mflux / MLX)
# ──────────────────────────────────────────────────────────

def _aspect_to_wh(aspect_ratio, resolution):
    w, h = AR_BASE.get(aspect_ratio or "1:1", (1024, 1024))
    scale = RES_SCALE.get(resolution or "1K", 1.0)
    w, h = int(round(w * scale / 16) * 16), int(round(h * scale / 16) * 16)
    return w, h


def generate_local(model, prompt, aspect_ratio, resolution, count, ref_paths=None):
    """Render locally by shelling out to the image-gen skill (text-to-image only)."""
    from PIL import Image as PILImage

    script = keylib.find_image_gen_script()
    py = keylib.image_gen_python()
    if script is None:
        print("Error: local fallback needs the 'image-gen' skill (not found).", file=sys.stderr)
        return []
    if py is None:
        print("Error: image-gen is not set up. Run: bash <image-gen>/scripts/setup_env.sh",
              file=sys.stderr)
        return []
    if not keylib.is_apple_silicon():
        print("Warning: local generation uses mflux/MLX and needs an Apple Silicon Mac.",
              file=sys.stderr)
    if ref_paths:
        print("Note: the local model is text-to-image only — reference images are ignored. "
              "Brand consistency relies on the palette + style described in the prompt.",
              file=sys.stderr)

    if model not in keylib.LOCAL_MODELS:
        model = keylib.DEFAULT_LOCAL_MODEL

    full_prompt = f"{prompt.rstrip('. ')}. {LOCAL_LOGO_BOOST}."
    w, h = _aspect_to_wh(aspect_ratio, resolution)

    tmpdir = Path(tempfile.mkdtemp(prefix="blk_local_"))
    out_prefix = tmpdir / "gen.png"
    cmd = [str(py), str(script), "--model", model, "--prompt", full_prompt,
           "--width", str(w), "--height", str(h), "--count", str(count),
           "--out", str(out_prefix)]
    if model == "z-image-turbo":
        cmd += ["--negative-prompt", LOCAL_NEGATIVE]

    print(f"Local render: {model} at {w}x{h} (first run downloads weights)...", file=sys.stderr)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        print(f"Error launching image-gen: {e}", file=sys.stderr)
        return []

    if proc.stderr:
        tail = "\n".join(proc.stderr.strip().splitlines()[-6:])
        print(tail, file=sys.stderr)
    if proc.returncode != 0:
        print(f"Error: image-gen exited {proc.returncode}.", file=sys.stderr)
        return []

    images = []
    for line in proc.stdout.strip().splitlines():
        p = Path(line.strip())
        if p.exists():
            with PILImage.open(str(p)) as im:
                images.append(im.copy())
    if not images:
        print("Error: image-gen produced no output image.", file=sys.stderr)
    return images


def _handle_retry(err, attempt):
    s = str(err)
    if "429" in s:
        delay = min(RETRY_BASE_DELAY * (2 ** attempt), 300)
        print(f"Rate limited, waiting {delay:.0f}s (attempt {attempt+1}/{MAX_RETRIES})...", file=sys.stderr)
        time.sleep(delay)
    else:
        print(f"Attempt {attempt+1} failed: {s[:200]}", file=sys.stderr)
        if attempt < MAX_RETRIES - 1:
            time.sleep(4)


# ──────────────────────────────────────────────────────────
# Post-processing (background removal, trim, save) — shared
# ──────────────────────────────────────────────────────────

def chroma_remove(image, color_hex, tolerance=40):
    import numpy as np
    from PIL import Image as PILImage, ImageFilter
    import colorsys

    img = image.convert("RGBA")
    data = np.array(img, dtype=np.float64)
    tr, tg, tb = (int(color_hex[0:2], 16), int(color_hex[2:4], 16), int(color_hex[4:6], 16))
    target = np.array([tr, tg, tb], dtype=np.float64)

    rgb = data[:, :, :3]
    dist = np.sqrt(np.sum((rgb - target) ** 2, axis=2))
    hard = float(tolerance)
    soft = hard + 60.0
    alpha = np.where(dist < hard, 0.0,
             np.where(dist < soft, ((dist - hard) / (soft - hard)) * 255.0, data[:, :, 3]))

    target_h, _, _ = colorsys.rgb_to_hsv(tr / 255.0, tg / 255.0, tb / 255.0)
    r_n, g_n, b_n = data[:, :, 0] / 255.0, data[:, :, 1] / 255.0, data[:, :, 2] / 255.0
    cmax = np.maximum(np.maximum(r_n, g_n), b_n)
    cmin = np.minimum(np.minimum(r_n, g_n), b_n)
    delta = cmax - cmin
    hue = np.zeros_like(cmax)
    mr = (cmax == r_n) & (delta > 0)
    mg = (cmax == g_n) & (delta > 0)
    mb = (cmax == b_n) & (delta > 0)
    hue[mr] = (((g_n[mr] - b_n[mr]) / delta[mr]) % 6) / 6.0
    hue[mg] = (((b_n[mg] - r_n[mg]) / delta[mg]) + 2) / 6.0
    hue[mb] = (((r_n[mb] - g_n[mb]) / delta[mb]) + 4) / 6.0
    sat = np.where(cmax > 0, delta / cmax, 0)
    hue_diff = np.abs(hue - target_h)
    hue_diff = np.minimum(hue_diff, 1.0 - hue_diff)
    hue_match = ((hue_diff < 0.08) & (sat > 0.08)) | \
                ((hue_diff < 0.12) & (sat > 0.01) & (sat < 0.15) & (cmax > 0.75))
    hue_kill = hue_match & (alpha > 128)
    if np.any(hue_kill):
        hue_alpha = np.where(hue_diff < 0.03, 0.0,
                    np.where(hue_diff < 0.08, ((hue_diff - 0.03) / 0.05) * 255.0, 255.0))
        alpha = np.where(hue_kill, np.minimum(alpha, hue_alpha), alpha)

    data[:, :, 3] = alpha
    result = PILImage.fromarray(data.astype(np.uint8))
    a_eroded = result.split()[3].filter(ImageFilter.MinFilter(3))
    result.putalpha(a_eroded)
    return result


def remove_background(image, chroma_hex=None):
    if chroma_hex:
        return chroma_remove(image, chroma_hex)
    try:
        from rembg import remove
        return remove(image)
    except ImportError:
        print("Warning: rembg not installed; returning image as-is.", file=sys.stderr)
        return image


def trim_transparent(image, padding=2):
    if image.mode != "RGBA":
        return image
    import numpy as np
    alpha = np.array(image.split()[3])
    rows = np.any(alpha > 0, axis=1)
    cols = np.any(alpha > 0, axis=0)
    if not rows.any():
        return image
    top, bottom = np.argmax(rows), alpha.shape[0] - np.argmax(rows[::-1])
    left, right = np.argmax(cols), alpha.shape[1] - np.argmax(cols[::-1])
    top = max(0, top - padding)
    left = max(0, left - padding)
    bottom = min(alpha.shape[0], bottom + padding)
    right = min(alpha.shape[1], right + padding)
    return image.crop((left, top, right, bottom))


def save_image(image, output_path, fmt="png"):
    from PIL import Image as PILImage
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt in ("jpeg", "jpg"):
        if image.mode == "RGBA":
            bg = PILImage.new("RGB", image.size, (255, 255, 255))
            bg.paste(image, mask=image.split()[3])
            image = bg
        image.save(str(output_path), "JPEG", quality=95)
    elif fmt == "webp":
        image.save(str(output_path), "WEBP", quality=95, lossless=image.mode == "RGBA")
    else:
        image.save(str(output_path), "PNG")
    return output_path


def export_sizes(image, base_path, sizes, fmt="png"):
    from PIL import Image as PILImage
    base = Path(base_path)
    exported = []
    for size in sizes:
        resized = image.copy().resize((size, size), PILImage.LANCZOS)
        ext = "jpg" if fmt == "jpeg" else fmt
        size_path = base.parent / f"{base.stem}_{size}x{size}.{ext}"
        save_image(resized, size_path, fmt)
        exported.append(str(size_path))
        print(f"  Exported: {size_path} ({size}x{size})", file=sys.stderr)
    return exported


# ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate brand assets with Gemini image models")
    parser.add_argument("prompt", nargs="?", help="Description of the asset")
    parser.add_argument("--style", "-s", default="logo", help="Brand style preset")
    parser.add_argument("--aspect-ratio", "-ar", default=None,
                        help="1:1, 3:2, 2:3, 16:9, 9:16, 4:3, 3:4, 4:5, 5:4, 21:9")
    parser.add_argument("--resolution", "-r", default=None, choices=["1K", "2K", "4K"])
    parser.add_argument("--transparent", "-t", action="store_true", help="Transparent background PNG")
    parser.add_argument("--format", "-f", default=None, choices=["png", "webp", "jpeg"])
    parser.add_argument("--output", "-o", default=None, help="Output file path")
    parser.add_argument("--ref", action="append", dest="references", metavar="PATH",
                        help="Reference image (repeatable). Use {image1}.. in prompt (Google).")
    parser.add_argument("--count", "-n", type=int, default=1, help="Number of variations (1-4)")
    parser.add_argument("--sizes", help="Also export square sizes, e.g. 64,128,256")
    parser.add_argument("--provider", choices=["google", "openrouter", "local"],
                        help="Force provider (local = on-device image-gen, no key)")
    parser.add_argument("--model", help="Override the model for the chosen provider "
                                        "(local: flux2-klein-4b or z-image-turbo)")
    parser.add_argument("--raw-prompt", action="store_true", help="Use prompt verbatim, no style wrapping")
    parser.add_argument("--list-styles", action="store_true")
    args = parser.parse_args()

    if args.list_styles:
        list_styles()
        return
    if not args.prompt:
        parser.print_help()
        sys.exit(1)

    try:
        key, provider, source = keylib.resolve_key(prefer=args.provider)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    model = args.model or keylib.model_for(provider)

    styles = load_styles()
    style_choice = args.style
    cfg = keylib.load_config()
    fmt = args.format or cfg.get("default_format", "png")

    aspect_ratio = args.aspect_ratio
    if not aspect_ratio and style_choice in styles:
        aspect_ratio = styles[style_choice].get("recommended_aspect_ratio")

    transparent = args.transparent
    if transparent:
        fmt = "png"

    ref_paths = args.references or []
    ext = "jpg" if fmt == "jpeg" else fmt
    output_base = args.output or f"brand_asset.{ext}"

    print(f"Provider: {provider} (source: {source})", file=sys.stderr)
    print(f"Model: {model}", file=sys.stderr)
    print(f"Style: {style_choice}", file=sys.stderr)
    print(f"Aspect ratio: {aspect_ratio or 'default'}", file=sys.stderr)
    if args.resolution:
        print(f"Resolution: {args.resolution}", file=sys.stderr)
    if transparent:
        print("Background removal: enabled", file=sys.stderr)
    if ref_paths:
        print(f"Reference images: {len(ref_paths)}", file=sys.stderr)

    bg_color_info = None
    if transparent:
        bg_color_info = pick_bg_color(args.prompt, ref_paths or None)
        print(f"Background color: #{bg_color_info[0]} ({bg_color_info[1]})", file=sys.stderr)

    if args.raw_prompt:
        prompt = args.prompt
        if transparent and bg_color_info:
            hex_code, label = bg_color_info
            prompt = prompt.rstrip(". ") + (
                f". The background is plain #{hex_code} {label}. No shadows, no gradients, "
                f"just a single flat #{hex_code} color filling the entire background.")
    else:
        prompt = build_natural_prompt(args.prompt, style_choice, styles,
                                      transparent=transparent, bg_color_info=bg_color_info)

    print("Generating...", file=sys.stderr)
    count = min(max(args.count, 1), 4)
    all_images = []
    if provider == "local":
        all_images = generate_local(model, prompt, aspect_ratio, args.resolution, count,
                                    ref_paths=ref_paths)
    else:
        for i in range(count):
            if provider == "openrouter":
                imgs = generate_openrouter(key, model, prompt, ref_paths, aspect_ratio, args.resolution)
            else:
                imgs = generate_google(key, model, prompt, ref_paths, aspect_ratio, args.resolution)
            all_images.extend(imgs)
            if not imgs:
                print(f"Warning: no image for variation {i+1}", file=sys.stderr)

    if not all_images:
        print("Error: no images generated. Try a different prompt or provider.", file=sys.stderr)
        sys.exit(1)

    files = []
    for idx, image in enumerate(all_images):
        if transparent:
            chroma_hex = bg_color_info[0] if bg_color_info else None
            print(f"Removing background (chroma #{chroma_hex})...", file=sys.stderr)
            image = remove_background(image, chroma_hex=chroma_hex)
            image = trim_transparent(image, padding=2)

        if idx == 0 and len(all_images) == 1:
            out_path = output_base
        else:
            base = Path(output_base)
            out_path = str(base.parent / f"{base.stem}_{idx+1}{base.suffix}")

        saved = save_image(image, out_path, fmt)
        files.append(str(saved))
        print(f"Saved: {saved} ({image.size[0]}x{image.size[1]}, {image.mode})", file=sys.stderr)

        if args.sizes:
            sizes = [int(s.strip()) for s in args.sizes.split(",")]
            export_sizes(image, out_path, sizes, fmt)

    print(json.dumps({
        "prompt": args.prompt,
        "provider": provider,
        "model": model,
        "style": style_choice,
        "aspect_ratio": aspect_ratio,
        "resolution": args.resolution,
        "transparent": transparent,
        "format": fmt,
        "reference_images": ref_paths or None,
        "files": files,
    }, indent=2))


if __name__ == "__main__":
    main()
