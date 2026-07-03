# Talking Head - Reference

Architecture, model matrix + licenses, the macOS/MPS adaptations, rejected
alternatives, and troubleshooting for the talking-head skill (JoyVASA +
LivePortrait).

## Architecture

JoyVASA is a two-stage, identity-independent talking-face pipeline:

```mermaid
flowchart LR
  Audio["narration wav (16 kHz)"] --> Hub["chinese-hubert-base<br/>audio features"]
  Hub --> Diff["DiT motion diffusion<br/>(JoyVASA motion generator)"]
  Diff --> Motion["facial motion + head-pose sequence"]
  Img["avatar image"] --> LP["LivePortrait encoders<br/>(appearance + 3D keypoints)"]
  Motion --> Warp["LivePortrait warp + SPADE generator"]
  LP --> Warp
  Warp --> Frames["frames"] --> Mux["ffmpeg mux + audio"] --> Mp4["mp4"]
end
```

1. **Audio -> features:** HuBERT (Chinese) encodes the speech.
2. **Features -> motion:** a diffusion transformer samples a sequence of facial
   dynamics + head pose, independent of the person's identity.
3. **Motion -> video:** LivePortrait extracts the avatar's 3D appearance and
   keypoints, warps them by the generated motion, and the SPADE generator renders
   each frame. ffmpeg muxes the original audio back in.

Because motion is decoupled from identity and driven by phonetic audio features,
it lip-syncs any language (English and Spanish both work; the encoder was trained
on Chinese+English).

## Model matrix + licenses

| Component | Repo | License |
|-----------|------|---------|
| Motion generator + template | `jdh-algo/JoyVASA` | **MIT** (code + weights) |
| Renderer code | JoyVASA + LivePortrait (`KwaiVGI/LivePortrait`) | **MIT** |
| Renderer weights | `KlingTeam/LivePortrait` (`liveportrait/*`) | **Apache-2.0** |
| Audio encoder | `TencentGameMate/chinese-hubert-base` | Apache-2.0 |
| Face detection / landmarks | InsightFace `buffalo_l` (`insightface/*`) | **research / non-commercial** |

### The InsightFace licensing caveat

Everything in this pipeline is permissively licensed **except** the InsightFace
`buffalo_l` ONNX models (`det_10g.onnx`, `2d106det.onnx`) used to detect and
align the face before animation. InsightFace's pretrained models are released for
**non-commercial research** use only.

- Fine for demos, prototypes, internal reels, and evaluation.
- For a **commercial** product, replace the detector: LivePortrait has a
  MediaPipe-based path (see `ComfyUI-LivePortraitKJ`, which uses MediaPipe instead
  of InsightFace) that avoids the InsightFace license. That swap is out of scope
  for this skill's default setup but is the documented path to full commercial
  safety.

## Why JoyVASA (and not the alternatives)

| Option | Verdict |
|--------|---------|
| **JoyVASA + LivePortrait** | Chosen. Single image + audio -> video, MIT code, runs natively on Apple Silicon (the wrapper already detects MPS), best quality-on-Mac of the open options. |
| **SadTalker** | Apache-2.0 (fully commercial-safe) but 2023-era quality, and its Python 3.8 / torch 1.12 pins make it brittle to install on modern Macs. Good fallback if the InsightFace license is a blocker. |
| **Ditto (Ant Group)** | Apache-2.0 and high quality, but same warping-renderer class as LivePortrait (same artifacts), TensorRT/CUDA-centric, and bundles InsightFace. Higher porting risk, no quality-class upgrade. |
| **FLOAT (DeepBrain)** | CC-BY-NC - non-commercial. Excluded. |
| **EchoMimicV3 / Wan2.2-S2V / HunyuanVideo-Avatar / OmniAvatar / InfiniteTalk** | The video-diffusion class - see below. Not practical on a 16 GB Mac. |
| **Wav2Lip / MuseTalk / LatentSync** | Require an input *video* of a face, not a single still image. |

## The two engine classes (and why corners warp / eyes pop)

Open-source talking-head engines fall into two classes:

1. **Warping renderers** (LivePortrait, and therefore JoyVASA; also Ditto,
   FLOAT): encode the still image once, then *deform* it per frame by the
   generated motion field. Fast, small (~2 GB), runs on a Mac - but the warp
   acts on the whole frame, which produces the two signature artifacts:
   - **background/corner wobble** - fixed here by `--crop` (animate only the
     face crop, paste back onto the untouched original frame; requires
     `flag_do_crop + flag_pasteback + flag_stitching`, which `animate.py`
     sets together);
   - **eye popping / rubbery exaggeration** at higher guidance - mitigated by
     `--cfg-scale 1.5` and, if needed, `--animation-region lip`.
   Head motion in this class can never move truly independently of the source
   pixels, so some rubberiness is inherent.

2. **Video-diffusion models** (EchoMimicV3 1.3B, Wan2.2-S2V 14B,
   HunyuanVideo-Avatar 13B, OmniAvatar, Hallo3): generate every frame from
   scratch with the audio and identity as conditioning. Natural head/body
   motion, coherent backgrounds, no warping artifacts. This is the class that
   commercial offerings like **Pruna's P-Video-Avatar** belong to (hosted,
   proprietary, datacenter GPUs). The open-source ones are CUDA-first and
   heavy:
   - **EchoMimicV3** (Apache-2.0, 1.3B on Wan2.1-Fun) is the only one that
     could even fit in 16 GB - it needs ~12 GB VRAM on CUDA. But Wan-1.3B-class
     video diffusion on Apple Silicon runs at roughly 12 min per ~1 s of video
     (M4, community MPS ports), i.e. **hours per clip**, with heavy swap on a
     16 GB machine, and would need a CUDA->MPS porting effort. Revisit if a
     Mac gets 32-64 GB or an MLX port of EchoMimicV3/Wan-S2V appears.
   - Everything larger (Wan-S2V, HunyuanVideo-Avatar, OmniAvatar) needs
     10-24 GB+ of CUDA VRAM and is out of reach locally.

Bottom line: on a 16 GB Apple Silicon machine the warping class is the only
practical option today; `--crop` + moderate `--cfg-scale` removes most of its
visible artifacts. For P-Video-Avatar-level motion you currently need either a
CUDA box (EchoMimicV3 is the commercially-safe pick) or a hosted API.

## macOS / Apple Silicon adaptations

JoyVASA targets CUDA/Linux. `setup_env.sh` makes it run natively on Apple Silicon:

1. **Curated dependency set.** JoyVASA's `requirements.txt` pins CUDA-only packages
   (`onnxruntime-gpu`, `xformers`, `bitsandbytes`, `decord`, `tensorrt`,
   `audio-separator`, `mediapipe`) that the human image+audio path never imports.
   Setup installs only what the runtime needs, with `onnxruntime` (CPU/CoreML).
2. **torch >= 2.8.** Earlier PyTorch raises `Conv3D is not supported on MPS`
   instead of falling back. torch 2.8 runs Conv3D on MPS and auto-falls-back
   `grid_sampler_3d` to CPU. `numpy` is pinned `<2` for numba/opencv ABI compat.
3. **Three CUDA-default patches.** JoyVASA hardcodes `device='cuda'` in three
   constructor defaults (`enc_dec_mask`, `DitTalkingHead`, `DenoisingNetwork`).
   `load_model()` calls `.to(device)` with the real (mps) device right after, so
   setup flips those defaults to `'cpu'` (idempotent string replacement). The
   model wrapper's own device selection already prefers MPS.
4. **Runtime shims (in `animate.py`, not the checkout):**
   - `PYTORCH_ENABLE_MPS_FALLBACK=1` before torch import.
   - `torch.load(weights_only=False)` - torch >= 2.6 defaults to `True`, which
     rejects the motion-generator checkpoint (it stores an `argparse.Namespace`).
     All weights come from the repos downloaded in setup, so loading them fully is
     safe.
5. **Hubert folder name.** On non-Windows the code loads the audio encoder from a
   folder literally named `TencentGameMate:chinese-hubert-base` (with the colon,
   valid on APFS); setup downloads it to exactly that path.

## Checkpoint layout

Under `~/.talking-head/JoyVASA/pretrained_weights/`:

```
JoyVASA/motion_generator/motion_generator_hubert_chinese.pt
JoyVASA/motion_template/motion_template.pkl
TencentGameMate:chinese-hubert-base/{config.json,preprocessor_config.json,pytorch_model.bin}
liveportrait/base_models/{appearance_feature_extractor,motion_extractor,warping_module,spade_generator}.pth
liveportrait/retargeting_models/stitching_retargeting_module.pth
liveportrait/landmark.onnx
insightface/models/buffalo_l/{det_10g.onnx,2d106det.onnx}
```

Animals mode (`liveportrait_animals/`, X-Pose) is intentionally **not** installed:
it needs a CUDA-only custom op and is out of scope.

## Performance (M4 / 16 GB)

- ~24 s of compute per 1 s of output at 512x512; the bottleneck is
  `grid_sampler_3d` running on CPU (MPS lacks it).
- Model load is ~10 s; motion diffusion is fast; the per-frame LivePortrait
  render dominates.
- Memory stays well within 16 GB (models are small: ~2 GB total resident).

## Tuning

| Knob | Effect |
|------|--------|
| `--crop` | Animate the face crop, paste back onto the still original frame. Keeps background/corners static and preserves source resolution. Recommended always. |
| `--cfg-scale` (2.0 default) | Expressiveness of the generated motion. 1.5 for a calm read; 2.5 livelier; higher pops the eyes and looks jittery. |
| `--animation-region` | Restrict motion to `lip` (talking only, still head/eyes - most stable), `exp`, `pose`, `eyes`, or `all` (default). |
| source image | Front-facing, mouth-closed, single face gives the best sync and stability. |

## Storage & privacy

- Data root `~/.talking-head/` (`JoyVASA/` checkout + `.venv`, `pretrained_weights/`,
  `out/`). Override with `TALKING_HEAD_HOME`. Nothing is written into the repo.
- Weights (~5 GB) are anonymous HF downloads. Avatars, audio, and videos stay
  local. Never upload them.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `uv`/`git`/`ffmpeg not found` | Install per the message; re-run setup. |
| `Conv3D is not supported on MPS` | You are on old torch. Setup pins torch 2.8; re-run setup to upgrade. |
| `Weights only load failed ... argparse.Namespace` | Old torch.load default; `animate.py` sets `weights_only=False`. Run via `animate.py`, not JoyVASA's `inference.py` directly. |
| `No face detected in the source image` | Use a front-facing, single-face portrait; try `--crop`; ensure the face is not tiny/occluded. |
| Mouth barely moves | Raise `--cfg-scale` to 2.5; ensure the source mouth is closed. |
| Jittery / over-animated / eyes pop wide | Lower `--cfg-scale` to 1.5; consider `--animation-region lip`. |
| Background / frame corners warp | Add `--crop` (crop + pasteback keeps the frame static). |
| Some MPS op errors out | Re-run with `--force-cpu` (slower but most compatible). |
| Very slow | Expected on Mac (~24 s per output second). Keep clips short; batch longer renders. |
| Not on Apple Silicon | Use `--force-cpu`; there is no CUDA path in this skill's setup. |

## Sources

- JoyVASA: https://github.com/jdh-algo/JoyVASA (MIT), weights https://huggingface.co/jdh-algo/JoyVASA
- LivePortrait: https://github.com/KwaiVGI/LivePortrait (MIT), weights https://huggingface.co/KlingTeam/LivePortrait (Apache-2.0)
- Chinese-HuBERT: https://huggingface.co/TencentGameMate/chinese-hubert-base
- InsightFace (buffalo_l, non-commercial): https://github.com/deepinsight/insightface
- MediaPipe detector swap for commercial use: https://github.com/kijai/ComfyUI-LivePortraitKJ
