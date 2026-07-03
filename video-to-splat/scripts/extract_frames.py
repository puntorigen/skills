#!/usr/bin/env python3
"""
video-to-splat step 1 - turn an mp4 tour into a clean set of frames for SfM.

Structure-from-Motion (COLMAP) and Gaussian-splat training want sharp, well
spread, non-duplicate views with plenty of overlap. Phone/drone tours are full
of motion blur and near-identical frames, so we:

  1. ffprobe the video for duration/fps
  2. ffmpeg-extract candidates oversampled above the target rate
  3. score each candidate's sharpness = variance of the Laplacian
  4. keep the sharpest candidate per 1/fps window (drops motion blur)
  5. drop near-duplicate consecutive frames (dHash) so static stretches don't
     dominate and COLMAP doesn't choke on redundant views
  6. downscale the long side (default 1600px) - the SfM + training sweet spot
  7. write frame-0001.jpg ... into <project>/images/ + frames.json

Output layout (consumed by run_colmap.py):
    <VIDEO_TO_SPLAT_HOME>/projects/<name>/images/frame-0001.jpg ...

Nothing is uploaded anywhere; all frames stay under the data root.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def eprint(*a):
    print(*a, file=sys.stderr)


def fail(msg, code=1):
    eprint(f"extract_frames: {msg}")
    sys.exit(code)


def have(cmd):
    return shutil.which(cmd) is not None


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ffprobe_info(path):
    info = {"duration": None, "fps": None}
    r = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ])
    if r.returncode != 0:
        eprint(r.stderr.strip())
        return info
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return info
    fmt = data.get("format", {})
    if fmt.get("duration"):
        try:
            info["duration"] = float(fmt["duration"])
        except ValueError:
            pass
    streams = data.get("streams", [])
    if streams:
        rate = streams[0].get("avg_frame_rate") or streams[0].get("r_frame_rate")
        if rate and "/" in rate:
            num, den = rate.split("/")
            try:
                den_f = float(den)
                info["fps"] = float(num) / den_f if den_f else None
            except ValueError:
                pass
    return info


# --------------------------------------------------------------------------- #
# sharpness + fingerprint backends (opencv if available, else numpy+pillow)
# --------------------------------------------------------------------------- #
def load_backends():
    """Return (name, sharpness_fn(path)->float, fingerprint_fn(path)->int|None)."""
    try:
        import cv2
        import numpy as np

        def sharp(path):
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                return 0.0
            return float(cv2.Laplacian(img, cv2.CV_64F).var())

        def fp(path):
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                return None
            small = cv2.resize(img, (9, 8), interpolation=cv2.INTER_AREA).astype(np.int16)
            diff = small[:, 1:] > small[:, :-1]
            val = 0
            for b in diff.flatten():
                val = (val << 1) | int(b)
            return val

        return "opencv", sharp, fp
    except Exception:
        pass

    try:
        import numpy as np
        from PIL import Image
    except Exception:
        fail("need OpenCV, or Pillow + numpy. Run setup_env.sh first.")

    def sharp(path):
        try:
            with Image.open(path) as im:
                g = np.asarray(im.convert("L"), dtype=np.float64)
        except Exception:
            return 0.0
        if g.ndim != 2 or g.shape[0] < 3 or g.shape[1] < 3:
            return 0.0
        c = g[1:-1, 1:-1]
        lap = (-4.0 * c) + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
        return float(lap.var())

    def fp(path):
        try:
            with Image.open(path) as im:
                small = np.asarray(im.convert("L").resize((9, 8)), dtype=np.int16)
        except Exception:
            return None
        diff = small[:, 1:] > small[:, :-1]
        val = 0
        for b in diff.flatten():
            val = (val << 1) | int(b)
        return val

    return "numpy", sharp, fp


def hamming(a, b):
    return bin(a ^ b).count("1") if (a is not None and b is not None) else 64


def resize_longest(path, max_size):
    """Downscale so the longest side <= max_size (in place). No-op if smaller."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            longest = max(w, h)
            if longest <= max_size:
                im.save(path, quality=95)
                return w, h
            scale = max_size / float(longest)
            nw, nh = int(round(w * scale)), int(round(h * scale))
            im = im.resize((nw, nh), Image.LANCZOS)
            im.save(path, quality=95)
            return nw, nh
    except Exception as e:
        eprint(f"extract_frames: resize failed for {path}: {e}")
        return None


def slugify(name):
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-.")
    return s or "tour"


def main(argv=None):
    p = argparse.ArgumentParser(prog="extract_frames", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", help="path to the mp4 tour")
    p.add_argument("--name", default=None, help="project name (default: from filename)")
    p.add_argument("--fps", type=float, default=2.0,
                   help="target selected frames per second (default 2)")
    p.add_argument("--oversample", type=int, default=3,
                   help="candidate extraction fps multiplier for blur rejection (default 3)")
    p.add_argument("--max-frames", type=int, default=200, dest="max_frames",
                   help="cap on selected frames; COLMAP time grows fast (default 200)")
    p.add_argument("--max-size", type=int, default=1600, dest="max_size",
                   help="downscale longest side to this many px (default 1600)")
    p.add_argument("--dedup-dist", type=int, default=4, dest="dedup_dist",
                   help="drop a frame if within this dHash Hamming distance of the "
                        "previous kept frame; 0 disables (default 4)")
    p.add_argument("--home", default=None, help="override VIDEO_TO_SPLAT_HOME")
    args = p.parse_args(argv)

    if not have("ffmpeg") or not have("ffprobe"):
        fail("ffmpeg and ffprobe are required (brew install ffmpeg)")
    if args.fps <= 0:
        fail("--fps must be > 0")
    if args.oversample < 1:
        fail("--oversample must be >= 1")
    if args.max_frames < 2:
        fail("--max-frames must be >= 2")

    video = Path(args.video).expanduser()
    if not video.exists():
        fail(f"video not found: {video}")

    home = Path(args.home).expanduser() if args.home else \
        Path(os.environ.get("VIDEO_TO_SPLAT_HOME", Path.home() / ".video-to-splat"))
    name = slugify(args.name or video.stem)
    project = home / "projects" / name
    images = project / "images"
    if images.exists():
        shutil.rmtree(images)
    images.mkdir(parents=True, exist_ok=True)

    info = ffprobe_info(video)
    backend, sharp_fn, fp_fn = load_backends()
    cand_fps = args.fps * args.oversample

    # 1. extract candidates into a scratch dir
    scratch = project / "_candidates"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    pattern = str(scratch / "cand-%05d.jpg")
    cmd = ["ffmpeg", "-y", "-i", str(video), "-vf", f"fps={cand_fps:g}",
           "-q:v", "2", pattern]
    r = run(cmd)
    if r.returncode != 0:
        eprint(r.stderr.strip()[-2000:])
        fail("ffmpeg frame extraction failed")
    cands = sorted(scratch.glob("cand-*.jpg"))
    if not cands:
        fail("no frames extracted (empty or unreadable video)")

    step = 1.0 / cand_fps
    scored = [{"path": c, "t": i * step, "sharp": sharp_fn(c)}
              for i, c in enumerate(cands)]

    # 2. sharpest candidate per 1/fps window
    window = 1.0 / args.fps
    buckets = {}
    for f in scored:
        buckets.setdefault(int(f["t"] // window), []).append(f)
    picked = [max(buckets[w], key=lambda x: x["sharp"]) for w in sorted(buckets)]

    # 3. drop near-duplicate consecutive frames
    kept = []
    last_fp = None
    for f in picked:
        fp = fp_fn(f["path"])
        if args.dedup_dist > 0 and last_fp is not None and fp is not None:
            if hamming(last_fp, fp) <= args.dedup_dist:
                continue
        kept.append(f)
        last_fp = fp

    # 4. cap to max_frames, evenly across the timeline
    if len(kept) > args.max_frames:
        n = args.max_frames
        idx = {round(i * (len(kept) - 1) / (n - 1)) for i in range(n)}
        kept = [f for i, f in enumerate(kept) if i in idx]

    # 5. write final frames (resized) with stable names
    manifest_frames = []
    for i, f in enumerate(kept, start=1):
        dest = images / f"frame-{i:04d}.jpg"
        shutil.copy2(f["path"], dest)
        dims = resize_longest(dest, args.max_size)
        manifest_frames.append({
            "name": dest.name,
            "t": round(f["t"], 3),
            "sharpness": round(f["sharp"], 2),
            "size": list(dims) if dims else None,
        })

    shutil.rmtree(scratch, ignore_errors=True)

    manifest = {
        "project": str(project),
        "name": name,
        "video": str(video),
        "duration": info["duration"],
        "source_fps": info["fps"],
        "target_fps": args.fps,
        "oversample": args.oversample,
        "max_size": args.max_size,
        "dedup_dist": args.dedup_dist,
        "backend": backend,
        "count_candidates": len(cands),
        "count_selected": len(manifest_frames),
        "frames": manifest_frames,
    }
    (project / "frames.json").write_text(json.dumps(manifest, indent=2))

    eprint(f"[frames] project : {project}")
    eprint(f"[frames] backend : {backend}")
    eprint(f"[frames] selected {len(manifest_frames)} of {len(cands)} candidates "
           f"(target {args.fps} fps, oversample x{args.oversample})")
    if len(manifest_frames) < 20:
        eprint(f"[frames] WARNING: only {len(manifest_frames)} frames. Reconstruction is "
               f"unreliable below ~20-30 views. Raise --fps or shoot a longer/slower tour.")
    if len(manifest_frames) >= args.max_frames:
        eprint(f"[frames] NOTE: capped at --max-frames={args.max_frames}. More frames = "
               f"slower COLMAP; raise the cap only if coverage is poor.")
    # stdout: the project dir (input for run_colmap.py)
    print(project)
    return 0


if __name__ == "__main__":
    sys.exit(main())
