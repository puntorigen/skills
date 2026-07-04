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

COLMAP can split into disconnected sub-models when overlap is poor; all are
written, ranked by size (sparse/0 = largest = what training uses; the others
remain useful for analyze_scene.py). The registered-frame fraction is reported
so you can judge quality before spending hours training.
"""

import argparse
import json
import os
import shutil
import sys
import time
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
    p.add_argument("--overlap", type=int, default=10,
                   help="sequential matching: pair each frame with this many neighbors "
                        "(default 10). Raise to 20-30 for fast-moving tours where "
                        "consecutive frames share little content")
    p.add_argument("--max-features", type=int, default=8192, dest="max_features",
                   help="SIFT features per image (default 8192). Raise to 12000-16000 "
                        "for low-texture interiors / compressed video")
    p.add_argument("--loop-detection", action="store_true", dest="loop_detection",
                   help="add vocab-tree loop closure to sequential matching")
    p.add_argument("--vocab-tree", default=None, dest="vocab_tree",
                   help="path to a COLMAP vocabulary tree .bin (for --loop-detection)")
    p.add_argument("--relaxed", action="store_true",
                   help="lower the mapper's registration thresholds (min matches/inliers). "
                        "Registers more frames on hard footage (motion blur, stairs, "
                        "low texture) at some risk of drift")
    p.add_argument("--skip-matching", action="store_true", dest="skip_matching",
                   help="reuse the existing database.db (features+matches) and only "
                        "redo the mapping step; for iterating on mapper options")
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

    # merged-capture check: overlap BETWEEN videos is only found by exhaustive
    # matching or loop detection, never by plain sequential neighbors
    n_videos = 1
    manifest_path = project / "frames.json"
    if manifest_path.exists():
        try:
            n_videos = max(1, len(json.loads(manifest_path.read_text()).get("videos", [])))
        except (json.JSONDecodeError, OSError):
            pass
    if n_videos > 1 and args.matcher == "sequential" and not args.loop_detection:
        eprint(f"[colmap] WARNING: this project merges {n_videos} videos, but the "
               f"sequential matcher only pairs temporal neighbors WITHIN each video. "
               f"The captures will not connect. Use --matcher exhaustive (recommended "
               f"up to ~300-400 frames) or --loop-detection with a vocab tree.")
    if n_videos > 1 and args.single_camera:
        eprint(f"[colmap] NOTE: merged project uses one shared camera intrinsics. "
               f"That's right for the same phone/camera; pass --multi-camera if the "
               f"videos come from different devices.")

    db_path = project / "database.db"
    sparse = project / "sparse"
    if args.skip_matching and not db_path.exists():
        fail(f"--skip-matching but no {db_path}; run once without it first")
    if not args.skip_matching and db_path.exists():
        db_path.unlink()
    if sparse.exists():
        shutil.rmtree(sparse)
    sparse.mkdir(parents=True, exist_ok=True)

    eprint(f"[colmap] project : {project}")
    eprint(f"[colmap] images  : {n_images}")
    eprint(f"[colmap] pycolmap: {getattr(pycolmap, '__version__', '?')}")

    if args.skip_matching:
        eprint("[colmap] reusing existing features+matches (--skip-matching)")
    else:
        # 1. feature extraction (shared camera by default: one intrinsics for the tour)
        eprint(f"[colmap] extracting SIFT features (max {args.max_features}/image) ...")
        camera_mode = pycolmap.CameraMode.SINGLE if args.single_camera else pycolmap.CameraMode.AUTO
        extraction = pycolmap.FeatureExtractionOptions()
        extraction.sift.max_num_features = args.max_features
        try:
            pycolmap.extract_features(db_path, image_dir, camera_mode=camera_mode,
                                      extraction_options=extraction)
        except TypeError:
            # older/newer signatures may not accept these kwargs
            pycolmap.extract_features(db_path, image_dir)

        # 2. matching
        eprint(f"[colmap] matching ({args.matcher}"
               + (f", overlap {args.overlap}" if args.matcher == "sequential" else "") + ") ...")
        if args.matcher == "exhaustive":
            pycolmap.match_exhaustive(db_path)
        else:
            try:
                opts = {"overlap": args.overlap}
                if args.loop_detection:
                    vocab = Path(args.vocab_tree).expanduser() if args.vocab_tree else \
                        home / "vocab_tree_faiss_flickr100K_words32K.bin"
                    if vocab.is_file():
                        opts["loop_detection"] = True
                        opts["vocab_tree_path"] = str(vocab)
                    else:
                        eprint(f"[colmap] no vocab tree at {vocab}; skipping loop detection "
                               f"(setup_env.sh downloads one, or pass --vocab-tree)")
                pairing = pycolmap.SequentialPairingOptions(**opts)
            except Exception as e:
                eprint(f"[colmap] pairing options unavailable ({e}); using defaults.")
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
    pipeline = pycolmap.IncrementalPipelineOptions()
    if args.relaxed:
        # accept weaker two-view links and poses; helps blurred/fast footage
        pipeline.min_num_matches = 8
        pipeline.mapper.init_min_num_inliers = 50
        pipeline.mapper.abs_pose_min_num_inliers = 15
        pipeline.mapper.abs_pose_min_inlier_ratio = 0.15
        eprint("[colmap] relaxed mapper thresholds enabled")

    # live progress: pycolmap invokes these callbacks as the mapper works
    progress = {"n": 0, "t": time.time()}

    def on_image():
        progress["n"] += 1
        now = time.time()
        if progress["n"] % 20 == 0 or now - progress["t"] > 30:
            eprint(f"[colmap]   registered {progress['n']}/{n_images} images "
                   f"(cumulative across sub-models)")
            progress["t"] = now
        sys.stderr.flush()

    try:
        recs = pycolmap.incremental_mapping(
            db_path, image_dir, sparse, options=pipeline,
            initial_image_pair_callback=lambda: on_image() or on_image(),
            next_image_callback=on_image)
    except TypeError:
        recs = pycolmap.incremental_mapping(db_path, image_dir, sparse, options=pipeline)
    if not recs:
        fail("reconstruction failed: no model produced. Common causes: too few/blurry "
             "frames, too little overlap, or textureless scene. Try --matcher exhaustive, "
             "a higher --fps in extract_frames, or a better capture.")

    # recs is a dict {index: Reconstruction}. Renumber by size: the largest at
    # sparse/0 (what Brush trains on), the rest kept as sparse/1.. - disconnected
    # sub-models still hold valid geometry (e.g. other floors) that
    # analyze_scene.py can use even if they can't be trained together.
    items = list(recs.items()) if hasattr(recs, "items") else list(enumerate(recs))
    ranked = sorted((kv[1] for kv in items), key=num_reg_images, reverse=True)

    for sub in sparse.glob("*"):
        if sub.is_dir():
            shutil.rmtree(sub, ignore_errors=True)
    for i, rec in enumerate(ranked):
        out = sparse / str(i)
        out.mkdir(parents=True, exist_ok=True)
        rec.write(out)

    best = ranked[0]
    reg = num_reg_images(best)
    pts = num_points(best)

    frac = reg / n_images if n_images else 0.0
    eprint(f"[colmap] best model: {reg}/{n_images} images registered "
           f"({frac*100:.0f}%), {pts} sparse points")
    if len(ranked) > 1:
        sizes = ", ".join(f"sparse/{i}: {num_reg_images(r)}" for i, r in enumerate(ranked))
        eprint(f"[colmap] NOTE: COLMAP produced {len(ranked)} disconnected sub-models "
               f"({sizes} images). Training uses sparse/0 only; the rest usually "
               f"means weak overlap between parts of the tour (doorways, stairs).")
    if frac < args.min_registered:
        eprint(f"[colmap] WARNING: only {frac*100:.0f}% of frames registered (< "
               f"{args.min_registered*100:.0f}%). The splat will only cover the registered "
               f"region. Consider more overlap, --matcher exhaustive, or --loop-detection.")

    eprint(f"[colmap] wrote model to {sparse / '0'}")
    eprint(f"[colmap] ready to train: pass {project} to train_splat.sh")
    # stdout: the project dir (the source you hand to Brush)
    print(project)
    return 0


if __name__ == "__main__":
    sys.exit(main())
