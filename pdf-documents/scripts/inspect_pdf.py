#!/usr/bin/env python3
"""Inspect a PDF file structure using Docling and report metadata.

Usage:
    python3 inspect_pdf.py <input.pdf> [--verbose]

Output: JSON summary of pages, tables, images, sections, and metadata.
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


def inspect_document(result, verbose=False):
    """Build inspection summary from conversion result."""
    doc = result.document

    info = {
        "file": str(result.input.file) if hasattr(result.input, 'file') else "unknown",
        "page_count": len(doc.pages) if hasattr(doc, 'pages') and doc.pages else None,
        "table_count": len(list(doc.tables)) if doc.tables else 0,
    }

    # File size
    if hasattr(result.input, 'file'):
        try:
            fsize = Path(str(result.input.file)).stat().st_size
            if fsize > 1_048_576:
                info["file_size"] = f"{fsize / 1_048_576:.1f} MB"
            else:
                info["file_size"] = f"{fsize / 1024:.1f} KB"
        except Exception:
            pass

    # Document structure overview
    element_types = {}
    for item, level in doc.iterate_items():
        label = str(getattr(item, 'label', 'unknown'))
        element_types[label] = element_types.get(label, 0) + 1

    info["element_types"] = element_types

    # Sections / headings
    headings = []
    for item, level in doc.iterate_items():
        text = getattr(item, 'text', None)
        label = str(getattr(item, 'label', ''))
        if text and ('heading' in label.lower() or 'title' in label.lower()):
            headings.append({
                "level": level,
                "label": label,
                "text": text.strip()[:100],
            })
    info["headings"] = headings

    # Tables summary
    if doc.tables:
        tables_summary = []
        for idx, table in enumerate(doc.tables):
            table_info = {"index": idx}
            try:
                df = table.export_to_dataframe(doc=doc)
                table_info["columns"] = list(df.columns)
                table_info["row_count"] = len(df)
                if verbose and len(df) > 0:
                    table_info["sample_row"] = df.iloc[0].to_dict()
            except Exception:
                table_info["columns"] = []
                table_info["row_count"] = 0
            tables_summary.append(table_info)
        info["tables"] = tables_summary

    # Text length
    try:
        full_text = doc.export_to_text()
        info["total_characters"] = len(full_text)
        info["total_words"] = len(full_text.split())
        if verbose:
            info["text_preview"] = full_text[:500] + ("..." if len(full_text) > 500 else "")
    except Exception:
        pass

    return info


def main():
    parser = argparse.ArgumentParser(description="Inspect PDF structure with Docling")
    parser.add_argument("input", help="Input PDF file path or URL")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Include sample data and text preview")
    args = parser.parse_args()

    input_source = args.input
    if not input_source.startswith("http"):
        input_path = Path(input_source)
        if not input_path.exists():
            print(f"Error: File not found: {input_source}", file=sys.stderr)
            sys.exit(1)

    pipeline_opts = PdfPipelineOptions(
        do_ocr=False,
        do_table_structure=True,
    )

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_opts)
        }
    )

    print("Inspecting document...", file=sys.stderr)
    result = converter.convert(input_source)
    info = inspect_document(result, verbose=args.verbose)

    print(json.dumps(info, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
