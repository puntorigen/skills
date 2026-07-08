# Edit DOCX — Reference

Background for [SKILL.md](SKILL.md): what a `.docx` actually is, how
`docx_tool.py` models the document, why the find/replace engine preserves
formatting, how each command mutates the file, and the tool's limits.

## What a `.docx` is

A `.docx` is a ZIP archive of Office Open XML (OOXML) parts. The main story is
`word/document.xml`; styles live in `word/styles.xml`; headers/footers, images
(`word/media/`), and relationships live in their own parts. This is why you must
never read a `.docx` with a plain text tool — you'd get ZIP bytes, not text —
and why edits go through `python-docx`, which understands the XML schema and
keeps every part consistent on save.

## Environment / data layout

Setup (`scripts/setup_env.sh`) creates a self-contained venv outside the repo:

```
~/.edit-docx/
└── .venv/                # python + python-docx (+ lxml), never committed
    └── bin/python        # this is the PY the commands use
```

Override the root with `EDIT_DOCX_HOME`; pick the venv's Python (uv path) with
`EDIT_DOCX_PYTHON`. Setup is idempotent and prints the venv python on its last
stdout line; `--update` refreshes `python-docx`.

## The element model

`docx_tool.py` walks the document body (`<w:body>`) in document order and yields
only two kinds of top-level children:

- **paragraphs** (`<w:p>`) — including headings, list items, and image-bearing
  paragraphs (an image is a `<w:drawing>`/`<w:pict>` inside a run).
- **tables** (`<w:tbl>`).

`inspect` numbers these **1-based, in document order** — that index is the
contract every other command relies on (`--element`, `--after`). Headers and
footers are listed separately per section as `[H<section>.<n>]` /
`[F<section>.<n>]` and are only edited via `replace --header/--footer`.

Two consequences to keep in mind:

- **Indices are positional, not stable IDs.** `insert`, `delete`, and `add-row`
  change what follows. Re-run `inspect` before the next structural edit.
- **Tables also get a body index _and_ a 1-based table number.** `--element`
  (for `replace`/`delete`) uses the body index shown in `[N]`; `--table` (for
  `edit-cell`/`add-row`) uses the table number shown in `TABLE #N`.

`inspect` also prints "Styles in use" (the distinct paragraph styles actually
applied) so you can pick sensible `--style` values without a separate `styles`
call; image paragraphs are flagged `[IMAGE WxHin]` when extents are readable.

## The run-aware replacement engine

In OOXML a paragraph's text is split across **runs** (`<w:r>`), each carrying its
own formatting (`<w:rPr>`: bold, italic, font, size, color). Word splits runs
for many invisible reasons — a spell-check boundary, a tracked edit, an inserted
space — so a phrase like "old text" is frequently spread over several runs. The
naive fix (`paragraph.text = ...`) collapses the paragraph into one unformatted
run, destroying all styling. That is why the skill forbids it.

`_replace_in_paragraph` instead:

1. Builds the paragraph's full text and a **run map** — for each run its
   `(start, end)` character offsets and element.
2. Finds matches over the full text (literal via `re.escape`, or raw regex with
   `--regex`), processing them **right-to-left** so earlier offsets stay valid.
3. For each match, finds the runs it overlaps, clones the **first overlapped
   run's `<w:rPr>`**, and inserts a new run carrying the replacement text with
   that formatting.
4. Re-attaches any un-matched **prefix** (in the first run) and **suffix** (in
   the last run) as their own runs with their original formatting, then removes
   the fully/partially consumed original runs.

Net effect: the replacement text inherits the formatting of where the match
started, and surrounding text keeps its own. Whitespace-only edges are protected
with `xml:space="preserve"` so leading/trailing spaces survive.

Scope is controlled by flags:

- default → every body paragraph **and** every table cell paragraph
- `--element N` → only that body element (a paragraph, or all cells if it's a
  table)
- `--header` / `--footer` → section headers/footers instead of the body

## What each command mutates

| Command | Targets | Formatting behavior |
|---|---|---|
| `inspect` | read-only | — |
| `replace` | body + tables, or `--element`, or `--header/--footer` | preserves run formatting across split runs |
| `insert --after N` | new `<w:p>` after element N | applies `--style` if it exists (warns and uses default if not) |
| `edit-cell --table t --row r --col c` | one cell (0-based r/c) | replace clones the cell's first run's `<w:rPr>`; `--append` adds a run to the last paragraph |
| `add-row --table t` | appends a row | copies the previous row's per-column run formatting; comma-splits `--values` |
| `delete --element N` | removes that paragraph or table | — |
| `styles` | read-only | lists paragraph / character / table styles |

Every edit command writes **in place** unless `--output`/`-o` is given, and
prints a one-line confirmation (e.g. `Replaced 3 occurrence(s). Saved to ...`).

## Sections, headers, footers

Header/footer content is per **section**. `inspect` lists it only when it holds
text. There is no positional index for header/footer editing — use
`replace --header` / `--footer`, which iterate every section's header/footer
paragraphs. Empty headers/footers are skipped in the listing.

## Limits & rationale

- **Tracked changes / comments / OLE objects** aren't modeled — editing text
  under a tracked change or inside a comment isn't supported.
- **Image insertion** isn't supported (existing images are preserved on save);
  adding media needs relationship + content-type wiring beyond this tool.
- **`add-row` uses comma-separated `--values`.** For cell text that contains
  commas, add the row then set those cells with `edit-cell`.
- **Styles must already exist** in the document. `insert --style "X"` warns and
  falls back to the default paragraph style if `X` isn't defined; run `styles`
  first to see valid names.

## Troubleshooting

- **`ERROR: python-docx is not installed`** — the wrong Python ran the tool.
  Use the venv python: `PY="$HOME/.edit-docx/.venv/bin/python"` (re-run
  `setup_env.sh` if `~/.edit-docx/.venv` is missing).
- **`Element index N out of range`** — the index is stale (a prior structural
  edit shifted it) or you used a table number where a body index was expected.
  Re-run `inspect`.
- **`replace` reports 0 occurrences** — the on-screen text may differ from the
  stored characters (curly vs straight quotes, non-breaking spaces, a hyphen vs
  en-dash), or the match spans a field code. Copy the exact substring from
  `inspect`, or narrow with `--element` and try `--regex`.
- **A replacement lost its styling** — confirm you used `replace` and not inline
  `paragraph.text` assignment; the engine only preserves formatting when it owns
  the edit.
- **Cell edit hit the wrong cell** — `--row`/`--col` are **0-based**, while
  `--table` is **1-based**; `inspect` prints rows as `Row 0, Row 1, ...`.
