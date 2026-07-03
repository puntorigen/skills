#!/usr/bin/env python3
"""Upscale an image locally with SeedVR2 (native MLX, Apple Silicon).

SeedVR2 (ByteDance, Apache-2.0) is a one-step diffusion super-resolution model.
It needs no prompt and is faithful to the input, so it pairs well with
generate_image.py: render near ~1 MP for best native quality, then upscale here
for hi-res (2x/3x or an explicit target). Weights (~8 GB fp16 for the 3B model)
download to the HF cache on first use, never into the repo.

Run with the skill's venv python:
  ~/.image-gen/.venv/bin/python upscale_image.py --input in.png --resolution 2x

Usage:
  upscale_image.py --input IMG [--out OUT] [--resolution 2x|3x|<px>]
                   [--softness 0.5] [--model seedvr2-3b|seedvr2-7b]
                   [--quantize {4,6,8}] [--seed N] [--mlx-cache-limit-gb G]
                   [--low-ram]

--resolution is either a scale factor ("2x", "3x") or an integer target for the
shortest side in pixels (aspect ratio preserved). Prints the output path on stdout.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)
    sys.stderr.flush()


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def img_home() -> str:
    return os.environ.get("IMAGE_GEN_HOME", os.path.expanduser("~/.image-gen"))


def parse_resolution(value: str):
    """Return an int (target shortest side) or a ScaleFactor for '2x'/'3x'."""
    v = value.strip().lower()
    if v.endswith("x"):
        from mflux.utils.scale_factor import ScaleFactor

        return ScaleFactor.parse(v)
    return int(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="Upscale an image locally with SeedVR2 (MLX).")
    ap.add_argument("--input", required=True, help="Input image to upscale.")
    ap.add_argument("--out", default=None, help="Output path (default: <input>_upscaled.png in the out dir).")
    ap.add_argument("--resolution", default="2x", help='Scale factor ("2x"/"3x") or target shortest side in px.')
    ap.add_argument("--softness", type=float, default=0.0, help="Input pre-downsampling 0.0-1.0 (0.5 = smoother).")
    ap.add_argument(
        "--model",
        choices=["seedvr2-3b", "seedvr2-7b"],
        default="seedvr2-3b",
        help="SeedVR2 size (3B is the 16 GB-friendly default).",
    )
    ap.add_argument("--quantize", type=int, default=None, choices=[4, 6, 8], help="Quantize weights on load.")
    ap.add_argument("--seed", type=int, default=42, help="Seed (default 42).")
    ap.add_argument("--mlx-cache-limit-gb", type=float, default=None, help="Cap the MLX cache to N GB (saves RAM).")
    ap.add_argument("--low-ram", action="store_true", help="Aggressively cap the MLX cache (~1 GB) for big targets.")
    args = ap.parse_args()

    src = os.path.expanduser(args.input)
    if not os.path.isfile(src):
        eprint(f"[upscale] input not found: {src}")
        return 2

    if not is_apple_silicon():
        eprint("[upscale] WARNING: mflux requires Apple Silicon (MLX/Metal); this will likely fail here.")

    if args.out:
        out = os.path.abspath(args.out)
    else:
        stem = os.path.splitext(os.path.basename(src))[0]
        out = os.path.join(img_home(), "out", f"{stem}_upscaled.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    try:
        resolution = parse_resolution(args.resolution)
    except Exception as e:  # noqa: BLE001
        eprint(f"[upscale] bad --resolution {args.resolution!r}: {e}")
        return 2

    # Optionally cap the MLX cache before loading to keep large upscales in budget.
    try:
        import mlx.core as mx

        if args.low_ram:
            mx.set_cache_limit(1000**3)  # ~1 GB
        elif args.mlx_cache_limit_gb is not None:
            mx.set_cache_limit(int(args.mlx_cache_limit_gb * (1000**3)))
    except Exception:  # noqa: BLE001
        pass

    try:
        from mflux.models.common.config import ModelConfig
        from mflux.models.seedvr2 import SeedVR2

        model_config = ModelConfig.seedvr2_7b() if args.model == "seedvr2-7b" else ModelConfig.seedvr2_3b()
        eprint(f"[upscale] loading {args.model} (quantize={args.quantize}) - first run downloads weights...")
        model = SeedVR2(quantize=args.quantize, model_config=model_config)
    except Exception as e:  # noqa: BLE001
        eprint(f"[upscale] failed to load SeedVR2: {e}")
        eprint("[upscale] Make sure setup_env.sh completed and you are on an Apple Silicon Mac.")
        return 1

    eprint(f"[upscale] upscaling {src} -> {out} (resolution={args.resolution}, softness={args.softness})...")
    try:
        image = model.generate_image(
            seed=args.seed,
            image_path=src,
            resolution=resolution,
            softness=args.softness,
        )
        image.save(path=out, overwrite=True)
    except Exception as e:  # noqa: BLE001
        eprint(f"[upscale] failed: {e}")
        eprint("[upscale] If this is an out-of-memory error, retry with --low-ram and/or --quantize 8, "
               "or a smaller --resolution.")
        return 1

    eprint("[upscale] done.")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
