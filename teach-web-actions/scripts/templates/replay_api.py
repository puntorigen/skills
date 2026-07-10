#!/usr/bin/env python3
"""Replay this skill's recorded API flow with substituted parameters.

Self-contained: reads the embedded ../data/flow.json + ../data/session.har and
reissues every step of the recorded flow (auth warm-up, prerequisites, then the
primary data call) in order, reusing the live cookies/headers/bodies from the
HAR. Substitute the action's knobs with --set. Prints the PRIMARY step's
response so the caller can read the result.

Stdlib only (+ sibling har_auth.py). NEVER prints cookies, tokens, or auth
header values.

Usage:
  replay_api.py [--set NAME=VALUE ...] [--params-json '{"from":"SFO"}']
                [--only-primary] [--confirm-mutating] [--timeout SECONDS]
                [--max-print CHARS]

  --set from=SFO --set to=SEA --set date=2026-09-02   substitute query/body knobs
  --only-primary        run just the primary call (skip prerequisites; use when
                        the embedded session cookies are still valid)
  --confirm-mutating    required if the flow contains state-changing steps
                        (POST/PUT/PATCH/DELETE) — booking/purchase/etc.

If this skill was generated --with-setup, the setup host(s) ship no login: run
`bash scripts/setup.sh` once to capture your own session (data/user-auth.*.har),
which is overlaid here per host. Replay refuses those hosts until setup is done
(bypass with --skip-setup-check to use only the shipped session).
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import ssl
import sys
import zlib
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import har_auth

DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
DROP_REQUEST_HEADERS = {
    "host", "content-length", "connection", "accept-encoding",
    "transfer-encoding", "cookie",  # cookie re-added explicitly below
}
SETUP_PLACEHOLDER = "<setup-required>"


def load_json(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def parse_sets(pairs, params_json):
    subs = {}
    if params_json:
        subs.update(json.loads(params_json))
    for p in pairs or []:
        if "=" not in p:
            sys.exit(f"error: --set expects NAME=VALUE, got {p!r}")
        name, _, value = p.partition("=")
        subs[name.strip()] = value
    return subs


def coerce(new_value, original):
    """Keep the original scalar type where sensible."""
    if isinstance(original, bool):
        return str(new_value).lower() in ("1", "true", "yes")
    if isinstance(original, int) and not isinstance(original, bool):
        try:
            return int(new_value)
        except (TypeError, ValueError):
            return new_value
    if isinstance(original, float):
        try:
            return float(new_value)
        except (TypeError, ValueError):
            return new_value
    return new_value


def substitute_query(url, subs):
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    changed = []
    new_q = []
    for k, v in q:
        if k in subs:
            new_q.append((k, str(subs[k])))
            changed.append(k)
        else:
            new_q.append((k, v))
    if not changed:
        return url, changed
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(new_q), parts.fragment)), changed


def substitute_body(obj, subs, prefix="", changed=None):
    changed = changed if changed is not None else []
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            path = f"{prefix}.{k}" if prefix else str(k)
            v = obj[k]
            if isinstance(v, (dict, list)):
                substitute_body(v, subs, path, changed)
            elif path in subs:
                obj[k] = coerce(subs[path], v)
                changed.append(path)
            elif str(k) in subs:
                obj[k] = coerce(subs[str(k)], v)
                changed.append(str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (dict, list)):
                substitute_body(v, subs, f"{prefix}[{i}]", changed)
    return changed


def build_headers(req_headers, cookie_header, overrides=None):
    headers = {}
    for h in req_headers or []:
        name = (h.get("name") or "").strip()
        if not name or name.startswith(":"):  # HTTP/2 pseudo-headers
            continue
        if name.lower() in DROP_REQUEST_HEADERS:
            continue
        headers[name] = h.get("value", "")
    if overrides:
        low = {k.lower() for k in overrides}
        for k in list(headers):
            if k.lower() in low:
                del headers[k]
        headers.update(overrides)
    # never send stripped placeholders (setup host header not captured by user)
    for k in [k for k, v in headers.items()
              if isinstance(v, str) and SETUP_PLACEHOLDER in v]:
        del headers[k]
    if cookie_header and SETUP_PLACEHOLDER not in cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def decode_response(resp):
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in enc:
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    return raw.decode("utf-8", "replace")


def run_step(entry, subs, timeout, ctx, overlay=None):
    req = entry.get("request") or {}
    method = (req.get("method") or "GET").upper()
    url, _ = substitute_query(req.get("url") or "", subs)
    host = urlsplit(url).netloc

    body_bytes = None
    post = req.get("postData") or {}
    text = post.get("text")
    mime = (post.get("mimeType") or "").split(";")[0].strip()
    if text is not None:
        if "json" in mime or (text.lstrip()[:1] in "{[" if text.strip() else False):
            try:
                data = json.loads(text)
                substitute_body(data, subs)
                text = json.dumps(data)
            except Exception:
                pass
        elif "x-www-form-urlencoded" in mime:
            form = parse_qsl(text, keep_blank_values=True)
            form = [(k, str(subs[k]) if k in subs else v) for k, v in form]
            text = urlencode(form)
        body_bytes = text.encode("utf-8")
    elif post.get("params"):
        form = [(p.get("name", ""),
                 str(subs.get(p.get("name", ""), p.get("value", ""))))
                for p in post["params"]]
        body_bytes = urlencode(form).encode("utf-8")

    if overlay is not None and overlay.has(host):
        cookie_header = overlay.cookie_header(host)
        header_over = overlay.header_overrides(host)
    else:
        cookie_header = har_auth.cookie_header_for_entry(entry)
        header_over = None
    headers = build_headers(req.get("headers"), cookie_header, header_over)
    request = Request(url, data=body_bytes, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout, context=ctx) as resp:
            return resp.getcode(), decode_response(resp)
    except HTTPError as e:
        return e.code, decode_response(e)
    except URLError as e:
        return None, f"<request failed: {e.reason}>"


def print_primary(status, body, max_print):
    print(f"[replay] primary response: HTTP {status}")
    if body is None:
        print("[replay] no response body")
        return
    stripped = body.lstrip()[:1]
    if stripped in "{[":
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                print(f"[replay] JSON object keys: {', '.join(list(data)[:20])}")
            elif isinstance(data, list):
                print(f"[replay] JSON array: {len(data)} item(s)")
            out = json.dumps(data, ensure_ascii=False, indent=2)
            print(out[:max_print])
            if len(out) > max_print:
                print(f"... [truncated at {max_print} chars; use --max-print]")
            return
        except Exception:
            pass
    print(body[:max_print])
    if len(body) > max_print:
        print(f"... [truncated at {max_print} chars; use --max-print]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", action="append", metavar="NAME=VALUE",
                    help="substitute a query/body knob (repeatable)")
    ap.add_argument("--params-json", help="JSON object of knob substitutions")
    ap.add_argument("--only-primary", action="store_true",
                    help="run only the primary call, skipping prerequisites")
    ap.add_argument("--confirm-mutating", action="store_true",
                    help="allow running state-changing (POST/PUT/PATCH/DELETE) steps")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--max-print", type=int, default=20000)
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS certificate verification")
    ap.add_argument("--skip-setup-check", action="store_true",
                    help="don't require per-user setup logins (use only the "
                         "shipped session; setup hosts will likely 401)")
    args = ap.parse_args()

    flow = load_json("flow.json")
    har = har_auth.load_har(os.path.join(DATA_DIR, "session.har"))
    entries = (har.get("log") or {}).get("entries") or []
    subs = parse_sets(args.set, args.params_json)

    steps = flow.get("api_steps") or []
    if args.only_primary:
        steps = [s for s in steps if s.get("role") == "primary"]
    if not steps:
        sys.exit("error: no api_steps to replay in flow.json")

    mutating = [s for s in steps if s.get("mutating")]
    if mutating and not args.confirm_mutating:
        ids = ", ".join(f"step {s['step']} {s['endpoint_id']}" for s in mutating)
        sys.exit("refusing to replay: this flow contains state-changing steps "
                 f"({ids}). These may book/purchase/modify data. Re-run with "
                 "--confirm-mutating only after the user approves.")

    # per-user setup overlay: use each installer's own login for setup hosts
    setup_hosts = flow.get("setup_hosts") or []
    overlay = har_auth.AuthOverlay(setup_hosts, DATA_DIR) if setup_hosts else None
    if flow.get("requires_setup") and not args.skip_setup_check:
        needed = set()
        for s in steps:
            idx = s.get("har_entry_index")
            if idx is not None and 0 <= idx < len(entries):
                h = urlsplit((entries[idx].get("request") or {}).get("url") or "").netloc
                if overlay and overlay.is_setup_host(h) and not overlay.has(h):
                    needed.add(h.lower())
        if needed:
            sys.exit("setup required: no captured login for "
                     f"{', '.join(sorted(needed))}. Run `bash scripts/setup.sh` "
                     "first (or pass --skip-setup-check to use only the shipped "
                     "session, which will likely 401 for these hosts).")

    ctx = ssl.create_default_context()
    if args.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    if subs:
        print(f"[replay] substituting: {', '.join(sorted(subs))}", file=sys.stderr)

    primary_result = None
    for s in steps:
        idx = s.get("har_entry_index")
        if idx is None or idx < 0 or idx >= len(entries):
            sys.exit(f"error: step {s.get('step')} points at missing HAR entry {idx}")
        status, body = run_step(entries[idx], subs, args.timeout, ctx, overlay)
        tag = s.get("role", "step")
        print(f"[replay] step {s['step']} ({tag}) {s['method']} "
              f"{s['endpoint_id']} -> HTTP {status}", file=sys.stderr)
        if s.get("role") == "primary":
            primary_result = (status, body)

    if primary_result is None:
        # --only-primary excluded, or no primary flagged: use the last step
        primary_result = (status, body)
    print_primary(primary_result[0], primary_result[1], args.max_print)


if __name__ == "__main__":
    main()
