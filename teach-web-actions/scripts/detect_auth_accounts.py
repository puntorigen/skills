#!/usr/bin/env python3
"""Phase 4 helper: detect the auth accounts a recorded flow needs.

A single action can require more than one logged-in service (e.g. an app host
plus an SSO provider). This reads a lesson dir (session.har + flow.json) and
lists every host in the flow that carries auth material — request cookies,
Set-Cookie responses, or auth headers — so the generated skill can offer a
per-account setup step.

Emits JSON on stdout ({"accounts": [...]}) and a human summary on stderr. Only
names/hosts are reported — never cookie or token values.

Stdlib only. Usage: detect_auth_accounts.py <lesson-dir>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict

from har_lib import (
    AUTH_HEADER_NAMES, entry_basics, har_entries, header_map, is_noise_host,
    load_har,
)


def detect_accounts(har, flow):
    """Return a list of auth-account dicts for the flow's api_steps."""
    entries = har_entries(har)
    steps = flow.get("api_steps") or []
    primary_host = flow.get("host")
    source_url = flow.get("source_url")

    accounts = OrderedDict()
    for s in steps:
        idx = s.get("har_entry_index")
        if idx is None or idx < 0 or idx >= len(entries):
            continue
        b = entry_basics(entries[idx])
        host = b["host"]
        if not host or is_noise_host(host):
            continue
        req, resp = b["req"], b["resp"]
        hmap = header_map(req.get("headers"))

        cookie_names = set()
        for c in req.get("cookies") or []:
            if c.get("name"):
                cookie_names.add(c["name"])
        if hmap.get("cookie"):
            for kv in hmap["cookie"].split(";"):
                nm = kv.split("=")[0].strip()
                if nm:
                    cookie_names.add(nm)
        for h in resp.get("headers") or []:
            if (h.get("name") or "").lower() == "set-cookie":
                nm = (h.get("value") or "").split("=")[0].strip()
                if nm:
                    cookie_names.add(nm)
        auth_hdr = {n for n in hmap if n in AUTH_HEADER_NAMES}

        acct = accounts.get(host)
        if acct is None:
            acct = {"id": host, "host": host, "label": host, "login_url": None,
                    "cookie_names": set(), "auth_header_names": set(),
                    "seen_in_steps": [], "is_primary": False, "_has_auth": False}
            accounts[host] = acct
        acct["cookie_names"] |= cookie_names
        acct["auth_header_names"] |= auth_hdr
        if s.get("step") is not None:
            acct["seen_in_steps"].append(s["step"])
        if cookie_names or auth_hdr:
            acct["_has_auth"] = True

    result = []
    for host, acct in accounts.items():
        if not acct["_has_auth"]:
            continue
        is_primary = bool(primary_host) and host == primary_host
        acct["is_primary"] = is_primary
        acct["login_url"] = (source_url if (is_primary and source_url)
                             else f"https://{host}/")
        acct["cookie_names"] = sorted(acct["cookie_names"])
        acct["auth_header_names"] = sorted(acct["auth_header_names"])
        del acct["_has_auth"]
        result.append(acct)

    result.sort(key=lambda a: (not a["is_primary"],
                               a["seen_in_steps"][0] if a["seen_in_steps"] else 10**9))
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lesson_dir")
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

    accounts = detect_accounts(har, flow)

    if accounts:
        print(f"[accounts] {len(accounts)} auth account(s) in this flow:",
              file=sys.stderr)
        for a in accounts:
            tag = " (primary)" if a["is_primary"] else ""
            bits = []
            if a["cookie_names"]:
                bits.append(f"{len(a['cookie_names'])} cookie(s)")
            if a["auth_header_names"]:
                bits.append("headers " + ", ".join(a["auth_header_names"]))
            print(f"[accounts]   {a['host']}{tag} — {'; '.join(bits) or 'auth'}",
                  file=sys.stderr)
    else:
        print("[accounts] no auth accounts detected (anonymous flow)",
              file=sys.stderr)

    json.dump({"accounts": accounts}, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
