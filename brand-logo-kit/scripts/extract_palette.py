#!/usr/bin/env python3
"""Extract a brand color palette from a logo (or any image) into brand.json.

Quantizes the image to its dominant colors, ignoring transparent and near-white
pixels, and writes a reusable brand descriptor the generate step can reference
for consistency.

Usage:
    python3 extract_palette.py logo.png --name "Acme" -o brand.json
    python3 extract_palette.py logo.png --colors 6
"""

import argparse
import json
import sys
from pathlib import Path


def rgb_to_hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(*rgb[:3])


def relative_luminance(rgb):
    r, g, b = [c / 255.0 for c in rgb[:3]]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def extract_palette(path, n_colors=6, ignore_bg=True):
    from PIL import Image
    import numpy as np

    img = Image.open(str(path)).convert("RGBA")
    arr = np.array(img).reshape(-1, 4)

    mask = np.ones(len(arr), dtype=bool)
    if ignore_bg:
        mask &= arr[:, 3] > 128  # drop transparent
        near_white = (arr[:, 0] > 244) & (arr[:, 1] > 244) & (arr[:, 2] > 244)
        mask &= ~near_white
    pixels = arr[mask][:, :3]
    if len(pixels) == 0:
        pixels = arr[:, :3]

    rgb_img = Image.fromarray(pixels.reshape(-1, 1, 3).astype("uint8"))
    quant = rgb_img.quantize(colors=max(n_colors * 2, n_colors), method=Image.MEDIANCUT)
    palette = quant.getpalette()
    counts = quant.getcolors() or []

    colors = []
    for count, idx in sorted(counts, reverse=True):
        rgb = tuple(palette[idx * 3: idx * 3 + 3])
        colors.append((count, rgb))

    # Merge visually similar colors, keep the most frequent distinct ones.
    distinct = []
    for count, rgb in colors:
        if all(sum((a - b) ** 2 for a, b in zip(rgb, kept)) > 900 for _, kept in distinct):
            distinct.append((count, rgb))
        if len(distinct) >= n_colors:
            break

    total = sum(c for c, _ in distinct) or 1
    return [
        {"hex": rgb_to_hex(rgb), "rgb": list(rgb),
         "weight": round(count / total, 3),
         "luminance": round(relative_luminance(rgb), 3)}
        for count, rgb in distinct
    ]


def main():
    parser = argparse.ArgumentParser(description="Extract a brand palette from an image")
    parser.add_argument("image", help="Path to the logo/image")
    parser.add_argument("--name", default="", help="Brand name to embed in brand.json")
    parser.add_argument("--colors", "-c", type=int, default=6, help="Number of palette colors")
    parser.add_argument("--output", "-o", default="brand.json", help="Output JSON path")
    parser.add_argument("--keep-bg", action="store_true", help="Include background/white pixels")
    args = parser.parse_args()

    if not Path(args.image).exists():
        print(f"Error: image not found: {args.image}", file=sys.stderr)
        sys.exit(1)

    palette = extract_palette(args.image, n_colors=args.colors, ignore_bg=not args.keep_bg)

    ordered = sorted(palette, key=lambda c: c["luminance"])
    darkest = ordered[0]["hex"] if ordered else "#111111"
    lightest = ordered[-1]["hex"] if ordered else "#FFFFFF"
    primary = palette[0]["hex"] if palette else "#000000"
    secondary = palette[1]["hex"] if len(palette) > 1 else primary
    accent = max(palette, key=lambda c: max(c["rgb"]) - min(c["rgb"]))["hex"] if palette else primary

    brand = {
        "name": args.name,
        "source_image": str(args.image),
        "palette": palette,
        "roles": {
            "primary": primary,
            "secondary": secondary,
            "accent": accent,
            "ink": darkest,
            "paper": lightest,
        },
        "prompt_snippet": (
            "Use the brand palette "
            + ", ".join(c["hex"] for c in palette)
            + f" (primary {primary}, accent {accent}). Keep the same visual style as the logo."
        ),
    }

    Path(args.output).write_text(json.dumps(brand, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    print(json.dumps(brand["roles"], indent=2))
    print("Palette:", " ".join(c["hex"] for c in palette))


if __name__ == "__main__":
    main()
