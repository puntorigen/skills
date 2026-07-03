#!/usr/bin/env python3
"""Generate an MP3 narration in a cloned/designed voice, 100% locally.

Splits the script into sentence-sized chunks, synthesizes each with the SAME
reference voice and settings (so the voice stays consistent), concatenates them,
and encodes a single MP3 with ffmpeg.

Backends (auto-detected):
  - mlx  : mlx-audio + Chatterbox (Apple Silicon, fast)
  - torch: chatterbox-tts (PyTorch; CUDA/CPU) elsewhere

Usage:
  narrate.py --voice <name|path> (--text "..." | --text-file f.txt)
             [--lang en] [--exaggeration 0.5] [--cfg-weight 0.5]
             [--out out.mp3] [--model multilingual|turbo|<repo>]
             [--max-chars 280] [--mp3-quality 2] [--keep-wav]
             [--backend auto|mlx|torch]

Prints the output mp3 path on the last stdout line.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _audio import (  # noqa: E402
    audio_duration_s,
    concat_with_gaps,
    encode_mp3,
    eprint,
    save_wav,
    to_mono_f32,
)

SUPPORTED_LANGS = {
    "ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it", "ja",
    "ko", "ms", "nl", "no", "pl", "pt", "ru", "sv", "sw", "tr", "zh",
}

MLX_MODELS = {
    "multilingual": "mlx-community/chatterbox-fp16",
    "turbo": "mlx-community/chatterbox-turbo-fp16",
}


def _vc_home() -> str:
    return os.environ.get("VOICE_CLONE_HOME", os.path.expanduser("~/.voice-clone-narration"))


def resolve_voice(voice: str) -> str:
    """Accept a saved voice name or a path to a wav; return an existing path."""
    if os.path.isfile(voice):
        return os.path.abspath(voice)
    cand = os.path.join(_vc_home(), "voices", voice if voice.endswith(".wav") else f"{voice}.wav")
    if os.path.isfile(cand):
        return cand
    raise FileNotFoundError(
        f"voice '{voice}' not found (looked for a file and {cand}). "
        "Prep a reference with prep_reference.sh or design one with design_voice.py."
    )


def _hard_split(s: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    while len(s) > max_chars:
        cut = s.rfind(",", 0, max_chars)
        if cut < max_chars * 0.5:
            cut = s.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars - 1
        parts.append(s[: cut + 1].strip())
        s = s[cut + 1:].strip()
    if s:
        parts.append(s)
    return parts


def split_into_chunks(text: str, max_chars: int = 280) -> list[str]:
    """Sentence-aware chunking that keeps chunks <= ~max_chars."""
    text = " ".join(text.split())
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?\u2026])\s+", text)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(s) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_hard_split(s, max_chars))
            continue
        if buf and len(buf) + len(s) + 1 <= max_chars:
            buf = f"{buf} {s}"
        elif not buf:
            buf = s
        else:
            chunks.append(buf)
            buf = s
    if buf:
        chunks.append(buf)
    return chunks


def pick_backend(requested: str) -> str:
    if requested in ("mlx", "torch"):
        return requested
    if importlib.util.find_spec("mlx_audio") is not None:
        return "mlx"
    if importlib.util.find_spec("chatterbox") is not None:
        return "torch"
    raise RuntimeError("no TTS backend installed - run setup_env.sh first")


def generate_mlx(model_arg, voice_path, chunks, lang, exag, cfg):
    from mlx_audio.tts.utils import load_model

    model_id = MLX_MODELS.get(model_arg, model_arg)
    eprint(f"[narrate] backend=mlx model={model_id} (first run downloads weights)")
    model = load_model(model_id)
    sr = int(getattr(model, "sample_rate", 0) or getattr(model, "sr", 0) or 24000)

    # Precompute speaker conditioning once so every chunk uses an identical voice.
    conds = None
    prep = getattr(model, "prepare_conditionals", None)
    if callable(prep):
        try:
            conds = prep(voice_path, sr, exaggeration=exag)
        except TypeError:
            try:
                conds = prep(voice_path, sr)
            except Exception as e:  # noqa: BLE001
                eprint(f"[narrate] (conditionals reuse unavailable: {e}); using per-chunk reference")
                conds = None
        except Exception as e:  # noqa: BLE001
            eprint(f"[narrate] (conditionals reuse unavailable: {e}); using per-chunk reference")
            conds = None

    parts = []
    for i, ch in enumerate(chunks, 1):
        eprint(f"[narrate]   chunk {i}/{len(chunks)} ({len(ch)} chars)")
        kw = dict(exaggeration=exag, cfg_weight=cfg, lang_code=lang, verbose=False)
        if conds is not None:
            it = model.generate(text=ch, conds=conds, **kw)
        else:
            it = model.generate(text=ch, ref_audio=voice_path, **kw)
        for r in it:
            parts.append(to_mono_f32(getattr(r, "audio", r)))
    return parts, sr


def generate_torch(model_arg, voice_path, chunks, lang, exag, cfg):
    import torch

    if torch.cuda.is_available():
        device = "cuda"
    elif getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
    eprint(f"[narrate] backend=torch device={device} model={model_arg}")

    if model_arg == "turbo":
        from chatterbox.tts_turbo import ChatterboxTurboTTS

        model = ChatterboxTurboTTS.from_pretrained(device=device)

        def synth(ch):
            return model.generate(ch, audio_prompt_path=voice_path)
    else:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        model = ChatterboxMultilingualTTS.from_pretrained(device=device)

        def synth(ch):
            return model.generate(
                ch, language_id=lang, audio_prompt_path=voice_path,
                exaggeration=exag, cfg_weight=cfg,
            )

    sr = int(getattr(model, "sr", 0) or getattr(model, "sample_rate", 0) or 24000)
    parts = []
    for i, ch in enumerate(chunks, 1):
        eprint(f"[narrate]   chunk {i}/{len(chunks)} ({len(ch)} chars)")
        parts.append(to_mono_f32(synth(ch)))
    return parts, sr


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a local MP3 narration in a cloned voice.")
    ap.add_argument("--voice", required=True, help="Saved voice name or path to a reference wav.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="Script text to narrate.")
    g.add_argument("--text-file", help="Path to a file containing the script.")
    ap.add_argument("--lang", default="en", help="Language code (en, es, ... 23 supported).")
    ap.add_argument("--exaggeration", type=float, default=0.5, help="Emotional intensity (0-1).")
    ap.add_argument("--cfg-weight", type=float, default=0.5, help="Cadence adherence; lower=slower; 0 avoids accent transfer.")
    ap.add_argument("--out", default=None, help="Output mp3 path.")
    ap.add_argument("--model", default="multilingual", help="multilingual | turbo | <hf-repo-id>.")
    ap.add_argument("--max-chars", type=int, default=280, help="Target max characters per chunk.")
    ap.add_argument("--mp3-quality", type=int, default=2, help="ffmpeg libmp3lame -q:a (0=best..9=smallest).")
    ap.add_argument("--keep-wav", action="store_true", help="Keep the intermediate wav next to the mp3.")
    ap.add_argument("--backend", default="auto", choices=["auto", "mlx", "torch"], help="TTS backend.")
    args = ap.parse_args()

    lang = args.lang.lower().strip()
    if lang not in SUPPORTED_LANGS:
        eprint(f"[narrate] WARNING: '{lang}' is not in the supported set; proceeding anyway.")

    if args.model == "turbo" and lang != "en":
        eprint("[narrate] NOTE: 'turbo' is English-only; forcing --lang en.")
        lang = "en"

    try:
        voice_path = resolve_voice(args.voice)
    except FileNotFoundError as e:
        eprint(f"[narrate] {e}")
        return 2

    text = args.text if args.text is not None else open(args.text_file, encoding="utf-8").read()
    chunks = split_into_chunks(text, args.max_chars)
    if not chunks:
        eprint("[narrate] no text to narrate.")
        return 2
    eprint(f"[narrate] voice={os.path.basename(voice_path)} lang={lang} "
           f"exaggeration={args.exaggeration} cfg_weight={args.cfg_weight} chunks={len(chunks)}")

    try:
        backend = pick_backend(args.backend)
    except RuntimeError as e:
        eprint(f"[narrate] {e}")
        return 1

    try:
        if backend == "mlx":
            parts, sr = generate_mlx(args.model, voice_path, chunks, lang, args.exaggeration, args.cfg_weight)
        else:
            parts, sr = generate_torch(args.model, voice_path, chunks, lang, args.exaggeration, args.cfg_weight)
    except Exception as e:  # noqa: BLE001
        eprint(f"[narrate] generation failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

    audio = concat_with_gaps(parts, sr, gap_s=0.15)
    if len(audio) == 0:
        eprint("[narrate] no audio produced.")
        return 1

    out = args.out
    if not out:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = os.path.join(_vc_home(), "out", f"narration-{ts}.mp3")
    out = os.path.abspath(out)

    # Write wav (either kept next to the mp3, or a temp we clean up).
    if args.keep_wav:
        wav_path = os.path.splitext(out)[0] + ".wav"
        save_wav(wav_path, audio, sr)
        tmp = False
    else:
        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        save_wav(wav_path, audio, sr)
        tmp = True

    try:
        encode_mp3(wav_path, out, quality=args.mp3_quality)
    except Exception as e:  # noqa: BLE001
        eprint(f"[narrate] mp3 encode failed: {e}")
        return 1
    finally:
        if tmp:
            try:
                os.remove(wav_path)
            except OSError:
                pass

    dur = audio_duration_s(audio, sr)
    eprint(f"[narrate] done: {dur:.1f}s narration")
    eprint(f"[narrate]   mp3 : {out}")
    if args.keep_wav:
        eprint(f"[narrate]   wav : {wav_path}")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
