#!/usr/bin/env python3
"""Turn a raw dialogue .txt into a structured script.json (+ story.md), locally.

This local skill has no cloud LLM, so there are two authoring paths:

  1. From an existing dialogue file (this script):
       write_script.py --script-file dialogo.txt --mode theater --language es --out <dir>
     Parses "Name: line" per line into characters + lines.

  2. From an idea/brief: **you (the agent) author script.json directly** following
     the schema in SKILL.md (title, language, mode, characters[], lines[]). You are
     the scriptwriter - no model call is needed. Optionally drop your draft dialogue
     into a .txt and normalize it with this script.

Voices are left unassigned here; run setup_cast.py next to design or clone one
voice per character.

Usage:
    write_script.py --script-file dialogo.txt --mode theater --language es --out audio-theater/ep
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from _common import load_config, save_json, slugify, resolve_out_dir  # noqa: E402

VALID_MODES = ("theater", "lipsync", "podcast")

# A leading screenplay-style stage direction like "(whispering)" or "[angrily]"
# becomes the line's `emotion`; the spoken text keeps only the words. Inline
# non-verbal cues elsewhere in the text (e.g. "[gasp]") are left in place - the
# voice generator handles them (native on Chatterbox turbo, stripped otherwise).
_STAGE_DIR_RE = re.compile(r"^\s*[\(\[]\s*([a-zA-Z][a-zA-Z \-]{1,24}?)\s*[\)\]]\s*")


def split_stage_direction(content):
    """Pull a leading (stage direction) off a line -> (emotion_or_None, text)."""
    m = _STAGE_DIR_RE.match(content)
    if not m:
        return None, content.strip()
    return m.group(1).strip().lower(), content[m.end():].strip()


def parse_dialogue_text(text):
    """Parse 'Name: line' style dialogue into characters + lines."""
    characters = {}
    lines = []
    order = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\s*([^:]{1,40}?)\s*:\s*(.+)$", line)
        if not m:
            speaker, content = "Narrador", line
        else:
            speaker, content = m.group(1).strip(), m.group(2).strip()
        emotion, content = split_stage_direction(content)
        if speaker not in characters:
            characters[speaker] = {"name": speaker, "voice": None, "persona": ""}
            order.append(speaker)
        lines.append({
            "index": len(lines),
            "speaker": speaker,
            "text": content,
            "emotion": emotion or "",
            "tags": [],
            "pause_after": 0.3,
        })
    return [characters[name] for name in order], lines


def normalize_script(data, *, mode, language):
    characters = data.get("characters") or []
    for ch in characters:
        ch.setdefault("voice", None)
        ch.setdefault("persona", "")
    lines = []
    for i, ln in enumerate(data.get("lines") or []):
        entry = {
            "index": i,
            "speaker": ln.get("speaker", "Narrador"),
            "text": (ln.get("text") or "").strip(),
            "emotion": (ln.get("emotion") or "").strip(),
            "tags": ln.get("tags") or [],
            "pause_after": ln.get("pause_after", 0.3),
        }
        if ln.get("intensity") is not None:
            entry["intensity"] = ln["intensity"]
        lines.append(entry)
    known = {c["name"] for c in characters}
    for ln in lines:
        if ln["speaker"] not in known:
            characters.append({"name": ln["speaker"], "voice": None, "persona": ""})
            known.add(ln["speaker"])
    return {
        "title": data.get("title") or "Untitled",
        "language": data.get("language") or language,
        "mode": mode,
        "characters": characters,
        "lines": lines,
    }


def write_story_md(script, path):
    lines = [f"# {script['title']}", ""]
    lines.append(f"_Mode: {script['mode']} · Language: {script['language']}_")
    lines.append("")
    lines.append("## Characters")
    for c in script["characters"]:
        persona = f" — {c['persona']}" if c.get("persona") else ""
        lines.append(f"- **{c['name']}** (voice: `{c.get('voice') or 'unassigned'}`){persona}")
    lines.append("")
    lines.append("## Script")
    lines.append("")
    for ln in script["lines"]:
        cue = ln.get("emotion") or (", ".join(ln["tags"]) if ln.get("tags") else "")
        cue = f" _({cue})_" if cue else ""
        lines.append(f"**{ln['speaker']}:**{cue} {ln['text']}")
        lines.append("")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Write script.json from a dialogue .txt")
    parser.add_argument("--script-file", required=True,
                        help="Existing dialogue file ('Name: text' per line)")
    parser.add_argument("--mode", choices=VALID_MODES, default="theater")
    parser.add_argument("--language", "-l", default=None, help="Language (default from config: es)")
    parser.add_argument("--out", "-o", required=True, help="Output project folder")
    args = parser.parse_args()

    config = load_config()
    language = args.language or config.get("default_language", "es")

    text = Path(args.script_file).read_text(encoding="utf-8")
    characters, lines = parse_dialogue_text(text)
    title = slugify(Path(args.script_file).stem).replace("-", " ").title()
    script = normalize_script(
        {"title": title, "language": language, "characters": characters, "lines": lines},
        mode=args.mode, language=language,
    )

    out_dir = resolve_out_dir(args.out)
    script_path = out_dir / "script.json"
    story_path = out_dir / "story.md"
    save_json(script_path, script)
    write_story_md(script, story_path)

    print(f"  Title: {script['title']}", file=sys.stderr)
    print(f"  Characters: " + ", ".join(c["name"] for c in script["characters"]), file=sys.stderr)
    print(f"  Lines: {len(script['lines'])}", file=sys.stderr)
    print("  Next: run setup_cast.py to assign a voice to each character.", file=sys.stderr)
    print(json.dumps({
        "script": str(script_path),
        "story": str(story_path),
        "title": script["title"],
        "mode": script["mode"],
        "language": script["language"],
        "characters": [c["name"] for c in script["characters"]],
        "line_count": len(script["lines"]),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
