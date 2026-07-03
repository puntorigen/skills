#!/usr/bin/env python3
"""Generate instrumental background music MP3s locally with ACE-Step 1.5.

Drives the documented ACE-Step API (AceStepHandler + LLMHandler + generate_music)
- see the repo's docs/en/INFERENCE.md and run_generate_test.py - forcing
instrumental output, then encodes each track to MP3 with ffmpeg.

Run this with the ACE-Step venv python:
  ~/.bg-music/ACE-Step-1.5/.venv/bin/python generate_music.py --prompt "..." ...

Usage:
  generate_music.py --prompt "<music description>" [--duration 60] [--bpm 90]
                    [--count 2] [--seed N] [--keyscale "A minor"] [--vocals]
                    [--steps 8] [--out out.mp3] [--mp3-quality 2] [--keep-wav]

Prints one output mp3 path per line on stdout.
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

# Silence tokenizer fork warnings and allow torch MPS ops to fall back to CPU.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
# The 2B model's peak sits near the MPS memory ceiling on 16GB Macs. Lifting the
# upper-limit watermark lets allocations spill into swap (slower) instead of
# hard-failing with "MPS backend out of memory". Override by exporting your own
# value before running.
os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.0")


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)
    sys.stderr.flush()


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def bg_home() -> str:
    return os.environ.get("BG_MUSIC_HOME", os.path.expanduser("~/.bg-music"))


def ace_dir() -> str:
    return os.path.join(bg_home(), "ACE-Step-1.5")


def encode_mp3(wav_path: str, mp3_path: str, quality: int = 2) -> str:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH (needed for MP3 encoding)")
    os.makedirs(os.path.dirname(os.path.abspath(mp3_path)), exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
         "-vn", "-c:a", "libmp3lame", "-q:a", str(quality), mp3_path],
        check=True,
    )
    return mp3_path


def out_paths(out_arg: str | None, count: int) -> list[str]:
    if not out_arg:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.join(bg_home(), "out", f"music-{ts}.mp3")
    else:
        base = os.path.abspath(out_arg)
    if count == 1:
        return [base]
    root, ext = os.path.splitext(base)
    ext = ext or ".mp3"
    return [f"{root}-{i + 1}{ext}" for i in range(count)]


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate local instrumental background music (ACE-Step 1.5).")
    ap.add_argument("--prompt", required=True, help="Music description: genre, mood, instruments, tempo feel.")
    ap.add_argument("--duration", type=float, default=60.0, help="Length in seconds (10-600).")
    ap.add_argument("--bpm", type=int, default=None, help="Tempo in BPM (30-300); omit for auto.")
    ap.add_argument("--count", type=int, default=2, help="Number of variations to generate.")
    ap.add_argument("--seed", type=int, default=None, help="Fix the seed for reproducibility.")
    ap.add_argument("--keyscale", default="", help='Musical key, e.g. "A minor" (default auto).')
    ap.add_argument("--vocals", action="store_true", help="Allow vocals (default: instrumental).")
    ap.add_argument("--steps", type=int, default=8, help="Diffusion steps (turbo default 8).")
    ap.add_argument("--out", default=None, help="Output mp3 path (or prefix when --count > 1).")
    ap.add_argument("--mp3-quality", type=int, default=2, help="ffmpeg libmp3lame -q:a (0=best..9=smallest).")
    ap.add_argument("--keep-wav", action="store_true", help="Keep the intermediate wav files.")
    ap.add_argument("--config", default="acestep-v15-turbo", help="DiT model config name.")
    ap.add_argument("--lm-model", default="acestep-5Hz-lm-0.6B", help="Language-model planner path.")
    args = ap.parse_args()

    duration = max(10.0, min(600.0, args.duration))
    count = max(1, args.count)

    ad = ace_dir()
    if not os.path.isdir(ad):
        eprint(f"[music] ACE-Step not found at {ad} - run setup_env.sh first.")
        return 1
    sys.path.insert(0, ad)
    # Run from inside the checkout so ACE-Step's relative runtime caches
    # (e.g. .cache/acestep/) land under ~/.bg-music, never in the repo or the
    # caller's working directory.
    try:
        os.chdir(ad)
    except OSError:
        pass
    checkpoint_dir = os.path.join(ad, "checkpoints")

    try:
        from acestep.handler import AceStepHandler
        from acestep.llm_inference import LLMHandler
        from acestep.inference import GenerationParams, GenerationConfig, generate_music
    except Exception as e:  # noqa: BLE001
        eprint(f"[music] failed to import ACE-Step ({e}).")
        eprint("[music] Make sure you run this with the ACE-Step venv python and that setup_env.sh completed.")
        return 1

    lm_backend = "mlx" if is_apple_silicon() else "pt"
    use_mlx_dit = is_apple_silicon()

    eprint(f"[music] initializing DiT ({args.config}, backend={'mlx' if use_mlx_dit else 'torch'})...")
    dit_handler = AceStepHandler()
    status, ok = dit_handler.initialize_service(
        project_root=ad,
        config_path=args.config,
        device="auto",
        offload_to_cpu=False,
        use_mlx_dit=use_mlx_dit,
    )
    if not ok:
        eprint(f"[music] DiT init failed: {status}")
        return 1

    # The DiT auto-downloads during initialize_service, but the planner LM does
    # not - fetch it explicitly (text2music needs the LM to produce audio codes).
    try:
        from acestep.model_downloader import ensure_lm_model
        eprint(f"[music] ensuring planner LM present ({args.lm_model})...")
        ok_dl, msg_dl = ensure_lm_model(model_name=args.lm_model, checkpoints_dir=checkpoint_dir)
        if not ok_dl:
            eprint(f"[music] LM download issue: {msg_dl}")
    except Exception as e:  # noqa: BLE001
        eprint(f"[music] could not pre-download LM ({e}); trying init anyway")

    eprint(f"[music] initializing planner LM ({args.lm_model}, backend={lm_backend})...")
    llm_handler = LLMHandler()
    status, ok = llm_handler.initialize(
        checkpoint_dir=checkpoint_dir,
        lm_model_path=args.lm_model,
        backend=lm_backend,
        device="auto",
        offload_to_cpu=False,
        dtype=None,
    )
    if not ok:
        eprint(f"[music] LM init failed: {status}")
        return 1

    targets = out_paths(args.out, count)
    save_dir = tempfile.mkdtemp(prefix="bgmusic-")

    # Generate candidates SEQUENTIALLY (batch_size=1). Batching multiplies peak
    # memory and OOMs on 16GB unified memory; one-at-a-time keeps it in budget.
    written = []
    for i in range(count):
        seed = (args.seed + i) if args.seed is not None else -1
        params = GenerationParams(
            task_type="text2music",
            thinking=True,
            caption=args.prompt[:512],
            lyrics="" if args.vocals else "[Instrumental]",
            instrumental=not args.vocals,
            bpm=args.bpm,
            keyscale=args.keyscale,
            duration=duration,
            inference_steps=args.steps,
            guidance_scale=1.0,  # turbo bakes guidance in; value is auto-corrected
            seed=seed,
        )
        config = GenerationConfig(batch_size=1, audio_format="wav")

        eprint(f"[music] generating track {i + 1}/{count} ({duration:.0f}s)...")
        result = generate_music(dit_handler, llm_handler, params=params, config=config, save_dir=save_dir)

        if not getattr(result, "success", False):
            eprint(f"[music] track {i + 1} failed: {getattr(result, 'status_message', '')} {getattr(result, 'error', '')}")
            continue
        audios = result.audios or []
        wav = audios[0].get("path") if audios else None
        if not wav or not os.path.isfile(wav):
            eprint(f"[music] track {i + 1}: no wav produced; skipping")
            continue

        mp3 = targets[i]
        try:
            encode_mp3(wav, mp3, quality=args.mp3_quality)
        except Exception as e:  # noqa: BLE001
            eprint(f"[music] track {i + 1}: mp3 encode failed: {e}")
            continue
        if args.keep_wav:
            try:
                shutil.copyfile(wav, os.path.splitext(mp3)[0] + ".wav")
            except OSError:
                pass
        written.append(mp3)
        eprint(f"[music]   track {i + 1}: {mp3}")

        # Free accelerator memory between takes (helps count>1 fit on 16GB).
        try:
            import gc
            gc.collect()
        except Exception:  # noqa: BLE001
            pass
        try:
            import torch
            if hasattr(torch, "mps") and torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:  # noqa: BLE001
            pass

    shutil.rmtree(save_dir, ignore_errors=True)

    if not written:
        eprint("[music] nothing was written.")
        return 1

    eprint(f"[music] done: {len(written)} track(s).")
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
