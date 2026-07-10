#!/usr/bin/env bash
# Per-user setup for a generated web-action skill (self-contained).
#
# Some services in this skill's flow do NOT ship a login — you sign in with your
# OWN account. This opens a browser for each such service, waits while you log
# in, and saves your session locally to data/user-auth.<host>.har (gitignored).
# Replay then acts as you on those services.
#
# Re-run any time to refresh or switch accounts.
# Usage:  setup.sh [host ...]     # limit to specific hosts (default: all needed)
# Env:    PW_CHANNEL   chrome (default) | chromium | msedge | "" (bundled chromium)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$SKILL_DIR/data"
ACCOUNTS_JSON="$DATA_DIR/auth_accounts.json"
PW_CHANNEL="${PW_CHANNEL-chrome}"

[[ -f "$ACCOUNTS_JSON" ]] || { echo "[setup] no data/auth_accounts.json — this skill has no per-user setup." >&2; exit 1; }
command -v node >/dev/null 2>&1 || { echo "[setup] node is required but not on PATH" >&2; exit 1; }

# hosts requested on the CLI (optional filter)
declare -a ONLY=("$@")
in_only() {
  (( ${#ONLY[@]} == 0 )) && return 0
  local h; for h in "${ONLY[@]}"; do [[ "$h" == "$1" ]] && return 0; done
  return 1
}

# emit "host<TAB>login_url<TAB>label" for each account needing setup
mapfile -t ROWS < <(python3 - "$ACCOUNTS_JSON" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for a in data.get("accounts", []):
    if a.get("setup_required"):
        host = a.get("host", "")
        url = a.get("login_url") or f"https://{host}/"
        label = a.get("label") or host
        print("\t".join([host, url, label]))
PY
)

(( ${#ROWS[@]} > 0 )) || { echo "[setup] no accounts require per-user setup." >&2; exit 0; }

# ensure playwright is available (first run installs it locally)
if [[ ! -d "$SCRIPT_DIR/node_modules/playwright" ]]; then
  echo "[setup] installing playwright npm package (first run)..." >&2
  (cd "$SCRIPT_DIR" && npm install --no-audit --no-fund >&2)
fi
if [[ -z "$PW_CHANNEL" ]]; then
  (cd "$SCRIPT_DIR" && npx --no-install playwright install chromium >&2) || true
fi

host_slug() { echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'; }

done_count=0
for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r host login_url label <<<"$row"
  in_only "$host" || continue
  slug="$(host_slug "$host")"
  out_har="$DATA_DIR/user-auth.${slug}.har"
  echo "[setup] --- $label ($host) ---" >&2
  PW_CHANNEL="$PW_CHANNEL" node "$SCRIPT_DIR/setup_login.js" "$host" "$login_url" "$out_har" "$label" >&2
  done_count=$((done_count + 1))
done

echo "[setup] done — captured $done_count account(s). Your logins are saved under data/ (gitignored)." >&2
echo "[setup] you can now run the skill's replay." >&2
