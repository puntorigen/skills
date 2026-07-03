"""Shared audio helpers for the voice-clone-narration scripts.

Kept dependency-light: numpy + soundfile for WAV I/O, and the system ffmpeg
(via subprocess) for MP3 encoding. No network, no uploads.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Iterable

import numpy as np


def eprint(*args, **kwargs) -> None:
    """Print to stderr so stdout stays reserved for machine-readable output."""
    print(*args, file=sys.stderr, **kwargs)
    sys.stderr.flush()


def to_mono_f32(audio) -> np.ndarray:
    """Coerce a waveform (mx.array / torch.Tensor / ndarray / list) to a 1-D
    float32 numpy array in [-1, 1]."""
    # mlx arrays and torch tensors both convert cleanly via np.array / .cpu()
    if hasattr(audio, "detach"):  # torch.Tensor
        audio = audio.detach().to("cpu").float().numpy()
    else:
        audio = np.asarray(audio, dtype=np.float32)
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        # (channels, samples) or (samples, channels) -> mono
        audio = audio.reshape(audio.shape[0], -1) if audio.shape[0] <= 2 else audio
        audio = audio.mean(axis=0) if audio.shape[0] <= 2 else audio.mean(axis=-1)
    return np.ascontiguousarray(audio.reshape(-1), dtype=np.float32)


def concat_with_gaps(parts: Iterable[np.ndarray], sr: int, gap_s: float = 0.15) -> np.ndarray:
    """Join waveform chunks with a short silence between them."""
    parts = [to_mono_f32(p) for p in parts if p is not None and len(p) > 0]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    if len(parts) == 1:
        return parts[0]
    gap = np.zeros(max(0, int(sr * gap_s)), dtype=np.float32)
    out = []
    for i, p in enumerate(parts):
        if i:
            out.append(gap)
        out.append(p)
    return np.concatenate(out).astype(np.float32)


def save_wav(path: str, samples: np.ndarray, sr: int) -> str:
    import soundfile as sf

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    samples = to_mono_f32(samples)
    # clip to avoid wrap-around distortion on the odd out-of-range sample
    samples = np.clip(samples, -1.0, 1.0)
    sf.write(path, samples, sr, subtype="PCM_16")
    return path


def encode_mp3(wav_path: str, mp3_path: str, quality: int = 2) -> str:
    """Encode a WAV to MP3 with libmp3lame VBR (-q:a; 0=best..9=smallest)."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH (needed for MP3 encoding)")
    os.makedirs(os.path.dirname(os.path.abspath(mp3_path)), exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", wav_path,
        "-vn", "-c:a", "libmp3lame", "-q:a", str(quality),
        mp3_path,
    ]
    subprocess.run(cmd, check=True)
    return mp3_path


def audio_duration_s(samples: np.ndarray, sr: int) -> float:
    return len(to_mono_f32(samples)) / float(sr) if sr else 0.0


def is_apple_silicon() -> bool:
    import platform

    return platform.system() == "Darwin" and platform.machine() == "arm64"
