#!/usr/bin/env python3
"""Batch voice synthesis for audio-theater, run under the voice-clone-narration venv.

generate_voices.py invokes this with the sibling skill's venv python:
    ~/.voice-clone-narration/venv/bin/python _vc_batch.py \
        --manifest lines.manifest.json --vc-scripts <voice-clone-narration/scripts>

It imports the installed voice-clone-narration internals (narrate.py + _audio.py),
loads the Chatterbox model ONCE, and synthesizes every line clip - avoiding the
per-line model reload that narrate.py would otherwise incur. Speaker conditioning
is cached per (voice, exaggeration) so a character's voice stays consistent.

Manifest schema:
{
  "backend": "auto" | "mlx" | "torch",
  "model": "multilingual" | "turbo" | "<hf-repo>",
  "max_chars": 280,
  "items": [
    {"index": 0, "voice": "at-demo-marco", "text": "...", "lang": "es",
     "exaggeration": 0.5, "cfg_weight": 0.5, "out_wav": "/abs/lines/line-000.wav"}
  ]
}

Prints a JSON results object: {"results": [{"index", "file", "duration", "ok"}]}.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _seed_rng(backend: str, seed: int) -> None:
    """Seed all RNGs that affect Chatterbox sampling for reproducible takes."""
    try:
        import random as _random
        _random.seed(seed)
        import numpy as _np
        _np.random.seed(seed % (2 ** 32))
    except Exception:  # noqa: BLE001
        pass
    try:
        if backend == "mlx":
            import mlx.core as mx
            mx.random.seed(seed)
        else:
            import torch
            torch.manual_seed(seed)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch Chatterbox synthesis (model loaded once).")
    ap.add_argument("--manifest", required=True, help="Path to the JSON manifest.")
    ap.add_argument("--vc-scripts", required=True, help="voice-clone-narration/scripts dir.")
    ap.add_argument("--results", default=None, help="Optional path to write results JSON.")
    args = ap.parse_args()

    vc_scripts = os.path.abspath(args.vc_scripts)
    sys.path.insert(0, vc_scripts)
    try:
        import narrate  # noqa: E402  (brings in _audio helpers into its namespace)
    except Exception as e:  # noqa: BLE001
        print(f"[vc-batch] could not import voice-clone-narration internals from "
              f"{vc_scripts}: {e}", file=sys.stderr)
        return 1

    with open(args.manifest, encoding="utf-8") as f:
        manifest = json.load(f)

    model_arg = manifest.get("model", "multilingual")
    max_chars = int(manifest.get("max_chars", 280))
    items = manifest.get("items", [])

    try:
        backend = narrate.pick_backend(manifest.get("backend", "auto"))
    except RuntimeError as e:
        print(f"[vc-batch] {e}", file=sys.stderr)
        return 1
    print(f"[vc-batch] backend={backend} model={model_arg} lines={len(items)}", file=sys.stderr)

    # ── Load the model ONCE ────────────────────────────────────────────────
    model = None
    sr = 24000
    conds_cache: dict = {}

    if backend == "mlx":
        from mlx_audio.tts.utils import load_model
        model_id = narrate.MLX_MODELS.get(model_arg, model_arg)
        print(f"[vc-batch] loading MLX model {model_id} (first run downloads weights)",
              file=sys.stderr)
        model = load_model(model_id)
        sr = int(getattr(model, "sample_rate", 0) or getattr(model, "sr", 0) or 24000)
    else:
        import torch
        if torch.cuda.is_available():
            device = "cuda"
        elif getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
        print(f"[vc-batch] loading torch model on {device}", file=sys.stderr)
        if model_arg == "turbo":
            from chatterbox.tts_turbo import ChatterboxTurboTTS
            model = ChatterboxTurboTTS.from_pretrained(device=device)
        else:
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS
            model = ChatterboxMultilingualTTS.from_pretrained(device=device)
        sr = int(getattr(model, "sr", 0) or getattr(model, "sample_rate", 0) or 24000)

    def mlx_conds(voice_path, exag):
        """Compute (and cache) speaker conditionals for reuse across a voice's lines."""
        key = (voice_path, round(float(exag), 3))
        if key in conds_cache:
            return conds_cache[key]
        conds = None
        prep = getattr(model, "prepare_conditionals", None)
        if callable(prep):
            try:
                conds = prep(voice_path, sr, exaggeration=exag)
            except TypeError:
                try:
                    conds = prep(voice_path, sr)
                except Exception:  # noqa: BLE001
                    conds = None
            except Exception:  # noqa: BLE001
                conds = None
        conds_cache[key] = conds
        return conds

    def synth_item(voice_path, text, lang, exag, cfg):
        chunks = narrate.split_into_chunks(text, max_chars)
        parts = []
        if backend == "mlx":
            conds = mlx_conds(voice_path, exag)
            kw = dict(exaggeration=exag, cfg_weight=cfg, lang_code=lang, verbose=False)
            for ch in chunks:
                if conds is not None:
                    it = model.generate(text=ch, conds=conds, **kw)
                else:
                    it = model.generate(text=ch, ref_audio=voice_path, **kw)
                for r in it:
                    parts.append(narrate.to_mono_f32(getattr(r, "audio", r)))
        else:
            for ch in chunks:
                if model_arg == "turbo":
                    out = model.generate(ch, audio_prompt_path=voice_path)
                else:
                    out = model.generate(ch, language_id=lang, audio_prompt_path=voice_path,
                                         exaggeration=exag, cfg_weight=cfg)
                parts.append(narrate.to_mono_f32(out))
        return narrate.concat_with_gaps(parts, sr, gap_s=0.05)

    results = []
    for it in items:
        idx = it.get("index")
        out_wav = it.get("out_wav")
        text = (it.get("text") or "").strip()
        if not text or not out_wav:
            results.append({"index": idx, "file": out_wav, "duration": 0.0, "ok": False})
            continue
        try:
            voice_path = narrate.resolve_voice(it["voice"])
        except Exception as e:  # noqa: BLE001
            print(f"[vc-batch] line {idx}: voice '{it.get('voice')}' not found: {e}",
                  file=sys.stderr)
            results.append({"index": idx, "file": out_wav, "duration": 0.0, "ok": False})
            continue

        lang = (it.get("lang") or "en").lower()
        exag = float(it.get("exaggeration", 0.5))
        cfg = float(it.get("cfg_weight", 0.5))
        seed = it.get("seed")
        seed_note = f" seed={seed}" if seed is not None else ""
        print(f"[vc-batch] line {idx:03d} [{it.get('voice')}] "
              f"exag={exag} cfg={cfg}{seed_note}: {text[:48]}", file=sys.stderr)
        try:
            if seed is not None:
                _seed_rng(backend, int(seed))
            audio = synth_item(voice_path, text, lang, exag, cfg)
            if audio is None or len(audio) == 0:
                raise RuntimeError("no audio produced")
            narrate.save_wav(out_wav, audio, sr)
            dur = narrate.audio_duration_s(audio, sr)
            results.append({"index": idx, "file": out_wav, "duration": round(dur, 3), "ok": True})
        except Exception as e:  # noqa: BLE001
            print(f"[vc-batch] line {idx}: synthesis failed: {e}", file=sys.stderr)
            results.append({"index": idx, "file": out_wav, "duration": 0.0, "ok": False})

    payload = {"backend": backend, "sample_rate": sr, "results": results}
    if args.results:
        with open(args.results, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    print(json.dumps(payload, ensure_ascii=False))
    ok_count = sum(1 for r in results if r["ok"])
    print(f"[vc-batch] done: {ok_count}/{len(results)} lines synthesized.", file=sys.stderr)
    return 0 if ok_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
