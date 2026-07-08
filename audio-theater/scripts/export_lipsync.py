#!/usr/bin/env python3
"""Export lipsync.json from lines.json - a manifest for the local talking-head skill.

Each clean per-line clip becomes a lip-sync reference: speaker, voice, exact
transcript, duration, and an `ok` flag (clip present + reasonable length). The
skill does NOT generate avatars; it maps each on-camera line's audio clip to a
talking-head render you drive with an avatar image (make one per character with
the image-gen skill).

Usage:
    python3 export_lipsync.py --out audio-theater/ep
    python3 export_lipsync.py --lines audio-theater/ep/lines.json --out audio-theater/ep
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from _common import (  # noqa: E402
    load_config, load_json, save_json, resolve_out_dir,
    get_audio_duration, MAX_CLIP_SECONDS,
)


def main():
    parser = argparse.ArgumentParser(description="Export talking-head lipsync manifest")
    parser.add_argument("--out", "-o", required=True, help="Project folder")
    parser.add_argument("--lines", default=None, help="Path to lines.json (default <out>/lines.json)")
    parser.add_argument("--max-clip-seconds", type=float, default=None,
                        help="Flag clips longer than this as slow to render (default 15)")
    args = parser.parse_args()

    config = load_config()
    out_dir = resolve_out_dir(args.out)
    lines_path = Path(args.lines) if args.lines else out_dir / "lines.json"
    if not lines_path.exists():
        print(f"Error: {lines_path} not found. Run generate_voices.py first.", file=sys.stderr)
        sys.exit(1)

    data = load_json(lines_path)
    max_clip = args.max_clip_seconds or data.get("max_clip_seconds") \
        or config.get("max_clip_seconds", MAX_CLIP_SECONDS)

    clips = []
    over = []
    missing = []
    speakers = {}
    for ln in data.get("lines", []):
        rel = ln.get("file")
        duration = ln.get("duration")
        if rel:
            abs_path = out_dir / rel
            if not abs_path.exists():
                missing.append(ln["index"])
            elif duration is None:
                duration = round(get_audio_duration(abs_path), 3)
        ok = bool(rel) and duration is not None
        if rel and duration is not None and duration > max_clip:
            over.append(ln["index"])
        speakers.setdefault(ln["speaker"], ln.get("voice"))
        clips.append({
            "index": ln["index"],
            "speaker": ln["speaker"],
            "voice": ln.get("voice"),
            "transcript": ln.get("text", ""),
            "duration": duration,
            "file": rel,
            "ok": ok,
        })

    manifest = {
        "title": data.get("title"),
        "language": data.get("language"),
        "max_clip_seconds": max_clip,
        "clip_count": len(clips),
        "speakers": speakers,
        "all_ok": not missing and all(c["ok"] for c in clips),
        "clips": clips,
        "talking_head_handoff": (
            "Per on-camera character, make ONE front-facing, mouth-closed avatar with "
            "the image-gen skill. Then per line clip run the talking-head skill: "
            "animate.py --image <avatar-for-speaker>.png --audio <out>/<file> --crop "
            "--out <clip>.mp4. talking-head lip-syncs whatever voice is in the clip, so "
            "feed the clean per-line clip (not the full mix). Longer clips render slower "
            "(~24s compute per 1s of video on an M4); keep lines short."
        ),
    }

    out_path = out_dir / "lipsync.json"
    save_json(out_path, manifest)

    print(f"  Clips: {len(clips)} | speakers: {', '.join(speakers) or '-'}", file=sys.stderr)
    if over:
        print(f"  Slow-to-render (> {max_clip}s): lines {over}", file=sys.stderr)
    if missing:
        print(f"  Missing clip files: lines {missing}", file=sys.stderr)
    print(json.dumps({
        "lipsync_json": str(out_path),
        "clip_count": len(clips),
        "all_ok": manifest["all_ok"],
        "over_limit": over,
        "missing": missing,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
