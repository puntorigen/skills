# video-to-splat - Reference

Deeper notes on the tools, formats, flags, and failure modes behind the
[SKILL.md](SKILL.md) workflow.

## Pipeline stages and tools

| Stage | Tool | What it does | License |
|-------|------|--------------|---------|
| Frames | ffmpeg + variance-of-Laplacian scoring | Extract sharp, spread, non-duplicate views | ffmpeg: LGPL/GPL |
| Poses (SfM) | [pycolmap](https://github.com/colmap/pycolmap) 4.x | SIFT features -> matching -> incremental mapping; camera intrinsics/extrinsics + sparse point cloud | BSD |
| Training | [Brush](https://github.com/ArthurBrussee/brush) v0.3.0 | 3DGS training on the Apple GPU (WebGPU/Metal via Burn); exports standard 3DGS `.ply` | Apache-2.0 |
| Compress | [@playcanvas/splat-transform](https://github.com/playcanvas/splat-transform) | Convert `.ply` -> `.sog`/`.spz` | MIT |
| Preview | [@manycore/aholo-viewer](https://aholojs.dev/) | Browser 3DGS renderer | MIT |

All are open source and commercial-safe. The pieces are decoupled by files on
disk, so any stage can be swapped (see "Portability").

## Data layout

Everything is under `~/.video-to-splat/` (override with `VIDEO_TO_SPLAT_HOME`):

```
~/.video-to-splat/
  .venv/                       # uv venv: pycolmap, opencv, numpy, pillow
  brush/brush_app             # Brush training binary (macOS arm64)
  vocab_tree_faiss_flickr100K_words32K.bin  # COLMAP vocab tree (loop detection)
  viewer/                      # vite + @manycore/aholo-viewer app (+ public/)
  projects/<name>/
    images/frame-0001.jpg ...  # selected frames (extract_frames.py)
    frames.json                # selection manifest
    database.db                # COLMAP feature/match DB
    sparse/0/                  # COLMAP model: cameras.bin, images.bin, points3D.bin
    analysis/                  # analyze_scene.py: floors.json, floorplan-f<N>.png
    splat.ply                  # trained splat (Brush export, lossless master)
    splat.sog                  # compressed splat for the web (convert_splat.sh)
```

## Brush CLI (what train_splat.sh drives)

The macOS release ships one binary, `brush_app`. Given a **source path** it trains
**headlessly** (the viewer only opens when no source is given, or with
`--with-viewer true`) and exits after export. Flags we use / that matter:

| Flag | Default | Notes |
|------|---------|-------|
| `<PATH_OR_URL>` | - | Source: a COLMAP dir (`sparse/` + `images/`) or Nerfstudio dataset. |
| `--total-steps` | `30000` | Training steps (`--steps` in our wrapper). Named `--total-train-iters` on Brush `main`; the v0.3.0 release binary this skill installs uses `--total-steps`. |
| `--sh-degree` | `3` (we default `2`) | Spherical-harmonics degree 0-4; higher = view-dependent shine + bigger file. |
| `--max-resolution` | `1920` (we `1600`) | Cap training image long side. |
| `--max-splats` | `10000000` | Upper bound on splat count. |
| `--export-path` | `.` (cwd) | We pass the absolute project dir. |
| `--export-name` | `export_{iter}.ply` | We use `splat.ply` (overwritten each export); a find-newest fallback covers `{iter}` naming. |
| `--export-every` | `5000` (we `1000`) | Checkpoint cadence; each export overwrites `splat.ply`. Headless Brush prints nothing, so the file's mtime is the progress signal, and a killed run keeps its last checkpoint. |
| `--refine-every` | `200` | Densification cadence (~"images to cover the scene"). |
| `--eval-every` | `1000` | Eval cadence. |
| `--with-viewer` | off (headless) | Plain flag; present opens the live GUI (won't auto-exit). Absent = headless train + export + exit. |

Brush's COLMAP loader indexes the source directory case-insensitively and finds
`cameras.bin`/`images.bin`/`points3D.bin` plus the images by name, so the standard
`sparse/0/` + `images/` layout that `run_colmap.py` writes loads directly. It reads
both `.bin` and `.txt` COLMAP models.

**Critical gotcha**: that scan is *recursive and takes the first match* - and it
locates `points3d.bin` independently of `cameras.bin`/`images.bin`. If the project
keeps several sub-models (`sparse/0`, `sparse/1`, ...), Brush can silently train
cameras from one sub-model against points from another; the geometry is
inconsistent, most splats get pruned, and the export is an unusable noise cloud
in the wrong coordinate frame. `train_splat.sh` guards against this by staging a
temp dir containing only `images/` + `sparse/0` whenever extra sub-models exist.
When trained on a clean model, Brush preserves the COLMAP world frame exactly
(verified point-for-point), so COLMAP camera poses remain valid in splat space.

## pycolmap notes (run_colmap.py)

- API used: `extract_features(db, image_dir, camera_mode=SINGLE)` ->
  `match_sequential(db)` or `match_exhaustive(db)` -> `incremental_mapping(db, image_dir, out)`.
- **Single camera** (`CameraMode.SINGLE`) is the default: one intrinsics shared
  across all frames, which is correct for a single-camera tour and more stable.
- SIFT runs on CPU on Apple Silicon (pycolmap has no Metal SIFT); it's fine for a
  few hundred frames. Matching + mapping dominate the ~10-30 min SfM time.
- **Sequential** matching suits ordered video frames. **Exhaustive** is O(n^2) but
  more robust for small (<~150), unordered, or loopy sets.
- **Loop detection** (`--loop-detection`) retrieves visually similar frames via
  a vocabulary tree and matches them regardless of temporal distance - it is
  what reconnects a tour that revisits rooms/hallways. `setup_env.sh` downloads
  the faiss-format 32K-word tree (from the COLMAP 3.11.1 release assets) to
  `~/.video-to-splat/` and `run_colmap.py` finds it there automatically;
  `--vocab-tree` overrides the path. Off by default. Note the legacy flann
  trees hosted on demuc.de **crash** pycolmap >= 3.12 (COLMAP moved to faiss
  in May 2025) - use the release-asset trees only.
- COLMAP can produce several disconnected sub-models when overlap is weak;
  `run_colmap.py` writes them all, ranked by size (`sparse/0` = largest, which
  is what training uses), and reports the registered-image fraction as a
  quality gate. The smaller islands still hold valid geometry (often the other
  floors) for `analyze_scene.py --model N`.
- `--skip-matching` reuses the existing `database.db` and redoes only the
  mapping step - iterate on `--relaxed` or mapper behavior without paying for
  feature extraction + matching again (~8 of the ~20 min on 900 frames).

### Fast-motion / low-registration playbook

Fast walkthroughs (WhatsApp-style room tours) break SfM in a specific way: at
2 fps the camera moves so far between kept frames that neighbors share too few
inliers, the two-view links fail, and the model shatters into many disconnected
sub-models (doorways and staircases are the usual break points). Knobs, in the
order to try them:

| Knob | Where | Effect |
|------|-------|--------|
| `--fps 4-6` + matching `--max-frames` | extract_frames.py | The real fix: restores temporal overlap. Frame count grows; sequential matching cost grows only linearly. |
| `--overlap 20-30` | run_colmap.py | Each frame matched to more neighbors; bridges longer blur gaps. |
| `--max-features 12000-16000` | run_colmap.py | More SIFT features per image; helps bare walls / compressed video. |
| `--relaxed` | run_colmap.py | Lowers mapper gates (`min_num_matches` 15->8, `init_min_num_inliers` 100->50, `abs_pose_min_num_inliers` 30->15, ratio 0.25->0.15). Registers more frames, small drift risk. |
| `--loop-detection` | run_colmap.py | Vocab-tree retrieval pairs each frame with visually similar ones anywhere in the tour; reconnects revisited rooms/hallways across islands. |
| `--matcher exhaustive` | run_colmap.py | Any-to-any pairing; O(n^2), practical to ~400 frames. Connects islands only if they truly share visual content. |

Note that exhaustive matching does **not** fix missing temporal overlap - if no
pair of frames shares enough content, no matcher can link them. Density first.

## Floors + floorplan (analyze_scene.py)

Runs on any `sparse/N` model (`--model`, default the largest = 0) - no training
required. Method:

1. **Gravity/up**: mean camera "up" axis (the `-Y` row of each cam-to-world
   rotation; phones are held roughly upright), refined by a coarse-to-fine
   cone search (25 deg then 3 deg) for the direction that maximizes the
   "peakiness" of the point cloud's height histogram - floors/ceilings are
   horizontal planes, so the true vertical concentrates them into sharp bins.
   (A naive PCA snap fails here: a house's principal axes follow its horizontal
   extent, and a few degrees of tilt turns horizontal walking into fake height
   drift that splits one floor into several.)
2. **Scale from eye height**: for a sample of cameras, the local floor level is
   the 5th percentile of point heights within a horizontal radius; the median
   camera-minus-floor distance is the walker's eye height (~1.5 m in reality).
   This is the one physically known quantity in a scale-free reconstruction.
3. **Floors**: peaks of the camera-height histogram separated by at least
   1.2 eye heights (~1.8 m - a real story, not a split-level or garden step),
   each holding >= 6% of frames. Peak-finding beats gap-splitting because
   stairs produce heights *between* floors - there is no clean gap. Frames
   within 0.45 eye heights of a level belong to it; the rest are stair
   transitions. `--floors N` forces a count when auto-detection is wrong.
4. **Floorplan**: sparse points in a wall band around eye level (-0.6 to +0.4
   eye heights - excludes floor slab and ceiling), projected onto the
   horizontal plane, PCA-aligned so dominant wall directions run along the
   image axes, rendered as a log-scaled density map (PIL, no matplotlib)
   with the camera path in orange and a green start marker.

Caveats:

- **No metric scale** - SfM is scale-free, so `floors.json` heights are scene
  units; `eye_height_scene_units` is the scale anchor (~1.5 m) if you need
  approximate meters.
- Quality tracks registration: if only one floor's frames registered into the
  analyzed model, only that floor can be detected there. Fast tours usually
  shatter *at the stairs*, so each sub-model tends to be one floor: analyze
  `--model 1`, `--model 2`, ... (outputs get a `-mN` suffix) and identify each
  island's floor by its frame-number span. Sub-models have independent scale
  and orientation - never mix their coordinates.
- The wall-density "plan" is a sparse-point sketch, not CAD: enough to see the
  room layout and walk path, but walls are fuzzy where SIFT found little
  texture (bare drywall, glass).
- Density peaks can merge when the tour spends very little time on a floor
  (< ~6% of frames); use `--floors` to force the known count.

## Formats and Aholo compatibility

Aholo `SplatLoader.SplatFileType`: `PLY=0, SPZ=1, SPLAT=2, KSPLAT=3, SOG=4,
LCC=5, ESZ=6`. `SplatPackType`: `Raw=0, Compressed=1, SuperCompressed=2, Sog=3`.

| Format | Role here | Notes |
|--------|-----------|-------|
| `.ply` | Brush output, lossless master | Large (tens-hundreds of MB). Aholo can load it, but it's not stream-friendly. |
| `.sog` | Primary web deliverable | Bundled super-compressed; Aholo/PlayCanvas recommended. ~10-20x smaller. |
| `.spz` | Optional web deliverable | Niantic compressed format; also small. |

Load a `.sog` in an Aholo app (2-arg URL form fetches + decodes):

```ts
import { SplatLoader, SplatUtils } from '@manycore/aholo-viewer';
const data = await SplatLoader.parseSplatData(SplatLoader.SplatFileType.SOG, sogUrl);
const splat = await SplatUtils.createSplat(data);
viewer.getScene().add(splat);
```

Splats here follow the OpenCV convention (`-Y` up); the bundled viewer sets
`camera.up` accordingly (see below).

## Bundled viewer navigation (viewer/index.ts)

The Aholo SDK ships rendering only - camera *controls* exist only in their
website harness - so the bundled viewer implements a first-person walkthrough
controller against the public `PerspectiveCamera` API (position/up/lookAt):

- **Controls**: WASD/arrows walk (left/right arrows turn, A/D strafe, shift
  runs at 2.6x), drag looks around (grab-the-world), wheel moves along the view
  direction, Q/E moves down/up, R returns to the start pose, number keys jump
  floors, clicking the minimap teleports.
- **World frame**: walking happens in the horizontal plane of the scene's
  gravity vector. preview.sh embeds it in `scene.json` under `nav` (from
  `analysis/floors.json`); without it the viewer assumes the OpenCV `-Y` up,
  which is usually a few degrees off - run analyze_scene.py for level walking.
  The controller handles any frame handedness (yaw direction is derived from
  `sign(dot(cross(e1, e2), up))`).
- **Speed scale**: SfM units are arbitrary, so walk speed is calibrated from
  the analysis' `eye_height` (~1.5 m real) - ~1.4 eye heights per second.
- **Minimap**: shows the active floor's floorplan PNG. analyze_scene.py exports
  a `plan_transform` per floor (`px = (plan_xy - origin_xy) * px_per_unit +
  offset_px`, where `plan_xy = [p . plan_x, p . plan_y]` and `plan_x/plan_y`
  are world 3-vectors in floors.json) - the viewer uses it forward to draw the
  live camera marker/view cone and inverted for click-to-teleport. The active
  floor follows the camera height (nearest floor `level`); floor `level` is a
  *camera-height* cluster, i.e. already eye level - teleports go to `level`,
  not `level + eye`.
- **Floor jumps** use the per-floor `camera` capture pose from floors.json
  (median-in-time frame on that floor), guaranteeing the landing spot is a
  real interior viewpoint.
- **scene.json schema** (written by preview.sh): `{ file, type,
  camera?: {position, forward}, nav?: { up, plan_x, plan_y, eye_height,
  floors: [{index, level, plan, plan_transform, camera}] } }`. Floorplan PNGs
  are copied to `public/plan-f<N>.png`. Only `sparse/0`'s analysis is embedded -
  other sub-models live in different coordinate frames than the trained splat.

Note: Aholo's own `splat-transform` tool (referenced in their docs) is proprietary
and not redistributable, so this skill uses the open-source PlayCanvas
`@playcanvas/splat-transform` instead - it reads standard PLY and writes SOG/SPZ.

## Merging multiple captures of one location

`extract_frames.py --append` pools frames from several videos into one project
(each with its own filename prefix); `run_colmap.py` then solves one joint
reconstruction and the overlap between captures registers everything into a
single coordinate frame. This is the right way to extend a scene - training two
splats separately and merging the `.ply` files leaves duplicated geometry and a
brightness seam in the shared rooms, and requires hand-estimating the alignment
transform. Joint reconstruction costs nothing extra since 3DGS training is a
from-scratch optimization either way.

What decides the result quality:

- **Matcher**: plain sequential matching pairs only temporal neighbors *within*
  a video, so merged captures never connect. Use `--matcher exhaustive`
  (fine to ~300-400 total frames) or `--loop-detection` + vocab tree.
  Prefixed names still sort each video contiguously, so sequential *within*
  each video remains valid when loop detection provides the cross-video pairs.
- **Appearance drift**: SIFT registration tolerates moderate lighting change,
  but 3DGS training averages what the views saw - big exposure/white-balance/
  sun-angle differences make the overlap muddy, and **moved objects become
  ghosts**. Prefer similar time of day; if lighting differs badly, use the
  second video mostly for the new rooms plus just enough overlap to register.
- **Intrinsics**: the default single shared camera assumes one device. Pass
  `--multi-camera` when the videos come from different phones/cameras.
- **Connectivity check**: after SfM, if COLMAP reports multiple disconnected
  sub-models, the cross-video overlap was too weak - the script keeps only the
  largest model, which silently drops the other capture's rooms. Recapture the
  transition areas or add overlap frames.

## Performance expectations (M-series Mac)

| Stage | Rough time |
|-------|------------|
| Frame extraction | seconds - a minute |
| COLMAP SfM (50-200 frames) | ~10-30 min |
| COLMAP SfM (900+ frames, fast-tour settings) | ~20 min (matching ~8, mapping ~12) |
| Brush training (measured, M4 Pro) | single room ~100 imgs: ~1.3 min / 1000 steps (4k in ~5 min). Full floor ~400 imgs: ~3.4 min / 1000 steps (30k in ~1h45m). Per-step cost grows with splat count, so big scenes are super-linear - budget 2x+ on older chips |
| SOG compression | ~1-2 min for a 400 MB ply (falls back to CPU automatically) |

A 2000-step smoke run end-to-end is typically well under ~30 min including SfM -
always do it before a long full run, and **look at the result in the preview**
(a well-formed .ply that renders as noise means broken poses/input, not
under-training). For faster iteration on a big scene, train a single ~100-frame
sub-model for ~4000 steps (~5 min) instead of the whole reconstruction.

## Portability to a GPU server (later)

Every stage is a CLI with explicit file inputs/outputs, so lifting to a Linux/
NVIDIA box is mostly a per-stage swap:

- Frames/SfM: identical (`pycolmap` has Linux wheels; add a CUDA COLMAP for GPU SIFT).
- Training: Brush ships a Linux binary, or swap in `nerfstudio` splatfacto /
  `gsplat` for CUDA training - both consume the same COLMAP `sparse/0` layout.
- Compress/preview: unchanged (Node tools).

## Rejected alternatives

- **nerfstudio / gsplat training on Mac** - CUDA-only for practical training; no
  native Metal path. Great on an NVIDIA server, not for a local Mac skill.
- **VGGT / MASt3R COLMAP-free pipelines** (e.g. recon3d) - fast and impressive,
  but need an NVIDIA GPU and heavy PyTorch/model checkpoints. Out of scope for a
  100%-local Mac skill; ideal candidates for the server variant.
- **OpenSplat** - cross-platform C++ 3DGS trainer, but less maintained and fiddlier
  to build on macOS than dropping in Brush's prebuilt binary.
- **Aholo's proprietary `splat-transform`** - usable but not redistributable; the
  MIT PlayCanvas tool covers our PLY->SOG/SPZ needs.

## Troubleshooting

- **`import pycolmap` fails / no wheel** - needs macOS 14+ arm64. On Intel/old
  macOS there's no wheel; use the server variant or build COLMAP from source.
- **Low "% registered" in SfM** - weak overlap or blur. Work through the
  fast-motion playbook above: denser `--fps`, wider `--overlap`, `--relaxed`,
  then `--matcher exhaustive`; recapture with slower motion if all fail.
- **Splat is a noisy cloud after training** - two known causes. (1) Bad poses:
  verify with a 2k smoke run; if poses are wrong, no amount of steps fixes it.
  (2) Mixed sub-models: Brush scans its source dir *recursively* and can pair
  `cameras.bin` from one `sparse/N` with `points3D.bin` from another -
  train_splat.sh guards against this by staging `sparse/0` alone, but hit this
  if training from a hand-built dir. Telltale signs: splat count far below
  normal for the step count, and bounds spanning wildly different scales.
- **Brush won't launch / "no source"** - pass the project dir (containing
  `sparse/0`); confirm the binary is at `~/.video-to-splat/brush/brush_app` and is
  executable (Gatekeeper may quarantine it - `xattr -dr com.apple.quarantine` the
  brush dir if macOS blocks it).
- **splat-transform GPU error** - rerun `convert_splat.sh --cpu` (WebGPU in Node
  can be unavailable in some environments; CPU is slower but always works).
- **Blank/black preview** - use Chrome/Edge 134+ (WebGPU). Check the on-screen
  error and the devtools console; try `?url=/scene.sog` explicitly. If the scene
  is off-camera, press `R` to reset. Two hard-earned gotchas baked into the
  bundled viewer: (1) Aholo's default camera has `near=100` (mm-scale scenes),
  which clips an entire COLMAP-scale scene to black - the viewer resets it to
  0.05; (2) an indoor splat viewed from an arbitrary *outside* viewpoint shows
  only wall-backs and floaters - preview.sh starts the camera at a real
  mid-tour capture pose from `sparse/0`.
- **Walking feels tilted / minimap missing** - the preview was started without
  `analysis/floors.json`. Run analyze_scene.py, then restart preview.sh.
