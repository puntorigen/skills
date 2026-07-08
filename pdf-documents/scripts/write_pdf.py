#!/usr/bin/env python3
"""Create a professional PDF from a JSON config.

Usage:
    python3 write_pdf.py <config.json> <output.pdf>

Config schema documented in SKILL.md.
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
except ImportError:
    print("Error: fpdf2 not installed. Run: pip3 install fpdf2", file=sys.stderr)
    sys.exit(1)


# --- PDF class with header/footer ---

class ProfessionalPDF(FPDF):
    def __init__(self, config):
        page_cfg = config.get("page", {})
        orientation = page_cfg.get("orientation", "portrait")[0].upper()
        fmt = page_cfg.get("format", "A4")
        super().__init__(orientation=orientation, format=fmt)

        self.config = config
        margin = page_cfg.get("margin", 15)
        self.set_margins(margin, margin, margin)
        self.set_auto_page_break(auto=True, margin=margin + 5)
        self.alias_nb_pages()

        # Metadata
        meta = config.get("metadata", {})
        if meta.get("title"):
            self.set_title(meta["title"])
        if meta.get("author"):
            self.set_author(meta["author"])
        if meta.get("subject"):
            self.set_subject(meta["subject"])

    def header(self):
        header_cfg = self.config.get("header", {})
        if not header_cfg:
            return

        if header_cfg.get("logo") and Path(header_cfg["logo"]).exists():
            logo_w = header_cfg.get("logo_width", 25)
            self.image(header_cfg["logo"], self.l_margin, 8, logo_w)

        if header_cfg.get("text"):
            self.set_font("Helvetica", "B", 9)
            self.set_text_color(120, 120, 120)
            self.cell(0, 10, header_cfg["text"], align="R",
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        else:
            self.ln(10)

        # Separator line
        self.set_draw_color(31, 78, 121)
        self.set_line_width(0.4)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)

    def footer(self):
        footer_cfg = self.config.get("footer", {})
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)

        left_text = footer_cfg.get("text", "")
        if footer_cfg.get("show_page_numbers", True):
            right_text = f"Page {self.page_no()}/{{nb}}"
        else:
            right_text = ""

        if left_text:
            self.cell(0, 10, left_text, align="L")
        if right_text:
            self.cell(0, 10, right_text, align="R" if left_text else "C")


# --- Content renderers ---

def render_title(pdf, block):
    size = block.get("size", 24)
    color = block.get("color", [31, 78, 121])
    pdf.set_font("Helvetica", "B", size)
    pdf.set_text_color(*color)
    pdf.cell(0, size * 0.6, block["text"], align=block.get("align", "C"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)


def render_subtitle(pdf, block):
    size = block.get("size", 14)
    color = block.get("color", [46, 117, 182])
    pdf.set_font("Helvetica", "", size)
    pdf.set_text_color(*color)
    pdf.cell(0, size * 0.55, block["text"], align=block.get("align", "C"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(6)


def render_heading(pdf, block):
    level = block.get("level", 1)
    sizes = {1: 16, 2: 14, 3: 12}
    size = block.get("size", sizes.get(level, 14))
    color = block.get("color", [31, 78, 121])

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", size)
    pdf.set_text_color(*color)
    pdf.cell(0, size * 0.55, block["text"],
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    if level == 1:
        pdf.set_draw_color(*color)
        pdf.set_line_width(0.3)
        pdf.line(pdf.l_margin, pdf.get_y() + 1, pdf.w - pdf.r_margin, pdf.get_y() + 1)
        pdf.ln(4)
    else:
        pdf.ln(2)


def render_paragraph(pdf, block):
    size = block.get("size", 11)
    color = block.get("color", [64, 64, 64])
    align = block.get("align", "J")
    spacing = block.get("spacing", 5)

    pdf.set_font("Helvetica", "", size)
    pdf.set_text_color(*color)
    pdf.multi_cell(0, spacing, block["text"], align=align,
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)


def render_table(pdf, block):
    headers = block.get("headers", [])
    rows = block.get("rows", [])
    col_widths = block.get("col_widths")
    aligns = block.get("align", [])
    header_style = block.get("header_style", {})
    row_style = block.get("row_style", {})

    # Calculate column widths if not provided
    num_cols = len(headers) if headers else (len(rows[0]) if rows else 0)
    if not col_widths:
        available = pdf.w - pdf.l_margin - pdf.r_margin
        col_widths = [available / num_cols] * num_cols

    # Normalize alignment list
    while len(aligns) < num_cols:
        aligns.append("L")

    line_height = 7
    start_x = pdf.l_margin

    # Header
    if headers:
        bg = header_style.get("bg_color", [31, 78, 121])
        fg = header_style.get("font_color", [255, 255, 255])
        h_size = header_style.get("font_size", 10)
        h_bold = header_style.get("bold", True)

        pdf.set_font("Helvetica", "B" if h_bold else "", h_size)
        pdf.set_fill_color(*bg)
        pdf.set_text_color(*fg)
        pdf.set_draw_color(200, 200, 200)

        for i, h in enumerate(headers):
            pdf.cell(col_widths[i], line_height, str(h),
                     border=1, fill=True, align="C")
        pdf.ln()

    # Data rows
    r_size = row_style.get("font_size", 10)
    alt_color = row_style.get("alternating_color", [242, 242, 242])

    pdf.set_font("Helvetica", "", r_size)
    pdf.set_text_color(64, 64, 64)

    for row_idx, row in enumerate(rows):
        if row_idx % 2 == 1 and alt_color:
            pdf.set_fill_color(*alt_color)
        else:
            pdf.set_fill_color(255, 255, 255)

        pdf.set_x(start_x)
        for i, val in enumerate(row):
            a = aligns[i] if i < len(aligns) else "L"
            pdf.cell(col_widths[i], line_height, str(val),
                     border=1, fill=True, align=a)
        pdf.ln()

    pdf.ln(4)


def render_image(pdf, block):
    img_path = block.get("path", "")
    if not Path(img_path).exists():
        print(f"Warning: Image not found: {img_path}", file=sys.stderr)
        return

    width = block.get("width", 100)
    align = block.get("align", "C")

    if align == "C":
        x = (pdf.w - width) / 2
    elif align == "R":
        x = pdf.w - pdf.r_margin - width
    else:
        x = pdf.l_margin

    pdf.image(img_path, x=x, w=width)
    pdf.ln(2)

    # Caption
    if block.get("caption"):
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 5, block["caption"], align="C",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)


def render_bullet_list(pdf, block):
    items = block.get("items", [])
    size = block.get("size", 11)
    color = block.get("color", [64, 64, 64])
    indent = block.get("indent", 10)

    pdf.set_font("Helvetica", "", size)
    pdf.set_text_color(*color)

    bullet = block.get("bullet", "-")
    for item in items:
        pdf.set_x(pdf.l_margin + indent)
        pdf.cell(6, 5.5, bullet)
        pdf.multi_cell(0, 5.5, str(item),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(2)


def render_numbered_list(pdf, block):
    items = block.get("items", [])
    size = block.get("size", 11)
    color = block.get("color", [64, 64, 64])
    indent = block.get("indent", 10)

    pdf.set_font("Helvetica", "", size)
    pdf.set_text_color(*color)

    for idx, item in enumerate(items, 1):
        pdf.set_x(pdf.l_margin + indent)
        pdf.cell(8, 5.5, f"{idx}.")
        pdf.multi_cell(0, 5.5, str(item),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(2)


def render_key_value(pdf, block):
    items = block.get("items", [])
    key_width = block.get("key_width", 50)
    size = block.get("size", 11)

    for item in items:
        pdf.set_font("Helvetica", "B", size)
        pdf.set_text_color(64, 64, 64)
        pdf.cell(key_width, 6, str(item.get("key", "")))
        pdf.set_font("Helvetica", "", size)
        pdf.set_text_color(31, 78, 121)
        pdf.cell(0, 6, str(item.get("value", "")),
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(2)


def render_spacer(pdf, block):
    pdf.ln(block.get("height", 10))


def render_divider(pdf, block):
    color = block.get("color", [200, 200, 200])
    pdf.ln(3)
    pdf.set_draw_color(*color)
    pdf.set_line_width(0.3)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(5)


def render_page_break(pdf, block):
    pdf.add_page()


# --- Content type dispatcher ---

RENDERERS = {
    "title": render_title,
    "subtitle": render_subtitle,
    "heading": render_heading,
    "paragraph": render_paragraph,
    "table": render_table,
    "image": render_image,
    "bullet_list": render_bullet_list,
    "numbered_list": render_numbered_list,
    "key_value": render_key_value,
    "spacer": render_spacer,
    "divider": render_divider,
    "page_break": render_page_break,
}


def build_pdf(config):
    """Create PDF from config."""
    pdf = ProfessionalPDF(config)
    pdf.add_page()

    for block in config.get("content", []):
        block_type = block.get("type", "")
        renderer = RENDERERS.get(block_type)
        if renderer:
            renderer(pdf, block)
        else:
            print(f"Warning: Unknown block type '{block_type}', skipping", file=sys.stderr)

    return pdf


def main():
    parser = argparse.ArgumentParser(description="Create professional PDF from JSON config")
    parser.add_argument("config", help="JSON config file path")
    parser.add_argument("output", help="Output PDF file path")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    pdf = build_pdf(config)
    pdf.output(args.output)
    print(f"Written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
