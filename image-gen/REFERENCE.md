# Image Generation - Reference

Models, licenses, quantization trade-offs, the mflux API, and troubleshooting for
the image-gen skill.

## Why mflux

- **License: MIT** (the runtime). mflux is a line-by-line MLX port of modern
  image models from Hugging Face Diffusers - the same "load a local GenAI model
  and drive it programmatically" approach as Draw Things, but as a scriptable
  Python/CLI package with no GUI.
- **Apple Silicon native (MLX):** runs the DiT and VAE directly on the Apple GPU
  via Metal, with built-in weight quantization (4/6/8-bit) so 6B-class models fit
  16 GB unified memory.
- **Arbitrary resolution:** width/height are free parameters (multiples of 16);
  any aspect ratio.
- **Anonymous downloads:** weights pull from Hugging Face without a token, into
  the shared HF cache (`~/.cache/huggingface`).
- **One toolchain, many models:** generation (Z-Image, FLUX.2) and upscaling
  (SeedVR2) all live in the same package, so the whole pipeline is one dependency.

## Model matrix (what this skill wires in)

All three are **Apache-2.0** (commercial-safe).

| Role | Model | Params | Steps | Weights (as used) | License |
|------|-------|--------|-------|-------------------|---------|
| Generate (default) | Z-Image-Turbo (Tongyi-MAI) | 6B | 9 | `filipstrand/Z-Image-Turbo-mflux-4bit` (~5.5 GB, pre-quantized 4-bit) | Apache-2.0 |
| Generate (alt) | FLUX.2-klein-4B (Black Forest Labs) | 4B | 4 | `black-forest-labs/FLUX.2-klein-4B` (~15 GB bf16, quantized to 8-bit on load) | Apache-2.0 |
| Upscale | SeedVR2 (ByteDance) | 3B | 1 | `seedvr2-3b` (~8 GB fp16) | Apache-2.0 |

- **Z-Image-Turbo** is the default because it is photorealism-first, needs no CFG
  or negative prompt, and the pre-quantized 4-bit weights are the best
  quality-per-GB fit for a 16 GB Mac.
- **FLUX.2-klein-4B** is a strong second engine for stylistic variety and
  composition. It uses guidance (default 1.0) and has no negative prompt.
- **SeedVR2-3B** is a one-step, prompt-free super-resolution model - fast and
  faithful. A 7B variant exists (`--model seedvr2-7b`) but is heavy for 16 GB.

### Measured on an M4 / 16 GB (this machine)

| Task | Time |
|------|------|
| First-ever run: download Z-Image-Turbo 4-bit | ~5-6 min (one time) |
| Z-Image-Turbo 768x768, 9 steps | ~12 s/step (~2 min) |
| Z-Image-Turbo 1024x1024, 9 steps | ~23 s/step (~3.5 min) |
| Z-Image-Turbo 1280x720, 9 steps | ~20 s/step (~3 min) |

Memory during 1 MP generation stays within the 16 GB budget (the 4-bit model is
~5.5 GB on disk; MLX caches are cleared between candidates). Times are wall-clock
including Metal kernel compilation on the first generation of a session; later
images in the same process are a touch faster.

## mflux Python API (what the scripts call)

Generation (`generate_image.py`):

```python
from mflux.models.common.config import ModelConfig
from mflux.models.z_image.variants.z_image import ZImage

model = ZImage(
    model_config=ModelConfig.z_image_turbo(),
    model_path="filipstrand/Z-Image-Turbo-mflux-4bit",  # pre-quantized 4-bit
)
img = model.generate_image(
    seed=42, prompt="...", width=1024, height=1024, num_inference_steps=9,
)
img.save(path="out.png", overwrite=True)
```

FLUX.2 uses `Flux2Klein(model_config=ModelConfig.flux2_klein_4b(), quantize=8)`
and passes `guidance=1.0` (no `negative_prompt`).

Upscaling (`upscale_image.py`):

```python
from mflux.models.common.config import ModelConfig
from mflux.models.seedvr2 import SeedVR2
from mflux.utils.scale_factor import ScaleFactor

model = SeedVR2(model_config=ModelConfig.seedvr2_3b())
img = model.generate_image(
    seed=42, image_path="in.png", resolution=ScaleFactor.parse("2x"), softness=0.5,
)
img.save(path="out.png", overwrite=True)
```

`resolution` is either a `ScaleFactor` (`"2x"`/`"3x"`) or an int (target for the
shortest side, aspect preserved).

## Quantization trade-offs

| Setting | Effect |
|---------|--------|
| Pre-quantized 4-bit (default for Z-Image-Turbo) | Smallest download (~5.5 GB) and memory; excellent quality for this model. |
| `--quantize 8` | Higher fidelity, more memory/disk. For Z-Image-Turbo this loads the full ~31 GB repo and quantizes on the fly (large first download). |
| `--quantize 4/6` | Progressively smaller/faster, slightly lower fidelity. |
| FLUX.2 default `--quantize 8` | Keeps the 4B model within 16 GB; drop to 4 if memory is tight. |

The script auto-snaps width/height to multiples of 16 (the VAE downsample factor).

## Rejected alternatives

| Option | Why not |
|--------|---------|
| **Draw Things (app)** | No official programmatic API (no first-party HTTP/gRPC server); it is a GUI. mflux gives the same "local model, load-and-run" approach as a real Python API. |
| **diffusers + PyTorch MPS** | Works, but slower and more memory-hungry than native MLX on Apple Silicon, and Z-Image needs diffusers-from-source. |
| **FLUX.1-dev / FLUX.2-dev / FLUX.2-klein-9B** | Non-commercial licenses. Excluded in favor of Apache-2.0 engines. FLUX.1-schnell is Apache-2.0 but now a legacy option next to FLUX.2. |
| **Qwen-Image (20B)** | Too large for a comfortable 16 GB experience (heavy swap, slow). |
| **SD3.5 / SDXL** | SD3.5 expects more VRAM for smooth use; SDXL-tier realism trails Z-Image/FLUX.2. |
| **Cloud APIs (Midjourney, DALL-E, fal, Replicate)** | Not local - violate the offline requirement. |

## Storage & privacy

- Data root: `~/.image-gen/` (`.venv` + `out/`). Override with `IMAGE_GEN_HOME`.
  Nothing is written into the repo.
- Weights cache under `~/.cache/huggingface/hub` (shared with other HF tools).
  Anonymous download; delete those model folders to reclaim space.
- Prompts and images are local only. Never upload them.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `uv not found` | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, then re-run setup. |
| `No Metal device available` on import | You are in a headless/sandboxed session. Run from a normal desktop session with GPU access. |
| First generation is slow / stalls | It is downloading ~5.5 GB of weights (one time). Subsequent runs skip this. |
| Out of memory (generation) | Stick to ~1 MP; use the default 4-bit Z-Image-Turbo; close other apps; generate one candidate at a time (the script already runs sequentially and clears caches). |
| Out of memory (upscale) | Add `--low-ram` and/or `--quantize 8`, or use a smaller `--resolution`. The 7B upscaler is not recommended on 16 GB. |
| Image looks soft at high resolution | Generate near 1 MP and upscale with `upscale_image.py` instead of rendering huge directly. |
| Negative prompt / guidance "does nothing" | Z-Image-Turbo has no CFG. Use `--model flux2-klein-4b` with `--guidance` if you need it. |
| Not on Apple Silicon | This skill is MLX-only. Use a Mac with an Apple GPU. |

## Sources

- mflux (runtime, model support, CLI/API): https://github.com/filipstrand/mflux
- Z-Image-Turbo (Apache-2.0, 6B S3-DiT): https://huggingface.co/Tongyi-MAI/Z-Image-Turbo
- Z-Image-Turbo 4-bit mflux weights: https://huggingface.co/filipstrand/Z-Image-Turbo-mflux-4bit
- FLUX.2-klein-4B (Apache-2.0): https://huggingface.co/black-forest-labs/FLUX.2-klein-4B
- SeedVR2 (Apache-2.0): https://huggingface.co/ByteDance-Seed/SeedVR2-3B
