#!/usr/bin/env python3
"""Assign a local voice to every character in script.json (design or clone).

For each character it either:
  - clones a voice from a reference clip you provide (--clip NAME=path or a
    --clips-dir with <character-slug>.<ext> files), via voice-clone-narration's
    prep_reference.sh, or
  - designs a brand-new voice from the character's `persona` text, via
    voice-clone-narration's design_voice.py (Apple Silicon only), or
  - reuses an already-saved voice of the same target name.

Voices are namespaced `at-<project>-<character>` so they never clobber your
personal voice library. The chosen voice name is written back into script.json.

Off Apple Silicon, voice DESIGN is unavailable - provide reference clips instead.

Usage:
    python3 setup_cast.py --out audio-theater/ep
    python3 setup_cast.py --out audio-theater/ep --clip "Marco=marco.m4a" --clip "Ines=ines.wav"
    python3 setup_cast.py --out audio-theater/ep --clips-dir refs/
"""

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from _common import (  # noqa: E402
    load_json, save_json, slugify, resolve_out_dir,
    sibling_venv_python, sibling_script,
)

LANG_NAMES = {
    "en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French",
    "de": "German", "it": "Italian", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "hi": "Hindi", "ar": "Arabic", "ru": "Russian",
    "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "sv": "Swedish",
}
CLIP_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".webm")


def is_apple_silicon():
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def vc_home():
    return Path(os.path.expanduser(os.environ.get("VOICE_CLONE_HOME", "~/.voice-clone-narration")))


def voice_exists(name):
    return (vc_home() / "voices" / f"{name}.wav").exists()


def find_clip_in_dir(clips_dir, char_name):
    slug = slugify(char_name)
    for p in sorted(Path(clips_dir).iterdir()):
        if p.is_file() and p.suffix.lower() in CLIP_EXTS:
            if slugify(p.stem) == slug or slug in slugify(p.stem):
                return p
    return None


def clone_voice(prep_sh, target_name, clip_path):
    cmd = ["bash", str(prep_sh), target_name, str(clip_path)]
    print(f"  cloning '{target_name}' from {clip_path} ...", file=sys.stderr)
    proc = subprocess.run(cmd, text=True, capture_output=True)
    sys.stderr.write(proc.stderr)
    return proc.returncode == 0 and voice_exists(target_name)


def design_voice(vc_py, design_py, target_name, describe, language_name, *, model=None):
    cmd = [str(vc_py), str(design_py), "--name", target_name,
           "--describe", describe, "--language", language_name]
    if model:
        cmd += ["--model", model]
    print(f"  designing '{target_name}' ({language_name}): {describe[:60]}", file=sys.stderr)
    proc = subprocess.run(cmd, text=True, capture_output=True)
    sys.stderr.write(proc.stderr)
    return proc.returncode == 0 and voice_exists(target_name)


def main():
    ap = argparse.ArgumentParser(description="Design or clone a voice per character.")
    ap.add_argument("--out", "-o", required=True, help="Project folder (contains script.json)")
    ap.add_argument("--script", default=None, help="Path to script.json (default <out>/script.json)")
    ap.add_argument("--clip", action="append", default=[],
                    help='Reference clip for a character: "Name=path" (repeatable).')
    ap.add_argument("--clips-dir", default=None,
                    help="Directory of reference clips named <character-slug>.<ext>.")
    ap.add_argument("--language", default=None,
                    help="Audition language name for design (default: mapped from script language).")
    ap.add_argument("--model", default=None, help="Qwen3-TTS VoiceDesign HF repo (design only).")
    ap.add_argument("--force", action="store_true",
                    help="Re-create voices even if a same-named voice already exists.")
    args = ap.parse_args()

    out_dir = resolve_out_dir(args.out)
    script_path = Path(args.script) if args.script else out_dir / "script.json"
    if not script_path.exists():
        print(f"Error: {script_path} not found (run write_script.py or author it first).",
              file=sys.stderr)
        sys.exit(1)
    script = load_json(script_path)

    lang2 = (script.get("language") or "en").strip().lower()
    language_name = args.language or LANG_NAMES.get(lang2, "English")
    project = slugify(script.get("title") or out_dir.name, maxlen=20)

    # Explicit per-character clips.
    clip_map = {}
    for spec in args.clip:
        if "=" not in spec:
            print(f"Error: --clip must be NAME=path (got '{spec}')", file=sys.stderr)
            sys.exit(2)
        name, path = spec.split("=", 1)
        clip_map[name.strip()] = path.strip()

    vc_py, vc_expected = sibling_venv_python("voice-clone-narration")
    prep_sh = sibling_script("voice-clone-narration", "prep_reference.sh")
    design_py = sibling_script("voice-clone-narration", "design_voice.py")
    if not prep_sh or not design_py:
        print("Error: voice-clone-narration scripts not found (install it alongside audio-theater).",
              file=sys.stderr)
        sys.exit(1)
    if not vc_py:
        print(f"Warning: voice-clone-narration venv not found at {vc_expected}.", file=sys.stderr)
        print("  Cloning (prep_reference.sh) only needs ffmpeg, but DESIGN needs the venv.",
              file=sys.stderr)

    assigned, failed = [], []
    for ch in script.get("characters", []):
        name = ch.get("name")
        char_slug = slugify(name, maxlen=20)
        target = f"at-{project}-{char_slug}"
        # Honor a pre-assigned, existing custom voice unless --force.
        pre = ch.get("voice")
        if pre and voice_exists(pre) and not args.force:
            print(f"  '{name}': keeping existing voice '{pre}'", file=sys.stderr)
            assigned.append({"character": name, "voice": pre, "method": "existing"})
            continue

        clip = clip_map.get(name)
        if not clip and args.clips_dir:
            found = find_clip_in_dir(args.clips_dir, name)
            clip = str(found) if found else None

        ok = False
        method = None
        if voice_exists(target) and not args.force:
            ok, method = True, "cached"
        elif clip:
            if not Path(clip).exists():
                print(f"  '{name}': clip not found: {clip}", file=sys.stderr)
            else:
                ok = clone_voice(prep_sh, target, clip)
                method = "cloned"
        else:
            # Design from persona (Apple Silicon + venv required).
            if not is_apple_silicon():
                print(f"  '{name}': no reference clip and voice DESIGN needs Apple Silicon. "
                      f"Provide --clip \"{name}=path\".", file=sys.stderr)
            elif not vc_py:
                print(f"  '{name}': design needs the voice-clone-narration venv "
                      f"(expected {vc_expected}).", file=sys.stderr)
            else:
                persona = (ch.get("persona") or "").strip()
                if not persona:
                    persona = f"a clear, natural {language_name} speaking voice for a character named {name}"
                    print(f"  '{name}': no persona set; using a generic voice description.",
                          file=sys.stderr)
                ok = design_voice(vc_py, design_py, target, persona, language_name,
                                  model=args.model)
                method = "designed"

        if ok:
            ch["voice"] = target if method in ("cloned", "designed", "cached") else pre
            assigned.append({"character": name, "voice": ch["voice"], "method": method})
            print(f"  ✓ '{name}' -> {ch['voice']} ({method})", file=sys.stderr)
        else:
            failed.append(name)
            print(f"  ✗ '{name}' voice not assigned", file=sys.stderr)

    save_json(script_path, script)
    print(json.dumps({
        "script": str(script_path),
        "assigned": assigned,
        "failed": failed,
    }, indent=2, ensure_ascii=False))
    if failed:
        print(f"\n  {len(failed)} character(s) still need a voice: {failed}", file=sys.stderr)
        print("  Provide reference clips with --clip NAME=path (works off Apple Silicon too).",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
