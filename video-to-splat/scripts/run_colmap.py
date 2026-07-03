#!/usr/bin/env python3
"""
video-to-splat step 2 - recover camera poses + a sparse point cloud with COLMAP.

Gaussian-splat training needs to know where each frame was shot from (camera
intrinsics + extrinsics) and a sparse SfM point cloud to initialize the splats.
We do this locally with pycolmap (no COLMAP GUI, no CUDA required):

    extract SIFT features  ->  match  ->  incremental mapping (SfM)

Matching strategy (pick with --matcher):
  - sequential (default): frames are consecutive video frames, so match each to
    its neighbors. Fast and the right prior for a walking/driving tour.
  - exhaustive: match every pair. Slower (O(n^2)) but most robust for small,
    unordered, or loopy sets (<~150 frames).
Optional --loop-detection adds vocab-tree loop closure to sequential matching
(needs --vocab-tree pointing at a COLMAP vocab tree .bin).

Output is the de-facto 3DGS/COLMAP layout that Brush loads directly:
    <project>/images/                      (from extract_frames.py)
    <project>/sparse/0/{cameras,images,points3D}.bin

Only the largest reconstruction is kept (COLMAP can split into disconnected
sub-models when overlap is poor); we report how many frames registered so you
can judge quality before spending hours training.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path


def eprint(*a):
    print(*a, file=sys.stderr)


def fail(msg, code=1):
    eprint(f"run_colmap: {msg}")
    sys.exit(code)


def resolve_project(arg, home):
    """Accept a project name, a project dir, or an images dir; return the project dir."""
    p = Path(arg).expanduser()
    if p.is_dir():
        if (p / "images").is_dir():
            return p
        if p.name == "images" and p.parent.is_dir():
            return p.parent
    cand = home / "projects" / arg
    if (cand / "images").is_dir():
        return cand
    fail(f"could not find a project with an images/ dir for {arg!r} "
         f"(looked at {p} and {cand})")


def num_reg_images(rec):
    for attr in ("num_reg_images", "num_registered_images"):
        fn = getattr(rec, attr, None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:
                pass
    try:
        return len(rec.images)
    except Exception:
        return 0


def num_points(rec):
    fn = getattr(rec, "num_points3D", None)
    if callable(fn):
        try:
            return int(fn())
        except Exception:
            pass
    try:
        return len(rec.points3D)
    except Exception:
        return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="run_colmap", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("project", help="project name or path (from extract_frames.py)")
    p.add_argument("--matcher", choices=["sequential", "exhaustive"], default="sequential",
                   help="feature matcher (default sequential; best for video)")
    p.add_argument("--loop-detection", action="store_true", dest="loop_detection",
                   help="add vocab-tree loop closure to sequential matching")
    p.add_argument("--vocab-tree", default=None, dest="vocab_tree",
                   help="path to a COLMAP vocabulary tree .bin (for --loop-detection)")
    p.add_argument("--single-camera", dest="single_camera", action="store_true", default=True,
                   help="treat all frames as one physical camera (default; correct for a tour)")
    p.add_argument("--multi-camera", dest="single_camera", action="store_false",
                   help="estimate per-image intrinsics instead of a shared camera")
    p.add_argument("--min-registered", type=float, default=0.8, dest="min_registered",
                   help="warn if fewer than this fraction of frames register (default 0.8)")
    p.add_argument("--home", default=None, help="override VIDEO_TO_SPLAT_HOME")
    args = p.parse_args(argv)

    try:
        import pycolmap
    except Exception as e:
        fail(f"could not import pycolmap ({e}). Run setup_env.sh first.")

    home = Path(args.home).expanduser() if args.home else \
        Path(os.environ.get("VIDEO_TO_SPLAT_HOME", Path.home() / ".video-to-splat"))
    project = resolve_project(args.project, home)
    image_dir = project / "images"
    n_images = len(list(image_dir.glob("*.jpg"))) + len(list(image_dir.glob("*.png")))
    if n_images == 0:
        fail(f"no images in {image_dir}")

    db_path = project / "database.db"
    sparse = project / "sparse"
    if db_path.exists():
        db_path.unlink()
    if sparse.exists():
        shutil.rmtree(sparse)
    sparse.mkdir(parents=True, exist_ok=True)

    eprint(f"[colmap] project : {project}")
    eprint(f"[colmap] images  : {n_images}")
    eprint(f"[colmap] pycolmap: {getattr(pycolmap, '__version__', '?')}")

    # 1. feature extraction (shared camera by default: one intrinsics for the tour)
    eprint("[colmap] extracting SIFT features ...")
    camera_mode = pycolmap.CameraMode.SINGLE if args.single_camera else pycolmap.CameraMode.AUTO
    try:
        pycolmap.extract_features(db_path, image_dir, camera_mode=camera_mode)
    except TypeError:
        # older/newer signatures may not accept camera_mode positionally
        pycolmap.extract_features(db_path, image_dir)

    # 2. matching
    eprint(f"[colmap] matching ({args.matcher}) ...")
    if args.matcher == "exhaustive":
        pycolmap.match_exhaustive(db_path)
    else:
        pairing = None
        if args.loop_detection:
            try:
                opts = {"loop_detection": True}
                if args.vocab_tree:
                    opts["vocab_tree_path"] = str(Path(args.vocab_tree).expanduser())
                pairing = pycolmap.SequentialPairingOptions(**opts)
            except Exception as e:
                eprint(f"[colmap] loop detection unavailable ({e}); plain sequential.")
                pairing = None
        try:
            if pairing is not None:
                pycolmap.match_sequential(db_path, pairing_options=pairing)
            else:
                pycolmap.match_sequential(db_path)
        except TypeError:
            # some versions name the sequential matcher differently
            matcher = getattr(pycolmap, "match_sequential", None)
            if matcher is None:
                fail("this pycolmap build has no sequential matcher; use --matcher exhaustive")
            matcher(db_path)

    # 3. incremental mapping (SfM)
    eprint("[colmap] incremental mapping (this is the slow part) ...")
    recs = pycolmap.incremental_mapping(db_path, image_dir, sparse)
    if not recs:
        fail("reconstruction failed: no model produced. Common causes: too few/blurry "
             "frames, too little overlap, or textureless scene. Try --matcher exhaustive, "
             "a higher --fps in extract_frames, or a better capture.")

    # recs is a dict {index: Reconstruction}; keep the largest, place it at sparse/0
    items = list(recs.items()) if hasattr(recs, "items") else list(enumerate(recs))
    best_idx, best = max(items, key=lambda kv: num_reg_images(kv[1]))
    reg = num_reg_images(best)
    pts = num_points(best)

    # wipe COLMAP's numbered sub-model dirs, write the winner as sparse/0
    for sub in sparse.glob("*"):
        if sub.is_dir():
            shutil.rmtree(sub, ignore_errors=True)
    out0 = sparse / "0"
    out0.mkdir(parents=True, exist_ok=True)
    best.write(out0)

    frac = reg / n_images if n_images else 0.0
    eprint(f"[colmap] best model: {reg}/{n_images} images registered "
           f"({frac*100:.0f}%), {pts} sparse points")
    if len(items) > 1:
        eprint(f"[colmap] NOTE: COLMAP produced {len(items)} disconnected sub-models; "
               f"kept the largest. This usually means weak overlap between parts of the tour.")
    if frac < args.min_registered:
        eprint(f"[colmap] WARNING: only {frac*100:.0f}% of frames registered (< "
               f"{args.min_registered*100:.0f}%). The splat will only cover the registered "
               f"region. Consider more overlap, --matcher exhaustive, or --loop-detection.")

    eprint(f"[colmap] wrote model to {out0}")
    eprint(f"[colmap] ready to train: pass {project} to train_splat.sh")
    # stdout: the project dir (the source you hand to Brush)
    print(project)
    return 0


if __name__ == "__main__":
    sys.exit(main())
