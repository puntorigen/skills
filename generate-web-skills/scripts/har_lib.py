#!/usr/bin/env python3
"""Shared HAR parsing, filtering, redaction, and knob-detection helpers.

Used by the generate-web-skills pipeline:
  - process_har.py  (Phase 2 distillation)
  - infer_flow.py   (Phase 4 primary-action + prerequisite chain)
  - scan_secrets.py (Phase 4 credential scan)
  - trim_har.py     (Phase 4 embeddable HAR)

Stdlib only. This module never *prints* anything; redaction helpers keep
credential names but replace their values with a placeholder.
"""
from __future__ import annotations

import base64
import json
import re
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
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def classify_value(name: str, value: str) -> "str | None":
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


def collect_params(url, req_body):
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


def redact_body(body):
    if isinstance(body, dict):
        return {k: (REDACTED if SECRET_KEY_RE.search(str(k)) else redact_body(v))
                for k, v in body.items()}
    if isinstance(body, list):
        return [redact_body(v) for v in body[:10]]
    return body


# --- entry classification (shared keep filter) ------------------------------
def entry_basics(e):
    """Extract the fields the keep filter and grouping need from a HAR entry."""
    req = e.get("request") or {}
    resp = e.get("response") or {}
    url = req.get("url") or ""
    parts = urlsplit(url)
    return {
        "req": req,
        "resp": resp,
        "url": url,
        "method": (req.get("method") or "GET").upper(),
        "rtype": (e.get("_resourceType") or "").lower(),
        "host": parts.netloc,
        "path": parts.path or "/",
        "query": parts.query,
        "mime": ((resp.get("content") or {}).get("mimeType") or ""),
    }


def keep_entry(b) -> bool:
    """Whether a HAR entry (as basics dict) carries the action, not page chrome."""
    if not b["url"] or not b["host"]:
        return False
    if is_noise_host(b["host"]):
        return False
    if b["rtype"] not in KEEP_RESOURCE_TYPES and \
       looks_static(b["path"], b["rtype"], b["mime"]):
        return False
    # unknown/static resource types that are not xhr/fetch/document are dropped
    if b["rtype"] and b["rtype"] not in KEEP_RESOURCE_TYPES and \
       b["rtype"] in STATIC_RESOURCE_TYPES:
        return False
    return True


def endpoint_key(b) -> str:
    """The `METHOD host path-template` grouping key (matches lesson.json ids)."""
    return f"{b['method']} {b['host']}{path_template(b['path'])}"


def iter_kept_entries(entries):
    """Yield (index, entry, basics) for entries passing the keep filter."""
    for i, e in enumerate(entries):
        b = entry_basics(e)
        if keep_entry(b):
            yield i, e, b


def load_har(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return json.load(fh)


def har_entries(har):
    return (har.get("log") or {}).get("entries") or []
