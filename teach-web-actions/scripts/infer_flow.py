#!/usr/bin/env python3
"""Phase 4a of teach-web-actions: infer the primary action + prerequisite chain.

Reads a lesson directory (needs lesson.json + session.har, optional actions.js)
and writes flow.json: the ordered list of HAR requests needed to perform ONE
primary action end-to-end (auth warm-up, CSRF, autocomplete, the data call),
plus the parameter knobs and any recorded UI steps.

The primary endpoint is the top-ranked non-mutating, data-bearing endpoint from
lesson.json (override with --endpoint <id>). Every kept request captured up to
and including the primary endpoint's last call becomes a step, so the flow
carries all prerequisites.

Stdlib only. Values stay in session.har; flow.json holds redacted summaries and
har_entry_index pointers so a replay can pull the live request at call time.

Usage: infer_flow.py <lesson-dir> [--endpoint "GET host/path"] [--label TEXT]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import OrderedDict
from urllib.parse import parse_qsl, urlsplit

from har_lib import (
    endpoint_key, har_entries, iter_kept_entries, load_har, parse_body,
    path_template, redact, redact_body,
)


def _example_is_data_bearing(ep) -> bool:
    for ex in ep.get("examples", []):
        resp = ex.get("response") or {}
        if resp.get("json_keys"):
            return True
    return False


def pick_primary(endpoints, override=None):
    """Return the primary endpoint dict. lesson.json endpoints are pre-sorted
    (non-mutating first, richest params, highest count)."""
    if override:
        for ep in endpoints:
            if ep["id"] == override:
                return ep
        sys.exit(f"error: --endpoint {override!r} not found in lesson.json")
    non_mutating = [ep for ep in endpoints if not ep.get("mutating")]
    for ep in non_mutating:
        if _example_is_data_bearing(ep):
            return ep
    if non_mutating:
        return non_mutating[0]
    if endpoints:
        return endpoints[0]
    return None


def param_summary(url, req_body):
    """Redacted view of a request's query + body scalars for readability."""
    out = {}
    q = OrderedDict(
        (k, redact(k, v)) for k, v in
        parse_qsl(urlsplit(url).query, keep_blank_values=True))
    if q:
        out["query"] = q
    if isinstance(req_body, (dict, list)):
        out["body"] = redact_body(req_body)
    return out


UI_STEP_RE = re.compile(r"^\s*(await\s+)?page\b")


def extract_ui_steps(actions_path):
    """Pull the runnable page.* lines out of a recorded actions.js script."""
    steps = []
    try:
        with open(actions_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if UI_STEP_RE.match(s):
                    steps.append(s.rstrip(";"))
    except Exception:
        return []
    return steps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lesson_dir")
    ap.add_argument("--endpoint", help="endpoint id to force as primary "
                    "(e.g. 'GET host/api/search')")
    ap.add_argument("--label", help="human action label (default: action_guess)")
    args = ap.parse_args()

    ldir = os.path.abspath(args.lesson_dir)
    lesson_path = os.path.join(ldir, "lesson.json")
    har_path = os.path.join(ldir, "session.har")
    if not os.path.isfile(lesson_path):
        sys.exit(f"error: no lesson.json in {ldir} (run process_har.py first)")
    if not os.path.isfile(har_path):
        sys.exit(f"error: no session.har in {ldir}")

    with open(lesson_path, encoding="utf-8") as fh:
        lesson = json.load(fh)
    endpoints = lesson.get("endpoints") or []

    primary = pick_primary(endpoints, args.endpoint)
    if primary is None:
        sys.exit("error: lesson.json has no endpoints to build a flow from")
    primary_id = primary["id"]

    har = load_har(har_path)
    entries = har_entries(har)

    # Index kept entries and locate the last call of the primary endpoint.
    kept = list(iter_kept_entries(entries))  # [(idx, entry, basics), ...]
    last_primary_pos = None
    for pos, (_, _, b) in enumerate(kept):
        if endpoint_key(b) == primary_id:
            last_primary_pos = pos
    if last_primary_pos is None:
        sys.exit(f"error: primary endpoint {primary_id!r} not found in "
                 f"session.har (endpoint ids out of sync — re-run process_har.py)")

    api_steps = []
    for pos in range(last_primary_pos + 1):
        idx, entry, b = kept[pos]
        key = endpoint_key(b)
        _, req_body = parse_body(b["req"].get("postData"))
        api_steps.append({
            "step": len(api_steps) + 1,
            "har_entry_index": idx,
            "endpoint_id": key,
            "method": b["method"],
            "host": b["host"],
            "path_template": path_template(b["path"]),
            "role": "primary" if key == primary_id else "prerequisite",
            "mutating": b["method"] in ("POST", "PUT", "PATCH", "DELETE"),
            "started": entry.get("startedDateTime"),
            "param_summary": param_summary(b["url"], req_body),
        })

    actions_path = os.path.join(ldir, "actions.js")
    has_ui = os.path.isfile(actions_path)
    ui_steps = extract_ui_steps(actions_path) if has_ui else []

    flow = {
        "primary_endpoint_id": primary_id,
        "action_label": args.label or primary.get("action_guess") or primary_id,
        "action_guess": primary.get("action_guess"),
        "host": lesson.get("host") or primary.get("host"),
        "source_url": lesson.get("source_url"),
        "primary_param_candidates": primary.get("param_candidates") or [],
        "mutating_steps": [s["step"] for s in api_steps if s["mutating"]],
        "api_steps": api_steps,
        "has_ui": has_ui,
        "ui_steps": ui_steps,
        "auth_surface": lesson.get("auth_surface") or {},
    }

    out_path = os.path.join(ldir, "flow.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(flow, fh, indent=2, ensure_ascii=False)

    n_prereq = sum(1 for s in api_steps if s["role"] == "prerequisite")
    print(f"[flow] primary: {primary_id}")
    print(f"[flow] {len(api_steps)} api steps ({n_prereq} prerequisite) "
          f"+ {len(ui_steps)} ui steps")
    if flow["mutating_steps"]:
        print(f"[flow] WARNING: steps {flow['mutating_steps']} are MUTATING "
              f"(state-changing) — replay will require explicit confirmation")
    print(f"[flow] wrote {out_path}")


if __name__ == "__main__":
    main()
