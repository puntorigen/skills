#!/usr/bin/env python3
"""Generate hyper-realistic images locally with mflux (native MLX, Apple Silicon).

Wraps the documented mflux Python API. Two commercial-safe (Apache-2.0) engines:

  - z-image-turbo (default): Tongyi-MAI Z-Image-Turbo, 6B, photorealism-focused,
    9 steps, no CFG. Uses the pre-quantized 4-bit weights
    (filipstrand/Z-Image-Turbo-mflux-4bit, ~5.5 GB) so it fits a 16 GB Mac.
  - flux2-klein-4b: Black Forest Labs FLUX.2 Klein 4B, 4 steps, quantized on load.

Weights download from Hugging Face on first use into the HF cache
(~/.cache/huggingface); nothing is written into the repo. Output images go to
--out (or ~/.image-gen/out/ by default).

Run with the skill's venv python:
  ~/.image-gen/.venv/bin/python generate_image.py --prompt "..." [opts]

Usage:
  generate_image.py (--prompt "..." | --prompt-file P) [--model z-image-turbo]
                    [--width 1024] [--height 1024] [--count 1] [--seed N]
                    [--steps N] [--guidance G] [--negative-prompt "..."]
                    [--quantize {4,6,8}] [--model-path REPO_OR_DIR]
                    [--lora-paths ...] [--lora-scales ...] [--save-metadata]
                    [--out path.png]

Prints one output image path per line on stdout.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import platform
import random
import sys

# Anonymous, high-performance HF downloads; quiet tokenizers.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)
    sys.stderr.flush()


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def img_home() -> str:
    return os.environ.get("IMAGE_GEN_HOME", os.path.expanduser("~/.image-gen"))


# Per-model defaults. All three are Apache-2.0 (commercial-safe).
MODELS: dict[str, dict] = {
    "z-image-turbo": {
        "default_steps": 9,
        "default_quantize": None,  # the default model_path is already 4-bit
        "default_model_path": "filipstrand/Z-Image-Turbo-mflux-4bit",
        "full_model_path": "Tongyi-MAI/Z-Image-Turbo",  # for on-the-fly quant
        "guidance": None,  # turbo bakes guidance in; no CFG
        "supports_negative": True,
    },
    "flux2-klein-4b": {
        "default_steps": 4,
        "default_quantize": 8,  # quantize the bf16 download on load to fit 16 GB
        "default_model_path": None,
        "full_model_path": None,
        "guidance": 1.0,
        "supports_negative": False,  # FLUX.2 has no negative prompt
    },
}


def snap16(v: int) -> int:
    """Snap a dimension to the nearest positive multiple of 16 (VAE requirement)."""
    return max(16, int(round(v / 16.0)) * 16)


def out_paths(out_arg: str | None, count: int, model: str) -> list[str]:
    if not out_arg:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.join(img_home(), "out", f"{model}-{ts}.png")
    else:
        base = os.path.abspath(out_arg)
    root, ext = os.path.splitext(base)
    ext = ext or ".png"
    if count == 1:
        return [root + ext]
    return [f"{root}-{i + 1}{ext}" for i in range(count)]


def load_model(model: str, quantize, model_path, lora_paths, lora_scales):
    from mflux.models.common.config import ModelConfig

    if model == "z-image-turbo":
        from mflux.models.z_image.variants.z_image import ZImage

        return ZImage(
            model_config=ModelConfig.z_image_turbo(),
            quantize=quantize,
            model_path=model_path,
            lora_paths=lora_paths or None,
            lora_scales=lora_scales or None,
        )
    if model == "flux2-klein-4b":
        from mflux.models.flux2.variants import Flux2Klein

        return Flux2Klein(
            model_config=ModelConfig.flux2_klein_4b(),
            quantize=quantize,
            model_path=model_path,
            lora_paths=lora_paths or None,
            lora_scales=lora_scales or None,
        )
    raise ValueError(f"unknown model {model!r}")


def clear_caches():
    try:
        import gc

        gc.collect()
    except Exception:  # noqa: BLE001
        pass
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate hyper-realistic images locally (mflux / MLX).")
    ap.add_argument("--prompt", help="Text prompt describing the image.")
    ap.add_argument("--prompt-file", help="Read the prompt from a file instead of --prompt.")
    ap.add_argument("--model", choices=list(MODELS), default="z-image-turbo", help="Which engine to use.")
    ap.add_argument("--out", default=None, help="Output image path (or prefix when --count > 1).")
    ap.add_argument("--width", type=int, default=1024, help="Width in px (snapped to a multiple of 16).")
    ap.add_argument("--height", type=int, default=1024, help="Height in px (snapped to a multiple of 16).")
    ap.add_argument("--count", type=int, default=1, help="How many candidates to generate (distinct seeds).")
    ap.add_argument("--seed", type=int, default=None, help="Base seed (candidate i uses seed+i). Default random.")
    ap.add_argument("--steps", type=int, default=None, help="Diffusion steps (default per model).")
    ap.add_argument("--guidance", type=float, default=None, help="Guidance/CFG (FLUX.2 only; Turbo ignores it).")
    ap.add_argument("--negative-prompt", default=None, help="Negative prompt (Z-Image only).")
    ap.add_argument("--quantize", type=int, default=None, choices=[4, 6, 8], help="Quantize weights on load.")
    ap.add_argument("--model-path", default=None, help="Override HF repo id or local dir for the weights.")
    ap.add_argument("--lora-paths", nargs="*", default=None, help="LoRA repo ids / paths (Z-Image, FLUX.2).")
    ap.add_argument("--lora-scales", nargs="*", type=float, default=None, help="LoRA scales (match --lora-paths).")
    ap.add_argument("--save-metadata", action="store_true", help="Also write a JSON sidecar with the settings.")
    args = ap.parse_args()

    # --- resolve prompt --------------------------------------------------------
    prompt = args.prompt
    if args.prompt_file:
        try:
            with open(os.path.expanduser(args.prompt_file), encoding="utf-8") as f:
                prompt = f.read().strip()
        except OSError as e:
            eprint(f"[image] could not read --prompt-file: {e}")
            return 2
    if not prompt:
        eprint("[image] error: provide --prompt or --prompt-file.")
        return 2

    if not is_apple_silicon():
        eprint("[image] WARNING: mflux requires Apple Silicon (MLX/Metal); this will likely fail here.")

    spec = MODELS[args.model]
    steps = args.steps if args.steps is not None else spec["default_steps"]
    quantize = args.quantize if args.quantize is not None else spec["default_quantize"]
    model_path = args.model_path if args.model_path is not None else spec["default_model_path"]
    guidance = args.guidance if args.guidance is not None else spec["guidance"]

    # If the user forces a quantization for z-image-turbo but keeps the default
    # (already-4-bit) weights, switch to the full repo so quantize can apply.
    if (
        args.model == "z-image-turbo"
        and args.quantize is not None
        and args.model_path is None
        and spec["full_model_path"]
    ):
        model_path = spec["full_model_path"]
        eprint(
            f"[image] --quantize {args.quantize} set: loading the full {model_path} and quantizing on the "
            "fly (large ~31 GB first download). Omit --quantize to use the pre-quantized 4-bit weights."
        )

    if args.negative_prompt and not spec["supports_negative"]:
        eprint(f"[image] note: {args.model} does not support a negative prompt; ignoring --negative-prompt.")

    # --- resolution ------------------------------------------------------------
    w, h = snap16(args.width), snap16(args.height)
    if (w, h) != (args.width, args.height):
        eprint(f"[image] snapped resolution {args.width}x{args.height} -> {w}x{h} (multiples of 16)")
    megapixels = (w * h) / 1_000_000
    if megapixels > 2.1:
        eprint(
            f"[image] note: {w}x{h} (~{megapixels:.1f} MP) exceeds the native sweet spot (~1-2 MP); "
            "expect softer detail or repeated elements. Prefer generating near 1 MP and running "
            "upscale_image.py for hi-res."
        )

    count = max(1, args.count)
    targets = out_paths(args.out, count, args.model)
    os.makedirs(os.path.dirname(targets[0]), exist_ok=True)

    eprint(
        f"[image] loading {args.model} (quantize={quantize}, path={model_path or 'model default'}) "
        f"- first run downloads weights..."
    )
    try:
        model = load_model(args.model, quantize, model_path, args.lora_paths, args.lora_scales)
    except Exception as e:  # noqa: BLE001
        eprint(f"[image] failed to load model: {e}")
        eprint("[image] Make sure setup_env.sh completed and you are on an Apple Silicon Mac.")
        return 1

    base_seed = args.seed if args.seed is not None else random.randint(0, 2**31 - 1)

    written: list[str] = []
    for i in range(count):
        seed = base_seed + i
        gen_kwargs = dict(seed=seed, prompt=prompt, width=w, height=h, num_inference_steps=steps)
        if guidance is not None:
            gen_kwargs["guidance"] = guidance
        if spec["supports_negative"] and args.negative_prompt:
            gen_kwargs["negative_prompt"] = args.negative_prompt

        eprint(f"[image] generating {i + 1}/{count} (seed={seed}, {steps} steps, {w}x{h})...")
        try:
            image = model.generate_image(**gen_kwargs)
            image.save(path=targets[i], export_json_metadata=args.save_metadata, overwrite=True)
        except Exception as e:  # noqa: BLE001
            eprint(f"[image] candidate {i + 1} failed: {e}")
            continue

        written.append(targets[i])
        eprint(f"[image]   {targets[i]}")
        clear_caches()

    if not written:
        eprint("[image] nothing was written.")
        return 1

    eprint(f"[image] done: {len(written)} image(s).")
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
