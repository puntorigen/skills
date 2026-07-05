#!/usr/bin/env python3
"""
object-to-3d step 4 - isolate the object in a trained Gaussian splat.

A splat trained from an object orbit contains more than the object: a haze of
translucent floaters, a few huge diffuse background blobs, the table/floor the
object sits on, and detached background geometry. This script keeps only the
Gaussians that belong to the object, writing a `cleaned.ply` that still carries
every Gaussian attribute (so it previews identically in the orbit viewer and
feeds splat_to_mesh.py).

Filters apply IN ORDER; each prints how many Gaussians it removed:

  1. opacity   - drop sigmoid(opacity) < --min-opacity      (translucent haze)
  2. scale     - drop the top --scale-pctl percentile of max exp(scale)
                 (giant background blobs)
  3. plane     - RANSAC-detect the dominant support plane (table/floor) and
                 remove it if it holds >= --min-frac of points  (on by default;
                 --keep-plane to skip)
  4. cluster   - DBSCAN the centers; keep the largest --keep-clusters cluster(s)
                 (the orbited object), drop detached background islands
  5. crop      - optional --radius R around --center (default: surviving
                 centroid) for a final manual tighten

Requires open3d + numpy from the shared venv (run setup_env.sh). CPU-only, fast.
"""

import argparse
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _splat_ply as sp  # noqa: E402


def eprint(*a):
    print(*a, file=sys.stderr)


def fail(msg, code=1):
    eprint(f"clean_splat: {msg}")
    sys.exit(code)


def resolve_input(arg, file_opt, home):
    """Return (ply_path, project_dir_or_None) from a name/dir/ply + optional --file."""
    p = Path(arg).expanduser()
    if p.is_file():
        return p, (p.parent if p.parent.name else None)
    # a project dir or a bare project name
    for base in (p, home / "projects" / arg):
        if base.is_dir():
            fname = file_opt or "splat.ply"
            cand = base / fname
            if cand.is_file():
                return cand, base
            fail(f"no {fname} in project {base} (train first, or pass --file)")
    fail(f"could not resolve a splat for {arg!r} (looked at {p} and "
         f"{home / 'projects' / arg})")


def bbox_diag(xyz):
    return float(np.linalg.norm(xyz.max(axis=0) - xyz.min(axis=0)))


def median_nn_distance(o3d, xyz, sample=20000):
    """Median nearest-neighbor spacing (sampled for speed)."""
    n = len(xyz)
    if n > sample:
        sel = np.random.default_rng(0).choice(n, sample, replace=False)
        pts = xyz[sel]
    else:
        pts = xyz
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
    d = np.asarray(pc.compute_nearest_neighbor_distance())
    d = d[np.isfinite(d) & (d > 0)]
    return float(np.median(d)) if len(d) else 0.0


def main(argv=None):
    p = argparse.ArgumentParser(prog="clean_splat", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("project", help="project name/dir, or a .ply path")
    p.add_argument("--file", default=None,
                   help="splat file within the project (default splat.ply)")
    p.add_argument("--out", default=None,
                   help="output path (default <project>/cleaned.ply)")
    p.add_argument("--min-opacity", type=float, default=0.4, dest="min_opacity",
                   help="drop Gaussians with rendered opacity below this (default 0.4)")
    p.add_argument("--scale-pctl", type=float, default=98.0, dest="scale_pctl",
                   help="drop Gaussians above this percentile of max linear scale "
                        "(default 98; lower to cut more background blobs)")
    p.add_argument("--keep-plane", action="store_true", dest="keep_plane",
                   help="skip RANSAC support-plane (table/floor) removal")
    p.add_argument("--plane-thresh", type=float, default=None, dest="plane_thresh",
                   help="RANSAC inlier distance (default: 1%% of bbox diagonal)")
    p.add_argument("--min-frac", type=float, default=0.1, dest="min_frac",
                   help="remove the plane only if it holds >= this fraction (default 0.1)")
    p.add_argument("--eps", type=float, default=None,
                   help="DBSCAN neighborhood radius (default: 2x median NN spacing)")
    p.add_argument("--min-points", type=int, default=20, dest="min_points",
                   help="DBSCAN min points per cluster (default 20)")
    p.add_argument("--keep-clusters", type=int, default=1, dest="keep_clusters",
                   help="keep this many largest DBSCAN clusters (default 1)")
    p.add_argument("--min-dominant", type=float, default=0.5, dest="min_dominant",
                   help="auto-grow eps until the largest cluster holds this fraction "
                        "of points (default 0.5), so an object is not shattered")
    p.add_argument("--no-cluster", action="store_true", dest="no_cluster",
                   help="skip DBSCAN clustering (for already-isolated splats)")
    p.add_argument("--radius", type=float, default=None,
                   help="manual crop: keep Gaussians within this radius of --center")
    p.add_argument("--center", default=None,
                   help="manual crop center as 'x,y,z' (default: surviving centroid)")
    p.add_argument("--home", default=None, help="override VIDEO_TO_SPLAT_HOME")
    args = p.parse_args(argv)

    try:
        import open3d as o3d
    except Exception as e:
        fail(f"could not import open3d ({e}). Run setup_env.sh first.")

    home = Path(args.home).expanduser() if args.home else \
        Path(os.environ.get("VIDEO_TO_SPLAT_HOME", Path.home() / ".video-to-splat"))
    ply_path, proj = resolve_input(args.project, args.file, home)
    out = Path(args.out).expanduser() if args.out else \
        ((proj / "cleaned.ply") if proj else ply_path.with_name("cleaned.ply"))

    eprint(f"[clean] input : {ply_path}")
    splat = sp.read_ply(ply_path)
    n0 = len(splat)
    if n0 == 0:
        fail("splat has no Gaussians")
    for req in ("x", "y", "z", "opacity", "scale_0", "scale_1", "scale_2"):
        if not splat.has(req):
            fail(f"splat PLY is missing property {req!r}; is this a 3DGS export?")
    eprint(f"[clean] gaussians: {n0}")

    xyz_all = splat.xyz()
    idx = np.arange(n0)                       # surviving original indices

    def report(step, before):
        eprint(f"[clean] {step:<10}: {len(idx):>7} kept  ({before - len(idx)} removed)")

    # 1. opacity
    before = len(idx)
    alpha = splat.opacity_alpha()
    idx = idx[alpha[idx] >= args.min_opacity]
    report(f"opacity>={args.min_opacity:g}", before)
    if len(idx) == 0:
        fail("opacity filter removed everything; lower --min-opacity")

    # 2. scale (drop the largest blobs)
    before = len(idx)
    max_scale = splat.scales_linear().max(axis=1)
    thr = np.percentile(max_scale[idx], args.scale_pctl)
    idx = idx[max_scale[idx] <= thr]
    report(f"scale<=p{args.scale_pctl:g}", before)

    # 3. support plane (RANSAC)
    if not args.keep_plane and len(idx) >= 100:
        before = len(idx)
        pts = xyz_all[idx]
        thresh = args.plane_thresh if args.plane_thresh is not None \
            else max(1e-9, 0.01 * bbox_diag(pts))
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
        try:
            _model, inliers = pc.segment_plane(distance_threshold=thresh,
                                               ransac_n=3, num_iterations=1000)
            frac = len(inliers) / len(pts)
            if frac >= args.min_frac:
                mask = np.ones(len(pts), dtype=bool)
                mask[np.asarray(inliers, dtype=int)] = False
                idx = idx[mask]
                report(f"plane({frac*100:.0f}%)", before)
            else:
                eprint(f"[clean] plane     : dominant plane holds only {frac*100:.0f}% "
                       f"(< {args.min_frac*100:.0f}%); kept (looks like object, not a table)")
        except Exception as e:
            eprint(f"[clean] plane     : RANSAC failed ({e}); skipped")
    elif args.keep_plane:
        eprint("[clean] plane     : skipped (--keep-plane)")

    # 4. DBSCAN clustering -> keep the dominant object cluster(s).
    # eps is auto-grown until the largest cluster dominates (>= --min-dominant of
    # the points): a fixed eps that is a hair too small shatters an object of
    # uneven splat density into many pieces and would keep only a sliver.
    if not args.no_cluster and len(idx) >= args.min_points:
        before = len(idx)
        pts = xyz_all[idx]
        diag = bbox_diag(pts)
        if args.eps is not None:
            eps_seq = [args.eps]
        else:
            base = max(3.0 * median_nn_distance(o3d, pts), 0.01 * diag, 1e-9)
            eps_seq = [base * m for m in (1, 2, 4, 8)]
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
        chosen = None
        for eps in eps_seq:
            labels = np.asarray(pc.cluster_dbscan(eps=eps, min_points=args.min_points,
                                                  print_progress=False))
            valid = labels[labels >= 0]
            if not len(valid):
                continue
            uniq, counts = np.unique(valid, return_counts=True)
            chosen = (eps, labels, uniq, counts)
            if counts.max() / len(pts) >= args.min_dominant or args.eps is not None:
                break  # a dominant cluster emerged (or user fixed eps)
        if chosen is not None:
            eps, labels, uniq, counts = chosen
            order = uniq[np.argsort(counts)[::-1]]
            keep_labels = set(order[:max(1, args.keep_clusters)].tolist())
            mask = np.isin(labels, list(keep_labels))
            idx = idx[mask]
            report(f"dbscan(eps{eps:.4g})", before)
            eprint(f"[clean]           : {len(uniq)} clusters, kept {len(keep_labels)} "
                   f"largest ({counts.max()/before*100:.0f}% of pre-cluster points)")
            if counts.max() / before < args.min_dominant:
                eprint(f"[clean]           : WARNING: dominant cluster is small - the "
                       f"object may be fragmented. Try a larger --eps or --no-cluster.")
        else:
            eprint(f"[clean] dbscan    : no clusters found; kept all (try a larger --eps)")
    elif args.no_cluster:
        eprint("[clean] dbscan    : skipped (--no-cluster)")

    # 5. optional manual spherical crop
    if args.radius is not None:
        before = len(idx)
        if args.center:
            try:
                c = np.array([float(x) for x in args.center.split(",")], dtype=np.float64)
                assert c.shape == (3,)
            except Exception:
                fail("--center must be 'x,y,z'")
        else:
            c = xyz_all[idx].mean(axis=0)
        d = np.linalg.norm(xyz_all[idx] - c, axis=1)
        idx = idx[d <= args.radius]
        report(f"radius<={args.radius:g}", before)

    if len(idx) == 0:
        fail("all Gaussians removed; relax the filters (see the removal counts above)")

    cleaned = splat.select(idx)
    cleaned.comments = list(cleaned.comments) + [
        f"object-to-3d cleaned: {n0} -> {len(idx)} gaussians"]
    sp.write_ply(out, cleaned)

    kept_xyz = xyz_all[idx]
    dims = kept_xyz.max(axis=0) - kept_xyz.min(axis=0)
    eprint(f"[clean] wrote {out} ({len(idx)}/{n0} gaussians, "
           f"{len(idx)/n0*100:.0f}% kept)")
    eprint(f"[clean] object bbox (scene units): "
           f"{dims[0]:.3f} x {dims[1]:.3f} x {dims[2]:.3f}")
    eprint(f"[clean] next: preview.sh <project> --file {out.name}  |  "
           f"splat_to_mesh.py <project>")
    # stdout: the cleaned ply path
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
