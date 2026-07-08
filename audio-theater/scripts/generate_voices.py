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
import re
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

# ── Emotion / performance model ────────────────────────────────────────────
# Chatterbox has no inline emotion markup on the multilingual model, so a line's
# emotion is delivered through two continuous knobs:
#   exaggeration : emotional intensity (0.5 neutral; 0.7-0.9 dramatic; higher
#                  also speeds delivery up)
#   cfg_weight   : cadence adherence (lower = slower, more deliberate, more
#                  emotive pacing; also reduces accent bleed on non-English)
# The table below is a drama-tuned vocabulary an actor/director can reach for.
# Values are (exaggeration, cfg_weight). Neutral is (0.5, 0.5).
TONE_PRESETS = {
    # — restrained / intimate —
    "neutral": (0.5, 0.5), "narration": (0.5, 0.5),
    "calm": (0.4, 0.5), "warm": (0.46, 0.5), "gentle": (0.42, 0.55),
    "tender": (0.46, 0.5), "soft": (0.4, 0.55), "hushed": (0.38, 0.6),
    "whisper": (0.35, 0.6), "whispers": (0.35, 0.6),
    "reassuring": (0.5, 0.45), "wistful": (0.46, 0.5), "cold": (0.45, 0.45),
    # — weight / gravity —
    "serious": (0.5, 0.45), "solemn": (0.48, 0.45), "grave": (0.52, 0.4),
    "grim": (0.56, 0.4), "sad": (0.45, 0.45), "sorrowful": (0.5, 0.42),
    "tired": (0.38, 0.5), "weary": (0.38, 0.5), "hopeful": (0.55, 0.45),
    # — resolve / authority —
    "firm": (0.6, 0.42), "resolute": (0.62, 0.4), "determined": (0.62, 0.4),
    "stern": (0.66, 0.4), "commanding": (0.7, 0.38), "defiant": (0.75, 0.32),
    "proud": (0.6, 0.42), "menacing": (0.62, 0.35), "sarcastic": (0.6, 0.42),
    # — fear / distress —
    "worried": (0.58, 0.45), "anxious": (0.6, 0.42), "nervous": (0.62, 0.4),
    "trembling": (0.66, 0.38), "breathless": (0.72, 0.35), "pleading": (0.72, 0.35),
    "frightened": (0.8, 0.32), "afraid": (0.8, 0.32), "scared": (0.8, 0.32),
    "desperate": (0.84, 0.3), "panicked": (0.88, 0.3), "terrified": (0.9, 0.3),
    # — heat / force —
    "urgent": (0.74, 0.35), "intense": (0.8, 0.35), "dramatic": (0.82, 0.3),
    "angry": (0.84, 0.3), "furious": (0.9, 0.28), "shout": (0.92, 0.28),
    "shouting": (0.92, 0.28), "yelling": (0.92, 0.28),
    # — light / bright —
    "happy": (0.66, 0.4), "cheerful": (0.66, 0.4), "joyful": (0.7, 0.38),
    "playful": (0.68, 0.42), "excited": (0.75, 0.35), "energetic": (0.74, 0.35),
    "surprised": (0.74, 0.38), "awe": (0.6, 0.42), "awestruck": (0.62, 0.42),
    "triumphant": (0.8, 0.35), "laughs": (0.72, 0.4), "laughing": (0.72, 0.4),
    "promo": (0.72, 0.35),
}
DEFAULT_KNOBS = (0.5, 0.5)
NEUTRAL_EXAG = 0.5

# Forgiving synonyms so an author can write natural stage directions ("angrily",
# "whispering", "scared") and still hit a preset. Unlisted -ing/-ly forms are also
# reduced heuristically (see resolve_emotion_key).
EMOTION_ALIASES = {
    "angrily": "angry", "nervously": "nervous", "desperately": "desperate",
    "urgently": "urgent", "coldly": "cold", "softly": "soft", "firmly": "firm",
    "gently": "gentle", "calmly": "calm", "sadly": "sad", "warmly": "warm",
    "sternly": "stern", "fearfully": "frightened", "fearful": "frightened",
    "scared": "frightened", "afraid": "frightened", "terror": "terrified",
    "excitedly": "excited", "cheerfully": "cheerful", "proudly": "proud",
    "wearily": "weary", "tenderly": "tender", "grimly": "grim", "solemnly": "solemn",
    "gravely": "grave", "hopefully": "hopeful", "playfully": "playful",
    "joyfully": "joyful", "defiantly": "defiant", "menacingly": "menacing",
    "sarcastically": "sarcastic", "breathlessly": "breathless",
    "commandingly": "commanding", "triumphantly": "triumphant",
    "reassuringly": "reassuring", "wistfully": "wistful", "anxiously": "anxious",
    "worriedly": "worried", "fury": "furious", "rage": "furious", "raging": "furious",
    "whispered": "whisper", "shouted": "shout", "screaming": "shout",
    "scream": "shout", "crying": "sorrowful", "sobbing": "sorrowful", "sob": "sorrowful",
}

# Non-verbal paralinguistic cues. Chatterbox **turbo** (English-only) speaks these
# natively when they appear inline in the text; the multilingual model cannot, so
# on multilingual they are stripped from the spoken text and instead nudge the
# emotional intensity up (an author can still "write the gasp" in the script).
TURBO_NONVERBAL = {
    "laugh": "[laugh]", "laughs": "[laugh]", "laughter": "[laugh]",
    "sigh": "[sigh]", "sighs": "[sigh]",
    "chuckle": "[chuckle]", "chuckles": "[chuckle]",
    "cough": "[cough]", "coughs": "[cough]",
    "gasp": "[gasp]", "gasps": "[gasp]",
    "groan": "[groan]", "groans": "[groan]",
    "sniff": "[sniff]", "sniffs": "[sniff]",
    "shush": "[shush]",
    "clear throat": "[clear throat]", "clears throat": "[clear throat]",
}
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")


def _norm_key(s):
    return str(s or "").strip().strip("[]").lower()


def resolve_emotion_key(word):
    """Map a free-form emotion/tag word to a TONE_PRESETS key, or None.

    Tries the word verbatim, an alias table, then a light -ing/-ly reduction
    (e.g. "whispering" -> "whisper", "sternly" -> "stern")."""
    k = _norm_key(word)
    if not k:
        return None
    if k in TONE_PRESETS:
        return k
    if k in EMOTION_ALIASES:
        return EMOTION_ALIASES[k]
    for suf in ("ing", "ly"):
        if k.endswith(suf) and len(k) > len(suf) + 2:
            base = k[:-len(suf)]
            for cand in (base, base + "e"):
                if cand in TONE_PRESETS:
                    return cand
                if cand in EMOTION_ALIASES:
                    return EMOTION_ALIASES[cand]
    return None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def process_inline_tags(text, model_is_turbo):
    """Handle inline bracketed cues in a line's text.

    Returns (clean_text, nonverbal_hits). On turbo, recognized non-verbal cues are
    normalized to the model's native tokens and kept inline; on multilingual (and
    for any unrecognized bracket), the bracket is removed so it is never read
    aloud. Detected non-verbal cues are returned so the caller can compensate the
    emotional intensity when the model can't voice them.
    """
    hits = []

    def repl(m):
        canon = TURBO_NONVERBAL.get(_norm_key(m.group(1)))
        if canon:
            hits.append(canon)
            return canon if model_is_turbo else ""
        return ""  # drop unknown brackets from spoken text on every backend

    clean = _BRACKET_RE.sub(repl, text)
    clean = re.sub(r"\s{2,}", " ", clean).replace(" ,", ",").replace(" .", ".").strip()
    return clean, hits


def char_default_knobs(character):
    exag = character.get("exaggeration")
    cfg = character.get("cfg_weight")
    tone_key = resolve_emotion_key(character.get("tone", ""))
    base = TONE_PRESETS.get(tone_key, DEFAULT_KNOBS)
    return (float(exag) if exag is not None else base[0],
            float(cfg) if cfg is not None else base[1])


def performance_knobs(line, character, lang, expressiveness, nonverbal_hits=()):
    """Resolve (exaggeration, cfg_weight) for a line's delivery.

    Precedence, low to high: character default -> emotion/tags preset ->
    theater expressiveness (amplifies deviation from neutral) -> intensity dial
    -> explicit per-line numeric overrides. Non-English pulls cfg down to limit
    accent bleed. Returns (exag, cfg, emotion_label).
    """
    exag, cfg = char_default_knobs(character)

    # 1. emotion field wins over tags; both look up the same vocabulary.
    label = resolve_emotion_key(line.get("emotion"))
    if label is None:
        for t in line.get("tags") or []:
            label = resolve_emotion_key(t)
            if label:
                break
    if label is not None:
        exag, cfg = TONE_PRESETS[label]

    # 2. theater expressiveness: stretch emotional range around neutral.
    if expressiveness and expressiveness != 1.0:
        exag = NEUTRAL_EXAG + (exag - NEUTRAL_EXAG) * float(expressiveness)

    # 3. intensity dial (0..1; 0.5 = leave as-is). Line overrides character.
    inten = line.get("intensity", character.get("intensity"))
    # A non-verbal cue we can't voice (multilingual) bumps intensity to compensate.
    if nonverbal_hits and inten is None:
        inten = 0.66
    if inten is not None:
        i = _clamp(float(inten), 0.0, 1.0)
        exag = exag + (i - 0.5) * 0.5
        cfg = cfg - (i - 0.5) * 0.2

    # 4. explicit numeric overrides win outright.
    if "exaggeration" in line:
        exag = float(line["exaggeration"])
    if "cfg_weight" in line:
        cfg = float(line["cfg_weight"])

    # 5. non-English: limit accent transfer (voice-clone-narration guidance).
    if lang and lang != "en":
        cfg = min(cfg, 0.4)

    # Cap exaggeration at 1.0 - Chatterbox gets unstable / artifact-prone above it.
    exag = _clamp(exag, 0.2, 1.0)
    cfg = _clamp(cfg, 0.05, 0.7)
    return round(exag, 3), round(cfg, 3), label


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
    parser.add_argument("--expressiveness", type=float, default=None,
                        help="Amplify each line's emotional range around neutral (1.0 = presets "
                             "as-is; >1 = more dynamic/dramatic). Default: 1.2 for theater, else 1.0.")
    args = parser.parse_args()

    config = load_config()
    model = args.model or config.get("default_voice_model", "multilingual")
    model_is_turbo = str(model).strip().lower() == "turbo"
    max_clip = args.max_clip_seconds or config.get("max_clip_seconds", MAX_CLIP_SECONDS)

    script = load_json(args.script)
    out_dir = resolve_out_dir(args.out)
    lang = (script.get("language") or "en").strip().lower()
    mode = str(script.get("mode") or "theater").strip().lower()

    # Expressiveness precedence: CLI > script field > config > mode default.
    if args.expressiveness is not None:
        expressiveness = args.expressiveness
    elif script.get("expressiveness") is not None:
        expressiveness = float(script["expressiveness"])
    elif config.get("expressiveness") is not None:
        expressiveness = float(config["expressiveness"])
    else:
        expressiveness = 1.2 if mode == "theater" else 1.0

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
        raw_text = (ln.get("text") or "").strip()
        if not raw_text:
            continue
        speaker = ln.get("speaker")
        ch = characters.get(speaker, {})
        voice = ch.get("voice")
        # Inline [non-verbal] cues: kept for turbo, stripped (but noted) elsewhere.
        text, nonverbal = process_inline_tags(raw_text, model_is_turbo)
        if not text:
            text = raw_text  # a line that was only a bracket cue: fall back to raw
        exag, cfg, emotion = performance_knobs(ln, ch, lang, expressiveness, nonverbal)
        out_wav = lines_dir / f"line-{idx:03d}.wav"
        ordered.append(idx)
        meta[idx] = {"speaker": speaker, "voice": voice, "text": text,
                     "emotion": emotion, "tags": ln.get("tags", []),
                     "exaggeration": exag, "cfg_weight": cfg,
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
            "emotion": m["emotion"], "tags": m["tags"],
            "exaggeration": m["exaggeration"], "cfg_weight": m["cfg_weight"],
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
        "expressiveness": round(float(expressiveness), 3),
        "dialogue": str(dialogue_path.relative_to(out_dir)),
        "duration": round(total_dur, 3),
        "max_clip_seconds": max_clip,
        "lines": entries,
    }
    lines_json = out_dir / "lines.json"
    save_json(lines_json, lines_data)

    print(f"\n  dialogue.wav: {total_dur:.2f}s ({len(entries)} lines) · "
          f"model={model} · expressiveness={expressiveness:g}", file=sys.stderr)
    if warnings:
        print(f"  {len(warnings)} clip(s) exceed {max_clip}s: {warnings}", file=sys.stderr)
    print(json.dumps({
        "dialogue": str(dialogue_path),
        "lines_json": str(lines_json),
        "engine": "voice-clone-narration",
        "voice_model": model,
        "expressiveness": round(float(expressiveness), 3),
        "line_count": len(entries),
        "duration": round(total_dur, 3),
        "clips_over_limit": warnings,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
