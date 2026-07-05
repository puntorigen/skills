#!/usr/bin/env python3
"""
object-to-3d step 5 - turn a (cleaned) object splat into a printable mesh.

Screened Poisson surface reconstruction needs a dense, oriented point cloud.
Gaussian centers alone are too sparse with poor normals, so we first DENSIFY:
each Gaussian is a flat-ish ellipsoid, so we sample points on the disk spanned by
its two largest axes and take the smallest axis as the surface normal. Then:

  densify -> orient normals outward -> Poisson -> trim low-density fringe ->
  largest connected component -> ORIENT base down -> BASE REPAIR (flat, watertight)
  -> watertight validation -> scale to mm ->
  export object.stl (print) + object.glb (web) + turntable-*.png (checks)

Why the base repair: an object filmed sitting on a table is never seen from
below, and clean_splat.py also strips the support surface, so the underside is a
hole. A Gaussian splat has no topology, so this can only be fixed on the MESH.
We orient the object so its largest flat face points down, then cut the ragged
open underside with a horizontal plane and cap it - producing a flat, watertight
base ideal for slicing (and a stable print bed contact). The generated base is
printable but not a faithful scan of the real underside; capture the bottom too
(flip the object, second pass) if you need the true geometry.

SfM has no metric scale, so pass --size-mm N to set the object's real longest
dimension (STL/GLB carry no units but slicers assume mm).

Requires open3d + trimesh + numpy + pillow from the shared venv (setup_env.sh).
Prefers cleaned.ply (run clean_splat.py first); mesh the raw splat with
--file splat.ply.
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
    eprint(f"splat_to_mesh: {msg}")
    sys.exit(code)


def resolve_input(arg, file_opt, home):
    """Return (ply_path, project_dir_or_None). Default: cleaned.ply, then splat.ply."""
    p = Path(arg).expanduser()
    if p.is_file():
        return p, (p.parent if p.parent.name else None)
    for base in (p, home / "projects" / arg):
        if base.is_dir():
            if file_opt:
                cand = base / file_opt
                if cand.is_file():
                    return cand, base
                fail(f"--file {file_opt!r} not found in {base}")
            for f in ("cleaned.ply", "splat.ply"):
                if (base / f).is_file():
                    return base / f, base
            fail(f"no cleaned.ply or splat.ply in {base} (run clean_splat.py / train first)")
    fail(f"could not resolve a splat for {arg!r}")


def quats_to_matrices(q):
    """(N,4) wxyz -> (N,3,3) rotation matrices (columns are the local axes)."""
    w, x, y, z = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    N = len(q)
    R = np.empty((N, 3, 3), dtype=np.float64)
    R[:, 0, 0] = 1 - 2 * (y * y + z * z)
    R[:, 0, 1] = 2 * (x * y - w * z)
    R[:, 0, 2] = 2 * (x * z + w * y)
    R[:, 1, 0] = 2 * (x * y + w * z)
    R[:, 1, 1] = 1 - 2 * (x * x + z * z)
    R[:, 1, 2] = 2 * (y * z - w * x)
    R[:, 2, 0] = 2 * (x * z - w * y)
    R[:, 2, 1] = 2 * (y * z + w * x)
    R[:, 2, 2] = 1 - 2 * (x * x + y * y)
    return R


def densify(splat, k, rng):
    """Sample k oriented surface points per Gaussian.

    Returns (points (M,3), normals (M,3), colors (M,3) in [0,1]).
    """
    centers = splat.xyz()
    scales = splat.scales_linear()                 # (N,3) linear std-devs
    R = quats_to_matrices(splat.quats_wxyz())      # (N,3,3)
    rgb = splat.rgb()                              # (N,3)
    N = len(centers)
    ar = np.arange(N)

    # per-Gaussian axis ranking by scale: smallest = normal, two largest = disk
    order = np.argsort(scales, axis=1)             # ascending
    j_n, j_a, j_b = order[:, 0], order[:, 1], order[:, 2]
    axis_n = R[ar, :, j_n]                          # (N,3) surface normal
    axis_a = R[ar, :, j_a]
    axis_b = R[ar, :, j_b]
    s_a = scales[ar, j_a]
    s_b = scales[ar, j_b]

    pts = [centers]
    nrm = [axis_n]
    col = [rgb]
    for _ in range(max(0, k - 1)):
        # truncated Gaussian offsets on the disk (clip to +/-2 sigma, stay near surface)
        u = np.clip(rng.standard_normal(N), -2, 2) * s_a
        w = np.clip(rng.standard_normal(N), -2, 2) * s_b
        offset = axis_a * u[:, None] + axis_b * w[:, None]
        pts.append(centers + offset)
        nrm.append(axis_n)
        col.append(rgb)

    points = np.concatenate(pts, axis=0)
    normals = np.concatenate(nrm, axis=0)
    colors = np.concatenate(col, axis=0)

    # orient normals to point OUTWARD from the object centroid
    c = centers.mean(axis=0)
    flip = np.einsum("ij,ij->i", normals, points - c) < 0
    normals[flip] *= -1.0
    return points, normals, colors


def find_contact_normal(points, rng, iters=320):
    """Largest flat RESTING face of the point cloud -> its outward unit normal.

    A face the object can rest on is, by definition, a support plane of its
    convex hull - so instead of RANSAC-ing random planes (which can land on a
    diagonal slice or the wrong side, flipping the object), we enumerate convex
    hull facet planes (their normals point outward for free) and score each by:

      support area (in-plane PCA spread of the points near the plane, so a big
      sole ring wins over a thin top ridge) x sqrt(population), requiring the
      centroid to project inside the support region (resting stability).

    Deterministic. Returns the outward normal of the best face (the direction
    that should face the print bed), or None.
    """
    n_pts = len(points)
    if n_pts < 30:
        return None
    lo = points.min(axis=0)
    hi = points.max(axis=0)
    diag = float(np.linalg.norm(hi - lo))
    if diag < 1e-9:
        return None
    thresh = max(diag * 0.012, 1e-4)
    min_inliers = max(40, int(0.02 * n_pts))
    max_beyond = max(20, int(0.02 * n_pts))
    centroid = points.mean(axis=0)

    def score_face(n, d):
        """Score an OUTWARD-oriented candidate plane (n.x + d = 0, inside <= 0)."""
        signed = points @ n + d
        if int((signed > thresh).sum()) > max_beyond:   # not extremal
            return 0.0
        mask = signed >= -thresh                        # support points on the face
        cnt = int(mask.sum())
        if cnt < min_inliers:
            return 0.0
        ref = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
        t = np.cross(ref, n)
        tn = np.linalg.norm(t)
        if tn < 1e-9:
            return 0.0
        t /= tn
        b = np.cross(n, t)
        P = points[mask]
        u = P @ t
        v = P @ b
        su, sv = float(u.std()), float(v.std())
        area = (4.0 * su) * (4.0 * sv)                  # PCA-ish support footprint
        if area <= 0:
            return 0.0
        # stability: center of mass must project inside the support spread
        if abs(float(centroid @ t) - float(u.mean())) > 2.2 * su:
            return 0.0
        if abs(float(centroid @ b) - float(v.mean())) > 2.2 * sv:
            return 0.0
        return area * float(np.sqrt(cnt))

    # candidate planes: convex hull facets (deduped) + AABB faces as backstop
    cands = []
    try:
        from scipy.spatial import ConvexHull
        sub = points if n_pts <= 12000 else \
            points[rng.choice(n_pts, 12000, replace=False)]
        hull = ConvexHull(sub)
        seen = set()
        for eq in hull.equations:                       # n.x + d = 0, n outward
            n = eq[:3]
            d = float(eq[3])
            key = (round(n[0], 1), round(n[1], 1), round(n[2], 1),
                   round(d / diag, 2))
            if key in seen:
                continue
            seen.add(key)
            cands.append((np.asarray(n, float), d))
    except Exception as e:
        eprint(f"[mesh] convex hull failed ({e}); using AABB faces only")
    for idx in range(3):
        for sign, face in ((1.0, hi[idx]), (-1.0, lo[idx])):
            n = np.zeros(3)
            n[idx] = sign
            cands.append((n, float(-sign * face)))

    best = None
    for n, d in cands:
        s = score_face(n, d)
        if s > 0 and (best is None or s > best[0]):
            best = (s, n)
    if best is None:
        return None
    n = best[1]
    return n / (np.linalg.norm(n) or 1.0)


def orient_base_down(mesh, contact_normal, T):
    """Rotate `mesh` so contact_normal -> world -Z, then drop min Z to 0.

    Mutates `mesh` and returns the updated original->current transform (4x4),
    composed onto `T`.
    """
    import trimesh

    M = trimesh.geometry.align_vectors(contact_normal, np.array([0.0, 0.0, -1.0]))
    mesh.apply_transform(M)
    T = M @ T
    dz = -float(mesh.bounds[0][2])
    Tr = trimesh.transformations.translation_matrix([0.0, 0.0, dz])
    mesh.apply_transform(Tr)
    T = Tr @ T
    return T


def keep_largest_component(mesh):
    parts = mesh.split(only_watertight=False)
    if len(parts) > 1:
        parts = sorted(parts, key=lambda m: len(m.faces), reverse=True)
        return parts[0]
    return mesh


def _poisson_worker(in_npz, out_npz, depth, jitter, seed):
    """Run Poisson in a child process (open3d's PoissonRecon can hard-abort the
    whole process on a 'Failed to close loop' iso-surface bug, so we isolate it).
    Writes vertices/faces/densities to out_npz on success; writes nothing on abort.
    """
    import numpy as np
    import open3d as o3d
    d = np.load(in_npz)
    points = d["points"].astype(np.float64)
    normals = d["normals"].astype(np.float64)
    if jitter > 0:
        points = points + np.random.default_rng(seed).normal(0.0, jitter, points.shape)
    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    pcd.normals = o3d.utility.Vector3dVector(normals)
    with o3d.utility.VerbosityContextManager(o3d.utility.VerbosityLevel.Error):
        mesh, dens = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
            pcd, depth=depth, linear_fit=False)
    v = np.asarray(mesh.vertices)
    f = np.asarray(mesh.triangles)
    if len(f) == 0:
        return
    np.savez(out_npz, v=v, f=f, d=np.asarray(dens))


def poisson_reconstruct(points, normals, depth, attempts=5):
    """Poisson surface reconstruction, isolated + retried.

    open3d's bundled PoissonRecon intermittently aborts the process on certain
    octree configs ('Failed to close loop'). We run it in a spawned subprocess so
    an abort can't kill us, and retry with a tiny point jitter (and a slightly
    lower depth as a last resort) until it succeeds. Returns (V, F, densities).
    """
    import multiprocessing as mp
    import tempfile

    diag = float(np.linalg.norm(points.max(0) - points.min(0))) or 1.0
    ctx = mp.get_context("spawn")
    tmpdir = tempfile.mkdtemp(prefix="o3d_poisson_")
    in_npz = os.path.join(tmpdir, "in.npz")
    np.savez(in_npz, points=points.astype(np.float64), normals=normals.astype(np.float64))

    try:
        for k in range(attempts):
            jitter = 0.0 if k == 0 else diag * 2e-4 * k
            d = depth if k < attempts - 1 else max(6, depth - 1)
            out_npz = os.path.join(tmpdir, f"out{k}.npz")
            p = ctx.Process(target=_poisson_worker,
                            args=(in_npz, out_npz, d, jitter, k))
            p.start()
            p.join()
            if os.path.exists(out_npz):
                with np.load(out_npz) as r:
                    return r["v"], r["f"], r["d"]
            if k == 0:
                eprint("[mesh] Poisson aborted (open3d 'Failed to close loop'); "
                       "retrying with jittered input ...")
            else:
                eprint(f"[mesh] Poisson retry {k} (jitter {jitter:.2g}"
                       f"{', depth ' + str(d) if d != depth else ''}) ...")
        return None, None, None
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def _rezero_base(mesh, T):
    """Translate so the mesh sits on z=0; compose the move into transform T."""
    import trimesh
    dz = -float(mesh.bounds[0][2])
    Tr = trimesh.transformations.translation_matrix([0.0, 0.0, dz])
    mesh.apply_transform(Tr)
    return Tr @ T


def voxel_solidify(mesh, res, close_iters=2):
    """Shrink-wrap the mesh into a guaranteed-watertight manifold solid.

    Poisson output of a scan is often multi-shell / non-manifold and open on the
    unseen underside, so no amount of hole-filling makes it print-safe. We
    rasterize it to a voxel grid, morphologically CLOSE it (dilate+erode, sealing
    through-holes/tunnels in thin walls up to ~2*close_iters voxels wide), fill
    enclosed cavities, and re-extract with marching cubes: the result is always a
    single closed manifold. Detail is limited by the voxel pitch
    (bbox diagonal / res).
    """
    import trimesh
    from scipy import ndimage
    from skimage import measure

    pitch = float(mesh.scale) / max(32, res)
    vg = mesh.voxelized(pitch=pitch)
    mat = np.asarray(vg.matrix, dtype=bool)
    # pad so closing/marching cubes never touch the array border
    pad = int(close_iters) + 2
    mat = np.pad(mat, pad)
    if close_iters > 0:
        st = ndimage.generate_binary_structure(3, 1)
        mat = ndimage.binary_closing(mat, structure=st,
                                     iterations=int(close_iters))
    mat = ndimage.binary_fill_holes(mat)

    verts, faces, _, _ = measure.marching_cubes(mat.astype(np.float32), level=0.5)
    # marching-cubes vertices are in (padded) voxel-index space; unpad and map
    # back to the input mesh's world frame so downstream transforms/colors match.
    verts = verts - pad
    solid = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
    solid.apply_transform(vg.transform)
    solid.merge_vertices()
    trimesh.repair.fix_normals(solid)
    return keep_largest_component(solid)


def flat_cut(mesh, z0):
    """Cut everything below z0 and cap the cross-section (flat base)."""
    import trimesh
    import trimesh.intersections as ti
    sliced = ti.slice_mesh_plane(
        mesh, plane_normal=[0, 0, 1], plane_origin=[0, 0, z0], cap=True)
    if sliced is None or len(sliced.faces) == 0:
        return None
    sliced.merge_vertices()
    sliced = keep_largest_component(sliced)
    trimesh.repair.fix_normals(sliced)
    trimesh.repair.fix_winding(sliced)
    return sliced


def make_printable(mesh, base_mode, base_cut, solidify_mode, voxel_res,
                   close_iters, smooth, require_watertight, T, points=None):
    """Turn the reconstructed surface into a watertight, flat-based print mesh.

    Order: solidify (watertight + tunnel-sealing guarantee) -> smooth
    (de-blockify voxel result) -> flat base cut (stable, flat bed contact).
    Assumes `mesh` is already oriented base-down. `points` are the input surface
    samples in the ORIGINAL frame; when given, the base cut height is anchored to
    the object's real lowest scan data instead of the (possibly ballooned) mesh
    bottom. Returns (mesh, watertight, note, T).
    """
    import trimesh

    if base_mode == "none":
        if smooth > 0:
            try:
                import open3d as o3d
                om = o3d.geometry.TriangleMesh(
                    o3d.utility.Vector3dVector(np.asarray(mesh.vertices)),
                    o3d.utility.Vector3iVector(np.asarray(mesh.faces)))
                om = om.filter_smooth_taubin(number_of_iterations=smooth)
                mesh = trimesh.Trimesh(np.asarray(om.vertices),
                                       np.asarray(om.triangles), process=True)
                trimesh.repair.fix_normals(mesh)
            except Exception as e:
                eprint(f"[mesh] smooth skipped ({e})")
        return mesh, bool(mesh.is_watertight), "none (left as reconstructed)", T

    T = _rezero_base(mesh, T)
    notes = []

    # 1. watertight guarantee via voxel remesh (all holes/shells closed at once)
    did_solidify = False
    genus = None
    if mesh.is_watertight:
        genus = int(round((2 - mesh.euler_number) / 2))
    # A watertight surface can still be riddled with through-tunnels (genus > 0)
    # that read as "holes" and weaken the print, so 'auto' also solidifies then.
    do_solid = solidify_mode == "always" or (
        solidify_mode == "auto" and (not mesh.is_watertight or (genus or 0) > 0))
    if do_solid:
        try:
            solid = voxel_solidify(mesh, voxel_res, close_iters)
            if len(solid.faces) > 0:
                mesh = solid
                T = _rezero_base(mesh, T)
                did_solidify = True
                notes.append(f"voxel solidify (res {voxel_res}, close {close_iters})")
        except Exception as e:
            notes.append(f"solidify skipped ({e})")

    # 2. de-blockify the voxel surface a little (Taubin preserves volume/topology)
    eff_smooth = smooth if smooth > 0 else (3 if did_solidify else 0)
    if eff_smooth > 0:
        try:
            import open3d as o3d
            om = o3d.geometry.TriangleMesh(
                o3d.utility.Vector3dVector(np.asarray(mesh.vertices)),
                o3d.utility.Vector3iVector(np.asarray(mesh.faces)))
            om = om.filter_smooth_taubin(number_of_iterations=eff_smooth)
            mesh = trimesh.Trimesh(np.asarray(om.vertices),
                                   np.asarray(om.triangles), process=True)
            trimesh.repair.fix_normals(mesh)
            T = _rezero_base(mesh, T)
            notes.append(f"taubin x{eff_smooth}")
        except Exception as e:
            notes.append(f"smooth skipped ({e})")

    # 3. flat base cut for a stable, printable bed contact.
    #    Anchor the cut to the REAL scan data: Poisson can balloon below the
    #    object where nothing was seen, and cutting a % of the inflated mesh
    #    height would leave that balloon as a fake pedestal.
    height = float(mesh.bounds[1][2] - mesh.bounds[0][2])
    if height > 0:
        if points is not None and len(points):
            pz = (T[:3, :3] @ points.T).T[:, 2] + T[2, 3]
            z_low = float(np.percentile(pz, 1.0))
            z_span = float(np.percentile(pz, 99.0) - z_low) or height
            z0 = z_low + max(base_cut, 1e-4) * z_span
            z0 = min(max(z0, float(mesh.bounds[0][2])),
                     float(mesh.bounds[0][2]) + 0.45 * height)
        else:
            z0 = max(base_cut, 1e-4) * height
        cut = flat_cut(mesh, z0)
        if cut is not None and len(cut.faces) > 0 and \
                (cut.is_watertight or not require_watertight):
            mesh = cut
            T = _rezero_base(mesh, T)
            notes.append(f"flat base @ {base_cut * 100:.1f}%")
        else:
            notes.append("flat base cut skipped (would open the mesh)")

    return mesh, bool(mesh.is_watertight), "; ".join(notes) or "no-op", T


def software_turntable(vertices, faces, vcolors, out_dir, base_name, up, n_frames=4,
                       size=640):
    """Tiny dependency-free painter's-algorithm renderer for verification thumbs.

    Deliberately NOT a GPU/offscreen-GL path (fragile headless). Best-effort:
    any failure is caught by the caller and skipped.
    """
    from PIL import Image, ImageDraw

    V = vertices.astype(np.float64)
    F = faces.astype(np.int64)
    c = V.mean(axis=0)
    radius = float(np.linalg.norm(V - c, axis=1).max()) or 1.0

    up = np.asarray(up, dtype=np.float64)
    up = up / (np.linalg.norm(up) or 1.0)
    ref = np.array([1.0, 0.0, 0.0])
    if abs(float(up @ ref)) > 0.9:
        ref = np.array([0.0, 1.0, 0.0])
    ex = np.cross(up, ref); ex /= (np.linalg.norm(ex) or 1)
    ez = np.cross(up, ex)
    el = np.radians(20.0)

    # per-face flat color (mean of its vertex colors) and geometry
    if vcolors is not None and len(vcolors) == len(V):
        face_rgb = vcolors[F].mean(axis=1)         # (F,3) in [0,1]
    else:
        face_rgb = np.full((len(F), 3), 0.72)

    written = []
    for fi in range(n_frames):
        a = 2 * np.pi * fi / n_frames + 0.6
        horiz = np.cos(a) * ex + np.sin(a) * ez
        viewdir = horiz * np.cos(el) + up * np.sin(el)
        viewdir /= (np.linalg.norm(viewdir) or 1)
        cam = c + viewdir * radius * 3.0
        forward = -viewdir
        right = np.cross(forward, up); right /= (np.linalg.norm(right) or 1)
        camup = np.cross(right, forward)
        light = viewdir + up * 0.3
        light /= (np.linalg.norm(light) or 1)

        rel = V - cam
        xc = rel @ right
        yc = rel @ camup
        zc = rel @ forward                          # depth, larger = farther
        scale = (size * 0.42) / radius
        sx = size / 2 + xc * scale
        sy = size / 2 - yc * scale

        # face normals (world space) + backface-friendly Lambert shading
        v0 = V[F[:, 0]]; v1 = V[F[:, 1]]; v2 = V[F[:, 2]]
        fn = np.cross(v1 - v0, v2 - v0)
        ln = np.linalg.norm(fn, axis=1, keepdims=True); ln[ln == 0] = 1
        fn = fn / ln
        shade = 0.25 + 0.75 * np.abs(fn @ light)
        depth = zc[F].mean(axis=1)

        img = Image.new("RGB", (size, size), (12, 12, 14))
        d = ImageDraw.Draw(img)
        for t in np.argsort(depth)[::-1]:           # far -> near
            tri = F[t]
            poly = [(sx[tri[0]], sy[tri[0]]), (sx[tri[1]], sy[tri[1]]),
                    (sx[tri[2]], sy[tri[2]])]
            col = np.clip(face_rgb[t] * shade[t], 0, 1) * 255
            d.polygon(poly, fill=(int(col[0]), int(col[1]), int(col[2])))
        path = out_dir / f"{base_name}-{fi:02d}.png"
        img.save(path)
        written.append(path)
    return written


def main(argv=None):
    p = argparse.ArgumentParser(prog="splat_to_mesh", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("project", help="project name/dir, or a .ply path")
    p.add_argument("--file", default=None,
                   help="splat file within the project (default: cleaned.ply then splat.ply)")
    p.add_argument("--out-dir", default=None, dest="out_dir",
                   help="output dir (default <project>/mesh)")
    p.add_argument("--name", default="object", help="output base name (default 'object')")
    p.add_argument("--size-mm", type=float, default=100.0, dest="size_mm",
                   help="scale longest dimension to N millimeters (default 100; "
                        "set the object's real size for a correct print). 0 = keep scene units")
    p.add_argument("--depth", type=int, default=9,
                   help="Poisson octree depth (default 9; 8 smoother, 10+ finer/noisier)")
    p.add_argument("--density-quantile", type=float, default=None, dest="density_quantile",
                   help="trim this low-density fraction of Poisson vertices "
                        "(default: 0 when base repair is active - trimming punches "
                        "holes in Poisson's closed surface, and the solidify + "
                        "data-anchored base cut handle balloons instead; 0.03 "
                        "with --base-repair none)")
    p.add_argument("--samples-per-splat", type=int, default=4, dest="spp",
                   help="oriented surface samples per Gaussian when densifying (default 4)")
    p.add_argument("--no-densify", action="store_true", dest="no_densify",
                   help="use Gaussian centers only (faster, coarser)")
    p.add_argument("--smooth", type=int, default=0,
                   help="Taubin smoothing iterations on the final mesh (default 0)")
    p.add_argument("--base-repair", default="auto", dest="base_repair",
                   choices=("auto", "flat", "none"),
                   help="make the object printable: 'auto'/'flat' orient the "
                        "largest flat face down, solidify to watertight, and cut a "
                        "flat print base; 'none' leaves the reconstruction as-is "
                        "(default auto)")
    p.add_argument("--base-cut", type=float, default=0.03, dest="base_cut",
                   help="how far up from the bottom to cut the flat base, as a "
                        "fraction of object height (default 0.03). Larger removes "
                        "more of the ragged underside.")
    p.add_argument("--solidify", default="auto", choices=("auto", "always", "none"),
                   help="voxel-remesh into a guaranteed-watertight solid: 'auto' "
                        "when the reconstruction is not watertight OR still has "
                        "through-tunnels (genus > 0), 'always' unconditionally, "
                        "'none' never (default auto). This is what closes the "
                        "unseen underside and seals hole-like tunnels for printing.")
    p.add_argument("--voxel", type=int, default=200, dest="voxel_res",
                   help="voxel resolution for --solidify (voxels across the bbox "
                        "diagonal; default 200). Higher = more detail, slower.")
    p.add_argument("--close", type=int, default=2, dest="close_iters",
                   help="morphological closing iterations during --solidify; "
                        "seals through-holes/tunnels up to ~2*N voxels wide "
                        "(default 2). Raise if the report shows genus > 0.")
    p.add_argument("--allow-open", action="store_false", dest="require_watertight",
                   help="export the STL even if the mesh is not watertight "
                        "(default: refuse and exit non-zero, since it may not slice)")
    p.add_argument("--no-turntable", action="store_true", dest="no_turntable",
                   help="skip the turntable PNG renders")
    p.add_argument("--home", default=None, help="override VIDEO_TO_SPLAT_HOME")
    args = p.parse_args(argv)

    # Poisson always closes the surface; density-trimming punches holes in it.
    # With base repair active we keep the closed surface (solidify + the
    # data-anchored base cut deal with balloons), so trim defaults to 0 there.
    if args.density_quantile is None:
        args.density_quantile = 0.0 if args.base_repair != "none" else 0.03

    try:
        import open3d as o3d
    except Exception as e:
        fail(f"could not import open3d ({e}). Run setup_env.sh first.")
    try:
        import trimesh
    except Exception as e:
        fail(f"could not import trimesh ({e}). Run setup_env.sh first.")

    home = Path(args.home).expanduser() if args.home else \
        Path(os.environ.get("VIDEO_TO_SPLAT_HOME", Path.home() / ".video-to-splat"))
    ply_path, proj = resolve_input(args.project, args.file, home)
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else \
        ((proj / "mesh") if proj else ply_path.parent / "mesh")
    out_dir.mkdir(parents=True, exist_ok=True)

    eprint(f"[mesh] input : {ply_path}")
    splat = sp.read_ply(ply_path)
    if len(splat) == 0:
        fail("splat has no Gaussians")
    for req in ("x", "y", "z", "scale_0", "rot_0", "f_dc_0"):
        if not splat.has(req):
            fail(f"splat PLY missing property {req!r}; is this a 3DGS export?")
    eprint(f"[mesh] gaussians: {len(splat)}")
    if ply_path.name == "splat.ply":
        eprint("[mesh] NOTE: meshing the RAW splat. For a clean object, run "
               "clean_splat.py first and mesh cleaned.ply.")

    rng = np.random.default_rng(0)

    # 1. oriented point cloud (densified surface samples, or bare centers)
    if args.no_densify:
        points = splat.xyz()
        colors = splat.rgb()
        pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
        pcd.colors = o3d.utility.Vector3dVector(colors)
        diag = float(np.linalg.norm(points.max(0) - points.min(0)))
        pcd.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(
            radius=max(1e-6, 0.02 * diag), max_nn=30))
        pcd.orient_normals_towards_camera_location(points.mean(0))
        pcd.normals = o3d.utility.Vector3dVector(-np.asarray(pcd.normals))  # -> outward
        eprint(f"[mesh] point cloud: {len(points)} centers (no densify), "
               f"normals estimated")
    else:
        points, normals, colors = densify(splat, args.spp, rng)
        pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
        pcd.normals = o3d.utility.Vector3dVector(normals)
        pcd.colors = o3d.utility.Vector3dVector(colors)
        eprint(f"[mesh] point cloud: {len(points)} oriented samples "
               f"({args.spp}/gaussian)")

    # 2. Poisson reconstruction (subprocess-isolated + retried; see helper)
    eprint(f"[mesh] Poisson reconstruction (depth {args.depth}) ...")
    normals = np.asarray(pcd.normals)
    pv, pf, densities = poisson_reconstruct(points, normals, args.depth)
    if pv is None:
        fail("Poisson failed repeatedly (open3d 'Failed to close loop'). Try a "
             "lower --depth, a cleaner cleaned.ply, or fewer/denser points.", code=3)
    mesh = o3d.geometry.TriangleMesh(
        o3d.utility.Vector3dVector(pv), o3d.utility.Vector3iVector(pf))
    densities = np.asarray(densities)
    if len(mesh.triangles) == 0:
        fail("Poisson produced an empty mesh (too few/!oriented points). Try "
             "--no-densify off, a cleaner splat, or a lower --depth.")

    # 3. trim the low-density (extrapolated) fringe
    if 0 < args.density_quantile < 1 and len(densities):
        thr = np.quantile(densities, args.density_quantile)
        mesh.remove_vertices_by_mask(densities < thr)
        eprint(f"[mesh] trimmed vertices below density quantile "
               f"{args.density_quantile:g}")

    # 4. keep the largest connected component
    tri_labels, tri_counts, _ = mesh.cluster_connected_triangles()
    tri_labels = np.asarray(tri_labels)
    tri_counts = np.asarray(tri_counts)
    if len(tri_counts) > 1:
        biggest = int(np.argmax(tri_counts))
        mesh.remove_triangles_by_mask(tri_labels != biggest)
        mesh.remove_unreferenced_vertices()
        eprint(f"[mesh] kept largest of {len(tri_counts)} components "
               f"({tri_counts[biggest]} triangles)")

    V = np.asarray(mesh.vertices)
    Fc = np.asarray(mesh.triangles)
    if len(V) == 0 or len(Fc) == 0:
        fail("mesh empty after cleanup; relax --density-quantile or improve the splat")

    # 5. trimesh repair + initial hole fill
    tm = trimesh.Trimesh(vertices=V, faces=Fc, process=True)
    tm.update_faces(tm.nondegenerate_faces())
    tm.remove_unreferenced_vertices()
    trimesh.repair.fix_normals(tm)
    trimesh.repair.fix_winding(tm)
    if not tm.is_watertight:
        trimesh.repair.fill_holes(tm)
        trimesh.repair.fix_normals(tm)

    # 6. orient base-down + flat base repair (track original->current transform T)
    T = np.eye(4)
    orient_up = np.array([0.0, -1.0, 0.0])   # legacy: COLMAP/OpenCV up is -Y
    base_note = "none"
    watertight = bool(tm.is_watertight)
    if args.base_repair != "none":
        contact_pts = points if len(points) <= 60000 else \
            points[rng.choice(len(points), 60000, replace=False)]
        n = find_contact_normal(contact_pts, rng)
        if n is None:
            eprint("[mesh] orient    : could not find a contact face; skipping orient")
        else:
            T = orient_base_down(tm, n, T)
            orient_up = np.array([0.0, 0.0, 1.0])   # now +Z up, base at z=0
            eprint(f"[mesh] orient    : contact normal {np.round(n, 3)} -> base down (+Z up)")
    tm, watertight, base_note, T = make_printable(
        tm, args.base_repair, args.base_cut, args.solidify, args.voxel_res,
        args.close_iters, args.smooth, args.require_watertight, T,
        points=points)
    eprint(f"[mesh] base      : {base_note}")

    V = np.asarray(tm.vertices)
    Fc = np.asarray(tm.faces)
    if len(V) == 0 or len(Fc) == 0:
        fail("mesh empty after base repair; try --base-repair none or --base-cut smaller")

    # 7. sample vertex colors from the nearest Gaussian, in the ORIGINAL frame
    #    (undo the orient/base transform T so the KD-tree matches splat.xyz()).
    try:
        Tinv = np.linalg.inv(T)
        verts_orig = (Tinv[:3, :3] @ tm.vertices.T).T + Tinv[:3, 3]
        kdt = o3d.geometry.KDTreeFlann(
            o3d.geometry.PointCloud(o3d.utility.Vector3dVector(splat.xyz())))
        srgb = splat.rgb()
        vcols = np.empty((len(tm.vertices), 3), dtype=np.float64)
        for i, vert in enumerate(verts_orig):
            _, ni, _ = kdt.search_knn_vector_3d(vert, 1)
            vcols[i] = srgb[ni[0]]
    except Exception as e:
        eprint(f"[mesh] color sampling skipped ({e})")
        vcols = None

    # 8. scale so the longest dimension = size_mm
    extents = tm.extents.copy()
    longest = float(extents.max())
    if args.size_mm and args.size_mm > 0 and longest > 0:
        factor = args.size_mm / longest
        tm.apply_scale(factor)
        eprint(f"[mesh] scaled x{factor:.5g} so longest dim = {args.size_mm:g} mm")
    else:
        eprint("[mesh] NOTE: not scaled (scene units, arbitrary). Pass --size-mm N "
               "for a print-ready size.")
    ext_mm = tm.extents

    # 9. watertight gate
    unit = "mm" if (args.size_mm and args.size_mm > 0) else "scene units"
    if not watertight and args.require_watertight:
        eprint("[mesh] --------------------------------------------------")
        eprint("[mesh] FAILURE: mesh is NOT watertight, so it may not slice/print "
               "reliably.")
        eprint("[mesh]   - try a fuller capture (especially the underside),")
        eprint("[mesh]   - or --base-repair flat with a larger --base-cut,")
        eprint("[mesh]   - or --base-repair auto (default) on a cleaner cleaned.ply,")
        eprint("[mesh]   - or pass --allow-open to export anyway (not recommended).")
        # still write a GLB for inspection so the user can see what happened
        glb_path = out_dir / f"{args.name}.glb"
        glb_mesh = tm.copy()
        if vcols is not None:
            rgba = np.concatenate([np.clip(vcols, 0, 1),
                                   np.ones((len(vcols), 1))], axis=1)
            glb_mesh.visual = trimesh.visual.color.ColorVisuals(
                mesh=glb_mesh, vertex_colors=(rgba * 255).astype(np.uint8))
        glb_mesh.export(glb_path)
        eprint(f"[mesh] wrote inspection GLB (no STL): {glb_path}")
        sys.exit(2)

    # 10. export STL (geometry) + GLB (colored)
    stl_path = out_dir / f"{args.name}.stl"
    glb_path = out_dir / f"{args.name}.glb"
    tm.export(stl_path)
    glb_mesh = tm.copy()
    if vcols is not None:
        rgba = np.concatenate([np.clip(vcols, 0, 1),
                               np.ones((len(vcols), 1))], axis=1)
        glb_mesh.visual = trimesh.visual.color.ColorVisuals(
            mesh=glb_mesh, vertex_colors=(rgba * 255).astype(np.uint8))
    glb_mesh.export(glb_path)

    # 11. turntable verification renders (best-effort, software rasterizer)
    thumbs = []
    if not args.no_turntable:
        try:
            render_mesh = tm
            if len(tm.faces) > 15000:
                rm = o3d.geometry.TriangleMesh(
                    o3d.utility.Vector3dVector(tm.vertices),
                    o3d.utility.Vector3iVector(tm.faces))
                rm = rm.simplify_quadric_decimation(15000)
                render_mesh = trimesh.Trimesh(np.asarray(rm.vertices),
                                              np.asarray(rm.triangles), process=False)
                rv = None
            else:
                rv = vcols
            rv_use = rv if (rv is not None and len(rv) == len(render_mesh.vertices)) else None
            thumbs = software_turntable(render_mesh.vertices, render_mesh.faces,
                                        rv_use, out_dir, f"{args.name}-turntable",
                                        up=orient_up)
        except Exception as e:
            eprint(f"[mesh] turntable render skipped ({e})")

    # report
    genus = int(round((2 - tm.euler_number) / 2)) if watertight else None
    eprint("[mesh] --------------------------------------------------")
    eprint(f"[mesh] watertight : {watertight}")
    if genus is not None:
        eprint(f"[mesh] genus      : {genus} (0 = no through-holes/tunnels)")
        if genus > 0:
            eprint(f"[mesh] WARNING: surface still has ~{genus} through-tunnel(s) "
                   "that show as holes; raise --close (e.g. 4) or lower --voxel, "
                   "or capture the missing angles.")
    eprint(f"[mesh] base       : {base_note}")
    eprint(f"[mesh] vertices   : {len(tm.vertices)}   faces: {len(tm.faces)}")
    eprint(f"[mesh] dimensions : {ext_mm[0]:.2f} x {ext_mm[1]:.2f} x {ext_mm[2]:.2f} {unit}")
    if watertight:
        vol = abs(tm.volume)
        vunit = "mm^3" if unit == "mm" else "units^3"
        eprint(f"[mesh] volume     : {vol:.2f} {vunit}")
    else:
        eprint("[mesh] WARNING: mesh is NOT watertight (exported due to --allow-open). "
               "It may fail to slice; fill holes in your slicer or recapture.")
    eprint(f"[mesh] STL : {stl_path}")
    eprint(f"[mesh] GLB : {glb_path}")
    for t in thumbs:
        eprint(f"[mesh] png : {t}")
    # stdout: the STL path (the print deliverable)
    print(stl_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
