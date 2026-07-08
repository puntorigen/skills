#!/usr/bin/env python3
"""Read a PDF file using Docling and output structured JSON.

Usage:
    python3 read_pdf.py <input.pdf> [options]

Options:
    --output, -o      Output file path (default: stdout)
    --format          Output format: json, markdown, text, html (default: json)
    --ocr             Enable OCR for scanned documents
    --extract-tables  Extract tables as separate structured data
    --extract-images  Extract page images to directory
    --pages           Page range, e.g. "1-5" or "1,3,5"
"""

import argparse
import json
import sys
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING)

try:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
except ImportError:
    print("Error: docling not installed. Run: pip3 install docling", file=sys.stderr)
    sys.exit(1)


def build_converter(ocr=False, extract_images=False):
    """Build a DocumentConverter with the given options."""
    pipeline_opts = PdfPipelineOptions(
        do_ocr=ocr,
        do_table_structure=True,
    )

    if extract_images:
        pipeline_opts.generate_page_images = True
        pipeline_opts.images_scale = 2.0

    if ocr:
        try:
            from docling.datamodel.pipeline_options import OcrMacOptions
            pipeline_opts.ocr_options = OcrMacOptions()
        except ImportError:
            # Fall back to default OCR engine
            pass

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_opts)
        }
    )


def extract_tables_data(doc):
    """Extract tables from document as structured data."""
    tables = []
    for idx, table in enumerate(doc.tables):
        table_info = {"index": idx}

        # Export to dataframe for structured access
        try:
            df = table.export_to_dataframe(doc=doc)
            table_info["headers"] = list(df.columns)
            table_info["rows"] = df.values.tolist()
            table_info["row_count"] = len(df)
        except Exception:
            table_info["headers"] = []
            table_info["rows"] = []
            table_info["row_count"] = 0

        # Export HTML representation
        try:
            table_info["html"] = table.export_to_html(doc=doc)
        except Exception:
            table_info["html"] = None

        tables.append(table_info)

    return tables


def extract_sections(doc):
    """Extract document sections with hierarchy."""
    sections = []
    current_section = None

    for item, level in doc.iterate_items():
        text = getattr(item, 'text', None)
        if text is None:
            continue

        label = getattr(item, 'label', None)
        label_str = str(label) if label else ""

        if 'heading' in label_str.lower() or 'title' in label_str.lower():
            if current_section:
                sections.append(current_section)
            current_section = {
                "level": level,
                "title": text.strip(),
                "text": "",
            }
        elif current_section:
            if text.strip():
                current_section["text"] += text.strip() + "\n"
        else:
            # Text before any heading
            if text.strip():
                current_section = {
                    "level": 0,
                    "title": "",
                    "text": text.strip() + "\n",
                }

    if current_section:
        sections.append(current_section)

    # Clean up trailing newlines
    for s in sections:
        s["text"] = s["text"].strip()

    return sections


def convert_to_json(result, extract_tables=False):
    """Convert Docling result to structured JSON."""
    doc = result.document

    output = {
        "file": str(result.input.file) if hasattr(result.input, 'file') else "unknown",
        "metadata": {
            "page_count": len(doc.pages) if hasattr(doc, 'pages') and doc.pages else None,
        },
        "content": {
            "markdown": doc.export_to_markdown(),
            "text": doc.export_to_text(),
        },
    }

    # Tables
    if extract_tables:
        output["content"]["tables"] = extract_tables_data(doc)
    else:
        output["content"]["table_count"] = len(list(doc.tables)) if doc.tables else 0

    # Sections
    try:
        output["content"]["sections"] = extract_sections(doc)
    except Exception:
        output["content"]["sections"] = []

    return output


def save_images(result, output_dir):
    """Save extracted page images."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    doc = result.document
    saved = []
    if hasattr(doc, 'pages') and doc.pages:
        for page_no, page in doc.pages.items():
            if hasattr(page, 'image') and page.image:
                img_path = output_path / f"page_{page_no}.png"
                try:
                    page.image.pil_image.save(str(img_path))
                    saved.append(str(img_path))
                except Exception as e:
                    print(f"Warning: Could not save page {page_no} image: {e}", file=sys.stderr)

    return saved


def main():
    parser = argparse.ArgumentParser(description="Read PDF with Docling")
    parser.add_argument("input", help="Input PDF file path or URL")
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument("--format", choices=["json", "markdown", "text", "html"],
                        default="json", help="Output format")
    parser.add_argument("--ocr", action="store_true", help="Enable OCR")
    parser.add_argument("--extract-tables", action="store_true",
                        help="Extract tables as structured data")
    parser.add_argument("--extract-images", metavar="DIR",
                        help="Extract page images to directory")
    args = parser.parse_args()

    # Validate input
    input_source = args.input
    if not input_source.startswith("http"):
        input_path = Path(input_source)
        if not input_path.exists():
            print(f"Error: File not found: {input_source}", file=sys.stderr)
            sys.exit(1)

    # Build converter
    converter = build_converter(
        ocr=args.ocr,
        extract_images=bool(args.extract_images),
    )

    # Convert
    print("Converting document...", file=sys.stderr)
    result = converter.convert(input_source)

    # Extract images if requested
    if args.extract_images:
        saved = save_images(result, args.extract_images)
        print(f"Saved {len(saved)} page images to {args.extract_images}", file=sys.stderr)

    # Format output
    if args.format == "json":
        output_str = json.dumps(
            convert_to_json(result, extract_tables=args.extract_tables),
            indent=2, ensure_ascii=False, default=str
        )
    elif args.format == "markdown":
        output_str = result.document.export_to_markdown()
    elif args.format == "text":
        output_str = result.document.export_to_text()
    elif args.format == "html":
        try:
            output_str = result.document.export_to_html()
        except Exception:
            # Fallback: wrap markdown in basic HTML
            md = result.document.export_to_markdown()
            output_str = f"<html><body><pre>{md}</pre></body></html>"

    # Write output
    if args.output:
        Path(args.output).write_text(output_str, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output_str)


if __name__ == "__main__":
    main()
