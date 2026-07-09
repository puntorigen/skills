#!/usr/bin/env python3
"""Compose a crisp, on-brand WORDMARK from real fonts — and optional lockups.

Image models (especially local diffusion) render text with a generic, mushy
font and often misspell it. This script sidesteps that entirely: it sets the
brand name in a genuine high-quality typeface (Futura, Avenir Next, Gill Sans,
DIN, Optima, Didot, …) with real kerning and adjustable tracking, on a
transparent background — 100% locally and deterministically. Optionally it
combines the wordmark with a symbol you generated (generate.py) into a finished
horizontal or stacked lockup.

Usage:
    # Just the wordmark, in a chosen font + brand color:
    python3 wordmark.py --text "ACME" --font Futura --color "#0A7CFF" -o wordmark.png

    # With generous letter-spacing, uppercased:
    python3 wordmark.py --text "acme" --case upper --tracking 0.18 -o wordmark.png

    # Lockup: your symbol on the left, the name on the right:
    python3 wordmark.py --text "Acme" --mark mark.png --layout horizontal -o lockup.png

    # Pull the ink color from a brand.json produced by extract_palette.py:
    python3 wordmark.py --text "Acme" --brand brand.json -o wordmark.png

    python3 wordmark.py --list-fonts      # show curated fonts available on this machine
"""

import argparse
import json
import sys
from pathlib import Path

# Where to look for installed fonts (macOS first, then common Linux dirs).
FONT_DIRS = [
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    str(Path.home() / "Library" / "Fonts"),
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/usr/share/fonts/truetype",
]

# Curated, logo-appropriate typefaces: friendly name -> filename substrings to try.
CURATED_FONTS = [
    ("Futura", ["Futura"]),
    ("Avenir Next", ["Avenir Next", "AvenirNext"]),
    ("Avenir", ["Avenir"]),
    ("Gill Sans", ["GillSans", "Gill Sans"]),
    ("Optima", ["Optima"]),
    ("DIN Condensed", ["DIN Condensed", "DINCondensed"]),
    ("DIN Alternate", ["DIN Alternate", "DINAlternate"]),
    ("Helvetica Neue", ["HelveticaNeue", "Helvetica Neue"]),
    ("Helvetica", ["Helvetica"]),
    ("Copperplate", ["Copperplate"]),
    ("Didot", ["Didot"]),
    ("Baskerville", ["Baskerville"]),
    ("Palatino", ["Palatino"]),
    ("Georgia", ["Georgia"]),
    ("Menlo", ["Menlo"]),
    # Cross-platform / bundled fallbacks:
    ("Montserrat", ["Montserrat"]),
    ("Poppins", ["Poppins"]),
    ("DejaVu Sans", ["DejaVuSans"]),
]

FONT_EXTS = (".ttf", ".otf", ".ttc")


def _all_font_files():
    files = []
    for d in FONT_DIRS:
        p = Path(d)
        if not p.is_dir():
            continue
        for f in p.rglob("*"):
            if f.suffix.lower() in FONT_EXTS:
                files.append(f)
    return files


def _match(files, substr):
    """Best file for a family substring: prefer names that START with it, then the
    shortest name (the base weight, not 'Condensed'/'Italic'/'Bold' variants), and
    avoid loose substring hits like 'Georgia' inside 'SFGeorgian'."""
    s = substr.lower()
    starts = [f for f in files if f.stem.lower().startswith(s)]
    pool = starts or [f for f in files if s in f.stem.lower()]
    if not pool:
        return None
    return min(pool, key=lambda f: (len(f.stem), str(f)))


def resolve_font(spec):
    """Return (path, display_name) for a font spec (path | substring | friendly name)."""
    if spec:
        p = Path(spec)
        if p.exists() and p.suffix.lower() in FONT_EXTS:
            return p, p.stem

    files = _all_font_files()

    if spec:
        # Try friendly-name aliases first, then a raw substring match.
        for name, subs in CURATED_FONTS:
            if spec.lower() in name.lower():
                for sub in subs:
                    hit = _match(files, sub)
                    if hit:
                        return hit, name
        hit = _match(files, spec)
        if hit:
            return hit, hit.stem

    for name, subs in CURATED_FONTS:
        for sub in subs:
            hit = _match(files, sub)
            if hit:
                return hit, name

    # Last resort: whatever PIL bundles.
    try:
        from PIL import ImageFont
        ImageFont.truetype("DejaVuSans.ttf", 24)
        return "DejaVuSans.ttf", "DejaVu Sans (PIL bundled)"
    except Exception:
        return None, None


def list_fonts():
    files = _all_font_files()
    print("Curated logo fonts available on this machine:\n")
    any_found = False
    for name, subs in CURATED_FONTS:
        hit = None
        for sub in subs:
            hit = _match(files, sub)
            if hit:
                break
        if hit:
            any_found = True
            print(f"  {name:16s} -> {hit}")
        else:
            print(f"  {name:16s}    (not installed)")
    if not any_found:
        print("  (none of the curated fonts found; pass --font with a .ttf/.otf path)")
    print("\nYou can also pass --font with any family substring or a direct file path.")


def parse_hex(color):
    c = color.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        raise ValueError(f"Invalid color: {color}")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) + (255,)


def apply_case(text, mode):
    if mode == "upper":
        return text.upper()
    if mode == "lower":
        return text.lower()
    if mode == "title":
        return text.title()
    return text


def trim_alpha(img, pad=0):
    import numpy as np
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = np.array(img.split()[3])
    rows = np.any(alpha > 0, axis=1)
    cols = np.any(alpha > 0, axis=0)
    if not rows.any():
        return img
    top, bottom = int(np.argmax(rows)), int(alpha.shape[0] - np.argmax(rows[::-1]))
    left, right = int(np.argmax(cols)), int(alpha.shape[1] - np.argmax(cols[::-1]))
    top, left = max(0, top - pad), max(0, left - pad)
    bottom = min(alpha.shape[0], bottom + pad)
    right = min(alpha.shape[1], right + pad)
    return img.crop((left, top, right, bottom))


def render_wordmark(text, font_path, font_index, size, ink, tracking_em):
    """Render text with per-glyph tracking onto a tight, transparent RGBA image."""
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype(str(font_path), size, index=font_index)
    except Exception:
        font = ImageFont.truetype(str(font_path), size)

    tracking_px = tracking_em * size
    widths = [font.getlength(ch) for ch in text]
    total = sum(widths) + tracking_px * max(len(text) - 1, 0)

    canvas_w = int(total + 4 * size) + 2
    canvas_h = int(2.6 * size) + 2
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    x = float(size)
    baseline = canvas_h * 0.62
    for ch, w in zip(text, widths):
        draw.text((x, baseline), ch, font=font, fill=ink, anchor="ls")
        x += w + tracking_px

    return trim_alpha(img, pad=0)


def scale_to_height(img, target_h):
    from PIL import Image
    if img.height == 0:
        return img
    w = max(1, round(img.width * target_h / img.height))
    return img.resize((w, target_h), Image.LANCZOS)


def compose_lockup(mark_img, text_img, layout, mark_scale, gap_em):
    from PIL import Image
    th = text_img.height
    gap = int(gap_em * th)
    mark = trim_alpha(mark_img.convert("RGBA"), pad=0)

    if layout == "vertical":
        mark = scale_to_height(mark, int(th * mark_scale))
        w = max(mark.width, text_img.width)
        h = mark.height + gap + th
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        canvas.alpha_composite(mark, ((w - mark.width) // 2, 0))
        canvas.alpha_composite(text_img, ((w - text_img.width) // 2, mark.height + gap))
        return canvas

    # horizontal
    mark = scale_to_height(mark, int(th * mark_scale))
    h = max(mark.height, th)
    w = mark.width + gap + text_img.width
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.alpha_composite(mark, (0, (h - mark.height) // 2))
    canvas.alpha_composite(text_img, (mark.width + gap, (h - th) // 2))
    return canvas


def add_padding_and_bg(img, pad, bg_hex):
    from PIL import Image
    bg = (0, 0, 0, 0) if not bg_hex else parse_hex(bg_hex)
    w, h = img.width + 2 * pad, img.height + 2 * pad
    canvas = Image.new("RGBA", (w, h), bg)
    canvas.alpha_composite(img, (pad, pad))
    return canvas


def color_from_brand(brand_path, role):
    data = json.loads(Path(brand_path).read_text(encoding="utf-8"))
    roles = data.get("roles", {})
    return roles.get(role) or roles.get("ink") or roles.get("primary") or "#111111"


def main():
    parser = argparse.ArgumentParser(description="Compose a real-font wordmark / lockup")
    parser.add_argument("--text", "-T", help="The brand name / text to set")
    parser.add_argument("--font", help="Font family substring, friendly name, or .ttf/.otf path")
    parser.add_argument("--font-index", type=int, default=0, help="Face index inside a .ttc collection")
    parser.add_argument("--size", type=int, default=320, help="Font size in px (default 320)")
    parser.add_argument("--color", help="Ink color hex, e.g. #0A7CFF (default #111111)")
    parser.add_argument("--brand", help="brand.json from extract_palette.py to pull the color from")
    parser.add_argument("--color-role", default="ink",
                        help="Which brand.json role to use for color (ink|primary|secondary|accent)")
    parser.add_argument("--tracking", type=float, default=0.0,
                        help="Letter-spacing in em (e.g. 0.15 for airy caps)")
    parser.add_argument("--case", choices=["upper", "lower", "title", "as-is"], default="as-is")
    parser.add_argument("--mark", help="Optional symbol image to build a lockup")
    parser.add_argument("--layout", choices=["horizontal", "vertical"], default="horizontal")
    parser.add_argument("--mark-scale", type=float, default=1.6,
                        help="Mark height relative to text height in a lockup")
    parser.add_argument("--gap", type=float, default=0.35,
                        help="Gap between mark and text, in em of text height")
    parser.add_argument("--padding", type=int, default=None,
                        help="Padding around the result in px (default ~12%% of size)")
    parser.add_argument("--bg", help="Background fill hex (default transparent)")
    parser.add_argument("--output", "-o", default="wordmark.png", help="Output PNG path")
    parser.add_argument("--list-fonts", action="store_true")
    args = parser.parse_args()

    if args.list_fonts:
        list_fonts()
        return
    if not args.text:
        parser.error("--text is required (or use --list-fonts)")

    font_path, font_name = resolve_font(args.font)
    if not font_path:
        print("Error: no usable font found. Pass --font with a .ttf/.otf path.", file=sys.stderr)
        sys.exit(1)

    if args.color:
        color_hex = args.color
    elif args.brand:
        color_hex = color_from_brand(args.brand, args.color_role)
    else:
        color_hex = "#111111"

    try:
        ink = parse_hex(color_hex)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    text = apply_case(args.text, args.case)
    pad = args.padding if args.padding is not None else int(args.size * 0.12)

    print(f"Font:  {font_name}  ({font_path})", file=sys.stderr)
    print(f"Text:  {text!r}", file=sys.stderr)
    print(f"Color: {color_hex}", file=sys.stderr)
    print(f"Tracking: {args.tracking} em, size {args.size}px", file=sys.stderr)

    text_img = render_wordmark(text, font_path, args.font_index, args.size, ink, args.tracking)

    if args.mark:
        mp = Path(args.mark)
        if not mp.exists():
            print(f"Error: mark image not found: {args.mark}", file=sys.stderr)
            sys.exit(1)
        from PIL import Image
        mark_img = Image.open(str(mp)).convert("RGBA")
        result = compose_lockup(mark_img, text_img, args.layout, args.mark_scale, args.gap)
        print(f"Lockup: {args.layout} (mark scale {args.mark_scale}, gap {args.gap} em)",
              file=sys.stderr)
    else:
        result = text_img

    result = add_padding_and_bg(result, pad, args.bg)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.save(str(out), "PNG")
    print(f"Saved: {out} ({result.width}x{result.height})", file=sys.stderr)
    print(json.dumps({
        "text": text,
        "font": font_name,
        "font_path": str(font_path),
        "color": color_hex,
        "tracking_em": args.tracking,
        "size": args.size,
        "lockup": bool(args.mark),
        "layout": args.layout if args.mark else None,
        "file": str(out),
        "dimensions": [result.width, result.height],
    }, indent=2))


if __name__ == "__main__":
    main()
