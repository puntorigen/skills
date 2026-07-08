# PDF Documents — Reference

Background for [SKILL.md](SKILL.md): the environment layout, the full
`write_pdf.py` config schema, and advanced Docling (read) / fpdf2 (write)
recipes for cases the three scripts don't cover directly.

## Environment / data layout

Setup (`scripts/setup_env.sh`) creates a self-contained venv outside the repo:

```
~/.pdf-documents/
└── .venv/                # python + docling + fpdf2, never committed
    └── bin/python        # this is the PY the scripts use
~/.cache/huggingface/     # Docling layout/table models (~1-2 GB, first read)
```

Override the root with `PDF_DOCS_HOME`; pick the venv's Python (uv path) with
`PDF_DOCS_PYTHON`. Setup prints the venv python on its last stdout line and is
idempotent; `--update` refreshes docling + fpdf2. The model download happens on
the **first** `read_pdf.py`/`inspect_pdf.py` run, not during setup.

## Why Docling (not a text-layer dumper)

A `.pdf` stores glyphs positioned on a page, not paragraphs, tables, or reading
order. Naive text extraction yields jumbled columns and loses tables entirely.
Docling runs layout + table-structure models to reconstruct reading order,
heading hierarchy, and table cells — which is what makes `--extract-tables`
(headers/rows) and the section list possible. That model work is why the first
run downloads weights and why complex/merged-cell tables still deserve a
verification pass.

## write_pdf.py — full config schema

```json
{
  "metadata": {
    "title": "Document Title",
    "author": "Author Name",
    "subject": "Subject"
  },
  "page": {
    "format": "A4",
    "orientation": "portrait",
    "margin": 15
  },
  "header": {
    "text": "Company Name",
    "logo": "path/to/logo.png",
    "logo_width": 30
  },
  "footer": {
    "text": "Confidential",
    "show_page_numbers": true
  },
  "content": [
    { "type": "title", "text": "Main Title", "size": 24, "color": [31, 78, 121], "align": "C" },
    { "type": "subtitle", "text": "Subtitle text", "size": 14, "color": [46, 117, 182] },
    { "type": "heading", "text": "Section Heading", "level": 1, "size": 16, "color": [31, 78, 121] },
    { "type": "paragraph", "text": "Body text...", "size": 11, "color": [64, 64, 64], "align": "J", "spacing": 6 },
    { "type": "spacer", "height": 10 },
    { "type": "table",
      "headers": ["Column A", "Column B", "Column C"],
      "rows": [["value1", "100", "15%"], ["value2", "200", "25%"]],
      "col_widths": [60, 40, 40],
      "header_style": { "bg_color": [31, 78, 121], "font_color": [255, 255, 255], "font_size": 10, "bold": true },
      "row_style": { "font_size": 10, "alternating_color": [242, 242, 242] },
      "align": ["L", "R", "R"] },
    { "type": "image", "path": "path/to/image.png", "width": 120, "align": "C", "caption": "Figure 1: Description" },
    { "type": "bullet_list", "items": ["First point", "Second point"], "size": 11 },
    { "type": "numbered_list", "items": ["Step one", "Step two"], "size": 11 },
    { "type": "divider" },
    { "type": "key_value", "items": [{ "key": "Metric", "value": "$1.2M" }, { "key": "Growth", "value": "85%" }], "key_width": 50 },
    { "type": "page_break" }
  ]
}
```

Notes:
- **`page.orientation`** is read by its first letter (`portrait`/`landscape`);
  `format` is any fpdf2 size (`A4`, `Letter`, ...).
- **`header`** draws an optional logo + right-aligned text and a blue rule on
  every page; **`footer`** shows left text and `Page N/{nb}` when
  `show_page_numbers` is true. Omit `header` entirely for no header.
- **`table`** auto-computes equal `col_widths` if omitted; `align` is padded to
  the column count with `L`; rows alternate `row_style.alternating_color`.
- **Alignment** everywhere: `L` left, `C` center, `R` right, `J` justified.
- Unknown block `type`s are skipped with a warning (forward-compatible).

### Professional color palettes

| Role | Corporate Blue | Modern Green | Warm |
|---|---|---|---|
| Primary | `[31, 78, 121]` | `[39, 119, 83]` | `[139, 69, 19]` |
| Secondary | `[46, 117, 182]` | `[72, 169, 122]` | `[191, 143, 0]` |
| Light bg | `[214, 228, 240]` | `[220, 245, 233]` | `[255, 248, 230]` |
| Body text | `[64, 64, 64]` | `[64, 64, 64]` | `[64, 64, 64]` |
| Muted | `[150, 150, 150]` | `[150, 150, 150]` | `[150, 150, 150]` |

## Docling — advanced read usage

Drive Docling directly (in the venv python) when the three scripts' flags aren't
enough.

### Custom pipeline options

```python
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableStructureOptions

opts = PdfPipelineOptions(do_ocr=True, do_table_structure=True, generate_page_images=False, images_scale=2.0)
opts.table_structure_options = TableStructureOptions(do_cell_matching=True)
converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
```

### macOS Vision OCR

```python
from docling.datamodel.pipeline_options import OcrMacOptions
opts = PdfPipelineOptions(do_ocr=True)
opts.ocr_options = OcrMacOptions()
```

### Batch conversion & element iteration

```python
results = converter.convert_all([Path("a.pdf"), "https://example.com/b.pdf"])
for r in results:
    print(r.input.file, len(r.document.tables), "tables")

doc = converter.convert("document.pdf").document
for item, level in doc.iterate_items():
    if hasattr(item, "text"):
        print("  " * level + item.text)
for table in doc.tables:
    df = table.export_to_dataframe(doc=doc)   # pandas
    html = table.export_to_html(doc=doc)
```

## fpdf2 — advanced write usage

The `write_pdf.py` renderers cover the common blocks. For layouts beyond them,
subclass `FPDF` and build directly.

### KPI boxes (two-column)

```python
def two_column_kpi(pdf, l_label, l_val, r_label, r_val):
    cw = (pdf.w - 2 * pdf.l_margin) / 2 - 5
    y = pdf.get_y()
    for x, label, val in ((pdf.l_margin, l_label, l_val), (pdf.l_margin + cw + 10, r_label, r_val)):
        pdf.set_fill_color(240, 245, 250); pdf.rect(x, y, cw, 25, style="F")
        pdf.set_xy(x + 5, y + 3); pdf.set_font("Helvetica", "", 9); pdf.set_text_color(100, 100, 100)
        pdf.cell(cw - 10, 5, label)
        pdf.set_xy(x + 5, y + 10); pdf.set_font("Helvetica", "B", 18); pdf.set_text_color(31, 78, 121)
        pdf.cell(cw - 10, 12, val)
    pdf.set_y(y + 30)
```

### Callout box with accent bar

```python
def callout_box(pdf, text, bg=(240, 248, 255), border=(31, 78, 121)):
    x, y = pdf.get_x(), pdf.get_y(); width = pdf.w - 2 * pdf.l_margin
    pdf.set_fill_color(*bg); pdf.set_draw_color(*border); pdf.rect(x, y, width, 20, style="FD")
    pdf.set_fill_color(*border); pdf.rect(x, y, 3, 20, style="F")
    pdf.set_xy(x + 8, y + 3); pdf.set_font("Helvetica", "", 10); pdf.set_text_color(64, 64, 64)
    pdf.multi_cell(width - 16, 5, text); pdf.set_y(y + 25)
```

### Embedding a matplotlib chart

```python
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, io
fig, ax = plt.subplots(figsize=(6, 3))
ax.bar(["Q1", "Q2", "Q3", "Q4"], [15000, 28000, 52000, 95000], color="#1F4E79")
fig.tight_layout()
buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=150); buf.seek(0); plt.close(fig)
pdf.image(buf, x=30, w=150)   # then reference as an image block, or inline
```

### Watermark

```python
def add_watermark(pdf, text="DRAFT"):
    pdf.set_font("Helvetica", "B", 60); pdf.set_text_color(220, 220, 220)
    x, y = pdf.w / 2 - 40, pdf.h / 2
    with pdf.rotation(45, x + 40, y):
        pdf.text(x, y, text)
```

### Non-Latin / custom fonts

The core Helvetica font is Latin-1 only. For other scripts, register a TrueType
font and use it:

```python
pdf.add_font("Noto", "", "/path/to/NotoSans-Regular.ttf")
pdf.set_font("Noto", size=11)
```

## Troubleshooting

- **`Error: docling/fpdf2 not installed`** — the wrong Python ran the script.
  Use the venv python: `PY="$HOME/.pdf-documents/.venv/bin/python"` (re-run
  `setup_env.sh` if `~/.pdf-documents/.venv` is missing).
- **First read hangs/downloads for a while** — that's the one-time ~1-2 GB
  Docling model fetch; it needs network once, then runs offline.
- **Tables look wrong / empty** — try `--ocr` for scanned pages; for complex
  merged-cell tables, verify against the source, or drive Docling directly with
  `TableStructureOptions(do_cell_matching=True)`.
- **`--ocr` does nothing useful on a digital PDF** — born-digital PDFs already
  have a text layer; OCR is only for scans/images and just adds time otherwise.
- **Garbled non-Latin characters in a written PDF** — register a Unicode TTF via
  `add_font` instead of relying on core Helvetica.
- **A block didn't render** — an unknown `type` is skipped with a warning on
  stderr; check the block's `type` spelling against the schema.
