#!/usr/bin/env python3
"""Generate a lip-synced talking-head MP4 from an avatar image + narration audio.

Drives JoyVASA (audio -> facial-motion diffusion -> LivePortrait renderer) fully
locally on Apple Silicon. Applies the runtime shims needed on macOS:
  - PYTORCH_ENABLE_MPS_FALLBACK=1 (route the few MPS-unsupported ops to CPU),
  - torch.load(weights_only=False) (load the trusted JoyVASA/LivePortrait
    checkpoints under torch >= 2.6's stricter default).

Run with the skill's venv python (see setup_env.sh):
  ~/.talking-head/.venv/bin/python animate.py --image face.png --audio vo.mp3 --out th.mp4

Usage:
  animate.py --image IMG --audio AUDIO [--out OUT.mp4] [--cfg-scale 2.0]
             [--animation-region all|lip|exp|pose|eyes] [--crop] [--force-cpu]

Prints the output mp4 path on the last stdout line.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import platform
import shutil
import subprocess
import sys
import tempfile

# Must be set before torch is imported (inside JoyVASA).
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)
    sys.stderr.flush()


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def th_home() -> str:
    return os.environ.get("TALKING_HEAD_HOME", os.path.expanduser("~/.talking-head"))


def repo_dir() -> str:
    return os.path.join(th_home(), "JoyVASA")


def to_wav_16k(audio_path: str, tmpdir: str) -> str:
    """Normalize any input audio (mp3/wav/m4a/...) to mono 16 kHz wav."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH")
    out = os.path.join(tmpdir, "narration_16k.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", audio_path,
         "-ar", "16000", "-ac", "1", out],
        check=True,
    )
    return out


def finalize_mp4(src_mp4: str, out_mp4: str):
    """Copy streams into a clean, web-friendly container (faststart)."""
    os.makedirs(os.path.dirname(os.path.abspath(out_mp4)), exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src_mp4,
             "-c", "copy", "-movflags", "+faststart", out_mp4],
            check=True,
        )
    except subprocess.CalledProcessError:
        shutil.copyfile(src_mp4, out_mp4)


def install_torch_load_shim():
    """torch >= 2.6 defaults weights_only=True, which rejects the JoyVASA motion
    generator checkpoint (it stores an argparse.Namespace). All weights here come
    from the model repos we downloaded, so loading them fully is safe."""
    import torch

    _orig = torch.load

    def _load(*a, **k):
        k.setdefault("weights_only", False)
        return _orig(*a, **k)

    torch.load = _load


def main() -> int:
    ap = argparse.ArgumentParser(description="Local lip-synced talking-head video (JoyVASA + LivePortrait).")
    ap.add_argument("--image", required=True, help="Avatar/portrait image (front-facing, mouth closed).")
    ap.add_argument("--audio", required=True, help="Narration audio (mp3/wav/m4a). Converted to 16 kHz mono.")
    ap.add_argument("--out", default=None, help="Output mp4 path (default ~/.talking-head/out/talkinghead-<ts>.mp4).")
    ap.add_argument("--cfg-scale", type=float, default=2.0, help="Motion guidance; higher = more expressive (2-3).")
    ap.add_argument("--animation-region", default="all", choices=["all", "lip", "exp", "pose", "eyes"],
                    help="Which regions to animate (default all).")
    ap.add_argument("--crop", action="store_true",
                    help="Detect and crop to the face before animating (use if the face is small in frame).")
    ap.add_argument("--force-cpu", action="store_true", help="Disable MPS and run on CPU (slower, most compatible).")
    args = ap.parse_args()

    image = os.path.abspath(os.path.expanduser(args.image))
    audio = os.path.abspath(os.path.expanduser(args.audio))
    if not os.path.isfile(image):
        eprint(f"[talking-head] image not found: {image}")
        return 2
    if not os.path.isfile(audio):
        eprint(f"[talking-head] audio not found: {audio}")
        return 2

    rd = repo_dir()
    if not os.path.isdir(rd):
        eprint(f"[talking-head] JoyVASA not found at {rd} - run setup_env.sh first.")
        return 1

    if not is_apple_silicon() and not args.force_cpu:
        eprint("[talking-head] WARNING: not on Apple Silicon; MPS unavailable. Consider --force-cpu.")

    if args.out:
        out_mp4 = os.path.abspath(os.path.expanduser(args.out))
    else:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_mp4 = os.path.join(th_home(), "out", f"talkinghead-{ts}.mp4")

    # Run from inside the checkout: JoyVASA resolves weights via paths relative to
    # its own package, and this keeps any runtime caches under ~/.talking-head.
    sys.path.insert(0, rd)
    try:
        os.chdir(rd)
    except OSError:
        pass

    install_torch_load_shim()

    try:
        from src.config.argument_config import ArgumentConfig
        from src.config.inference_config import InferenceConfig
        from src.config.crop_config import CropConfig
        from src.live_portrait_wmg_pipeline import LivePortraitPipeline
    except Exception as e:  # noqa: BLE001
        eprint(f"[talking-head] failed to import JoyVASA ({e}).")
        eprint("[talking-head] Make sure setup_env.sh completed with the skill's venv python.")
        return 1

    def partial_fields(target_class, kwargs):
        return target_class(**{k: v for k, v in kwargs.items() if hasattr(target_class, k)})

    tmpdir = tempfile.mkdtemp(prefix="talkinghead-")
    try:
        wav = to_wav_16k(audio, tmpdir)

        arg = ArgumentConfig()
        arg.animation_mode = "human"
        arg.reference = image
        arg.audio = wav
        arg.output_dir = tmpdir
        arg.cfg_scale = args.cfg_scale
        arg.animation_region = args.animation_region
        arg.flag_force_cpu = bool(args.force_cpu)
        if args.crop:
            # All three flags are required for JoyVASA to animate the face crop
            # and paste it back onto the (static) original frame - this keeps the
            # background/corners perfectly still instead of warping the whole image.
            arg.flag_do_crop = True
            arg.flag_pasteback = True
            arg.flag_stitching = True

        inference_cfg = partial_fields(InferenceConfig, arg.__dict__)
        crop_cfg = partial_fields(CropConfig, arg.__dict__)

        eprint(f"[talking-head] animating {os.path.basename(image)} with {os.path.basename(audio)} "
               f"(device={'cpu' if args.force_cpu else 'mps'}, cfg_scale={args.cfg_scale}, region={args.animation_region})...")

        pipeline = LivePortraitPipeline(inference_cfg=inference_cfg, crop_cfg=crop_cfg)
        produced = pipeline.execute(arg)

        if not produced or not os.path.isfile(produced):
            eprint("[talking-head] pipeline did not produce a video.")
            return 1

        finalize_mp4(produced, out_mp4)
    except Exception as e:  # noqa: BLE001
        eprint(f"[talking-head] generation failed: {e}")
        if not args.force_cpu:
            eprint("[talking-head] If this looks like an MPS op error, retry with --force-cpu.")
        return 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    eprint(f"[talking-head] done: {out_mp4}")
    print(out_mp4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
