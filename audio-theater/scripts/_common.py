#!/usr/bin/env python3
"""Shared utilities for the (local) audio-theater skill.

No cloud, no API keys. This orchestrator composes three *sibling* local skills
via their own installed venvs:
  - voice-clone-narration : per-line TTS in a cloned/designed voice
  - bg-music              : instrumental score / beds (ACE-Step 1.5)
  - sound-effects         : foley / ambience (Stable Audio Open Small)

The ffmpeg mixing/timing helpers below are ported verbatim from the original
skill; only the cloud key/Replicate code was removed and sibling-skill
resolution added.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SKILL_DIR = SCRIPT_DIR.parent
# Where sibling skills are installed alongside this one (skills root).
SKILLS_ROOT = SKILL_DIR.parent
CONFIG_FILE = SKILL_DIR / "config.json"

MAX_CLIP_SECONDS = 15


# ──────────────────────────────────────────────────────────
# Sibling-skill resolution (scripts + their isolated venvs)
# ──────────────────────────────────────────────────────────

def _skill_root_candidates():
    roots = [SKILLS_ROOT]
    home = Path.home()
    cwd = Path.cwd()
    for r in (home / ".cursor/skills", home / ".agents/skills",
              cwd / ".cursor/skills", cwd / ".agents/skills"):
        if r not in roots:
            roots.append(r)
    return roots


def sibling_skill_dir(skill):
    """Locate an installed sibling skill directory (env override honored)."""
    env = os.environ.get(skill.upper().replace("-", "_") + "_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    for root in _skill_root_candidates():
        cand = root / skill
        if cand.is_dir():
            return cand
    return None


def sibling_script(skill, name):
    """Path to <skill>/scripts/<name> (or <skill>/<name>) if it exists."""
    d = sibling_skill_dir(skill)
    if d:
        for cand in (d / "scripts" / name, d / name):
            if cand.exists():
                return cand
    return None


# (data_home_env, [relative venv python paths in priority order])
_VENV_SPECS = {
    "voice-clone-narration": ("VOICE_CLONE_HOME", "~/.voice-clone-narration",
                              ["venv/bin/python", ".venv/bin/python"]),
    "bg-music": ("BG_MUSIC_HOME", "~/.bg-music",
                 ["ACE-Step-1.5/.venv/bin/python", ".venv/bin/python"]),
    "sound-effects": ("SOUND_EFFECTS_HOME", "~/.sound-effects",
                      [".venv/bin/python", "venv/bin/python"]),
}


def sibling_venv_python(skill):
    """Return (existing_python_path_or_None, expected_path_str) for a sibling skill."""
    spec = _VENV_SPECS.get(skill)
    if not spec:
        return None, ""
    env_var, default_home, rels = spec
    home = Path(os.path.expanduser(os.environ.get(env_var, default_home)))
    expected = home / rels[0]
    for rel in rels:
        cand = home / rel
        if cand.exists():
            return cand, str(expected)
    return None, str(expected)


# ──────────────────────────────────────────────────────────
# Config + JSON IO
# ──────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(config):
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def slugify(text, maxlen=48):
    slug = (text or "").lower().strip()
    out = []
    for ch in slug:
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")
    return slug[:maxlen] or "audio-theater"


def resolve_out_dir(out):
    """Resolve and create the output directory."""
    p = Path(out)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ──────────────────────────────────────────────────────────
# Audio helpers (ffmpeg / ffprobe)
# ──────────────────────────────────────────────────────────

def get_audio_duration(path):
    """Return duration in seconds via ffprobe (0.0 on failure)."""
    cmd = [
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return float(result.stdout.strip())
    except (ValueError, AttributeError, FileNotFoundError):
        return 0.0


def run_ffmpeg(args, *, description=""):
    """Run ffmpeg -y <args>. Returns True on success."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args
    if description:
        print(f"  ffmpeg: {description} ...", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ffmpeg error (exit {result.returncode}):", file=sys.stderr)
        for line in (result.stderr or "").strip().split("\n")[-6:]:
            print(f"    {line}", file=sys.stderr)
        return False
    return True


def stem_paths(output_path):
    """Derive the nomusic/music stem paths from the full-mix output path."""
    output_path = Path(output_path)
    return (output_path.with_suffix(".nomusic" + output_path.suffix),
            output_path.with_suffix(".music" + output_path.suffix))


def _amix_or_pass(filt, labels, out_label):
    """Append an amix (or a passthrough for a single label) into [out_label]."""
    if not labels:
        return False
    if len(labels) == 1:
        filt.append(f"{labels[0]}anull[{out_label}]")
    else:
        filt.append("".join(labels) +
                    f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0[{out_label}]")
    return True


def ratio_from_duck_db(duck_db):
    """Map a target duck attenuation (dB, negative) to a compressor ratio."""
    amt = abs(float(duck_db or 0))
    if amt <= 0:
        return 2.0
    return max(2.0, min(20.0, 1.5 + amt))


# Music ducking. Music is the bottom layer: it should sit clearly UNDER voices+SFX
# and lower a little while they play, but the motion must be SMOOTH (broadcast-style
# music ducking), NOT a fast compressor that pumps up/down between words. So: a slow
# release that rides through inter-word gaps (only recovers in real pauses), a gentle
# attack, and a capped ratio so the duck is shallow. Keep the cue's base gain well
# below the voice so even fully recovered it never exceeds the dialogue.
MUSIC_DUCK_DB = -8.0
MUSIC_DUCK_ATTACK = 40
MUSIC_DUCK_RELEASE = 1500
MUSIC_DUCK_THRESHOLD = 0.04
MUSIC_DUCK_RATIO_CAP = 4.0  # shallow duck -> small, smooth variation (no pumping)


def assemble_content_and_music(filt, nomusic_labels, music_specs, *,
                               crossfeed=False, duck=True,
                               attack=MUSIC_DUCK_ATTACK, release=MUSIC_DUCK_RELEASE,
                               threshold=MUSIC_DUCK_THRESHOLD,
                               ratio_cap=MUSIC_DUCK_RATIO_CAP):
    """Sum the no-music CONTENT (voices + SFX) and gently duck music under it.

    Music behaves like background score: it sits under voices AND sfx and lowers a
    little while they play, recovering only in real pauses. The duck is deliberately
    SHALLOW and SLOW (ratio capped, long release) so it doesn't pump between words;
    keep the cue's base gain well under the voice so it never exceeds the dialogue.

    Returns (nomusic_label, music_label_or_None). Each label is produced once;
    the caller splits/sums them for the full mix and the stems.
    """
    _amix_or_pass(filt, nomusic_labels, "nmraw")
    music_specs = list(music_specs or [])

    if not music_specs:
        nm = "[nmraw]"
        if crossfeed:
            filt.append("[nmraw]crossfeed=strength=0.3[nmcf]")
            nm = "[nmcf]"
        return nm, None

    n = len(music_specs)
    if duck:
        parts = "[nmkeep]" + "".join(f"[mck{i}]" for i in range(n))
        filt.append(f"[nmraw]asplit={n + 1}{parts}")
        ducked = []
        for i, (raw, duck_db) in enumerate(music_specs):
            ratio = min(ratio_from_duck_db(duck_db), ratio_cap)
            filt.append(
                f"{raw}[mck{i}]sidechaincompress=threshold={threshold}:"
                f"ratio={ratio:.1f}:attack={attack}:release={release}:makeup=1[mdk{i}]")
            ducked.append(f"[mdk{i}]")
        nm_label = "[nmkeep]"
    else:
        nm_label = "[nmraw]"
        ducked = [raw for (raw, _) in music_specs]

    _amix_or_pass(filt, ducked, "mraw")
    mu_label = "[mraw]"
    if crossfeed:
        filt.append(f"{nm_label}crossfeed=strength=0.3[nmcf]")
        filt.append(f"{mu_label}crossfeed=strength=0.3[mucf]")
        nm_label, mu_label = "[nmcf]", "[mucf]"
    return nm_label, mu_label


def finalize_stems(tmp_full, tmp_nomusic, tmp_music, full_out, nomusic_out, music_out,
                   *, target_i=-16.0, target_tp=-1.5, bitrate="192k"):
    """Combine the no-music + music pre-norm WAVs into a full mix, measure it, and
    write all three MP3s with the SAME linear gain so full == nomusic + music.
    """
    ok = run_ffmpeg(
        ["-i", str(tmp_nomusic), "-i", str(tmp_music),
         "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0:dropout_transition=0[m]",
         "-map", "[m]", "-c:a", "pcm_s16le", str(tmp_full)],
        description="combine stems -> full (pre-norm)",
    )
    if not ok:
        print("  Error: failed to combine stems into the full mix.", file=sys.stderr)
        sys.exit(1)

    gain_db, measured = shared_norm_gain_db(tmp_full, target_i=target_i, target_tp=target_tp)

    def _encode(src, dest):
        af = f"volume={gain_db:.3f}dB" if abs(gain_db) > 1e-4 else "anull"
        return run_ffmpeg(["-i", str(src), "-af", af,
                           "-c:a", "libmp3lame", "-b:a", bitrate, str(dest)],
                          description=f"encode {Path(dest).name} (gain {gain_db:+.2f} dB)")

    for src, dest in ((tmp_full, full_out), (tmp_nomusic, nomusic_out), (tmp_music, music_out)):
        if not _encode(src, dest):
            sys.exit(1)

    return {
        "final": str(full_out),
        "nomusic": str(nomusic_out),
        "music": str(music_out),
        "duration": round(get_audio_duration(full_out), 3),
        "shared_gain_db": round(gain_db, 3),
        "measured_input_i": measured.get("input_i"),
    }


def measure_loudness(path, *, target_i=-16.0, target_tp=-1.5, target_lra=11.0):
    """Measure integrated loudness + true peak of an audio file (loudnorm analysis)."""
    cmd = [
        "ffmpeg", "-hide_banner", "-i", str(path),
        "-af", f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    err = result.stderr or ""
    start = err.rfind("{")
    end = err.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        return json.loads(err[start:end + 1])
    except json.JSONDecodeError:
        return {}


def shared_norm_gain_db(path, *, target_i=-16.0, target_tp=-1.5):
    """Linear gain (dB) that brings `path` to target loudness without exceeding
    target true peak. Integrated loudness shifts 1:1 with a fixed gain, so the
    same gain applied to partition stems keeps full == nomusic + music exactly.
    Returns (gain_db, measured_dict).
    """
    m = measure_loudness(path, target_i=target_i, target_tp=target_tp)
    if not m:
        return 0.0, {}
    try:
        in_i = float(m.get("input_i"))
        in_tp = float(m.get("input_tp"))
    except (TypeError, ValueError):
        return 0.0, m
    gain_loud = target_i - in_i
    gain_peak = target_tp - in_tp
    return min(gain_loud, gain_peak), m


def make_silence(out_path, seconds, *, rate=24000):
    """Write a mono WAV of N seconds of silence."""
    ok = run_ffmpeg([
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=mono:sample_rate={rate}",
        "-t", f"{max(0.01, seconds):.3f}", str(out_path),
    ], description=f"silence {seconds:.2f}s")
    return str(out_path) if ok else None


def format_timecode(seconds):
    """Seconds -> MM:SS.mmm"""
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m:02d}:{s:06.3f}"
