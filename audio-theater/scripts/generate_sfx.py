#!/usr/bin/env python3
"""Generate the cued sounds listed in cues.json, 100% locally.

Backends (all local, no keys):
- ambient / oneshot cues -> the **sound-effects** skill (Stable Audio Open Small).
- music cues             -> the **bg-music** skill (ACE-Step 1.5), via generate_music.py.

Files land in <out>/sfx/<id>.mp3 and the generated path + duration are written
back into cues.json so mix.py / mix_spatial.py can place them.

Usage:
    python3 generate_sfx.py --cues audio-theater/ep/cues.json --out audio-theater/ep
    python3 generate_sfx.py --cues ... --out ... --only rain_bed,door_slam
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
import generate_music  # noqa: E402
from _common import (  # noqa: E402
    load_json, save_json, resolve_out_dir, get_audio_duration,
    sibling_venv_python, sibling_script,
)

# Stable Audio Open Small caps around 11s; ambient beds are generated short and
# looped by the mixer across the full cue window.
SFX_MAX_SECONDS = 11


def cue_needed_seconds(cue):
    start = float(cue.get("start", 0.0) or 0.0)
    end = cue.get("end")
    if end is None:
        return None
    return max(0.5, float(end) - start)


def gen_sound_effect(cue, out_file, sfx_py, sfx_script):
    """Generate an ambient/oneshot cue via the sound-effects skill.

    Optional per-cue quality knobs are passed straight through to the
    sound-effects CLI: `steps`, `sampler` ("euler"|"rk4"), `cfg_scale`, and
    `negative_prompt`. For a crisp one-shot impact (a door, a crack), bump steps
    to ~20-24 with `sampler: "rk4"` and add a `negative_prompt` to steer the model
    away from the wrong texture (e.g. "music, animal, voice, roar")."""
    is_ambient = cue.get("type") == "ambient"
    needed = cue_needed_seconds(cue)
    if is_ambient:
        # A short loopable bed; the mixer loops/trims it to the exact window.
        gen_dur = min(SFX_MAX_SECONDS, max(6, int(needed or 8)))
    else:
        gen_dur = int(cue.get("gen_seconds", 3))
    gen_dur = max(1, min(SFX_MAX_SECONDS, gen_dur))

    cmd = [str(sfx_py), str(sfx_script), cue.get("description", ""),
           "--duration", str(gen_dur), "--output", str(out_file)]
    if cue.get("seed") is not None:
        cmd += ["--seed", str(int(cue["seed"]))]
    if cue.get("steps") is not None:
        cmd += ["--steps", str(int(cue["steps"]))]
    if cue.get("sampler"):
        cmd += ["--sampler", str(cue["sampler"])]
    if cue.get("cfg_scale") is not None:
        cmd += ["--cfg-scale", str(float(cue["cfg_scale"]))]
    if cue.get("negative_prompt"):
        cmd += ["--negative-prompt", str(cue["negative_prompt"])]
    return _run(cmd, "sfx")


def gen_music(cue, out_file):
    """Generate an instrumental music cue via the bg-music skill."""
    needed = cue_needed_seconds(cue)
    return generate_music.generate(
        cue.get("description", "background music"), out_file,
        mood=cue.get("mood"),
        duration=int(needed) if needed else None,
        seed=cue.get("seed"),
        bpm=cue.get("bpm"),
        prompt_override=cue.get("prompt"),
    )


def _run(cmd, label):
    print(f"  $ {label}: {str(cmd[2])[:48]} ...", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        for line in (result.stderr or "").strip().split("\n")[-6:]:
            print(f"      {line}", file=sys.stderr)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Generate cued SFX/music from cues.json (local)")
    parser.add_argument("--cues", default=None, help="cues.json (default <out>/cues.json)")
    parser.add_argument("--out", "-o", required=True, help="Project folder")
    parser.add_argument("--only", default=None, help="Comma-separated cue ids to (re)generate")
    args = parser.parse_args()

    out_dir = resolve_out_dir(args.out)
    cues_path = Path(args.cues) if args.cues else out_dir / "cues.json"
    if not cues_path.exists():
        print(f"Error: {cues_path} not found. Author it first (see SKILL.md cues schema).",
              file=sys.stderr)
        sys.exit(1)

    data = load_json(cues_path)
    cues = data.get("cues", [])
    sfx_dir = out_dir / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)
    only = set(s.strip() for s in args.only.split(",")) if args.only else None

    # Resolve the sound-effects backend once (music backend is resolved lazily).
    sfx_py, sfx_expected = sibling_venv_python("sound-effects")
    sfx_script = sibling_script("sound-effects", "generate_sfx.py")
    have_sfx = bool(sfx_py and sfx_script)

    generated, failed, skipped = [], [], []
    for cue in cues:
        cid = cue.get("id")
        if not cid:
            continue
        if only and cid not in only:
            continue
        ctype = cue.get("type", "oneshot")
        out_file = sfx_dir / f"{cid}.mp3"

        if ctype == "music":
            ok = gen_music(cue, out_file)
        elif ctype in ("ambient", "oneshot"):
            if not have_sfx:
                print(f"  Skipping SFX cue '{cid}': sound-effects skill not set up "
                      f"(expected venv python at {sfx_expected}). Run its setup_env.sh.",
                      file=sys.stderr)
                skipped.append(cid)
                continue
            ok = gen_sound_effect(cue, out_file, sfx_py, sfx_script)
        else:
            print(f"  Skipping cue '{cid}': unknown type '{ctype}'", file=sys.stderr)
            failed.append(cid)
            continue

        if ok and out_file.exists():
            dur = round(get_audio_duration(out_file), 3)
            cue["file"] = str(out_file.relative_to(out_dir))
            cue["gen_duration"] = dur
            generated.append({"id": cid, "file": cue["file"], "duration": dur})
            print(f"  ✓ {cid} -> {cue['file']} ({dur:.2f}s)", file=sys.stderr)
        else:
            failed.append(cid)
            print(f"  ✗ {cid} failed", file=sys.stderr)

    save_json(cues_path, data)
    print(json.dumps({
        "cues_json": str(cues_path),
        "sfx_dir": str(sfx_dir),
        "generated": generated,
        "failed": failed,
        "skipped": skipped,
    }, indent=2, ensure_ascii=False))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
