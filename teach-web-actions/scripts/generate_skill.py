#!/usr/bin/env python3
"""Phase 4 of teach-web-actions: generate a self-contained web-action skill.

Turns a distilled lesson (~/.web-lessons/<host>/<lesson>/) into a standalone,
shareable skill directory that performs ONE primary action — including every
recorded prerequisite step — with the session (HAR) embedded. The output has no
runtime dependency on teach-web-actions or the original lesson.

Pipeline (delegates to sibling scripts):
  infer_flow.py  -> flow.json (primary + prerequisite chain)
  scan_secrets.py-> credential report (gate on CRITICAL findings)
  trim_har.py    -> data/session.har + data/flow.json (flow-scoped, re-indexed)
  then copies replay tooling + renders SKILL.md / REFERENCE.md / SECURITY.md.

Usage:
  generate_skill.py <lesson-dir> [--output DIR] [--name NAME]
      [--format cursor-personal|cursor-project|skills-sh]
      [--endpoint "GET host/path"] [--label TEXT]
      [--allow-critical] [--explicit-only] [--force] [--max-body-bytes N]

--allow-critical must be passed (after the user acknowledges) if the embedded
session contains CRITICAL material (cards, plaintext passwords, SSNs, keys).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(SCRIPTS_DIR, "templates")
COPY_SCRIPTS = ["har_auth.py", "replay_api.py", "run_variant.js",
                "replay_ui.sh", "package.json"]
TLDS = {"com", "net", "org", "io", "co", "uk", "gov", "edu", "ai", "app",
        "dev", "us", "info", "me", "tv"}


def run(cmd, capture=False):
    r = subprocess.run(cmd, stdout=(subprocess.PIPE if capture else None),
                       text=True)
    if r.returncode != 0:
        sys.exit(f"error: command failed ({r.returncode}): {' '.join(cmd)}")
    return r.stdout if capture else None


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "x"


def host_slug(host: str) -> str:
    h = (host or "").lower()
    if h.startswith("www."):
        h = h[4:]
    labels = [x for x in h.split(".") if x]
    while len(labels) > 1 and labels[-1] in TLDS:
        labels.pop()
    return slug("-".join(labels)) if labels else "site"


def read_tmpl(name):
    with open(os.path.join(TEMPLATES_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def replace_tokens(text, mapping):
    for k, v in mapping.items():
        text = text.replace(k, v)
    return text


def build_knobs(cands):
    if not cands:
        return "- (none detected — the primary call takes no obvious parameters)"
    return "\n".join(
        f"- `{p['name']}` ({p['location']}, {p['kind']}) — recorded value "
        f"`{p.get('sample')}`" for p in cands)


def build_set_example(cands):
    parts = []
    for p in cands[:4]:
        val = p.get("sample")
        if not val or val == "<redacted>":
            val = "VALUE"
        parts.append(f"--set {p['name']}={val}")
    return " ".join(parts)


def build_steps_table(steps):
    rows = ["| # | role | method | endpoint | knobs |",
            "|---|------|--------|----------|-------|"]
    for s in steps:
        ps = s.get("param_summary") or {}
        knobs = list((ps.get("query") or {}).keys())
        body = ps.get("body")
        if isinstance(body, dict):
            knobs += list(body.keys())
        knobtxt = ", ".join(knobs) if knobs else "—"
        flag = " (MUTATING)" if s.get("mutating") else ""
        rows.append(f"| {s['step']} | {s['role']}{flag} | {s['method']} | "
                    f"`{s['endpoint_id']}` | {knobtxt} |")
    return "\n".join(rows)


def build_ui_block(has_ui):
    if not has_ui:
        return "\n"
    return (
        "\n## UI replay (mp4 proof)\n\n"
        "Produce a video of the flow in a real browser (uses the embedded\n"
        "session cookies; edit `scripts/variant.js` to change inputs):\n\n"
        "```bash\n"
        "bash \"$SKILL_DIR/scripts/replay_ui.sh\"\n"
        "```\n\n"
        "It writes `runs/<timestamp>/proof.mp4`. Hand that to the review-mp4\n"
        "skill to verify the flow.\n")


def build_mutating_note(mutating_steps):
    if mutating_steps:
        return (f"This flow includes state-changing step(s) {mutating_steps} — "
                "replay refuses them unless you pass `--confirm-mutating` after "
                "the user approves.")
    return "No state-changing steps were recorded; the flow is read-only."


def build_findings(report):
    fs = [f for f in report.get("findings", []) if f["severity"] != "info"]
    if not fs:
        return ("- No warning/critical findings beyond the expected session "
                "cookies and auth headers.")
    rows = ["| severity | category | location | field |",
            "|----------|----------|----------|-------|"]
    for f in fs:
        rows.append(f"| {f['severity']} | {f['category']} | {f['location']} | "
                    f"{f['name'] or '(value)'} |")
    return "\n".join(rows)


def build_critical_block(report):
    if not report.get("has_critical"):
        return ""
    return (
        "> **CRITICAL:** the embedded session contains highly sensitive material\n"
        "> (see the table above — payment cards, plaintext passwords, SSNs, or\n"
        "> private keys). Do NOT share this skill, even privately, without\n"
        "> removing or rotating this data. The values are never written into\n"
        "> these docs, but they remain inside `data/session.har`.\n")


def build_ui_steps(flow):
    steps = flow.get("ui_steps") or []
    if steps:
        return "\n".join("  " + s + ";" for s in steps)
    url = flow.get("source_url") or ""
    return (f"  await page.goto({json.dumps(url)});\n"
            "  // TODO: add the UI steps for this action "
            "(no actions.js was recorded).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lesson_dir")
    ap.add_argument("--output", help="output skill directory")
    ap.add_argument("--name", help="skill name (kebab-case; default derived)")
    ap.add_argument("--format", default="cursor-personal",
                    choices=["cursor-personal", "cursor-project", "skills-sh"],
                    help="where to place the skill when --output is omitted")
    ap.add_argument("--endpoint", help="force a primary endpoint id (passed to "
                    "infer_flow.py)")
    ap.add_argument("--label", help="human action label")
    ap.add_argument("--allow-critical", action="store_true",
                    help="proceed even if CRITICAL secrets are detected "
                         "(only after the user acknowledges)")
    ap.add_argument("--explicit-only", action="store_true",
                    help="mark the skill disable-model-invocation (loads only "
                         "when named, not auto-triggered)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite the output directory if it exists")
    ap.add_argument("--max-body-bytes", type=int, default=20000)
    ap.add_argument("--reinfer", action="store_true",
                    help="regenerate flow.json even if it already exists")
    args = ap.parse_args()

    ldir = os.path.abspath(args.lesson_dir)
    lesson_path = os.path.join(ldir, "lesson.json")
    har_path = os.path.join(ldir, "session.har")
    if not os.path.isfile(lesson_path):
        sys.exit(f"error: no lesson.json in {ldir} (run process_har.py first)")
    if not os.path.isfile(har_path):
        sys.exit(f"error: no session.har in {ldir}")

    # 1. flow ---------------------------------------------------------------
    flow_path = os.path.join(ldir, "flow.json")
    if args.reinfer or not os.path.isfile(flow_path):
        cmd = [sys.executable, os.path.join(SCRIPTS_DIR, "infer_flow.py"), ldir]
        if args.endpoint:
            cmd += ["--endpoint", args.endpoint]
        if args.label:
            cmd += ["--label", args.label]
        run(cmd)

    # 2. secret scan --------------------------------------------------------
    scan_out = run([sys.executable, os.path.join(SCRIPTS_DIR, "scan_secrets.py"),
                    har_path], capture=True)
    report = json.loads(scan_out)
    if report.get("has_critical") and not args.allow_critical:
        sys.exit(
            "\nCRITICAL secrets detected in the recorded session (names/"
            "locations shown above; values never printed). Bundling them into a\n"
            "shareable skill is risky. Tell the user what was found, and only\n"
            "re-run with --allow-critical once they acknowledge (or re-record\n"
            "without entering that data).")

    with open(lesson_path, encoding="utf-8") as fh:
        lesson = json.load(fh)
    with open(flow_path, encoding="utf-8") as fh:
        flow = json.load(fh)

    host = flow.get("host") or lesson.get("host") or "site"
    action_label = args.label or flow.get("action_label") or "web action"
    primary_endpoint = flow.get("primary_endpoint_id") or ""
    source_url = flow.get("source_url") or lesson.get("source_url") or ""
    cands = flow.get("primary_param_candidates") or []

    # 3. name + output dir --------------------------------------------------
    name = args.name or f"{host_slug(host)}-{slug(action_label)}"
    name = name[:64].strip("-") or "web-action"
    if args.output:
        out = os.path.abspath(os.path.expanduser(args.output))
    elif args.format == "cursor-personal":
        out = os.path.join(os.path.expanduser("~/.cursor/skills"), name)
    elif args.format == "cursor-project":
        out = os.path.join(os.getcwd(), ".cursor/skills", name)
    else:  # skills-sh
        out = os.path.join(os.getcwd(), name)

    if os.path.exists(out):
        if not args.force:
            sys.exit(f"error: {out} already exists (use --force to overwrite)")
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "data"))
    os.makedirs(os.path.join(out, "scripts"))

    # 4. trimmed HAR + re-indexed flow into data/ ---------------------------
    run([sys.executable, os.path.join(SCRIPTS_DIR, "trim_har.py"), ldir,
         "--out-har", os.path.join(out, "data", "session.har"),
         "--out-flow", os.path.join(out, "data", "flow.json"),
         "--max-body-bytes", str(args.max_body_bytes)])
    # the embedded flow is the re-indexed one; use it for the docs
    with open(os.path.join(out, "data", "flow.json"), encoding="utf-8") as fh:
        embedded_flow = json.load(fh)

    # 5. copy reference data ------------------------------------------------
    shutil.copyfile(lesson_path, os.path.join(out, "data", "lesson.json"))
    for opt in ("meta.json", "actions.js"):
        src = os.path.join(ldir, opt)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(out, "data", opt))

    # 6. copy replay tooling ------------------------------------------------
    for fn in COPY_SCRIPTS:
        dst = os.path.join(out, "scripts", fn)
        shutil.copyfile(os.path.join(TEMPLATES_DIR, fn), dst)
        if fn.endswith((".sh", ".py")):
            os.chmod(dst, 0o755)

    # 7. render templates ---------------------------------------------------
    knobnames = ", ".join(p["name"] for p in cands[:6])
    description = (
        f"Perform '{action_label}' on {host} by replaying a recorded, embedded "
        f"browser session with your own parameters"
        + (f" ({knobnames})" if knobnames else "")
        + f". Self-contained: bundles the session needed to call "
        f"{primary_endpoint}. Use when the user asks to {action_label} on {host}, "
        f"mentions {host}, or wants to replay or vary this web action.")
    description = " ".join(description.split())[:1000]
    title = f"{action_label[:1].upper()}{action_label[1:]} on {host}"

    auth = lesson.get("auth_surface") or {}
    cookie_names = ", ".join(auth.get("cookies_seen") or []) or "none"
    auth_headers = ", ".join(auth.get("auth_headers_seen") or []) or "none"

    tokens = {
        "%%SKILL_NAME%%": name,
        "%%TITLE%%": title,
        "%%DESCRIPTION%%": description,
        "%%INVOCATION_LINE%%": ("disable-model-invocation: true\n"
                                if args.explicit_only else ""),
        "%%ACTION_LABEL%%": action_label,
        "%%HOST%%": host,
        "%%SOURCE_URL%%": source_url or "(unknown)",
        "%%PRIMARY_ENDPOINT%%": primary_endpoint,
        "%%RECORDED_AT%%": lesson.get("recorded_at") or "(unknown)",
        "%%KNOBS_LIST%%": build_knobs(cands),
        "%%SET_FLAGS_EXAMPLE%%": build_set_example(cands),
        "%%FLOW_STEPS_TABLE%%": build_steps_table(embedded_flow.get("api_steps") or []),
        "%%UI_BLOCK%%": build_ui_block(embedded_flow.get("has_ui")),
        "%%MUTATING_NOTE%%": build_mutating_note(embedded_flow.get("mutating_steps") or []),
        "%%SECURITY_FINDINGS%%": build_findings(report),
        "%%CRITICAL_BLOCK%%": build_critical_block(report),
        "%%COOKIE_NAMES%%": cookie_names,
        "%%AUTH_HEADER_NAMES%%": auth_headers,
        "%%UI_STEPS%%": build_ui_steps(embedded_flow),
    }

    for tmpl, dest in (("SKILL.md.tmpl", "SKILL.md"),
                       ("REFERENCE.md.tmpl", "REFERENCE.md"),
                       ("SECURITY.md.tmpl", "SECURITY.md")):
        with open(os.path.join(out, dest), "w", encoding="utf-8") as fh:
            fh.write(replace_tokens(read_tmpl(tmpl), tokens))
    with open(os.path.join(out, "scripts", "variant.js"), "w", encoding="utf-8") as fh:
        fh.write(replace_tokens(read_tmpl("variant.js.tmpl"), tokens))

    # 8. report -------------------------------------------------------------
    print(f"[generate] skill '{name}' -> {out}")
    print(f"[generate] primary: {primary_endpoint}")
    s = report["summary"]
    print(f"[generate] secret scan: {s['critical']} critical, "
          f"{s['warning']} warning, {s['info']} info")
    if report.get("has_critical"):
        print("[generate] NOTE: embedded session has CRITICAL material — see "
              "SECURITY.md; do not share publicly.")
    if args.format == "skills-sh":
        print(f"[generate] install/test: npx skills add {out}")
    print("[generate] API replay:  python3 "
          f"{os.path.join(out, 'scripts', 'replay_api.py')} "
          + build_set_example(cands))


if __name__ == "__main__":
    main()
