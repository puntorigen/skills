#!/usr/bin/env python3
"""Phase 2 of teach-web-actions: distill a recorded HAR into a reusable lesson.

Reads <lesson-dir>/session.har (+ optional actions.js, meta.json) and writes:
  - lesson.json  machine-readable endpoints, payloads, parameter knobs, auth
  - LESSON.md    human-readable summary

Stdlib only. Credential *values* (tokens, cookies, passwords) are redacted;
their names are kept so the agent knows what a replay needs. Shared HAR parsing
lives in har_lib.py.

Usage: process_har.py <lesson-dir> [--max-examples N] [--body-chars N]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from urllib.parse import parse_qsl

from har_lib import (
    guess_action, header_map, interesting_req_headers, iter_kept_entries,
    endpoint_key, parse_body, path_template, collect_params, redact,
    redact_body, response_summary, har_entries, load_har, MUTATING,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lesson_dir")
    ap.add_argument("--max-examples", type=int, default=3,
                    help="max example calls kept per endpoint (default 3)")
    ap.add_argument("--body-chars", type=int, default=1200,
                    help="max chars kept from a response body (default 1200)")
    args = ap.parse_args()

    ldir = os.path.abspath(args.lesson_dir)
    har_path = os.path.join(ldir, "session.har")
    if not os.path.isfile(har_path):
        sys.exit(f"error: no session.har in {ldir}")

    har = load_har(har_path)

    meta = {}
    meta_path = os.path.join(ldir, "meta.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path) as fh:
                meta = json.load(fh)
        except Exception:
            meta = {}

    entries = har_entries(har)
    total = len(entries)

    endpoints = OrderedDict()
    all_cookies = set()
    all_auth_headers = set()
    kept = 0

    for _, e, b in iter_kept_entries(entries):
        req = b["req"]
        resp = b["resp"]
        kept += 1
        tmpl = path_template(b["path"])
        key = endpoint_key(b)

        hmap = header_map(req.get("headers"))
        keep_headers, auth_headers = interesting_req_headers(hmap)
        entry_cookies = set()
        for c in req.get("cookies") or []:
            if c.get("name"):
                entry_cookies.add(c["name"])
        # some HARs put cookies only in the header
        if "cookie" in hmap and hmap["cookie"]:
            for kv in hmap["cookie"].split(";"):
                nm = kv.split("=")[0].strip()
                if nm:
                    entry_cookies.add(nm)
        all_cookies |= entry_cookies
        for an in auth_headers:
            all_auth_headers.add(an)

        req_mime, req_body = parse_body(req.get("postData"))
        params = collect_params(b["url"], req_body)

        ep = endpoints.get(key)
        if ep is None:
            ep = {
                "id": key,
                "action_guess": guess_action(b["method"], tmpl),
                "method": b["method"],
                "host": b["host"],
                "path_template": tmpl,
                "count": 0,
                "mutating": b["method"] in MUTATING,
                "param_candidates": OrderedDict(),
                "auth": {"cookies_required": set(), "headers": OrderedDict()},
                "examples": [],
            }
            endpoints[key] = ep

        ep["count"] += 1
        ep["auth"]["cookies_required"] |= entry_cookies
        for an, av in auth_headers.items():
            ep["auth"]["headers"][an] = av
        for p in params:
            ep["param_candidates"].setdefault((p["location"], p["name"]), p)

        if len(ep["examples"]) < args.max_examples:
            ex = {
                "url": b["url"],
                "query": OrderedDict(
                    (k, redact(k, v)) for k, v in
                    parse_qsl(b["query"], keep_blank_values=True)),
                "started": e.get("startedDateTime"),
                "request_headers": keep_headers,
            }
            if req_body is not None:
                ex["request_body_mime"] = req_mime
                ex["request_body"] = redact_body(req_body)
            ex["response"] = response_summary(resp, args.body_chars)
            ep["examples"].append(ex)

    # finalize: turn ordered maps into lists
    ep_list = []
    for ep in endpoints.values():
        ep["param_candidates"] = list(ep["param_candidates"].values())
        ep["auth"]["headers"] = list(ep["auth"]["headers"].keys())
        ep["auth"]["cookies_required"] = sorted(ep["auth"]["cookies_required"])
        ep_list.append(ep)

    # order: data-bearing GETs and richest endpoints first
    ep_list.sort(key=lambda x: (x["mutating"], -len(x["param_candidates"]),
                                -x["count"]))

    lesson = {
        "lesson": meta.get("lesson") or os.path.basename(ldir),
        "source_url": meta.get("url"),
        "host": meta.get("host"),
        "recorded_at": meta.get("created_at"),
        "counts": {"total_requests": total, "kept": kept,
                   "endpoints": len(ep_list)},
        "auth_surface": {
            "cookies_seen": sorted(all_cookies),
            "auth_headers_seen": sorted(all_auth_headers),
        },
        "endpoints": ep_list,
    }

    out_json = os.path.join(ldir, "lesson.json")
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(lesson, fh, indent=2, ensure_ascii=False)

    out_md = os.path.join(ldir, "LESSON.md")
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(lesson))

    print(f"[distill] {kept}/{total} requests kept across {len(ep_list)} endpoints")
    print(f"[distill] wrote {out_json}")
    print(f"[distill] wrote {out_md}")


def render_markdown(lesson) -> str:
    L = []
    L.append(f"# Lesson: {lesson['lesson']}\n")
    if lesson.get("source_url"):
        L.append(f"- Start URL: {lesson['source_url']}")
    if lesson.get("recorded_at"):
        L.append(f"- Recorded: {lesson['recorded_at']}")
    c = lesson["counts"]
    L.append(f"- Requests: {c['kept']} kept / {c['total_requests']} total "
             f"-> {c['endpoints']} endpoints\n")

    writes = [e for e in lesson["endpoints"] if e["mutating"]]

    L.append("## Actions learned\n")
    for i, ep in enumerate(lesson["endpoints"], 1):
        flag = "  [MUTATING — confirm before replay]" if ep["mutating"] else ""
        L.append(f"### {i}. {ep['action_guess']}{flag}")
        L.append(f"`{ep['method']} {ep['host']}{ep['path_template']}`  (x{ep['count']})")
        if ep["param_candidates"]:
            L.append("\nVariation knobs:")
            for p in ep["param_candidates"]:
                L.append(f"- `{p['name']}` ({p['location']}, {p['kind']}) "
                         f"= `{p['sample']}`")
        if ep["auth"]["headers"] or ep["auth"]["cookies_required"]:
            bits = []
            if ep["auth"]["headers"]:
                bits.append("headers " + ", ".join(ep["auth"]["headers"]))
            if ep["auth"]["cookies_required"]:
                bits.append(f"{len(ep['auth']['cookies_required'])} cookies")
            L.append(f"\nAuth: {'; '.join(bits)} (values redacted; read from session.har)")
        ex = ep["examples"][0] if ep["examples"] else None
        if ex:
            resp = ex.get("response", {})
            if resp.get("json_keys"):
                L.append(f"\nResponse {resp.get('status')} {resp.get('mime')} — "
                         f"keys: {', '.join(str(k) for k in resp['json_keys'][:12])}")
            elif resp.get("status") is not None:
                L.append(f"\nResponse {resp.get('status')} {resp.get('mime')}")
        L.append("")

    L.append("## Auth surface\n")
    a = lesson["auth_surface"]
    L.append(f"- Cookies seen: {', '.join(a['cookies_seen']) or 'none'}")
    L.append(f"- Auth headers seen: {', '.join(a['auth_headers_seen']) or 'none'}")
    L.append("- Values are redacted here; a replay reads them from `session.har`.\n")

    L.append("## How to replay\n")
    L.append("- API replay: reissue an endpoint above with substituted knobs; "
             "attach the cookies/auth headers from `session.har` (never print them).")
    L.append("- UI replay: adapt `actions.js` into `variant.js` and run "
             "`scripts/replay_ui.sh <lesson-dir> variant.js`.")
    if writes:
        L.append("- Endpoints flagged MUTATING change server state — confirm "
                 "with the user before firing them.")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
