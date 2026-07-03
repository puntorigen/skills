#!/usr/bin/env python3
"""Design a brand-new voice from a text description (Apple Silicon / mlx-audio).

Uses Qwen3-TTS VoiceDesign to synthesize a short audition of the described voice,
then saves that audition as a normal reference in the voice library so all
narration can run through the same Chatterbox cloning pipeline
("design once, clone everywhere").

Usage:
  design_voice.py --name <voice-name> --describe "<voice description>"
                  [--audition-text "..."] [--language English]
                  [--model <hf-repo>] [--mp3-quality 2]

Outputs:
  ~/.voice-clone-narration/voices/<name>.wav      (reference for narrate.py)
  ~/.voice-clone-narration/out/<name>-audition.mp3 (play/link for approval)

Prints the reference wav path on the last stdout line.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _audio import (  # noqa: E402
    audio_duration_s,
    concat_with_gaps,
    encode_mp3,
    eprint,
    is_apple_silicon,
    save_wav,
    to_mono_f32,
)

DEFAULT_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16"
DEFAULT_AUDITION = (
    "Here's what this voice sounds like. Clear, natural, and ready to narrate "
    "your next story."
)


def _vc_home() -> str:
    return os.environ.get("VOICE_CLONE_HOME", os.path.expanduser("~/.voice-clone-narration"))


def _extract_audio_and_sr(results, model):
    """Normalize mlx-audio generate output into (samples, sample_rate)."""
    parts = []
    sr = None
    for r in results:
        audio = getattr(r, "audio", r)
        parts.append(to_mono_f32(audio))
        if sr is None:
            sr = getattr(r, "sample_rate", None)
    if sr is None:
        sr = getattr(model, "sample_rate", None) or getattr(model, "sr", None) or 24000
    return concat_with_gaps(parts, sr, gap_s=0.0), int(sr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Design a voice from a text description.")
    ap.add_argument("--name", required=True, help="Voice name to save in the library.")
    ap.add_argument("--describe", required=True,
                    help="Natural-language voice description (age, gender, pitch, pace, tone, accent).")
    ap.add_argument("--audition-text", default=DEFAULT_AUDITION,
                    help="Sentence the designed voice will speak for the audition.")
    ap.add_argument("--language", default="English",
                    help="Language of the audition text (Qwen3-TTS language name, e.g. English, Spanish).")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="HF repo id of a Qwen3-TTS VoiceDesign model.")
    ap.add_argument("--mp3-quality", type=int, default=2, help="ffmpeg libmp3lame -q:a (0=best..9=smallest).")
    args = ap.parse_args()

    if not is_apple_silicon():
        eprint("[design] Voice design requires Apple Silicon (mlx-audio / Qwen3-TTS VoiceDesign).")
        eprint("[design] On this platform, provide a recorded reference clip and use prep_reference.sh instead.")
        return 3

    try:
        from mlx_audio.tts.utils import load_model
    except Exception as e:  # noqa: BLE001
        eprint(f"[design] mlx-audio not available: {e}")
        eprint("[design] Run setup_env.sh first.")
        return 1

    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in args.name)
    vc_home = _vc_home()
    ref_wav = os.path.join(vc_home, "voices", f"{safe}.wav")
    audition_mp3 = os.path.join(vc_home, "out", f"{safe}-audition.mp3")

    eprint(f"[design] loading VoiceDesign model: {args.model} (first run downloads ~3.5GB)")
    model = load_model(args.model)

    eprint(f"[design] describing: {args.describe}")
    eprint(f"[design] audition ({args.language}): {args.audition_text}")

    gen = getattr(model, "generate_voice_design", None)
    try:
        if callable(gen):
            results = gen(text=args.audition_text, language=args.language, instruct=args.describe)
        else:
            # Fallback: some builds route voice design through the generic generate()
            results = model.generate(text=args.audition_text, language=args.language, instruct=args.describe)
        results = list(results)
    except Exception as e:  # noqa: BLE001
        eprint(f"[design] generation failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

    samples, sr = _extract_audio_and_sr(results, model)
    if samples is None or len(samples) == 0:
        eprint("[design] no audio was produced.")
        return 1

    save_wav(ref_wav, samples, sr)
    try:
        encode_mp3(ref_wav, audition_mp3, quality=args.mp3_quality)
    except Exception as e:  # noqa: BLE001
        eprint(f"[design] (audition mp3 encode skipped: {e})")
        audition_mp3 = ""

    dur = audio_duration_s(samples, sr)
    eprint(f"[design] saved voice '{safe}' ({dur:.1f}s @ {sr} Hz)")
    eprint(f"[design]   reference : {ref_wav}")
    if audition_mp3:
        eprint(f"[design]   audition  : {audition_mp3}  <- play this for the user to approve")
    eprint("[design] If they want changes, re-run with an adjusted --describe.")
    # stdout: the reference wav path, for narrate.py
    print(ref_wav)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
