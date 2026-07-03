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
  viewer/                      # vite + @manycore/aholo-viewer app (+ public/)
  projects/<name>/
    images/frame-0001.jpg ...  # selected frames (extract_frames.py)
    frames.json                # selection manifest
    database.db                # COLMAP feature/match DB
    sparse/0/                  # COLMAP model: cameras.bin, images.bin, points3D.bin
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
| `--export-every` | `5000` | We set it to the step count => one export at the end. |
| `--refine-every` | `200` | Densification cadence (~"images to cover the scene"). |
| `--eval-every` | `1000` | Eval cadence. |
| `--with-viewer` | off (headless) | Plain flag; present opens the live GUI (won't auto-exit). Absent = headless train + export + exit. |

Brush's COLMAP loader indexes the source directory case-insensitively and finds
`cameras.bin`/`images.bin`/`points3D.bin` plus the images by name, so the standard
`sparse/0/` + `images/` layout that `run_colmap.py` writes loads directly. It reads
both `.bin` and `.txt` COLMAP models.

## pycolmap notes (run_colmap.py)

- API used: `extract_features(db, image_dir, camera_mode=SINGLE)` ->
  `match_sequential(db)` or `match_exhaustive(db)` -> `incremental_mapping(db, image_dir, out)`.
- **Single camera** (`CameraMode.SINGLE`) is the default: one intrinsics shared
  across all frames, which is correct for a single-camera tour and more stable.
- SIFT runs on CPU on Apple Silicon (pycolmap has no Metal SIFT); it's fine for a
  few hundred frames. Matching + mapping dominate the ~10-30 min SfM time.
- **Sequential** matching suits ordered video frames. **Exhaustive** is O(n^2) but
  more robust for small (<~150), unordered, or loopy sets.
- **Loop detection** (`--loop-detection`) needs a COLMAP vocabulary tree
  (`--vocab-tree path/to/vocab_tree.bin`, downloadable from the COLMAP site). It
  helps close loops when the camera returns to the start. Off by default.
- COLMAP can produce several disconnected sub-models when overlap is weak;
  `run_colmap.py` keeps the largest and writes it to `sparse/0`, reporting the
  registered-image fraction as a quality gate.

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
`camera.up.set(0, -1, 0)` accordingly.

Note: Aholo's own `splat-transform` tool (referenced in their docs) is proprietary
and not redistributable, so this skill uses the open-source PlayCanvas
`@playcanvas/splat-transform` instead - it reads standard PLY and writes SOG/SPZ.

## Performance expectations (M-series Mac)

| Stage | Rough time |
|-------|------------|
| Frame extraction | seconds - a minute |
| COLMAP SfM (50-200 frames) | ~10-30 min |
| Brush training | ~6 min / 1000 steps (so ~2 min at 2k, ~3 h at 30k) |
| SOG compression | seconds - a minute |

A 2000-step smoke run end-to-end is typically well under ~30 min including SfM -
always do it before a multi-hour full run.

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
- **Low "% registered" in SfM** - weak overlap or blur. Raise `--fps`, try
  `--matcher exhaustive`, enable `--loop-detection` (with a vocab tree), or
  recapture with slower, more overlapping motion.
- **Splat is a noisy cloud after training** - almost always bad poses. Verify with
  a 2k smoke run; if poses are wrong, no amount of steps fixes it.
- **Brush won't launch / "no source"** - pass the project dir (containing
  `sparse/0`); confirm the binary is at `~/.video-to-splat/brush/brush_app` and is
  executable (Gatekeeper may quarantine it - `xattr -dr com.apple.quarantine` the
  brush dir if macOS blocks it).
- **splat-transform GPU error** - rerun `convert_splat.sh --cpu` (WebGPU in Node
  can be unavailable in some environments; CPU is slower but always works).
- **Blank/black preview** - use Chrome/Edge 134+ (WebGPU). Check the on-screen
  error and the devtools console; try `?url=/scene.sog` explicitly. If the scene
  is off-camera, press `R` to reset, then zoom out with the wheel.
