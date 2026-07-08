#!/usr/bin/env python3
"""Instrumental music generation for audio-theater, via the local bg-music skill.

Thin wrapper over the sibling **bg-music** skill (ACE-Step 1.5, MLX on Apple
Silicon). Always instrumental (no sung vocals); the mixer owns fades/ducking, so
tracks are generated un-faded. bg-music's minimum length is 10s; shorter cue
windows are trimmed/looped by the mixer.

Importable:  generate(description, out_file, *, mood=None, duration=None, seed=None, bpm=None) -> bool
CLI:         generate_music.py "soft mystical underscore" --mood cinematic --duration 90 --out score.mp3
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from _common import sibling_venv_python, sibling_script, get_audio_duration  # noqa: E402

# Light mood -> style keyword hints (kept small; bg-music itself handles genre).
MOODS = {
    "generic": "versatile, balanced, clean, modern",
    "cinematic": "epic, orchestral, dramatic, wide, film-score",
    "ambient": "atmospheric, ethereal, spacious, meditative pads",
    "tense": "dark, suspenseful, low drones, uneasy",
    "playful": "light, bouncy, whimsical, cheerful",
    "pet-lullaby": "delicate lullaby, hushed, soothing, music box, celesta",
    "podcast-bed": "ambient, minimal, unobtrusive, warm, lo-fi bed",
    "podcast-intro": "upbeat, catchy, bright, punchy, radio-ready",
    "podcast-outro": "warm, mellow, resolving, gentle fade feel",
    "sad": "melancholic, slow, emotive strings and piano",
    "hopeful": "warm, uplifting, gently building, major key",
}


def build_prompt(description, mood):
    parts = []
    if description:
        parts.append(description.strip())
    kw = MOODS.get((mood or "").strip().lower())
    if kw:
        parts.append(kw)
    parts.append("instrumental, no vocals, professional production")
    seen, out = set(), []
    for p in parts:
        p = p.strip().strip(",")
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ", ".join(out)


def generate(description, out_file, *, mood=None, duration=None, seed=None, bpm=None,
             prompt_override=None):
    """Generate one instrumental track to out_file via bg-music. Returns True/False."""
    bg_py, bg_expected = sibling_venv_python("bg-music")
    bg_script = sibling_script("bg-music", "generate_music.py")
    if not bg_py:
        print(f"  Error: bg-music is not set up (expected venv python at {bg_expected}).",
              file=sys.stderr)
        print("  Run: bash <bg-music>/scripts/setup_env.sh", file=sys.stderr)
        return False
    if not bg_script:
        print("  Error: bg-music generate_music.py not found (install bg-music alongside audio-theater).",
              file=sys.stderr)
        return False

    prompt = prompt_override or build_prompt(description, mood)
    dur = max(10.0, float(duration)) if duration else 30.0
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [str(bg_py), str(bg_script), "--prompt", prompt,
           "--duration", str(int(dur)), "--count", "1", "--out", str(out_file)]
    if bpm:
        cmd += ["--bpm", str(int(bpm))]
    if seed is not None:
        cmd += ["--seed", str(int(seed))]

    print(f"  music (bg-music): {prompt[:60]} [{int(dur)}s]", file=sys.stderr)
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        for line in (proc.stderr or "").strip().split("\n")[-6:]:
            print(f"    {line}", file=sys.stderr)
        return False
    return out_file.exists()


def main():
    ap = argparse.ArgumentParser(description="Instrumental music via the bg-music skill.")
    ap.add_argument("description", nargs="?", help="Music style description.")
    ap.add_argument("--prompt", "-p", default=None, help="Explicit prompt (overrides description+mood).")
    ap.add_argument("--mood", "-m", default=None, help="Mood hint (cinematic, ambient, podcast-bed, ...).")
    ap.add_argument("--duration", "-d", type=float, default=None, help="Length in seconds (min 10).")
    ap.add_argument("--bpm", type=int, default=None, help="Tempo (optional).")
    ap.add_argument("--seed", type=int, default=None, help="Seed (optional).")
    ap.add_argument("--out", "-o", required=True, help="Output mp3 path.")
    args = ap.parse_args()

    if not args.description and not args.prompt:
        ap.error("provide a description or --prompt")

    ok = generate(args.description or "", args.out, mood=args.mood, duration=args.duration,
                  seed=args.seed, bpm=args.bpm, prompt_override=args.prompt)
    if not ok:
        sys.exit(1)
    print(json.dumps({"file": args.out, "duration": round(get_audio_duration(args.out), 3)},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
