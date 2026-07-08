#!/usr/bin/env python3
"""Generate one clean local TTS clip per line, concatenate into dialogue.wav.

Backend: the sibling **voice-clone-narration** skill (Chatterbox via MLX on Apple
Silicon, or PyTorch elsewhere). Each character speaks in its own cloned/designed
voice (set by setup_cast.py). Line delivery tags map to Chatterbox
exaggeration / cfg-weight presets (Chatterbox has no inline audio tags).

For speed, all lines are synthesized in a SINGLE subprocess (_vc_batch.py, run
with the voice-clone-narration venv python) that loads the model once. Each clip
is then padded with its pause_after and concatenated into dialogue.wav, with
exact per-line timing written to lines.json (the clean clips double as lipsync
reference for the talking-head skill).

Usage:
    python3 generate_voices.py --script audio-theater/ep/script.json --out audio-theater/ep
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from _common import (  # noqa: E402
    load_config, load_json, save_json, resolve_out_dir, get_audio_duration,
    run_ffmpeg, sibling_venv_python, sibling_script, MAX_CLIP_SECONDS,
)

TTS_RATE = 24000  # fixed rate/mono for clean concat (clips are re-encoded)

# Delivery tag -> (exaggeration, cfg_weight). Chatterbox uses these instead of
# inline audio tags. exaggeration: emotional intensity; cfg_weight: cadence
# adherence (lower = slower / calmer / less accent transfer).
TONE_PRESETS = {
    "neutral": (0.5, 0.5), "calm": (0.4, 0.5), "serious": (0.45, 0.5),
    "tired": (0.35, 0.5), "sad": (0.4, 0.45), "gentle": (0.4, 0.55),
    "whispers": (0.35, 0.6), "whisper": (0.35, 0.6), "soft": (0.4, 0.55),
    "trembling": (0.6, 0.4), "nervous": (0.6, 0.4), "worried": (0.55, 0.45),
    "happy": (0.65, 0.4), "excited": (0.7, 0.35), "energetic": (0.7, 0.35),
    "promo": (0.7, 0.35), "cheerful": (0.65, 0.4), "laughs": (0.7, 0.4),
    "dramatic": (0.8, 0.3), "angry": (0.8, 0.3), "panicked": (0.85, 0.3),
    "shout": (0.9, 0.3), "shouting": (0.9, 0.3), "intense": (0.8, 0.35),
}
DEFAULT_KNOBS = (0.5, 0.5)


def resolve_knobs(line, char_default, lang):
    """Resolve (exaggeration, cfg_weight) for a line from explicit fields or tags."""
    exag, cfg = char_default
    if "exaggeration" in line:
        exag = float(line["exaggeration"])
    if "cfg_weight" in line:
        cfg = float(line["cfg_weight"])
    else:
        for t in line.get("tags", []) or []:
            key = str(t).strip().strip("[]").lower()
            if key in TONE_PRESETS:
                _, cfg = TONE_PRESETS[key]
                if "exaggeration" not in line:
                    exag = TONE_PRESETS[key][0]
                break
    # Non-English with an English-leaning reference: pull cfg down to reduce
    # accent transfer (voice-clone-narration guidance: cfg 0 avoids accent).
    if lang and lang != "en":
        cfg = min(cfg, 0.4)
    return round(float(exag), 3), round(float(cfg), 3)


def char_default_knobs(character):
    exag = character.get("exaggeration")
    cfg = character.get("cfg_weight")
    tone = str(character.get("tone", "")).strip().lower()
    base = TONE_PRESETS.get(tone, DEFAULT_KNOBS)
    return (float(exag) if exag is not None else base[0],
            float(cfg) if cfg is not None else base[1])


def pad_clip(clip_path, pause_after, out_path):
    """Re-encode a clip to fixed format with trailing silence (pcm_s16le/24k/mono)."""
    af = f"apad=pad_dur={max(0.0, float(pause_after)):.3f}" if pause_after else "anull"
    return run_ffmpeg([
        "-i", str(clip_path), "-af", af,
        "-ar", str(TTS_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(out_path),
    ])


def concat_wavs(wav_paths, out_path):
    """Concatenate identically-formatted WAVs via the concat demuxer (-c copy)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        listfile = f.name
        for p in wav_paths:
            f.write(f"file '{Path(p).resolve()}'\n")
    try:
        ok = run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", listfile,
            "-c", "copy", str(out_path),
        ], description=f"concat {len(wav_paths)} clips")
    finally:
        Path(listfile).unlink(missing_ok=True)
    return ok


def run_batch(items, model, backend, max_chars, out_dir):
    """Synthesize all line clips in one subprocess (model loaded once)."""
    vc_py, vc_expected = sibling_venv_python("voice-clone-narration")
    vc_narrate = sibling_script("voice-clone-narration", "narrate.py")
    if not vc_py:
        print("Error: voice-clone-narration is not set up.", file=sys.stderr)
        print(f"  Expected its venv python at: {vc_expected}", file=sys.stderr)
        print("  Run: bash <voice-clone-narration>/scripts/setup_env.sh", file=sys.stderr)
        sys.exit(1)
    if not vc_narrate:
        print("Error: voice-clone-narration scripts not found (is the skill installed alongside "
              "audio-theater?).", file=sys.stderr)
        sys.exit(1)
    vc_scripts = str(Path(vc_narrate).parent)

    manifest = {"backend": backend, "model": model, "max_chars": max_chars, "items": items}
    manifest_path = out_dir / ".vc_manifest.json"
    results_path = out_dir / ".vc_results.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    batch_script = SCRIPT_DIR / "_vc_batch.py"
    cmd = [str(vc_py), str(batch_script), "--manifest", str(manifest_path),
           "--vc-scripts", vc_scripts, "--results", str(results_path)]
    print(f"  voices: synthesizing {len(items)} line(s) via voice-clone-narration ...",
          file=sys.stderr)
    proc = subprocess.run(cmd, text=True)
    if proc.returncode != 0 and not results_path.exists():
        print("Error: voice synthesis failed (see log above).", file=sys.stderr)
        sys.exit(1)

    results = load_json(results_path).get("results", []) if results_path.exists() else []
    manifest_path.unlink(missing_ok=True)
    results_path.unlink(missing_ok=True)
    return {r["index"]: r for r in results}


def main():
    parser = argparse.ArgumentParser(description="Generate per-line voices via voice-clone-narration")
    parser.add_argument("--script", required=True, help="Path to script.json")
    parser.add_argument("--out", "-o", required=True, help="Output project folder")
    parser.add_argument("--model", default=None,
                        help="voice-clone-narration model: multilingual (default) | turbo | <hf-repo>")
    parser.add_argument("--backend", default="auto", choices=["auto", "mlx", "torch"],
                        help="TTS backend (default auto: mlx on Apple Silicon, else torch)")
    parser.add_argument("--max-chars", type=int, default=280, help="Max characters per synthesis chunk")
    parser.add_argument("--max-clip-seconds", type=float, default=None,
                        help="Warn when a clip exceeds this (default 15, talking-head/lipsync limit)")
    parser.add_argument("--only", default=None,
                        help="Comma-separated line indices to (re)synthesize; all other lines "
                             "reuse their existing lines/line-NNN.wav clip. Use to re-roll a single "
                             "bad take (e.g. a repeated phrase) without disturbing good ones.")
    parser.add_argument("--seed", type=int, default=None,
                        help="Base RNG seed for reproducible synthesis (line i uses seed+i).")
    args = parser.parse_args()

    config = load_config()
    model = args.model or config.get("default_voice_model", "multilingual")
    max_clip = args.max_clip_seconds or config.get("max_clip_seconds", MAX_CLIP_SECONDS)

    script = load_json(args.script)
    out_dir = resolve_out_dir(args.out)
    lang = (script.get("language") or "en").strip().lower()

    characters = {c["name"]: c for c in script.get("characters", [])}
    missing_voice = [name for name, c in characters.items() if not c.get("voice")]
    if missing_voice:
        print(f"Error: these characters have no voice assigned: {missing_voice}", file=sys.stderr)
        print("  Run setup_cast.py first to design/clone a voice per character.", file=sys.stderr)
        sys.exit(1)

    lines_dir = out_dir / "lines"
    lines_dir.mkdir(parents=True, exist_ok=True)

    # Optional per-line regeneration: only re-synthesize the selected indices,
    # reuse existing clips for the rest (rebuild dialogue.wav + lines.json fully).
    only = None
    if args.only:
        only = set()
        for tok in str(args.only).replace(" ", "").split(","):
            if tok:
                only.add(int(tok))

    # Build the per-line plan (ordered) and the synthesis manifest (subset).
    ordered, meta = [], {}
    synth_items, synth_indices = [], set()
    for ln in script.get("lines", []):
        idx = ln.get("index")
        text = (ln.get("text") or "").strip()
        if not text:
            continue
        speaker = ln.get("speaker")
        ch = characters.get(speaker, {})
        voice = ch.get("voice")
        exag, cfg = resolve_knobs(ln, char_default_knobs(ch), lang)
        out_wav = lines_dir / f"line-{idx:03d}.wav"
        ordered.append(idx)
        meta[idx] = {"speaker": speaker, "voice": voice, "text": text,
                     "tags": ln.get("tags", []), "exaggeration": exag, "cfg_weight": cfg,
                     "pause_after": float(ln.get("pause_after", 0.3) or 0.0),
                     "out_wav": out_wav}
        # With --only, skip synthesis for lines that already have a clip on disk.
        if only is not None and idx not in only and out_wav.exists():
            continue
        item = {
            "index": idx, "voice": voice, "text": text, "lang": lang,
            "exaggeration": exag, "cfg_weight": cfg, "out_wav": str(out_wav),
        }
        if args.seed is not None:
            item["seed"] = int(args.seed) + idx
        synth_items.append(item)
        synth_indices.add(idx)

    if not ordered:
        print("Error: script.json has no non-empty lines.", file=sys.stderr)
        sys.exit(1)
    if only is not None:
        print(f"  --only {sorted(only)}: (re)synthesizing {sorted(synth_indices)}, "
              f"reusing {len(ordered) - len(synth_indices)} existing clip(s).", file=sys.stderr)

    results = run_batch(synth_items, model, args.backend, args.max_chars, out_dir) if synth_items else {}

    # Pad + concat into dialogue.wav; record exact timing.
    padded_dir = out_dir / ".padded"
    padded_dir.mkdir(parents=True, exist_ok=True)
    entries, padded_paths, warnings = [], [], []
    cumulative = 0.0
    for idx in ordered:
        m = meta[idx]
        clip_path = Path(m["out_wav"])
        if idx in synth_indices:
            r = results.get(idx)
            if not r or not r.get("ok") or not clip_path.exists():
                print(f"  Skipping line {idx} (synthesis failed)", file=sys.stderr)
                continue
        elif not clip_path.exists():
            print(f"  Skipping line {idx} (no existing clip to reuse; run without --only)",
                  file=sys.stderr)
            continue
        clip_dur = get_audio_duration(clip_path)
        if clip_dur > max_clip:
            warnings.append(idx)
            print(f"    Warning: clip {idx} is {clip_dur:.1f}s > {max_clip}s "
                  f"(too long for talking-head lipsync; split the line)", file=sys.stderr)
        pause_after = m["pause_after"]
        padded_path = padded_dir / f"pad-{idx:03d}.wav"
        if not pad_clip(clip_path, pause_after, padded_path):
            print(f"  Error: failed to pad clip {idx}", file=sys.stderr)
            continue
        padded_dur = get_audio_duration(padded_path)
        padded_paths.append(padded_path)
        entries.append({
            "index": idx, "speaker": m["speaker"], "voice": m["voice"], "text": m["text"],
            "tags": m["tags"], "exaggeration": m["exaggeration"], "cfg_weight": m["cfg_weight"],
            "file": str(clip_path.relative_to(out_dir)),
            "start": round(cumulative, 3), "end": round(cumulative + clip_dur, 3),
            "duration": round(clip_dur, 3), "pause_after": pause_after,
        })
        cumulative += padded_dur

    if not padded_paths:
        print("Error: no audio was generated.", file=sys.stderr)
        sys.exit(1)

    dialogue_path = out_dir / "dialogue.wav"
    if not concat_wavs(padded_paths, dialogue_path):
        sys.exit(1)
    for p in padded_paths:
        Path(p).unlink(missing_ok=True)
    try:
        padded_dir.rmdir()
    except OSError:
        pass

    total_dur = get_audio_duration(dialogue_path)
    lines_data = {
        "title": script.get("title"),
        "language": script.get("language"),
        "mode": script.get("mode"),
        "tts_mode": "per_line",
        "engine": "voice-clone-narration",
        "voice_model": model,
        "dialogue": str(dialogue_path.relative_to(out_dir)),
        "duration": round(total_dur, 3),
        "max_clip_seconds": max_clip,
        "lines": entries,
    }
    lines_json = out_dir / "lines.json"
    save_json(lines_json, lines_data)

    print(f"\n  dialogue.wav: {total_dur:.2f}s ({len(entries)} lines)", file=sys.stderr)
    if warnings:
        print(f"  {len(warnings)} clip(s) exceed {max_clip}s: {warnings}", file=sys.stderr)
    print(json.dumps({
        "dialogue": str(dialogue_path),
        "lines_json": str(lines_json),
        "engine": "voice-clone-narration",
        "line_count": len(entries),
        "duration": round(total_dur, 3),
        "clips_over_limit": warnings,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
