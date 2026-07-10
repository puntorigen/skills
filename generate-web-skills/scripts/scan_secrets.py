#!/usr/bin/env python3
"""Phase 4b of generate-web-skills: scan a HAR for embedded credentials/secrets.

Reports what sensitive material a HAR carries so the agent can warn the user
before bundling it into a shareable skill. It classifies findings by severity
and NEVER prints or emits any secret value — only names, locations, and hosts.

Severity:
  info      expected session material for replay (auth headers, cookies)
  warning   fields whose NAME looks secret (token/secret/api_key/...) with a
            value present, or long opaque high-entropy tokens
  critical  material that is dangerous to share even privately: payment-card
            numbers (Luhn-valid), plaintext password/cvv/ssn values, or
            private-key PEM blocks

Stdlib only. JSON report -> stdout; human summary -> stderr.

Usage: scan_secrets.py <session.har> [--max-response-chars N]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import OrderedDict
from urllib.parse import parse_qsl, urlsplit

from har_lib import (
    AUTH_HEADER_NAMES, SECRET_KEY_RE, decode_content, har_entries,
    header_map, load_har, parse_body,
)

PLAINTEXT_CRED_RE = re.compile(r"(password|passwd|pwd|passphrase|cvv|cvc|"
                               r"card[_-]?number|cardno|pan|ssn|secret[_-]?answer)",
                               re.I)
PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
SSN_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
CARD_CANDIDATE_RE = re.compile(r"(?:\d[ -]?){12,19}\d")
LONG_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-\.=+/]{40,}$")


def luhn_ok(digits: str) -> bool:
    if not (13 <= len(digits) <= 19) or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = {}
    for c in s:
        counts[c] = counts.get(c, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def looks_high_entropy(value: str) -> bool:
    v = value.strip()
    if len(v) < 40 or not LONG_TOKEN_RE.match(v):
        return False
    # JWTs and random tokens tend to exceed ~3.6 bits/char.
    return shannon_entropy(v) >= 3.6


class Scanner:
    def __init__(self):
        # dedup key -> finding (with count)
        self._by_key = OrderedDict()
        self._severity_rank = {"critical": 0, "warning": 1, "info": 2}

    def add(self, severity, category, location, name, host, note):
        key = (category, location, name or "", host or "")
        f = self._by_key.get(key)
        if f is None:
            self._by_key[key] = {
                "severity": severity, "category": category,
                "location": location, "name": name, "host": host,
                "note": note, "count": 1,
            }
        else:
            f["count"] += 1
            # escalate to the highest severity seen for this key
            if self._severity_rank[severity] < self._severity_rank[f["severity"]]:
                f["severity"] = severity
                f["note"] = note

    def scan_value(self, name, value, location, host):
        """Classify a single name/value pair. The value is never stored."""
        if value is None:
            return
        v = value if isinstance(value, str) else json.dumps(value)
        v = v.strip()
        if not v:
            return

        if PEM_RE.search(v):
            self.add("critical", "private-key", location, name, host,
                     "private-key PEM block present")
            return
        # payment card: only flag when Luhn-valid to avoid false positives.
        for m in CARD_CANDIDATE_RE.finditer(v):
            digits = re.sub(r"\D", "", m.group())
            if luhn_ok(digits):
                self.add("critical", "payment-card", location, name, host,
                         "value looks like a Luhn-valid card number")
                break
        if name and PLAINTEXT_CRED_RE.search(name):
            self.add("critical", "plaintext-credential", location, name, host,
                     "field name indicates a plaintext credential/PII value")
            return
        if SSN_RE.match(v):
            self.add("critical", "ssn", location, name, host,
                     "value matches a US SSN pattern")
            return
        if name and SECRET_KEY_RE.search(name):
            self.add("warning", "secret-field", location, name, host,
                     "field name looks secret (token/secret/api_key/...)")
            return
        if looks_high_entropy(v):
            self.add("warning", "opaque-token", location, name, host,
                     "long high-entropy value (likely a token/key)")

    def scan_json(self, data, location, host):
        if isinstance(data, dict):
            for k, val in data.items():
                if isinstance(val, (dict, list)):
                    self.scan_json(val, location, host)
                else:
                    self.scan_value(str(k), val, location, host)
        elif isinstance(data, list):
            for val in data[:50]:
                if isinstance(val, (dict, list)):
                    self.scan_json(val, location, host)
                else:
                    self.scan_value(None, val, location, host)

    def report(self):
        findings = sorted(
            self._by_key.values(),
            key=lambda f: (self._severity_rank[f["severity"]], f["category"],
                           f["location"], f["name"] or ""))
        summary = {"critical": 0, "warning": 0, "info": 0}
        for f in findings:
            summary[f["severity"]] += 1
        return {
            "findings": findings,
            "summary": summary,
            "has_critical": summary["critical"] > 0,
        }


def scan_har(har, max_response_chars):
    sc = Scanner()
    for e in har_entries(har):
        req = e.get("request") or {}
        resp = e.get("response") or {}
        host = urlsplit(req.get("url") or "").netloc

        hmap = header_map(req.get("headers"))
        for hn, hv in hmap.items():
            if hn in AUTH_HEADER_NAMES:
                sc.add("info", "auth-header", "request-header", hn, host,
                       "auth header required for replay")
            elif hn == "cookie":
                for kv in hv.split(";"):
                    nm = kv.split("=")[0].strip()
                    if nm:
                        sc.add("info", "cookie", "request-cookie", nm, host,
                               "session cookie required for replay")
            else:
                sc.scan_value(hn, hv, "request-header", host)

        for c in req.get("cookies") or []:
            nm = c.get("name")
            if nm:
                sc.add("info", "cookie", "request-cookie", nm, host,
                       "session cookie required for replay")

        for qn, qv in parse_qsl(urlsplit(req.get("url") or "").query,
                                keep_blank_values=True):
            sc.scan_value(qn, qv, "query", host)

        _, req_body = parse_body(req.get("postData"))
        if isinstance(req_body, (dict, list)):
            sc.scan_json(req_body, "request-body", host)
        elif isinstance(req_body, str):
            sc.scan_value(None, req_body, "request-body", host)

        for h in resp.get("headers") or []:
            if (h.get("name") or "").lower() == "set-cookie":
                nm = (h.get("value") or "").split("=")[0].strip()
                if nm:
                    sc.add("info", "cookie", "set-cookie", nm, host,
                           "server sets a session cookie")

        text = decode_content(resp.get("content") or {})
        if text:
            snippet = text[:max_response_chars]
            if PEM_RE.search(snippet):
                sc.add("critical", "private-key", "response-body", None, host,
                       "private-key PEM block in a response")
            stripped = snippet.lstrip()[:1]
            if stripped in "{[":
                try:
                    sc.scan_json(json.loads(text), "response-body", host)
                except Exception:
                    pass
    return sc.report()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("har_path")
    ap.add_argument("--max-response-chars", type=int, default=20000,
                    help="max chars scanned per response body (default 20000)")
    args = ap.parse_args()

    if not os.path.isfile(args.har_path):
        sys.exit(f"error: no such HAR file: {args.har_path}")

    har = load_har(args.har_path)
    report = scan_har(har, args.max_response_chars)

    # human summary to stderr — names/locations only, never values
    s = report["summary"]
    print(f"[scan] {s['critical']} critical, {s['warning']} warning, "
          f"{s['info']} info finding(s)", file=sys.stderr)
    for f in report["findings"]:
        if f["severity"] == "info":
            continue
        loc = f["location"]
        nm = f["name"] or "(value)"
        host = f["host"] or "?"
        print(f"[scan]   {f['severity'].upper():8} {f['category']:20} "
              f"{host} {loc}:{nm}  x{f['count']} — {f['note']}", file=sys.stderr)
    if report["has_critical"]:
        print("[scan] CRITICAL material detected. Warn the user before "
              "bundling this HAR; never print the values.", file=sys.stderr)

    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
