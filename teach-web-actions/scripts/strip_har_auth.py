#!/usr/bin/env python3
"""Phase 4 helper: strip auth values for selected hosts in a HAR.

When a generated skill includes a per-user setup step, the recorder's own
credentials for the selected hosts must NOT ship inside the skill. This removes
cookie values, auth-header values, and Set-Cookie values for those hosts,
keeping the *names* as `<setup-required>` placeholders so the overlay knows what
setup.sh must fill in. Request URLs, query knobs, and bodies are left intact for
replay fidelity.

Stdlib only. Usage:
  strip_har_auth.py <har> --hosts host1,host2 [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlsplit

from har_lib import AUTH_HEADER_NAMES, har_entries, load_har

SETUP_PLACEHOLDER = "<setup-required>"


def _strip_cookie_header(value: str) -> str:
    parts = []
    for kv in value.split(";"):
        nm = kv.split("=")[0].strip()
        if nm:
            parts.append(f"{nm}={SETUP_PLACEHOLDER}")
    return "; ".join(parts)


def strip_hosts(har, hosts):
    """Strip cookie/auth values for entries whose host is in `hosts`.

    Returns the number of entries touched."""
    hostset = {h.lower() for h in hosts}
    touched = 0
    for e in har_entries(har):
        req = e.get("request") or {}
        host = urlsplit(req.get("url") or "").netloc.lower()
        if host not in hostset:
            continue
        touched += 1

        for c in req.get("cookies") or []:
            if c.get("name"):
                c["value"] = SETUP_PLACEHOLDER

        for h in req.get("headers") or []:
            name = (h.get("name") or "").lower()
            if name == "cookie":
                h["value"] = _strip_cookie_header(h.get("value") or "")
            elif name in AUTH_HEADER_NAMES:
                val = h.get("value") or ""
                scheme = val.split(" ")[0] if " " in val else ""
                h["value"] = (f"{scheme} {SETUP_PLACEHOLDER}" if scheme
                              else SETUP_PLACEHOLDER)

        resp = e.get("response") or {}
        for h in resp.get("headers") or []:
            if (h.get("name") or "").lower() == "set-cookie":
                nm = (h.get("value") or "").split("=")[0].strip()
                h["value"] = f"{nm}={SETUP_PLACEHOLDER}" if nm else SETUP_PLACEHOLDER
    return touched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("har_path")
    ap.add_argument("--hosts", required=True,
                    help="comma-separated hosts whose auth values to strip")
    ap.add_argument("--out", help="output path (default: overwrite in place)")
    args = ap.parse_args()

    if not os.path.isfile(args.har_path):
        sys.exit(f"error: no such HAR: {args.har_path}")
    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    if not hosts:
        sys.exit("error: --hosts is empty")

    har = load_har(args.har_path)
    touched = strip_hosts(har, hosts)
    out = args.out or args.har_path
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(har, fh, indent=2, ensure_ascii=False)
    print(f"[strip] removed auth values for {', '.join(hosts)} "
          f"in {touched} entrie(s) -> {out}")


if __name__ == "__main__":
    main()
