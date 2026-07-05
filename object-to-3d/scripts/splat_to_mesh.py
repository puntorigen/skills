#!/usr/bin/env python3
"""
object-to-3d step 5 - turn a (cleaned) object splat into a printable mesh.

Screened Poisson surface reconstruction needs a dense, oriented point cloud.
Gaussian centers alone are too sparse with poor normals, so we first DENSIFY:
each Gaussian is a flat-ish ellipsoid, so we sample points on the disk spanned by
its two largest axes and take the smallest axis as the surface normal. Then:

  densify -> orient normals outward -> Poisson -> trim low-density fringe ->
  largest connected component -> fill holes / watertight check -> scale to mm ->
  export object.stl (print) + object.glb (web) + turntable-*.png (checks)

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


def software_turntable(vertices, faces, vcolors, out_dir, base_name, n_frames=4,
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

    up = np.array([0.0, -1.0, 0.0])                # COLMAP/OpenCV up is -Y
    ref = np.array([1.0, 0.0, 0.0])
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
    p.add_argument("--density-quantile", type=float, default=0.03, dest="density_quantile",
                   help="trim this low-density fraction of Poisson vertices (default 0.03)")
    p.add_argument("--samples-per-splat", type=int, default=4, dest="spp",
                   help="oriented surface samples per Gaussian when densifying (default 4)")
    p.add_argument("--no-densify", action="store_true", dest="no_densify",
                   help="use Gaussian centers only (faster, coarser)")
    p.add_argument("--smooth", type=int, default=0,
                   help="Taubin smoothing iterations on the final mesh (default 0)")
    p.add_argument("--no-turntable", action="store_true", dest="no_turntable",
                   help="skip the turntable PNG renders")
    p.add_argument("--home", default=None, help="override VIDEO_TO_SPLAT_HOME")
    args = p.parse_args(argv)

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

    # 2. Poisson reconstruction
    eprint(f"[mesh] Poisson reconstruction (depth {args.depth}) ...")
    with o3d.utility.VerbosityContextManager(o3d.utility.VerbosityLevel.Error):
        mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
            pcd, depth=args.depth, linear_fit=False)
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

    if args.smooth > 0:
        mesh = mesh.filter_smooth_taubin(number_of_iterations=args.smooth)
        mesh.compute_vertex_normals()

    V = np.asarray(mesh.vertices)
    Fc = np.asarray(mesh.triangles)
    if len(V) == 0 or len(Fc) == 0:
        fail("mesh empty after cleanup; relax --density-quantile or improve the splat")

    # 5. trimesh repair + watertight check
    tm = trimesh.Trimesh(vertices=V, faces=Fc, process=True)
    tm.update_faces(tm.nondegenerate_faces())
    tm.remove_unreferenced_vertices()
    trimesh.repair.fix_normals(tm)
    trimesh.repair.fix_winding(tm)
    if not tm.is_watertight:
        trimesh.repair.fill_holes(tm)
        trimesh.repair.fix_normals(tm)
    watertight = bool(tm.is_watertight)

    # 6. sample vertex colors from the nearest Gaussian (robust to repair changes)
    try:
        kdt = o3d.geometry.KDTreeFlann(
            o3d.geometry.PointCloud(o3d.utility.Vector3dVector(splat.xyz())))
        srgb = splat.rgb()
        vcols = np.empty((len(tm.vertices), 3), dtype=np.float64)
        for i, vert in enumerate(tm.vertices):
            _, ni, _ = kdt.search_knn_vector_3d(vert, 1)
            vcols[i] = srgb[ni[0]]
    except Exception:
        vcols = None

    # 7. scale so the longest dimension = size_mm
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

    # 8. export STL (geometry) + GLB (colored)
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

    # 9. turntable verification renders (best-effort, software rasterizer)
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
                                        rv_use, out_dir, f"{args.name}-turntable")
        except Exception as e:
            eprint(f"[mesh] turntable render skipped ({e})")

    # report
    eprint("[mesh] --------------------------------------------------")
    eprint(f"[mesh] watertight : {watertight}")
    eprint(f"[mesh] vertices   : {len(tm.vertices)}   faces: {len(tm.faces)}")
    unit = "mm" if (args.size_mm and args.size_mm > 0) else "scene units"
    eprint(f"[mesh] dimensions : {ext_mm[0]:.2f} x {ext_mm[1]:.2f} x {ext_mm[2]:.2f} {unit}")
    if watertight:
        vol = abs(tm.volume)
        vunit = "mm^3" if unit == "mm" else "units^3"
        eprint(f"[mesh] volume     : {vol:.2f} {vunit}")
    else:
        eprint("[mesh] WARNING: mesh is NOT watertight - hole-filling could not close "
               "it. Try a lower --density-quantile, higher --depth, or a fuller capture.")
    eprint(f"[mesh] STL : {stl_path}")
    eprint(f"[mesh] GLB : {glb_path}")
    for t in thumbs:
        eprint(f"[mesh] png : {t}")
    # stdout: the STL path (the print deliverable)
    print(stl_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
