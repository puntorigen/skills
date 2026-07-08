#!/usr/bin/env python3
"""Generate sound effects / ambience locally with Stable Audio Open Small (MLX).

Drives the `mlx-audiogen` CLI (Apple Silicon / MLX) to synthesize a short stereo
clip from a text prompt, then encodes it to MP3 (or keeps WAV) with ffmpeg.
Stable Audio Open Small is tuned for SOUND EFFECTS and field recordings (up to
~11s), which is exactly what foley/ambience cues need.

Run this with the sound-effects venv python:
  ~/.sound-effects/.venv/bin/python generate_sfx.py "rain on a tin roof" --duration 8 --output rain.mp3

Usage:
  generate_sfx.py "<prompt>" [--duration 8] [--count 1] [--steps 8]
                  [--cfg-scale 6.0] [--sampler euler|rk4] [--negative-prompt ""]
                  [--seed N] [--format mp3|wav] [--output out.mp3] [--mp3-quality 2]

Prints one output path per line on stdout.
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

# Stable Audio Open SMALL renders variable-length audio up to ~11s. We clamp to
# this so the model does not silently truncate or degrade; longer beds are made
# by looping a shorter clip in the mixer (see the audio-theater skill).
MAX_SECONDS = 11.0


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)
    sys.stderr.flush()


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def sfx_home() -> str:
    return os.environ.get("SOUND_EFFECTS_HOME", os.path.expanduser("~/.sound-effects"))


def resolve_cli() -> str:
    """Find the mlx-audiogen console script (same venv as this python)."""
    cand = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "mlx-audiogen")
    if os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    found = shutil.which("mlx-audiogen")
    if found:
        return found
    raise RuntimeError(
        "mlx-audiogen not found. Run setup_env.sh and use the sound-effects venv python."
    )


def resolve_weights_dir(cli_arg: str | None) -> str | None:
    """Return a local pre-converted weights dir to run fully offline, or None.

    Precedence: --weights-dir, then $SFX_WEIGHTS_DIR, then the default
    ~/.sound-effects/weights/mlx-stable-audio (populated by setup_env.sh). A dir
    only counts if it looks populated (has config.json). When None, mlx-audiogen
    auto-downloads the PUBLIC weights (jasonvassallo/mlx-stable-audio) - still no
    Hugging Face account or token required.
    """
    candidates = [
        cli_arg,
        os.environ.get("SFX_WEIGHTS_DIR"),
        os.path.join(sfx_home(), "weights", "mlx-stable-audio"),
    ]
    for d in candidates:
        if d and os.path.isfile(os.path.join(os.path.expanduser(d), "config.json")):
            return os.path.expanduser(d)
    return None


def encode_mp3(wav_path: str, mp3_path: str, quality: int = 2) -> str:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH (needed for MP3 encoding)")
    os.makedirs(os.path.dirname(os.path.abspath(mp3_path)) or ".", exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
         "-vn", "-c:a", "libmp3lame", "-q:a", str(quality), mp3_path],
        check=True,
    )
    return mp3_path


def out_paths(out_arg: str | None, count: int, fmt: str) -> list[str]:
    if not out_arg:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.join(sfx_home(), "out", f"sfx-{ts}.{fmt}")
    else:
        base = os.path.abspath(out_arg)
    if count == 1:
        return [base]
    root, ext = os.path.splitext(base)
    ext = ext or f".{fmt}"
    return [f"{root}-{i + 1}{ext}" for i in range(count)]


def generate_one(cli: str, prompt: str, seconds: float, steps: int, cfg_scale: float,
                 sampler: str, negative_prompt: str, seed: int | None,
                 dest: str, mp3_quality: int, weights_dir: str | None = None) -> bool:
    """Generate a single clip to `dest` (mp3 or wav)."""
    want_mp3 = dest.lower().endswith(".mp3")
    tmp_wav = None
    if want_mp3:
        fd, tmp_wav = tempfile.mkstemp(suffix=".wav", prefix="sfx-")
        os.close(fd)
        wav_target = tmp_wav
    else:
        wav_target = dest
        os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    cmd = [
        cli, "--model", "stable_audio",
        "--prompt", prompt,
        "--seconds", f"{seconds:.2f}",
        "--steps", str(steps),
        "--cfg-scale", f"{cfg_scale:g}",
        "--sampler", sampler,
        "--output", wav_target,
    ]
    if weights_dir:
        cmd += ["--weights-dir", weights_dir]
    if negative_prompt:
        cmd += ["--negative-prompt", negative_prompt]
    if seed is not None:
        cmd += ["--seed", str(seed)]

    eprint(f"[sfx] generating {seconds:.1f}s: \"{prompt[:60]}\"")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        for line in (result.stderr or "").strip().split("\n")[-8:]:
            eprint(f"    {line}")
        if tmp_wav and os.path.exists(tmp_wav):
            os.unlink(tmp_wav)
        return False

    if not os.path.isfile(wav_target):
        eprint(f"[sfx] expected output not found at {wav_target}")
        if tmp_wav and os.path.exists(tmp_wav):
            os.unlink(tmp_wav)
        return False

    if want_mp3:
        try:
            encode_mp3(wav_target, dest, quality=mp3_quality)
        except Exception as e:  # noqa: BLE001
            eprint(f"[sfx] mp3 encode failed: {e}")
            return False
        finally:
            if tmp_wav and os.path.exists(tmp_wav):
                os.unlink(tmp_wav)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Local sound-effects / ambience via Stable Audio Open Small (MLX).")
    ap.add_argument("prompt", nargs="?", help="Sound description (e.g. 'heavy wooden door slamming shut').")
    ap.add_argument("--prompt", dest="prompt_opt", help="Alternative way to pass the prompt.")
    ap.add_argument("--duration", "-d", type=float, default=8.0, help=f"Seconds (1-{MAX_SECONDS:g}).")
    ap.add_argument("--count", type=int, default=1, help="Number of variations to generate.")
    ap.add_argument("--steps", type=int, default=8, help="Diffusion steps (8-30; 8 is the fast default).")
    ap.add_argument("--cfg-scale", type=float, default=6.0, help="Classifier-free guidance scale.")
    ap.add_argument("--sampler", default="euler", choices=["euler", "rk4"], help="ODE sampler.")
    ap.add_argument("--negative-prompt", default="", help="What to avoid.")
    ap.add_argument("--seed", type=int, default=None, help="Fix for reproducibility (candidate i uses seed+i).")
    ap.add_argument("--format", "-f", default="mp3", choices=["mp3", "wav"], help="Output format.")
    ap.add_argument("--output", "-o", default=None, help="Output path (or prefix when --count > 1).")
    ap.add_argument("--mp3-quality", type=int, default=2, help="ffmpeg libmp3lame -q:a (0=best..9=smallest).")
    ap.add_argument("--weights-dir", default=None,
                    help="Local pre-converted MLX weights dir (offline). Defaults to "
                         "$SFX_WEIGHTS_DIR or ~/.sound-effects/weights/mlx-stable-audio; "
                         "if absent, mlx-audiogen auto-downloads the public weights.")
    # Accepted for orchestrator interop; not used by this backend.
    ap.add_argument("--category", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--no-trim", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    prompt = args.prompt_opt or args.prompt
    if not prompt:
        ap.error("provide a prompt (positional or --prompt)")

    if not is_apple_silicon():
        eprint("[sfx] WARNING: mlx-audiogen needs Apple Silicon (Metal); this may fail on this platform.")

    seconds = max(1.0, min(MAX_SECONDS, float(args.duration)))
    if float(args.duration) > MAX_SECONDS:
        eprint(f"[sfx] note: capped duration to {MAX_SECONDS:g}s (Stable Audio Open Small limit); "
               "loop it in the mixer for longer beds.")
    count = max(1, args.count)

    try:
        cli = resolve_cli()
    except RuntimeError as e:
        eprint(f"[sfx] {e}")
        return 1

    weights_dir = resolve_weights_dir(args.weights_dir)
    if weights_dir:
        eprint(f"[sfx] using local weights: {weights_dir}")
    else:
        eprint("[sfx] no local weights dir; mlx-audiogen will auto-download the "
               "public weights (no HF account needed).")

    targets = out_paths(args.output, count, args.format)
    written = []
    for i, dest in enumerate(targets):
        seed = (args.seed + i) if args.seed is not None else None
        ok = generate_one(cli, prompt, seconds, args.steps, args.cfg_scale,
                          args.sampler, args.negative_prompt, seed, dest, args.mp3_quality,
                          weights_dir=weights_dir)
        if ok:
            written.append(dest)
            eprint(f"[sfx]   -> {dest}")

    if not written:
        eprint("[sfx] nothing was written.")
        return 1
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
