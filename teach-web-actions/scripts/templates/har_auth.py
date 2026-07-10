#!/usr/bin/env python3
"""Session-material helper for a generated web-action skill.

Reads the skill's embedded HAR (../data/session.har) and extracts the live
session cookies needed to replay the recorded flow:

  - as a Playwright cookie array (for UI replay via run_variant.js), or
  - as a per-request `Cookie` header string (used as a library by replay_api.py).

Self-contained: stdlib only, no dependency on teach-web-actions. It never logs
cookie or token values.

Usage (UI replay): har_auth.py [<session.har>] [--out cookies.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlsplit


def _entries(har):
    return (har.get("log") or {}).get("entries") or []


def load_har(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return json.load(fh)


def default_har_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "data", "session.har"))


def cookie_header_for_entry(entry) -> str:
    """Raw `Cookie: name=value; ...` string for one request (values intact)."""
    req = entry.get("request") or {}
    for h in req.get("headers") or []:
        if (h.get("name") or "").lower() == "cookie":
            return h.get("value") or ""
    pairs = []
    for c in req.get("cookies") or []:
        if c.get("name"):
            pairs.append(f"{c['name']}={c.get('value', '')}")
    return "; ".join(pairs)


def cookies_for_har(har):
    """Unique cookies across the HAR as Playwright cookie objects ({name,value,url})."""
    seen = {}  # (name, host) -> cookie object; last write wins
    for e in _entries(har):
        req = e.get("request") or {}
        host = urlsplit(req.get("url") or "").netloc
        if not host:
            continue
        url = f"https://{host}/"
        for c in req.get("cookies") or []:
            if c.get("name"):
                seen[(c["name"], host)] = {
                    "name": c["name"], "value": c.get("value", ""), "url": url}
        for h in req.get("headers") or []:
            if (h.get("name") or "").lower() == "cookie":
                for kv in (h.get("value") or "").split(";"):
                    nm, sep, val = kv.strip().partition("=")
                    if sep and nm:
                        seen[(nm, host)] = {"name": nm, "value": val, "url": url}
        for h in (e.get("response") or {}).get("headers") or []:
            if (h.get("name") or "").lower() == "set-cookie":
                first = (h.get("value") or "").split(";")[0]
                nm, sep, val = first.partition("=")
                nm = nm.strip()
                if sep and nm:
                    seen[(nm, host)] = {"name": nm, "value": val, "url": url}
    return list(seen.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("har_path", nargs="?", default=default_har_path())
    ap.add_argument("--out", help="write cookies JSON here (default stdout)")
    args = ap.parse_args()

    if not os.path.isfile(args.har_path):
        sys.exit(f"error: no HAR at {args.har_path}")
    cookies = cookies_for_har(load_har(args.har_path))
    data = json.dumps(cookies, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(data)
        print(f"[auth] extracted {len(cookies)} cookie(s) -> {args.out}",
              file=sys.stderr)
    else:
        sys.stdout.write(data + "\n")


if __name__ == "__main__":
    main()
