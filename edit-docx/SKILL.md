---
name: edit-docx
description: >-
  Inspect and edit Microsoft Word .docx files while preserving formatting,
  styles, and layout - 100% locally with python-docx, no cloud or API keys. A
  small CLI (docx_tool.py) lists every document element with a stable index
  (paragraphs, tables, images, headers/footers), then applies surgical edits:
  run-aware find/replace that keeps bold/italic/font/color even across split
  runs, insert paragraphs with a chosen style, edit or append table cells, add
  table rows copying the previous row's formatting, delete elements, and list
  available styles. Use when the user wants to read, inspect, edit, modify,
  update, or fill in a Word document or .docx file; do find-and-replace in a
  .docx while keeping formatting; change or add table rows/cells; insert
  headings or paragraphs; work with headers/footers; or template a report,
  contract, or letter - without opening Word.
---

# Edit DOCX Files

Inspect and edit `.docx` files while preserving formatting, styles, and layout
using the local CLI at this skill's `scripts/docx_tool.py`. It's pure Python
(`python-docx`) — no models, no GPU, no network at edit time — and works on any
OS with Python 3.9+.

`.docx` files are ZIP archives of XML: **never open them with a text/Read
tool**. `inspect` is your window into the document, and every edit command
targets an element by the index that `inspect` prints.

## Prerequisites

- **Python 3.9+** (any OS). [`uv`](https://astral.sh/uv) is used if present for
  a faster install, otherwise the stdlib `venv` + `pip` are used.
- One dependency, installed by setup into a local venv: **python-docx**.

## Setup

Resolve the skill directory and run setup **once**. It creates a self-contained
venv at `~/.edit-docx/.venv`, installs `python-docx`, and prints the venv
python on its last line:

```bash
SKILL_DIR="<the folder this SKILL.md lives in>"   # e.g. .cursor/skills/edit-docx
bash "$SKILL_DIR/scripts/setup_env.sh"
```

Then set the two handles every command below uses (setup prints `PY` too):

```bash
PY="$HOME/.edit-docx/.venv/bin/python"
TOOL="$SKILL_DIR/scripts/docx_tool.py"
```

The venv lives **outside the repo** under `~/.edit-docx/` so it's never
committed. Your documents stay wherever they already are — nothing is uploaded.

## Workflow

Always follow this sequence — copy the checklist and track progress:

```
- [ ] 1. Inspect the document to see its structure and element indices
- [ ] 2. Plan the edits using those 1-based element indices
- [ ] 3. Edit with the appropriate subcommand(s) (use -o to keep the original)
- [ ] 4. Verify by running inspect again to confirm the changes
```

Element indices come **only** from a fresh `inspect`. Structural edits
(`insert`, `delete`, `add-row`) shift subsequent indices, so re-inspect before
the next structural edit.

## Commands

All examples assume `PY` and `TOOL` are set as above.

### inspect — read document structure

```bash
$PY "$TOOL" inspect report.docx
```

Outputs every body element with a 1-based index, plus headers/footers:

```
=== DOCUMENT: report.docx (1 section(s), 8 element(s), 1 table(s)) ===
Styles in use: Heading 1, Normal, List Bullet

--- BODY ---
[1] Heading 1: "Introduction"
[2] Normal: "This document describes..."
[3] TABLE #1 (3x4):
    Row 0: | Area   | Desc   | Owner  | Status |
    Row 1: | Train  | Emp..  | HR     | Active |
    Row 2: | Audits | Int..  | QA     | Done   |
[4] Heading 2: "Scope"
[5] Normal: [IMAGE 4.5x3.0in] ""

--- HEADER (Section 1) ---
[H1.1] Normal: "Company Name"
```

### replace — find/replace preserving formatting

```bash
# Replace in entire document (body + tables)
$PY "$TOOL" replace doc.docx --find "old text" --replace "new text"

# Replace only in element 2
$PY "$TOOL" replace doc.docx --find "old" --replace "new" --element 2

# Replace in headers/footers
$PY "$TOOL" replace doc.docx --find "Draft" --replace "Final" --header --footer

# Regex replacement
$PY "$TOOL" replace doc.docx --find "v\d+\.\d+" --replace "v2.0" --regex

# Write to a separate file
$PY "$TOOL" replace doc.docx --find "old" --replace "new" -o out.docx
```

The replacement engine preserves run-level formatting (bold, italic, font,
color) even when the target text spans multiple runs.

### insert — add a paragraph after element N

```bash
$PY "$TOOL" insert doc.docx --after 2 --text "New paragraph here" --style "Normal"
$PY "$TOOL" insert doc.docx --after 1 --text "Subsection" --style "Heading 2"
```

### edit-cell — edit a table cell

```bash
# Replace cell content (preserves the first run's formatting)
$PY "$TOOL" edit-cell doc.docx --table 1 --row 2 --col 3 --text "Completed"

# Append to a cell
$PY "$TOOL" edit-cell doc.docx --table 1 --row 1 --col 0 --text " (updated)" --append
```

Table number is 1-based (table #1 = first table). Row and col are 0-based.

### add-row — append a row to a table

```bash
$PY "$TOOL" add-row doc.docx --table 1 --values "Col1 value, Col2 value, Col3 value"
```

Copies formatting from the last existing row. Values are comma-separated.

### delete — remove an element

```bash
$PY "$TOOL" delete doc.docx --element 5
```

### styles — list available styles

```bash
$PY "$TOOL" styles doc.docx
```

Lists paragraph, character, and table styles available in the document. Use
those names with `--style` in `insert`.

## Critical rules

- **Never use the Read/text tool on `.docx` files** — they're binary (ZIP).
  Always use `inspect`.
- **Never write inline Python that assigns to `paragraph.text`** — it destroys
  all run formatting. Always use the `replace` command.
- **Always `inspect` first** to get element indices before editing, and
  re-inspect after any structural edit (`insert`/`delete`/`add-row`).
- **Use `--output` / `-o`** when the user wants to preserve the original file;
  every edit command otherwise modifies **in place**.

## Limitations

- Cannot modify tracked changes, comments, or embedded OLE objects.
- Cannot insert images (existing images are preserved during edits).
- The `add-row` separator is a comma — cell values containing commas need
  `edit-cell` per cell instead.

## Resources

- How the run-aware replacement engine, element indexing, and table addressing
  work, plus troubleshooting: [REFERENCE.md](REFERENCE.md).
