#!/usr/bin/env python3
"""
video-to-splat step 3b (optional) - analyze the reconstruction: detect floors
and draw a floorplan per floor.

Works on the COLMAP model produced by run_colmap.py (no training needed):

  1. gravity: average the per-camera "up" axis (phones are held roughly
     upright), refined by a cone search for the direction that makes the
     sparse cloud's height histogram peakiest (floors/ceilings are horizontal)
  2. scale: estimate the camera's eye height above the local floor (~1.5 m in
     reality) - the natural unit for "is this gap a story or a step?"
  3. floors: peaks in the camera-height density, separated by at least 1.2 eye
     heights = levels where the person actually walked (stairs fill the gaps
     between floors, so peak-finding is robust where gap-splitting is not);
     each frame is assigned to the nearest floor or marked as a transition
  4. floorplan: per floor, take sparse points in a wall band around eye level,
     project onto the horizontal plane, PCA-align the dominant wall directions
     to the image axes, and render a density map with the camera path overlaid

Outputs (under <project>/analysis/):
    floors.json          floors, heights, frame assignments, up vector
    floorplan-f<N>.png   one top-down plan per detected floor

Everything is scale-free (SfM has no metric scale); heights are relative.
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path

import numpy as np


def eprint(*a):
    print(*a, file=sys.stderr)


def fail(msg, code=1):
    eprint(f"analyze_scene: {msg}")
    sys.exit(code)


def resolve_project(arg, home, model):
    p = Path(arg).expanduser()
    if p.is_dir() and (p / "sparse" / str(model)).is_dir():
        return p
    cand = home / "projects" / arg
    if (cand / "sparse" / str(model)).is_dir():
        return cand
    fail(f"no COLMAP model 'sparse/{model}' found for {arg!r} (run run_colmap.py first)")


def call_or_get(obj, name):
    v = getattr(obj, name)
    return v() if callable(v) else v


# --------------------------------------------------------------------------- #
# gravity / up estimation
# --------------------------------------------------------------------------- #
def _peakiness(pts, directions):
    """For each candidate up direction, how concentrated are the point heights?
    Floors and ceilings are horizontal planes, so the true up axis makes the
    height histogram maximally peaky. Score = sum of squared bin probabilities."""
    H = pts @ directions.T                       # (n_pts, n_dirs)
    lo = np.percentile(H, 2, axis=0)
    hi = np.percentile(H, 98, axis=0)
    scores = np.empty(directions.shape[0])
    for j in range(directions.shape[0]):
        hist, _ = np.histogram(H[:, j], bins=100, range=(lo[j], hi[j]))
        p = hist / max(1, hist.sum())
        scores[j] = float((p * p).sum())
    return scores


def estimate_up(centers, cam_ups, points):
    """World up vector. Start from the mean camera up axis (phones are held
    roughly upright but usually pitched down a bit), then refine by searching
    a ~25 deg cone for the direction that makes the sparse cloud's height
    distribution most concentrated (floors/ceilings are horizontal planes).
    A PCA snap is NOT reliable here: a house's principal axes follow its
    horizontal extent, and a few degrees of tilt turn horizontal walking into
    fake height drift that splits one floor into several."""
    up = cam_ups.mean(axis=0)
    up /= np.linalg.norm(up)
    if len(points) < 200:
        return up

    pts = points - points.mean(axis=0)
    if len(pts) > 15000:
        idx = np.random.default_rng(0).choice(len(pts), 15000, replace=False)
        pts = pts[idx]

    # local tangent basis around the initial guess
    tmp = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 1.0, 0])
    e1 = np.cross(up, tmp); e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)

    best = up
    # coarse-to-fine search over a spherical cap
    for max_t, n_t, n_p in ((25.0, 8, 24), (3.0, 6, 16)):
        thetas = np.radians(np.linspace(0, max_t, n_t + 1)[1:])
        phis = np.linspace(0, 2 * math.pi, n_p, endpoint=False)
        dirs = [best]
        for t in thetas:
            for ph in phis:
                d = math.cos(t) * best + math.sin(t) * (math.cos(ph) * e1 + math.sin(ph) * e2)
                dirs.append(d / np.linalg.norm(d))
        dirs = np.vstack(dirs)
        best = dirs[int(np.argmax(_peakiness(pts, dirs)))]
        e1 = np.cross(best, tmp); e1 /= np.linalg.norm(e1)
        e2 = np.cross(best, e1)
    return best


def estimate_camera_height(cams_xy, cams_h, pts_xy, pts_h):
    """Median height of the camera above the local floor, in scene units.
    This is the natural scale unit of a walking tour (~1.4-1.6 m in reality):
    story gaps are ~1.7-2x it, garden steps / split levels well under 1x it.
    For each camera, the 'floor' is the low percentile of point heights within
    a horizontal radius."""
    n = len(cams_xy)
    sel = np.linspace(0, n - 1, min(n, 120)).astype(int)
    span = float(np.max(pts_xy.max(axis=0) - pts_xy.min(axis=0))) or 1.0
    radius = 0.08 * span
    ds = []
    for i in sel:
        d2 = ((pts_xy - cams_xy[i]) ** 2).sum(axis=1)
        near = pts_h[(d2 < radius * radius) & (pts_h < cams_h[i])]
        if len(near) >= 20:
            ds.append(cams_h[i] - np.percentile(near, 5))
    if not ds:
        return float(np.ptp(cams_h)) / 2 or 1.0
    return float(np.median(ds))


def median_filter(x, k=5):
    if len(x) < k:
        return np.asarray(x, dtype=float)
    pad = k // 2
    xp = np.pad(np.asarray(x, dtype=float), pad, mode="edge")
    return np.array([np.median(xp[i:i + k]) for i in range(len(x))])


# --------------------------------------------------------------------------- #
# floor detection (1D density peaks over camera heights)
# --------------------------------------------------------------------------- #
def detect_floors(heights, min_sep, n_floors=None, min_peak_frac=0.06):
    """Return sorted floor levels (heights). Peaks of the smoothed height
    histogram, separated by at least min_sep (scene units - calibrate with the
    camera's eye height: real stories are ~1.7-2 eye heights apart), each
    holding at least min_peak_frac of the frames. n_floors forces a count."""
    h = np.asarray(heights, dtype=float)
    rng = float(h.max() - h.min())
    if rng <= 1e-9 or rng < min_sep:
        return [float(np.median(h))]

    # bin width ~ 1/8 of the minimum separation
    bins = int(np.clip(round(rng / (min_sep / 8)), 30, 240))
    hist, edges = np.histogram(h, bins=bins)
    kw = max(1, bins // 20)
    kernel = np.exp(-0.5 * (np.arange(-3 * kw, 3 * kw + 1) / kw) ** 2)
    kernel /= kernel.sum()
    smooth = np.convolve(hist, kernel, mode="same")
    mids = (edges[:-1] + edges[1:]) / 2

    peaks = [i for i in range(1, bins - 1)
             if smooth[i] >= smooth[i - 1] and smooth[i] >= smooth[i + 1]
             and smooth[i] > 0]
    peaks.sort(key=lambda i: -smooth[i])

    min_mass = min_peak_frac * len(h)
    chosen = []
    for i in peaks:
        if any(abs(mids[i] - mids[j]) < min_sep for j in chosen):
            continue
        mass = hist[(np.abs(mids - mids[i]) < min_sep / 2)].sum()
        if n_floors is None and mass < min_mass:
            continue
        chosen.append(i)
        if n_floors is not None and len(chosen) >= n_floors:
            break
    if not chosen:
        chosen = [int(np.argmax(smooth))]
    levels = sorted(float(mids[i]) for i in chosen)
    # refine each level to the median of nearby heights (histogram mid is coarse)
    return [float(np.median(h[np.abs(h - lv) < min_sep / 2])) for lv in levels]


def assign_floors(heights, levels, tol):
    """Assign each height to the nearest floor level; heights further than tol
    from every level are transitions (stairs). Returns floor_idx (-1 = stairs)."""
    levels = np.asarray(levels, dtype=float)
    out = []
    for h in heights:
        d = np.abs(levels - h)
        i = int(np.argmin(d))
        out.append(i if d[i] <= tol else -1)
    return np.asarray(out)


# --------------------------------------------------------------------------- #
# floorplan rendering (PIL, no matplotlib)
# --------------------------------------------------------------------------- #
def render_floorplan(path, wall_xy, cam_xy, cam_floor_mask, size=1200, pad=0.06):
    """Density map of wall points (log-scaled) + camera path for this floor.
    Returns the plan-xy -> pixel transform so viewers can overlay live markers:
    px = (xy - lo) * scale + off."""
    from PIL import Image, ImageDraw

    all_xy = wall_xy if len(wall_xy) else cam_xy
    lo = all_xy.min(axis=0)
    hi = all_xy.max(axis=0)
    span = np.maximum(hi - lo, 1e-9)
    scale = (1 - 2 * pad) * size / span.max()
    off = (size - span * scale) / 2

    def to_px(xy):
        return (xy - lo) * scale + off

    # 2D histogram of wall points
    img = Image.new("RGB", (size, size), (17, 17, 20))
    if len(wall_xy):
        px = to_px(wall_xy)
        gx = np.clip(px[:, 0].astype(int), 0, size - 1)
        gy = np.clip(px[:, 1].astype(int), 0, size - 1)
        # bin at reduced resolution then upsample for a chunkier, plan-like look
        cell = 3
        g = size // cell
        histo, _, _ = np.histogram2d(gx // cell, gy // cell,
                                     bins=[g, g], range=[[0, g], [0, g]])
        d = np.log1p(histo)
        if d.max() > 0:
            d = d / d.max()
        arr = np.zeros((g, g, 3), dtype=np.uint8)
        # dark blue -> white ramp
        arr[..., 0] = (40 + 215 * d).astype(np.uint8)
        arr[..., 1] = (44 + 205 * d).astype(np.uint8)
        arr[..., 2] = (60 + 195 * d).astype(np.uint8)
        dens = Image.fromarray(np.transpose(arr, (1, 0, 2)), "RGB").resize(
            (size, size), Image.NEAREST)
        img.paste(dens)

    draw = ImageDraw.Draw(img)
    # camera path (only this floor's segments, gaps where the walker left)
    if len(cam_xy):
        px = to_px(cam_xy)
        seg = []
        for i in range(len(px)):
            if cam_floor_mask[i]:
                seg.append(tuple(px[i]))
            else:
                if len(seg) > 1:
                    draw.line(seg, fill=(255, 120, 40), width=3)
                seg = []
        if len(seg) > 1:
            draw.line(seg, fill=(255, 120, 40), width=3)
        # start marker of the first on-floor sample
        first = next((i for i in range(len(px)) if cam_floor_mask[i]), None)
        if first is not None:
            x, y = px[first]
            draw.ellipse([x - 7, y - 7, x + 7, y + 7],
                         outline=(90, 255, 120), width=3)
    img.save(path)
    return {"origin_xy": [float(lo[0]), float(lo[1])],
            "px_per_unit": float(scale),
            "offset_px": [float(off[0]), float(off[1])],
            "size_px": size}


def main(argv=None):
    p = argparse.ArgumentParser(prog="analyze_scene", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("project", help="project name or path (needs sparse/0)")
    p.add_argument("--model", type=int, default=0,
                   help="which sub-model to analyze (default 0 = largest). When the "
                        "reconstruction shattered at stairs, other floors often live "
                        "in sparse/1, sparse/2, ... - analyze each separately")
    p.add_argument("--floors", type=int, default=None,
                   help="force this many floors instead of auto-detecting")
    p.add_argument("--min-track", type=int, default=3, dest="min_track",
                   help="ignore sparse points seen by fewer images (default 3)")
    p.add_argument("--plan-size", type=int, default=1200, dest="plan_size",
                   help="floorplan image size in px (default 1200)")
    p.add_argument("--home", default=None, help="override VIDEO_TO_SPLAT_HOME")
    args = p.parse_args(argv)

    try:
        import pycolmap
    except Exception as e:
        fail(f"could not import pycolmap ({e}). Run setup_env.sh first.")

    home = Path(args.home).expanduser() if args.home else \
        Path(os.environ.get("VIDEO_TO_SPLAT_HOME", Path.home() / ".video-to-splat"))
    project = resolve_project(args.project, home, args.model)
    rec = pycolmap.Reconstruction(project / "sparse" / str(args.model))

    # cameras, ordered by name = temporal order
    images = sorted(rec.images.values(), key=lambda im: im.name)
    if len(images) < 10:
        fail(f"only {len(images)} registered images; not enough for analysis")
    centers, ups, fwds, names = [], [], [], []
    for im in images:
        cfw = call_or_get(im, "cam_from_world")
        r_wc = np.asarray(cfw.rotation.matrix()).T
        centers.append(np.asarray(call_or_get(im, "projection_center"), dtype=float))
        ups.append(-r_wc[:, 1])  # camera -y axis in world = "up" of an upright phone
        fwds.append(r_wc[:, 2])  # camera +z axis in world = look direction
        names.append(im.name)
    centers = np.vstack(centers)
    ups = np.vstack(ups)
    fwds = np.vstack(fwds)

    pts = np.array([p3.xyz for p3 in rec.points3D.values()
                    if p3.track.length() >= args.min_track], dtype=float)
    eprint(f"[analyze] project : {project}")
    eprint(f"[analyze] cameras : {len(images)} registered, "
           f"{len(pts)} sparse points (track >= {args.min_track})")

    up = estimate_up(centers, ups, pts)
    tilt = math.degrees(math.acos(np.clip(
        float(np.dot(up, ups.mean(axis=0) / np.linalg.norm(ups.mean(axis=0)))), -1, 1)))
    eprint(f"[analyze] up      : [{up[0]:+.3f} {up[1]:+.3f} {up[2]:+.3f}] "
           f"({tilt:.1f} deg from mean camera up)")

    # horizontal plane basis (PCA-align dominant wall directions to the axes)
    tmp = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 1.0, 0])
    e1 = np.cross(up, tmp); e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)
    pts_h = pts @ up if len(pts) else np.zeros(0)
    pts_xy = pts @ np.vstack([e1, e2]).T if len(pts) else np.zeros((0, 2))
    cams_xy = centers @ np.vstack([e1, e2]).T
    cams_h = centers @ up
    rot = np.eye(2)
    if len(pts_xy) > 200:
        c = pts_xy - pts_xy.mean(axis=0)
        cov = c.T @ c
        w, v = np.linalg.eigh(cov)
        rot = v[:, ::-1].T  # principal axis -> x
        pts_xy = pts_xy @ rot.T
        cams_xy = cams_xy @ rot.T
    # world 3-vectors of the final plan axes: plan_x = P . ex, plan_y = P . ey
    ex = rot[0, 0] * e1 + rot[0, 1] * e2
    ey = rot[1, 0] * e1 + rot[1, 1] * e2

    # scale calibration: the camera's eye height above the local floor is the
    # one known quantity of a walking tour (~1.5 m). Real stories are ~1.7-2x
    # it; anything closer is a split level / garden step, not a floor.
    eye = estimate_camera_height(cams_xy, cams_h, pts_xy, pts_h) if len(pts) else \
        (float(np.ptp(cams_h)) / 2 or 1.0)
    eprint(f"[analyze] eye     : camera ~{eye:.2f} scene units above local floor "
           f"(~1.5 m in reality)")

    # heights along up, smoothed over time; floors = peaks separated by at
    # least 1.2 eye heights (~1.8 m), i.e. a real story change
    heights = median_filter(cams_h, k=5)
    levels = detect_floors(heights, min_sep=1.2 * eye, n_floors=args.floors)
    floor_idx = assign_floors(heights, levels, tol=0.45 * eye)
    story = float(np.median(np.diff(levels))) if len(levels) > 1 else 1.9 * eye

    n_floors = len(levels)
    eprint(f"[analyze] floors  : {n_floors} detected "
           f"(story height ~{story:.2f} scene units ~ {story / eye:.1f} eye heights)")
    for i, lv in enumerate(levels):
        n = int((floor_idx == i).sum())
        eprint(f"[analyze]   floor {i + 1}: level {lv:+.2f}  ({n} frames, "
               f"{100.0 * n / len(heights):.0f}%)")
    n_trans = int((floor_idx == -1).sum())
    if n_trans:
        eprint(f"[analyze]   transitions (stairs): {n_trans} frames")

    outdir = project / "analysis"
    outdir.mkdir(exist_ok=True)
    tag = "" if args.model == 0 else f"-m{args.model}"

    plans, transforms, poses = [], [], []
    for i, lv in enumerate(levels):
        # wall band around eye level: floor slab is ~1 eye height below the
        # camera, the ceiling ~0.6 above; keep the middle slice = walls
        band_lo, band_hi = lv - 0.6 * eye, lv + 0.4 * eye
        sel = (pts_h >= band_lo) & (pts_h <= band_hi) if len(pts) else np.zeros(0, bool)
        wall_xy = pts_xy[sel] if len(pts) else np.zeros((0, 2))
        mask = floor_idx == i
        plan = outdir / f"floorplan{tag}-f{i + 1}.png"
        transforms.append(render_floorplan(plan, wall_xy, cams_xy, mask,
                                           size=args.plan_size))
        plans.append(str(plan))
        # representative capture pose: the median-in-time frame on this floor
        on = np.flatnonzero(mask)
        if len(on):
            j = int(on[len(on) // 2])
            poses.append({"position": [round(float(v), 5) for v in centers[j]],
                          "forward": [round(float(v), 5) for v in fwds[j]]})
        else:
            poses.append(None)
        eprint(f"[analyze] wrote {plan.name}  ({int(sel.sum())} wall points)")

    (outdir / f"floors{tag}.json").write_text(json.dumps({
        "project": str(project),
        "up": [float(x) for x in up],
        # world-space axes of the floorplan image: px_x grows along plan_x,
        # px_y along plan_y. plan_xy(world p) = [p . plan_x, p . plan_y]
        "plan_x": [float(x) for x in ex],
        "plan_y": [float(x) for x in ey],
        "n_registered": len(images),
        "n_points": int(len(pts)),
        "eye_height_scene_units": eye,
        "story_height_scene_units": story,
        "floors": [{
            "index": i + 1,
            "level": float(lv),
            "frames": int((floor_idx == i).sum()),
            "floorplan": plans[i],
            "plan_transform": transforms[i],
            "camera": poses[i],
        } for i, lv in enumerate(levels)],
        "transition_frames": n_trans,
        "frames": [{"name": names[i],
                    "height": round(float(heights[i]), 4),
                    "floor": int(floor_idx[i]) + 1 if floor_idx[i] >= 0 else 0}
                   for i in range(len(names))],
    }, indent=2))
    eprint(f"[analyze] wrote floors{tag}.json")
    # stdout: the analysis dir
    print(outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
