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
      [--with-setup] [--setup-hosts host1,host2] [--list-auth-accounts]

--allow-critical must be passed (after the user acknowledges) if the embedded
session contains CRITICAL material (cards, plaintext passwords, SSNs, keys).

--with-setup makes the skill capture each installer's OWN login: the selected
hosts' credentials are stripped from the shipped HAR and a setup.sh walks the
user through logging into each service. --setup-hosts limits which detected
hosts get setup (default: all detected). --list-auth-accounts prints the
detected auth accounts as JSON and exits (use it before asking the user).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from detect_auth_accounts import detect_accounts
from har_lib import load_har

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(SCRIPTS_DIR, "templates")
COPY_SCRIPTS = ["har_auth.py", "replay_api.py", "run_variant.js",
                "replay_ui.sh", "package.json"]
SETUP_SCRIPTS = ["setup.sh", "setup_login.js"]
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


def _account_bits(a):
    bits = []
    n = len(a.get("cookie_names") or [])
    if n:
        bits.append(f"{n} cookie(s)")
    if a.get("auth_header_names"):
        bits.append("auth header(s)")
    return f" — {', '.join(bits)}" if bits else ""


def build_setup_block(setup_accounts):
    if not setup_accounts:
        return ""
    lines = [
        "## Setup (first run — per user)",
        "",
        "This skill ships **no** login for the account(s) below. Before your "
        "first replay, capture your OWN session:",
        "",
    ]
    for a in setup_accounts:
        lines.append(f"- **{a['label']}** (`{a['host']}`){_account_bits(a)}")
    lines += [
        "",
        "```bash",
        'bash "$SKILL_DIR/scripts/setup.sh"',
        "```",
        "",
        "A browser opens for each service; log in, then press Enter in the "
        "terminal to continue to the next. Your session is saved locally to "
        "`data/user-auth.<host>.har` (gitignored) and never leaves your "
        "machine. Re-run setup to refresh or switch accounts.",
        "",
    ]
    return "\n".join(lines)


def build_credentials_note(setup_enabled):
    if setup_enabled:
        return ("This skill ships **no** login for its setup account(s); each "
                "user captures their own via the setup step below. See "
                "[SECURITY.md](SECURITY.md).")
    return ("Read [SECURITY.md](SECURITY.md) before sharing this skill — "
            "`data/session.har` holds **live session credentials**.")


def build_setup_security(setup_accounts, setup_hosts, kept_accounts):
    if not setup_hosts:
        return ""
    hosts = ", ".join(f"`{h}`" for h in setup_hosts)
    lines = [
        "## Per-user setup",
        "",
        f"Credentials for {hosts} were **stripped** from `data/session.har` "
        "(placeholders only). Each user runs `scripts/setup.sh` to capture "
        "their own login into `data/user-auth.<host>.har`, which:",
        "",
        "- is **gitignored** — never commit or share it,",
        "- stays on that user's machine,",
        "- is overlaid onto replay in place of the (absent) recorder session.",
        "",
    ]
    if kept_accounts:
        kept = ", ".join(f"`{a['host']}`" for a in kept_accounts)
        lines += [
            f"Note: {kept} still carr{'ies' if len(kept_accounts) == 1 else 'y'} "
            "the recorder's embedded auth (not part of setup).",
            "",
        ]
    return "\n".join(lines)


def build_session_intro(setup_enabled, all_stripped):
    if not setup_enabled:
        return ("This skill embeds a recorded browser session so it can replay "
                "the action independently. That session is **live credential "
                "material**.")
    if all_stripped:
        return ("This skill replays the action but ships **no** login: every "
                "auth host is captured per-user via setup (below). "
                "`data/session.har` holds request URLs, headers, and bodies "
                "only — no live session cookies or tokens.")
    return ("This skill embeds a recorded browser session. Some hosts ship "
            "**live credential material**; the per-user setup host(s) below "
            "were stripped and are supplied by each user via setup.")


def build_session_contents(setup_enabled, all_stripped, host, cookie_names,
                           auth_headers, kept_accounts):
    head = "## What `data/session.har` contains\n\n"
    if not setup_enabled:
        return (head
                + f"- Session **cookies** for `{host}`: {cookie_names}\n"
                + f"- **Auth headers**: {auth_headers}\n"
                + "- The exact request bodies and headers recorded during the "
                  "session.\n\n"
                + f"Anyone who can read `data/session.har` can act as the "
                  f"recorded user on `{host}` until those credentials expire or "
                  "are rotated.")
    if all_stripped:
        return (head
                + "- Request URLs, query params, headers, and bodies for the flow.\n"
                + "- **No** live session cookies or auth tokens — they were "
                  "stripped for per-user setup (placeholders only).")
    kept = ", ".join(f"`{a['host']}`" for a in kept_accounts)
    return (head
            + f"- Live session **cookies/auth headers** for the non-setup "
              f"host(s): {kept}.\n"
            + "- Request URLs, query params, headers, and bodies for the flow.\n"
            + "- Stripped placeholders for the per-user setup host(s) (see below).\n\n"
            + f"Anyone who can read `data/session.har` can act as the recorded "
              f"user on {kept} until those credentials expire or are rotated.")


def build_sharing_rules(all_stripped):
    if all_stripped:
        return (
            "- This skill ships **no** login, so it is safer to share broadly "
            "than a credential-embedding skill — each recipient signs in as "
            "themselves via `scripts/setup.sh`.\n"
            "- Never commit or post captured logins: `data/user-auth.*.har` is "
            "gitignored — keep it that way.\n"
            "- If replay returns 401/403, a user's captured session expired — "
            "re-run `scripts/setup.sh`.")
    return (
        "- **Safe:** sharing privately with people you already trust with this "
        "account (they gain the same access the recorded session had).\n"
        "- **Not safe:** committing this skill to a public repository, posting "
        "it, or sending it to untrusted parties. That leaks a live session.\n"
        "- Before publishing anywhere public, **rotate/expire the credentials** "
        "(log out the recorded session, revoke tokens) or re-record against a "
        "throwaway account. Or regenerate `--with-setup` so no login ships.\n"
        "- Sessions expire. When replay starts returning 401/403, the embedded "
        "session is dead — re-record with `teach-web-actions` and regenerate.")


def build_setup_reference(setup_hosts):
    if not setup_hosts:
        return ""
    hosts = ", ".join(f"`{h}`" for h in setup_hosts)
    return "\n".join([
        "## Per-user setup",
        "",
        f"`flow.json` has `requires_setup: true` and `setup_hosts` = {hosts}. "
        "Those hosts' cookies/auth headers were stripped from `session.har`; "
        "`data/auth_accounts.json` lists every detected account and whether it "
        "needs setup.",
        "",
        "`scripts/setup.sh` opens `scripts/setup_login.js` (Playwright) once per "
        "setup host, waits for the user to finish logging in, and writes the "
        "captured cookies/headers to `data/user-auth.har`. At replay time "
        "`har_auth.py` overlays `user-auth.har` on top of `session.har` per "
        "host, so each user acts as themselves on the setup services while the "
        "recorder's own session is used for any non-setup hosts.",
        "",
        "Replay refuses to run a setup host with no captured login (run "
        "`setup.sh` first).",
        "",
    ])


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
    ap.add_argument("--with-setup", action="store_true",
                    help="capture each installer's OWN login: strip selected "
                         "hosts' credentials from the shipped HAR and ship a "
                         "setup.sh that logs the user into each service")
    ap.add_argument("--setup-hosts",
                    help="comma-separated subset of detected auth hosts to set "
                         "up (default: all detected)")
    ap.add_argument("--list-auth-accounts", action="store_true",
                    help="print detected auth accounts as JSON and exit")
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

    # 1b. auth-account detection -------------------------------------------
    with open(flow_path, encoding="utf-8") as fh:
        _flow_for_detect = json.load(fh)
    accounts = detect_accounts(load_har(har_path), _flow_for_detect)
    detected_hosts = [a["host"] for a in accounts]

    if args.list_auth_accounts:
        json.dump({"accounts": accounts}, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return

    # resolve which hosts get a per-user setup step
    setup_hosts = []
    if args.with_setup:
        if not accounts:
            sys.exit("error: --with-setup given but no auth accounts were "
                     "detected in this flow (nothing to set up).")
        if args.setup_hosts:
            requested = [h.strip() for h in args.setup_hosts.split(",") if h.strip()]
            unknown = [h for h in requested if h not in detected_hosts]
            if unknown:
                sys.exit(f"error: --setup-hosts includes undetected host(s): "
                         f"{', '.join(unknown)}. Detected: {', '.join(detected_hosts)}")
            setup_hosts = requested
        else:
            setup_hosts = list(detected_hosts)
    elif args.setup_hosts:
        sys.exit("error: --setup-hosts requires --with-setup")
    setup_enabled = bool(setup_hosts)

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

    # 4b. setup mode: strip selected hosts + write account manifest --------
    if setup_enabled:
        data_har = os.path.join(out, "data", "session.har")
        run([sys.executable, os.path.join(SCRIPTS_DIR, "strip_har_auth.py"),
             data_har, "--hosts", ",".join(setup_hosts)])
        for a in accounts:
            a["setup_required"] = a["host"] in setup_hosts
        with open(os.path.join(out, "data", "auth_accounts.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"accounts": accounts, "setup_hosts": setup_hosts},
                      fh, indent=2, ensure_ascii=False)
        embedded_flow["requires_setup"] = True
        embedded_flow["setup_hosts"] = setup_hosts
        with open(os.path.join(out, "data", "flow.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(embedded_flow, fh, indent=2, ensure_ascii=False)

    # 5. copy reference data ------------------------------------------------
    shutil.copyfile(lesson_path, os.path.join(out, "data", "lesson.json"))
    for opt in ("meta.json", "actions.js"):
        src = os.path.join(ldir, opt)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(out, "data", opt))

    # 6. copy replay tooling ------------------------------------------------
    scripts_to_copy = list(COPY_SCRIPTS)
    if setup_enabled:
        scripts_to_copy += SETUP_SCRIPTS
    for fn in scripts_to_copy:
        dst = os.path.join(out, "scripts", fn)
        shutil.copyfile(os.path.join(TEMPLATES_DIR, fn), dst)
        if fn.endswith((".sh", ".py", ".js")):
            os.chmod(dst, 0o755)

    # generated .gitignore keeps runtime state + captured creds out of any repo
    with open(os.path.join(out, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("scripts/node_modules/\n.browser-profile/\nruns/\n"
                 "data/user-auth.har\ndata/user-auth.*.har\n")

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

    setup_accounts = [a for a in accounts if a["host"] in setup_hosts]
    kept_accounts = [a for a in accounts if a["host"] not in setup_hosts]
    all_stripped = setup_enabled and not kept_accounts

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
        "%%SETUP_BLOCK%%": build_setup_block(setup_accounts),
        "%%CREDENTIALS_NOTE%%": build_credentials_note(setup_enabled),
        "%%SETUP_SECURITY%%": build_setup_security(setup_accounts, setup_hosts,
                                                   kept_accounts),
        "%%SETUP_REFERENCE%%": build_setup_reference(setup_hosts),
        "%%SESSION_INTRO%%": build_session_intro(setup_enabled, all_stripped),
        "%%SESSION_CONTENTS%%": build_session_contents(
            setup_enabled, all_stripped, host, cookie_names, auth_headers,
            kept_accounts),
        "%%SHARING_RULES%%": build_sharing_rules(all_stripped),
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
    if setup_enabled:
        print(f"[generate] per-user setup: {len(setup_accounts)} account(s) "
              f"stripped -> {', '.join(setup_hosts)}")
        print("[generate] users must run: bash "
              f"{os.path.join(out, 'scripts', 'setup.sh')} before replay")
    elif accounts:
        print(f"[generate] auth accounts detected (no setup): "
              f"{', '.join(detected_hosts)} — pass --with-setup to enable "
              "per-user login")
    if args.format == "skills-sh":
        print(f"[generate] install/test: npx skills add {out}")
    print("[generate] API replay:  python3 "
          f"{os.path.join(out, 'scripts', 'replay_api.py')} "
          + build_set_example(cands))


if __name__ == "__main__":
    main()
