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
| Mesh | open3d Poisson + [trimesh](https://trimesh.org/) + scikit-image | Densify → normals → Poisson → orient → voxel-solidify (watertight) → flat base → STL/GLB | open3d MIT, trimesh MIT, skimage BSD |
| Preview | [three.js](https://threejs.org/) + [@mkkellogg/gaussian-splats-3d](https://github.com/mkkellogg/GaussianSplats3D) | Browser Splat Preview (3DGS) + Print Preview (GLB mesh via GLTFLoader), orbit + studio pedestal | MIT |

All are open source and commercial-safe. The stages are decoupled by files on
disk, so any one can be swapped.

## Data layout

Shared with video-to-splat under `~/.video-to-splat/` (override with
`VIDEO_TO_SPLAT_HOME`). A splat trained by either skill can be cleaned/meshed by
this one.

```
~/.video-to-splat/
  .venv/                       # uv venv: pycolmap, opencv, numpy, pillow, open3d, trimesh, scikit-image
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
      object.stl               # watertight, mm-scaled, flat-based mesh (print)
      object.glb               # colored, oriented mesh (web / QuickLook / Print Preview)
      object-turntable-*.png   # software-rendered check thumbnails
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
4. **Density trim** (`--density-quantile`; default **0 while base repair is
   active**, 0.03 with `--base-repair none`). Poisson invents a closed surface
   everywhere, including where it saw no points (e.g. the unseen bottom); its
   per-vertex density tells us where. Trimming the low-density fringe *punches
   holes into the closed surface*, so when the solidify stage is on we keep the
   surface closed and let solidify + the data-anchored base cut deal with the
   balloons instead. Keep the largest connected component (`trimesh.split`),
   then fix normals/winding.
5. **Orient base-down** (`--base-repair`, default `auto`; `none` to skip). Fit
   the object's **largest flat face** (the same RANSAC-scored "biggest, most
   populated plane" idea the splat viewer uses to stand it on the pedestal), and
   rotate so that face points down (+Z up, base at z=0). This is what makes the
   STL sit flat on a slicer bed and defines where the base cap goes.
6. **Solidify → watertight AND tunnel-free** (`--solidify`, default `auto`;
   `--voxel`, default 200; `--close`, default 2). This is the core printability
   fix. Two distinct problems: Poisson output of a real scan is often
   multi-shell / non-manifold (plain hole-filling can't fix it), and - subtler -
   a mesh can be perfectly *watertight yet full of through-tunnels* (genus > 0,
   like a donut) that read as holes and ruin the print. We **voxel-remesh**:
   rasterize to a grid (bbox-diagonal / `--voxel` voxels), apply **morphological
   closing** (`--close` iterations of dilate+erode, sealing tunnels up to
   ~2·close voxels wide), fill enclosed cavities (scipy), and re-extract with
   marching cubes (scikit-image). The result is a single closed manifold with
   genus ~0; a light Taubin smooth removes the voxel blockiness. `auto` runs
   this when the mesh isn't watertight **or** has genus > 0; `always` forces it;
   `none` disables it (detail-preserving but no guarantee).
7. **Flat base** (`--base-cut`, default 0.03). Slice a horizontal plane and cap
   the cross-section, removing the ragged unseen underside and leaving a flat,
   stable, watertight base. The cut height is **anchored to the lowest scan
   points** (1st percentile of the input samples + `--base-cut` of the data
   height), not to the mesh bottom - so a Poisson balloon hanging below the real
   sole is cut away rather than becoming a fake pedestal.
8. **Watertight gate + genus report** (strict by default; `--allow-open` to
   override). Report `is_watertight`, **genus** (0 = no through-holes/tunnels
   anywhere; each tunnel adds 1), volume, and bounding-box dimensions. If the
   mesh can't be made watertight the script writes an **inspection GLB only**,
   explains why, and **exits non-zero**. If genus > 0 it warns and suggests
   raising `--close`. `--allow-open` exports the STL anyway with a warning.
9. **Scale to mm** (`--size-mm`, default 100). SfM is scale-free, so the mesh is
   scaled so its **longest bounding-box dimension = `--size-mm` millimeters**
   (STL/GLB carry no units, but slicers assume mm). Set this to the object's real
   longest dimension for a correct print.
10. **Export**. `object.stl` (binary STL, geometry only, +Z-up base-at-0 - the
    standard print format), `object.glb` (same oriented geometry with per-vertex
    colors sampled from the nearest Gaussian's DC color, for web / QuickLook /
    Print Preview), and `object-turntable-*.png` verification renders.

### Underside / base repair (why the bottom is generated)

An object filmed on a table is never seen from below, and `clean_splat.py`
strips the support plane, so the raw scan has a **hole where the contact face
should be**. A Gaussian splat has no topology - it can't be "closed" - so this is
strictly a mesh-stage fix. The default `--base-repair auto` therefore *generates*
a flat base (steps 5-7). Consequences and knobs:

- The base is **printable but not a faithful scan** of the real underside. To get
  the true bottom, capture it: flip the object for a second pass, or shoot it
  elevated on a clear stand.
- `--base-repair flat` always cuts a flat base even if the mesh was already
  watertight (useful to force a clean, level contact); `none` leaves the
  reconstruction as-is (keeps a captured bottom, no orientation/cap).
- `--base-cut` controls how much of the ragged bottom is removed (anchored to
  the lowest scan points); increase it if the underside fringe is tall/noisy,
  decrease to preserve more of the real lower geometry.
- `--solidify` detail vs. guarantee: `--voxel` higher (e.g. 300-400) keeps more
  detail but thin-wall tunnels reappear sooner - raise `--close` together with
  it and re-check the genus report; `none` skips the voxel remesh (only safe
  when the reconstruction is already clean, watertight and genus 0).

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

## Viewer (viewer-object/index.ts) - two preview modes

An **orbit** viewer (vs. video-to-splat's first-person walker), built with
**Three.js** + **@mkkellogg/gaussian-splats-3d**. It loads the file named in
`public/scene.json` (written by preview.sh) and renders on an optional studio
pedestal with camera-synced lighting. It has two modes:

- **Splat Preview** (default): the raw Gaussian splat (`cleaned.ply` etc.). Honest
  scan data - **will show holes** on faces the camera never saw (e.g. the
  underside). Bounds are found by sampling splat centers; the object is
  auto-oriented onto the pedestal by its largest flat face (`?orient=0` to skip).
- **Print Preview**: the repaired, watertight **mesh** (`mesh/object.glb`) loaded
  via `GLTFLoader` - the geometry that actually prints. Lit evenly (hemisphere +
  ambient) for inspection rather than the moody splat rig. The mesh is exported
  +Z-up (base at z=0) and stood upright on the pedestal. Trigger with
  `preview.sh --print`, `?mode=print`, or a `.glb` `?url=`.

Controls (both modes): **drag** = orbit, **wheel** = zoom (dolly), **shift+drag**
/ **right-drag** = pan, **arrows / WASD** = keyboard orbit, **+/−** = zoom,
**R** = reset framing.

- **`?stage=0`** = disable pedestal and studio lighting (plain dark background).
- **`?dpr=N`** = override device pixel ratio (default: cap at 2×).
- Requires **WebGL2** (any modern browser). Brush exports are SH degree 0 only;
  the viewer sets `sphericalHarmonicsDegree: 0`.
- `.sog` files are converted to `.ply` on the fly by `preview.sh` (Three.js/GS3D
  does not read SOG directly).
- GLB vertex colors written as integer `COLOR_0` (normalized ubyte) are handled
  in-shader by three.js; a float-`COLOR_0`-in-0..255 fallback is normalized on load.

`scene.json` schema: splat = `{ "file": "/scene.ply", "type": "ply" }` (also
`splat` / `spz` / `ksplat`); print mesh = `{ "file": "/scene.glb", "type": "glb",
"mode": "print" }`. `preview.sh --file <name>` selects which project file to copy
in (splat default order: `cleaned.ply` → `splat.ply` → `splat.splat` →
`splat.spz` → `splat.sog`; `--print` default: `mesh/object.glb`).

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
- **Mesh not watertight (exits non-zero)** - the strict gate refused to emit a
  bad STL. With `--base-repair auto` (default) this is rare; if it happens, keep
  `--solidify auto`/`always`, try a larger `--base-cut`, raise `--voxel`, or mesh
  a cleaner `cleaned.ply`. Inspect the emitted `object.glb`. `--allow-open` forces
  export (not recommended - may not slice).
- **Visible holes / tunnels in the print mesh (genus > 0)** - "watertight" can
  still contain donut-like through-tunnels. The report prints `genus`; if > 0,
  raise `--close` (e.g. 4), lower `--voxel`, or capture the thin/missing areas
  better. Default settings target genus 0.
- **Print base cuts off too much / too little** - tune `--base-cut` (fraction,
  anchored to the lowest scan points). Standing on the wrong face? Orientation
  uses the largest flat face; a cleaner capture or `--base-repair none` (keep
  original orientation) can help.
- **Voxel/blocky or lost fine detail** - raise `--voxel` (300-400) for a finer
  solidify (raise `--close` with it and re-check genus), raise `--depth` to 10,
  increase `--samples-per-splat`; or set `--solidify none` if the reconstruction
  is already clean, watertight and genus 0.
- **Print mesh looks dark** - it's lit, so a dark-scanned object stays dark
  (splats are unlit and look brighter). This is expected; geometry is unaffected.
- **Turntable PNGs missing** - the software renderer skipped (see its warning);
  the STL/GLB are unaffected - open the GLB (or `preview.sh --print`) to inspect.
- **Blank/black preview** - use any WebGL2 browser (Chrome, Edge, Firefox,
  Safari); press `R` to reframe. For Print Preview, ensure `mesh/object.glb`
  exists (run `splat_to_mesh.py` first).

## Rejected alternatives

- **Marching cubes on a voxelized splat as the *primary* surface** - blocky and
  needs a density threshold per scene; Poisson gives a smoother surface directly.
  We do use voxel remesh + marching cubes, but only as the **repair/solidify**
  step on the Poisson mesh, where a guaranteed-watertight manifold matters more
  than surface fidelity (then Taubin-smoothed).
- **Meshing raw Gaussian centers without densify** - normals are noisy and the
  cloud too sparse; Poisson produces a lumpy, holey surface. Densify first.
- **GPU/offscreen-GL turntable (open3d OffscreenRenderer / trimesh pyglet)** -
  fragile in headless/CI contexts on macOS; the numpy+PIL software renderer is
  slower but always works.
- **Learned mesh extractors (SuGaR, 2DGS, GOF)** - higher quality but heavy
  PyTorch/CUDA dependencies; out of scope for a 100%-local Mac skill (good
  candidates for a GPU-server variant).
