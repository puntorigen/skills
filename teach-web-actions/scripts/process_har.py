#!/usr/bin/env python3
"""Phase 2 of teach-web-actions: distill a recorded HAR into a reusable lesson.

Reads <lesson-dir>/session.har (+ optional actions.js, meta.json) and writes:
  - lesson.json  machine-readable endpoints, payloads, parameter knobs, auth
  - LESSON.md    human-readable summary

Stdlib only. Credential *values* (tokens, cookies, passwords) are redacted;
their names are kept so the agent knows what a replay needs.

Usage: process_har.py <lesson-dir> [--max-examples N] [--body-chars N]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from collections import OrderedDict
from urllib.parse import urlsplit, parse_qsl

# --- what to drop -----------------------------------------------------------
NOISE_HOSTS = (
    "google-analytics", "googletagmanager", "doubleclick", "google.com/ads",
    "gstatic", "fonts.googleapis", "facebook", "connect.facebook",
    "segment.io", "segment.com", "sentry.io", "datadoghq", "nr-data",
    "newrelic", "fullstory", "hotjar", "mixpanel", "amplitude", "intercom",
    "cdn.segment", "clarity.ms", "bing.com", "optimizely", "branch.io",
    "cloudflareinsights", "launchdarkly", "heap", "mouseflow", "adroll",
)
STATIC_EXT = (
    ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".map", ".mp4", ".webm",
    ".mp3", ".wav", ".avif", ".bmp",
)
KEEP_RESOURCE_TYPES = {"xhr", "fetch", "document"}
STATIC_RESOURCE_TYPES = {
    "stylesheet", "script", "image", "font", "media", "manifest", "texttrack",
    "eventsource", "websocket", "other", "ping", "cspviolationreport",
}

# --- auth / secrets ---------------------------------------------------------
AUTH_HEADER_NAMES = {
    "authorization", "x-api-key", "x-auth-token", "x-access-token",
    "x-csrf-token", "x-xsrf-token", "x-csrftoken", "api-key", "apikey",
    "x-amz-security-token",
}
SECRET_KEY_RE = re.compile(
    r"(password|passwd|passphrase|pwd|secret|token|authorization|api[_-]?key|"
    r"access[_-]?key|refresh|client[_-]?secret|signature|(^|_)sig($|_)|csrf|"
    r"xsrf|otp|(^|_)pin($|_)|ssn|(^|_)card|cvv|credential)",
    re.I,
)
# request headers worth keeping (besides any x-*)
KEEP_HEADER_PREFIXES = ("x-",)
KEEP_HEADER_EXACT = {"content-type", "accept", "origin", "referer"}

# --- parameter-knob detection ----------------------------------------------
KNOB_NAME_RE = re.compile(
    r"(date|day|month|year|from|to|orig|dest|depart|arriv|return|checkin|"
    r"checkout|start|end|city|airport|station|station|loc|lat|lon|lng|"
    r"page|offset|limit|cursor|size|per_?page|sort|order|q|query|search|"
    r"term|keyword|filter|category|type|status|id$|_id$|code|currency|"
    r"passenger|adult|child|guest|room|qty|quantity|amount|price|min|max)",
    re.I,
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2})?")
DATE2_RE = re.compile(r"^\d{1,2}[/.]\d{1,2}[/.]\d{2,4}$")
IATA_RE = re.compile(r"^[A-Z]{3}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
LONGHEX_RE = re.compile(r"^[0-9a-fA-F]{16,}$")

REDACTED = "<redacted>"


def classify_value(name: str, value: str) -> str | None:
    """Return a knob kind for a name/value pair, or None if not a candidate."""
    v = value.strip() if isinstance(value, str) else value
    if isinstance(v, str):
        if DATE_RE.match(v) or DATE2_RE.match(v):
            return "date"
        if IATA_RE.match(v):
            return "code"       # airport/currency/state-like 3-letter code
        if UUID_RE.match(v):
            return "uuid"
        if FLOAT_RE.match(v):
            return "number"
        if INT_RE.match(v):
            return "number"
    if name and KNOB_NAME_RE.search(name):
        return "named"          # name looks like a knob even if value is opaque
    return None


def redact(name: str, value):
    if name and SECRET_KEY_RE.search(name):
        return REDACTED
    return value


def flatten(obj, prefix=""):
    """Yield (dotted_path, scalar_value) for a JSON-ish structure."""
    if isinstance(obj, dict):
        for k, val in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            yield from flatten(val, key)
    elif isinstance(obj, list):
        # index the first few; collapse the rest
        for i, val in enumerate(obj[:5]):
            yield from flatten(val, f"{prefix}[{i}]")
    else:
        yield prefix, obj


# --- HAR helpers ------------------------------------------------------------
def header_map(headers):
    out = OrderedDict()
    for h in headers or []:
        n = (h.get("name") or "").strip()
        if n:
            out[n.lower()] = h.get("value", "")
    return out


def is_noise_host(host: str) -> bool:
    h = host.lower()
    return any(n in h for n in NOISE_HOSTS)


def looks_static(path: str, rtype: str, mime: str) -> bool:
    if rtype in STATIC_RESOURCE_TYPES:
        return True
    p = path.lower().split("?")[0]
    if any(p.endswith(ext) for ext in STATIC_EXT):
        return True
    m = (mime or "").lower()
    if m.startswith(("image/", "font/", "video/", "audio/")) or \
       m in ("text/css", "application/javascript", "text/javascript"):
        return True
    return False


def parse_body(post_data):
    """Return (mime, parsed_or_text). parsed is a dict/list if JSON, else str."""
    if not post_data:
        return None, None
    mime = (post_data.get("mimeType") or "").split(";")[0].strip()
    text = post_data.get("text")
    params = post_data.get("params")
    if params and not text:
        # urlencoded form captured as params
        return mime or "application/x-www-form-urlencoded", {
            p.get("name", ""): p.get("value", "") for p in params
        }
    if not text:
        return mime, None
    if "json" in (mime or "") or (text and text.lstrip()[:1] in "{["):
        try:
            return mime or "application/json", json.loads(text)
        except Exception:
            pass
    if "x-www-form-urlencoded" in (mime or ""):
        return mime, dict(parse_qsl(text))
    return mime or "text/plain", text


def decode_content(content):
    """Return decoded response text or None."""
    if not content:
        return None
    text = content.get("text")
    if text is None:
        return None
    if content.get("encoding") == "base64":
        try:
            raw = base64.b64decode(text)
            return raw.decode("utf-8", "replace")
        except Exception:
            return None
    return text


def response_summary(response, body_chars):
    content = response.get("content", {}) or {}
    mime = (content.get("mimeType") or "").split(";")[0].strip()
    out = {"status": response.get("status"), "mime": mime, "size": content.get("size")}
    text = decode_content(content)
    if text is None:
        return out
    if "json" in mime or text.lstrip()[:1] in "{[":
        try:
            data = json.loads(text)
            out["json_keys"] = _top_keys(data)
            out["json_sample"] = _truncate_json(data, body_chars)
            return out
        except Exception:
            pass
    out["text_snippet"] = text[:body_chars]
    return out


def _top_keys(data):
    if isinstance(data, dict):
        return list(data.keys())[:40]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return ["[]"] + list(data[0].keys())[:40]
    if isinstance(data, list):
        return ["[%d items]" % len(data)]
    return []


def _truncate_json(data, limit):
    s = json.dumps(data, ensure_ascii=False)[:limit]
    return s


# --- path templating for grouping ------------------------------------------
def path_template(path: str) -> str:
    segs = path.split("/")
    out = []
    for s in segs:
        if not s:
            out.append(s)
            continue
        if UUID_RE.match(s) or LONGHEX_RE.match(s) or INT_RE.match(s):
            out.append("{id}")
        else:
            out.append(s)
    return "/".join(out) or "/"


def guess_action(method: str, template: str) -> str:
    segs = [s for s in template.split("/") if s and s != "{id}"]
    tail = segs[-1] if segs else template
    low = template.lower()
    for kw, label in (
        ("availab", "check availability"), ("search", "search"),
        ("autocomplete", "autocomplete"), ("suggest", "suggestions"),
        ("login", "log in"), ("auth", "authenticate"), ("logout", "log out"),
        ("price", "get pricing"), ("quote", "get a quote"),
        ("book", "book"), ("reserv", "reserve"), ("checkout", "checkout"),
        ("cart", "cart operation"), ("order", "order"), ("pay", "payment"),
        ("list", "list"), ("detail", "get details"), ("profile", "profile"),
    ):
        if kw in low:
            verb = label
            break
    else:
        verb = {"GET": "fetch", "POST": "submit", "PUT": "update",
                "PATCH": "update", "DELETE": "delete"}.get(method, method.lower())
    # collapse adjacent duplicate words ("search search" -> "search")
    words = f"{verb} {tail}".strip().split()
    out = []
    for w in words:
        if not out or out[-1].lower() != w.lower():
            out.append(w)
    return " ".join(out)


MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def collect_params(method, url, req_body_mime, req_body, examples_bucket):
    """Return list of param candidates from query string + JSON body."""
    cands = OrderedDict()
    q = urlsplit(url).query
    for name, value in parse_qsl(q, keep_blank_values=True):
        kind = classify_value(name, value)
        if kind:
            cands.setdefault(("query", name), {
                "location": "query", "name": name, "kind": kind,
                "sample": redact(name, value)})
    if isinstance(req_body, (dict, list)):
        for path, value in flatten(req_body):
            if isinstance(value, (dict, list)) or value is None:
                continue
            sval = value if isinstance(value, str) else json.dumps(value)
            kind = classify_value(path.split(".")[-1].split("[")[0], sval)
            if kind:
                cands.setdefault(("body", path), {
                    "location": "body", "name": path, "kind": kind,
                    "sample": redact(path, sval)})
    return list(cands.values())


def interesting_req_headers(hmap):
    out = OrderedDict()
    auth = OrderedDict()
    for name, value in hmap.items():
        if name in AUTH_HEADER_NAMES:
            scheme = value.split(" ")[0] if " " in value else ""
            auth[name] = f"{scheme} {REDACTED}".strip() if scheme else REDACTED
        elif name in KEEP_HEADER_EXACT or name.startswith(KEEP_HEADER_PREFIXES):
            out[name] = redact(name, value)
    return out, auth


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

    with open(har_path, "r", encoding="utf-8", errors="replace") as fh:
        har = json.load(fh)

    meta = {}
    meta_path = os.path.join(ldir, "meta.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path) as fh:
                meta = json.load(fh)
        except Exception:
            meta = {}

    entries = (har.get("log") or {}).get("entries") or []
    total = len(entries)

    endpoints = OrderedDict()
    all_cookies = set()
    all_auth_headers = set()
    kept = 0

    for e in entries:
        req = e.get("request") or {}
        resp = e.get("response") or {}
        url = req.get("url") or ""
        method = (req.get("method") or "GET").upper()
        rtype = (e.get("_resourceType") or "").lower()
        parts = urlsplit(url)
        host = parts.netloc
        path = parts.path or "/"
        mime = ((resp.get("content") or {}).get("mimeType") or "")

        if not url or not host:
            continue
        if is_noise_host(host):
            continue
        if rtype not in KEEP_RESOURCE_TYPES and looks_static(path, rtype, mime):
            continue
        # if resource type is unknown, keep only api-ish or json responses
        if rtype and rtype not in KEEP_RESOURCE_TYPES and rtype in STATIC_RESOURCE_TYPES:
            continue

        kept += 1
        tmpl = path_template(path)
        key = f"{method} {host}{tmpl}"

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
        params = collect_params(method, url, req_mime, req_body, None)

        ep = endpoints.get(key)
        if ep is None:
            ep = {
                "id": key,
                "action_guess": guess_action(method, tmpl),
                "method": method,
                "host": host,
                "path_template": tmpl,
                "count": 0,
                "mutating": method in MUTATING,
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
                "url": url,
                "query": OrderedDict(
                    (k, redact(k, v)) for k, v in
                    parse_qsl(parts.query, keep_blank_values=True)),
                "started": e.get("startedDateTime"),
                "request_headers": keep_headers,
            }
            if req_body is not None:
                ex["request_body_mime"] = req_mime
                ex["request_body"] = _redact_body(req_body)
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


def _redact_body(body):
    if isinstance(body, dict):
        return {k: (REDACTED if SECRET_KEY_RE.search(str(k)) else _redact_body(v))
                for k, v in body.items()}
    if isinstance(body, list):
        return [_redact_body(v) for v in body[:10]]
    return body


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

    reads = [e for e in lesson["endpoints"] if not e["mutating"]]
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
