#!/usr/bin/env python3
"""Session-material helper for a generated web-action skill.

Reads the skill's embedded HAR (../data/session.har) and extracts the live
session cookies needed to replay the recorded flow:

  - as a Playwright cookie array (for UI replay via run_variant.js), or
  - as a per-request `Cookie` header string (used as a library by replay_api.py).

If the skill was generated --with-setup, some hosts ship NO login: their
credentials were stripped and each user captures their own via setup.sh into
../data/user-auth.<host>.har. This module overlays those per-user sessions on
top of the shipped HAR (both for UI cookies and, via AuthOverlay, for API
replay headers).

Self-contained: stdlib only, no dependency on teach-web-actions. It never logs
cookie or token values.

Usage (UI replay):  har_auth.py [<session.har>] [--out cookies.json] [--overlay-setup]
Usage (setup gate): har_auth.py --check-setup
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.parse import urlsplit

SETUP_PLACEHOLDER = "<setup-required>"
AUTH_HEADER_NAMES = {
    "authorization", "x-api-key", "x-auth-token", "x-access-token",
    "x-csrf-token", "x-xsrf-token", "x-csrftoken", "api-key", "apikey",
    "x-amz-security-token",
}


def _entries(har):
    return (har.get("log") or {}).get("entries") or []


def load_har(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return json.load(fh)


def _script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def default_har_path():
    return os.path.normpath(os.path.join(_script_dir(), "..", "data", "session.har"))


def host_slug(host):
    return re.sub(r"[^a-z0-9]+", "-", (host or "").lower()).strip("-") or "host"


def user_har_path(data_dir, host):
    return os.path.join(data_dir, f"user-auth.{host_slug(host)}.har")


def flow_setup_info(data_dir):
    """(setup_hosts, requires_setup) read from ../data/flow.json (empty if none)."""
    fp = os.path.join(data_dir, "flow.json")
    if not os.path.isfile(fp):
        return [], False
    try:
        with open(fp, encoding="utf-8") as fh:
            flow = json.load(fh)
    except (OSError, ValueError):
        return [], False
    return list(flow.get("setup_hosts") or []), bool(flow.get("requires_setup"))


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


def cookies_with_overlay(har, setup_hosts, data_dir):
    """UI cookies: shipped cookies for normal hosts + each user's own for setup hosts.

    Setup hosts' shipped cookies were stripped (placeholders), so they are
    dropped and replaced by cookies from ../data/user-auth.<host>.har."""
    setupset = {h.lower() for h in (setup_hosts or [])}
    out = []
    for c in cookies_for_har(har):
        host = urlsplit(c["url"]).netloc.lower()
        if host in setupset or c["value"] == SETUP_PLACEHOLDER:
            continue
        out.append(c)
    for host in setup_hosts or []:
        p = user_har_path(data_dir, host)
        if os.path.isfile(p):
            out.extend(cookies_for_har(load_har(p)))
    return out


def _extract_auth_from_har(har):
    """(cookies{name:value}, headers{name:value}) from a captured user HAR."""
    cookies, headers = {}, {}
    for e in _entries(har):
        req = e.get("request") or {}
        for c in req.get("cookies") or []:
            nm, val = c.get("name"), c.get("value")
            if nm and val not in (None, SETUP_PLACEHOLDER):
                cookies[nm] = val
        for h in req.get("headers") or []:
            nm = h.get("name") or ""
            low = nm.lower()
            val = h.get("value") or ""
            if low == "cookie":
                for kv in val.split(";"):
                    n, sep, v = kv.strip().partition("=")
                    if sep and n and v != SETUP_PLACEHOLDER:
                        cookies[n] = v
            elif low in AUTH_HEADER_NAMES and SETUP_PLACEHOLDER not in val:
                headers[nm] = val
    return cookies, headers


class AuthOverlay:
    """Per-host, per-user auth captured by setup.sh, for API replay.

    For hosts in `setup_hosts`, replaces the (stripped) shipped cookies/auth
    headers with the values captured into ../data/user-auth.<host>.har."""

    def __init__(self, setup_hosts, data_dir):
        self.data_dir = data_dir
        self.setup_hosts = [h.lower() for h in (setup_hosts or [])]
        self._loaded = {}
        self._by_host = {}
        for host in self.setup_hosts:
            p = user_har_path(data_dir, host)
            self._loaded[host] = os.path.isfile(p)
            if self._loaded[host]:
                self._by_host[host] = _extract_auth_from_har(load_har(p))

    def is_setup_host(self, host):
        return (host or "").lower() in self.setup_hosts

    def has(self, host):
        return (host or "").lower() in self._by_host

    def missing(self):
        return [h for h in self.setup_hosts if not self._loaded.get(h)]

    def cookie_header(self, host):
        cookies, _ = self._by_host[(host or "").lower()]
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    def header_overrides(self, host):
        _, headers = self._by_host[(host or "").lower()]
        return dict(headers)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("har_path", nargs="?", default=default_har_path())
    ap.add_argument("--out", help="write cookies JSON here (default stdout)")
    ap.add_argument("--overlay-setup", action="store_true",
                    help="overlay per-user setup logins (data/user-auth.*.har)")
    ap.add_argument("--check-setup", action="store_true",
                    help="verify all required setup logins exist; exit 3 if not")
    args = ap.parse_args()

    data_dir = os.path.dirname(os.path.abspath(args.har_path)) or "."

    if args.check_setup:
        setup_hosts, requires = flow_setup_info(data_dir)
        missing = AuthOverlay(setup_hosts, data_dir).missing()
        if requires and missing:
            print("[auth] setup required — no captured login for: "
                  + ", ".join(missing) + ". Run scripts/setup.sh first.",
                  file=sys.stderr)
            sys.exit(3)
        sys.exit(0)

    if not os.path.isfile(args.har_path):
        sys.exit(f"error: no HAR at {args.har_path}")
    har = load_har(args.har_path)
    if args.overlay_setup:
        setup_hosts, _ = flow_setup_info(data_dir)
        cookies = cookies_with_overlay(har, setup_hosts, data_dir)
    else:
        cookies = cookies_for_har(har)
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
