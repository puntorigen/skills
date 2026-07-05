# object-to-3d - Reference

Deeper notes on the tools, formats, flags, and failure modes behind the
[SKILL.md](SKILL.md) workflow. The capture → splat half is shared with
video-to-splat; see [../video-to-splat/REFERENCE.md](../video-to-splat/REFERENCE.md)
for the Brush CLI, pycolmap internals, and the fast-motion SfM playbook. This
file focuses on the two object-specific stages: **splat cleanup** and **mesh
extraction**.

## Pipeline stages and tools

| Stage | Tool | What it does | License |
|-------|------|--------------|---------|
| Frames | ffmpeg + variance-of-Laplacian scoring | Extract sharp, spread, non-duplicate views | ffmpeg: LGPL/GPL |
| Poses (SfM) | [pycolmap](https://github.com/colmap/pycolmap) 4.x | SIFT → matching (exhaustive) → incremental mapping; camera poses + sparse cloud | BSD |
| Training | [Brush](https://github.com/ArthurBrussee/brush) v0.3.0 | 3DGS training on the Apple GPU (WebGPU/Metal); exports standard 3DGS `.ply` | Apache-2.0 |
| Cleanup | [open3d](https://www.open3d.org/) (numpy PLY parser + RANSAC + DBSCAN) | Isolate the object: opacity/scale filter, plane removal, clustering | MIT |
| Mesh | open3d Poisson + [trimesh](https://trimesh.org/) | Densify → normals → Poisson → watertight repair → STL/GLB | open3d MIT, trimesh MIT |
| Preview | [three.js](https://threejs.org/) + [@mkkellogg/gaussian-splats-3d](https://github.com/mkkellogg/GaussianSplats3D) | Browser 3DGS renderer (orbit controls, studio pedestal) | MIT |

All are open source and commercial-safe. The stages are decoupled by files on
disk, so any one can be swapped.

## Data layout

Shared with video-to-splat under `~/.video-to-splat/` (override with
`VIDEO_TO_SPLAT_HOME`). A splat trained by either skill can be cleaned/meshed by
this one.

```
~/.video-to-splat/
  .venv/                       # uv venv: pycolmap, opencv, numpy, pillow, open3d, trimesh
  brush/brush_app              # Brush training binary (macOS arm64)
  viewer-object/               # vite + three.js + gaussian-splats-3d (orbit) + public/
  projects/<name>/
    images/frame-0001.jpg ...  # selected frames (extract_frames.py)
    frames.json                # selection manifest
    database.db                # COLMAP feature/match DB
    sparse/0/                  # COLMAP model: cameras.bin, images.bin, points3D.bin
    splat.ply                  # trained splat (Brush export, lossless master)
    cleaned.ply                # object-only splat (clean_splat.py)
    mesh/
      object.stl               # watertight, mm-scaled mesh (print)
      object.glb               # colored mesh (web / QuickLook)
      turntable-*.png          # offscreen render checks
```

video-to-splat uses a separate `viewer/` (first-person walkthrough); this skill
uses `viewer-object/` (orbit) so the two don't clobber each other. Both share
the venv, Brush and `projects/`.

## The Brush PLY format (what the parsers read/write)

Brush exports a standard 3DGS binary PLY. `scripts/_splat_ply.py` parses it with
a numpy structured dtype built from the header, so it is robust to property
**order** (do not assume `x/y/z` come first - Brush emits SH first). Per Gaussian
(SH degree 2 → 38 float32 properties):

| Property | Count | Meaning |
|----------|-------|---------|
| `f_dc_0..2` | 3 | DC spherical-harmonic color; `rgb = 0.5 + 0.2820948 * f_dc`, clamped [0,1] |
| `f_rest_0..N` | 3·((d+1)²−1) | Higher-order SH (view-dependent); ignored by cleanup/mesh |
| `opacity` | 1 | Logit; rendered opacity = `sigmoid(opacity)` |
| `rot_0..3` | 4 | Rotation quaternion `(w, x, y, z)`, normalize before use |
| `scale_0..2` | 3 | Log std-devs; linear axis length = `exp(scale)` |
| `x, y, z` | 3 | Gaussian center (world/COLMAP frame) |

`_splat_ply.py` preserves **all** properties on write (structured array sliced by
a keep-mask), so `cleaned.ply` remains a fully valid, colored, previewable splat.

## `clean_splat.py` - isolating the object

The goal is to keep only the Gaussians that belong to the object. Filters apply
in order; each prints how many Gaussians it removed so you can see which one is
doing the work.

1. **Opacity** (`--min-opacity`, default 0.4). 3DGS training leaves a haze of
   near-transparent floaters around a scene. `sigmoid(opacity)` is the rendered
   alpha; dropping the low end removes haze cheaply without touching solid
   surfaces. Raise toward 0.6 if faint background remains; lower if the object
   looks eaten.
2. **Scale** (`--scale-pctl`, default 98). Diffuse background/sky is modeled by a
   handful of **huge** Gaussians. Using `max(exp(scale_xyz))` per Gaussian and
   dropping the top percentile removes those blobs while keeping the many small
   Gaussians that form the object's surface. This is a percentile (relative), so
   it adapts to scene scale.
3. **Support plane** (RANSAC, on by default; `--keep-plane` disables). The table
   or floor is a large flat sheet of Gaussians that Poisson would happily wrap
   into the mesh. `open3d.segment_plane` fits the dominant plane; it is removed
   **only if** its inliers form ≥ `--min-frac` (default 0.10) of the points, so a
   plane accidentally fit through the object is not deleted. `--plane-thresh`
   sets the inlier distance (default: 1% of the cloud's bounding-box diagonal).
4. **Clustering** (DBSCAN via `open3d.cluster_dbscan`). After plane removal the
   object is usually one dense blob and the background is detached islands.
   `min_points` (fixed 20) and `--eps` group the centers; the largest
   `--keep-clusters` (default 1) cluster(s) survive. **eps auto-grows**: a fixed
   eps that is a hair too small shatters an object of uneven splat density into
   many pieces and keeps only a sliver, so eps is doubled (from ~3× the median
   nearest-neighbor spacing) until the largest cluster holds `--min-dominant`
   (default 0.5) of the points. Set `--keep-clusters 2+` for a multi-part object,
   or `--no-cluster` for a splat that is already isolated.
5. **Manual crop** (optional). After eyeballing the preview, `--radius R` keeps
   only Gaussians within `R` of `--center` (default: the surviving centroid) -
   the reliable last resort for stubborn stragglers.

Output `cleaned.ply` carries every attribute of the survivors, so
`preview.sh <proj> --file cleaned.ply` renders it identically to the trained
splat. Cleanup is CPU-only and fast (seconds for typical splats).

### Tuning cheatsheet

| Symptom | Try |
|---------|-----|
| Background walls/sky survive | raise `--min-opacity` (0.5-0.6), lower `--scale-pctl` (95) |
| Table/floor still present | ensure plane removal on (drop `--keep-plane`); lower `--min-frac` |
| Object partially deleted | lower `--min-opacity`, raise `--scale-pctl`, add `--keep-plane` |
| Object split into pieces / holes | raise `--eps`, lower `--min-dominant`, or `--keep-clusters 2-3` |
| A few detached bits linger | `--radius` crop around the object center |
| Splat already isolated (no table, e.g. a downloaded object) | `--keep-plane --no-cluster` (a flat base is otherwise caught as the support plane, and clustering is unnecessary) |

## `splat_to_mesh.py` - printable mesh

Screened Poisson surface reconstruction turns an oriented point cloud into a
watertight mesh. Splat centers alone are too sparse and lack good normals, so we
first densify and orient using the Gaussian geometry.

1. **Densify** (`--samples-per-splat`, default 4; `--no-densify` to skip). Each
   Gaussian is a flat-ish ellipsoid. We build its rotation matrix from the
   quaternion, take the two largest `exp(scale)` axes as the local surface disk
   and the **smallest** axis as the surface normal, then draw samples on the disk
   (Gaussian-distributed along the two big axes). This yields a dense,
   near-surface, oriented cloud - far better Poisson input than raw centers.
   Sample count per Gaussian is weighted by rendered opacity.
2. **Normals**. Densify assigns each sample its Gaussian's smallest-axis normal;
   `--no-densify` falls back to `open3d` normal estimation. Either way normals
   are oriented **outward** from the object centroid (an object is seen from
   outside), which is what Poisson needs for a consistent surface.
3. **Poisson** (`--depth`, default 9). `create_from_point_cloud_poisson` builds
   an octree to `depth` and solves for the indicator surface. Deeper = more
   detail but also more spurious geometry in unseen/low-density regions and more
   time/memory. 8 = smooth/robust, 9 = balanced, 10+ = high detail (needs a clean
   dense cloud).
4. **Density trim** (`--density-quantile`, default 0.03). Poisson invents a
   closed surface everywhere, including where it saw no points (e.g. the unseen
   bottom). It returns a per-vertex density; we drop the lowest quantile to cut
   the ballooned low-confidence fringe while keeping the object closed.
5. **Repair + watertight check**. Keep the largest connected component
   (`trimesh.split`), fill holes, fix normals/winding (`trimesh.repair`), then
   report `is_watertight`, volume, and bounding-box dimensions. A watertight
   mesh is required for a reliable slice; a warning is printed if repair can't
   close it (usually means a too-aggressive density trim or a very holey splat).
6. **Scale to mm** (`--size-mm`, default 100). SfM is scale-free, so the mesh is
   scaled so its **longest bounding-box dimension = `--size-mm` millimeters**
   (STL/GLB carry no units, but slicers assume mm). Set this to the object's real
   longest dimension for a correct print.
7. **Export**. `object.stl` (binary STL, geometry only - the standard print
   format), `object.glb` (with per-vertex colors sampled from the nearest
   Gaussian's DC color, for web/QuickLook), and `turntable-*.png` verification
   renders.

### Turntable renders

Rendered with a tiny dependency-free software rasterizer (numpy + PIL: painter's
algorithm, flat Lambert shading) on a decimated copy of the mesh - deliberately
**not** a GPU/offscreen-GL path, which is fragile headless. They are low-fi
verification thumbnails, not beauty shots; use the GLB in a real viewer for a
proper look. If rendering fails for any reason it is skipped with a warning (the
STL/GLB are the real deliverables).

### Why Poisson (and its limits)

- **Poisson vs. ball-pivoting / alpha-shapes**: Poisson is the robust default for
  a watertight, printable result - it always closes the surface. Ball-pivoting
  preserves detail but leaves holes (not printable without repair); alpha-shapes
  are sensitive to sampling density.
- **Unseen regions balloon.** The classic Poisson artifact: with no points on the
  object's bottom/interior it extrapolates a bulging closed surface. The density
  trim mitigates it; a full, even capture prevents it.
- **Thin features and concavities** below the sampling resolution get bridged or
  smoothed. Raise `--depth` and capture more angles for fine detail.
- **No metric scale** comes from SfM - `--size-mm` is the only thing anchoring
  real dimensions.

## Splat viewer (viewer-object/index.ts)

An **orbit** viewer (vs. video-to-splat's first-person walker). Built with
**Three.js** and **@mkkellogg/gaussian-splats-3d**. It loads the file named in
`public/scene.json` (written by preview.sh), computes bounds by sampling splat
centers, frames the object, and renders it on an optional studio pedestal with
camera-synced lighting.

- **drag** = orbit around the object, **wheel** = zoom (dolly), **shift+drag** or
  **right-drag** = pan, **R** = reset framing.
- **Arrow keys / WASD** = keyboard orbit, **+/−** = zoom.
- **`?stage=0`** = disable pedestal and studio lighting (plain dark background).
- **`?dpr=N`** = override device pixel ratio (default: cap at 2×).
- Requires **WebGL2** (any modern browser). Brush exports are SH degree 0 only;
  the viewer sets `sphericalHarmonicsDegree: 0`.
- `.sog` files are converted to `.ply` on the fly by `preview.sh` (Three.js/GS3D
  does not read SOG directly).

`scene.json` schema: `{ "file": "/scene.ply", "type": "ply" }` (also accepts
`splat` / `spz`). `preview.sh --file <name>` selects which file in the project
to copy in (default: `cleaned.ply` → `splat.ply` → `splat.splat` → `splat.spz`
→ `splat.sog`).

## Performance expectations (M-series Mac)

| Stage | Rough time |
|-------|------------|
| Frame extraction | seconds |
| COLMAP SfM (80-150 frames, exhaustive) | ~5-15 min |
| Brush training (object ~100-150 imgs) | ~1-2 min / 1000 steps (30k ≈ 30-60 min) |
| clean_splat.py | seconds (CPU) |
| splat_to_mesh.py (densify + Poisson depth 9) | ~10-60 s depending on splat/point count |

## Troubleshooting

- **`import open3d` fails** - needs the venv from `setup_env.sh` (macOS arm64
  wheels, Python 3.11). Re-run setup; on Intel/old macOS there is no wheel.
- **Cleaned splat is empty / lost the object** - a filter was too aggressive.
  Re-run with `--keep-plane`, a lower `--min-opacity`, a higher `--scale-pctl`,
  and/or a larger `--eps`; each filter prints its removal count so you can see
  the culprit.
- **Cleaned splat still has background** - raise `--min-opacity`, lower
  `--scale-pctl`, confirm the plane step ran, or finish with a `--radius` crop.
- **Mesh has a huge blob / balloon** - background survived cleanup (mesh a
  cleaner `cleaned.ply`), or Poisson extrapolated an unseen region: raise
  `--density-quantile` (0.05-0.1) and/or capture the missing angles.
- **Mesh not watertight** - lower `--density-quantile` (less trimming), raise
  `--depth`, or improve the splat; the repair step tries to close holes but a
  very incomplete capture can't be fully closed.
- **Mesh too smooth / lost detail** - raise `--depth` to 10, increase
  `--samples-per-splat`, and capture closer/denser.
- **Turntable PNGs missing** - the software renderer skipped (see its warning);
  the STL/GLB are unaffected - open the GLB to inspect.
- **Blank/black preview** - use Chrome/Edge 134+ (WebGPU); press `R` to reframe.
  See the same entry in video-to-splat's REFERENCE for the near-plane gotcha.

## Rejected alternatives

- **Marching cubes on a voxelized splat** - blocky and needs a density threshold
  per scene; Poisson gives a smoother watertight result directly.
- **Meshing raw Gaussian centers without densify** - normals are noisy and the
  cloud too sparse; Poisson produces a lumpy, holey surface. Densify first.
- **GPU/offscreen-GL turntable (open3d OffscreenRenderer / trimesh pyglet)** -
  fragile in headless/CI contexts on macOS; the numpy+PIL software renderer is
  slower but always works.
- **Learned mesh extractors (SuGaR, 2DGS, GOF)** - higher quality but heavy
  PyTorch/CUDA dependencies; out of scope for a 100%-local Mac skill (good
  candidates for a GPU-server variant).
