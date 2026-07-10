#!/usr/bin/env python3
"""Phase 4c of teach-web-actions: trim a HAR down to the primary-action flow.

Given a lesson dir with session.har + flow.json, writes:
  - a trimmed HAR containing ONLY the entries referenced by flow.json's
    api_steps (in step order) — the full auth + prerequisite + result chain
  - a rewritten flow.json whose har_entry_index values point into the trimmed
    HAR (0-based), so the embedded pair is self-consistent inside a shared skill

Request bodies, headers, and cookies are preserved verbatim so a replay has the
live session material. Large response bodies are truncated to --max-body-bytes
(they are reference-only; replay does not read embedded responses).

Stdlib only. Usage:
  trim_har.py <lesson-dir> --out-har PATH --out-flow PATH [--max-body-bytes N]
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys

from har_lib import har_entries, load_har


def truncate_response(entry, max_bytes):
    """Truncate an oversized response body in place (reference-only data)."""
    resp = entry.get("response") or {}
    content = resp.get("content") or {}
    text = content.get("text")
    if not isinstance(text, str) or len(text) <= max_bytes:
        return
    if content.get("encoding") == "base64":
        # partial base64 would not decode; drop it and mark truncated
        content["text"] = ""
    else:
        content["text"] = text[:max_bytes]
    content["_twa_truncated"] = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lesson_dir")
    ap.add_argument("--out-har", required=True)
    ap.add_argument("--out-flow", required=True)
    ap.add_argument("--max-body-bytes", type=int, default=20000,
                    help="max chars kept per response body in the copy "
                         "(default 20000; request bodies are never truncated)")
    args = ap.parse_args()

    ldir = os.path.abspath(args.lesson_dir)
    har_path = os.path.join(ldir, "session.har")
    flow_path = os.path.join(ldir, "flow.json")
    if not os.path.isfile(har_path):
        sys.exit(f"error: no session.har in {ldir}")
    if not os.path.isfile(flow_path):
        sys.exit(f"error: no flow.json in {ldir} (run infer_flow.py first)")

    har = load_har(har_path)
    with open(flow_path, encoding="utf-8") as fh:
        flow = json.load(fh)

    entries = har_entries(har)
    api_steps = flow.get("api_steps") or []

    trimmed_entries = []
    index_map = {}  # original har_entry_index -> new index in trimmed HAR
    for step in api_steps:
        oi = step.get("har_entry_index")
        if oi is None or oi < 0 or oi >= len(entries):
            sys.exit(f"error: flow step {step.get('step')} references missing "
                     f"har_entry_index {oi}")
        if oi in index_map:
            # same entry already added (endpoint called once) — reuse
            continue
        entry = copy.deepcopy(entries[oi])
        truncate_response(entry, args.max_body_bytes)
        index_map[oi] = len(trimmed_entries)
        trimmed_entries.append(entry)

    log = har.get("log") or {}
    trimmed_har = {
        "log": {
            "version": log.get("version", "1.2"),
            "creator": log.get("creator", {"name": "teach-web-actions",
                                           "version": "1.0"}),
            "entries": trimmed_entries,
        }
    }

    # rewrite flow indices to point into the trimmed HAR
    new_flow = copy.deepcopy(flow)
    for step in new_flow.get("api_steps") or []:
        step["har_entry_index"] = index_map[step["har_entry_index"]]

    with open(args.out_har, "w", encoding="utf-8") as fh:
        json.dump(trimmed_har, fh, indent=2, ensure_ascii=False)
    with open(args.out_flow, "w", encoding="utf-8") as fh:
        json.dump(new_flow, fh, indent=2, ensure_ascii=False)

    print(f"[trim] kept {len(trimmed_entries)} entries "
          f"(from {len(entries)} total)")
    print(f"[trim] wrote {args.out_har}")
    print(f"[trim] wrote {args.out_flow}")


if __name__ == "__main__":
    main()
