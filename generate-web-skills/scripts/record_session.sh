#!/usr/bin/env bash
# Phase 1 of generate-web-skills: record a user-driven Chrome session.
#
# Opens Chrome via `playwright codegen` under a persistent profile and captures:
#   - session.har   all network activity (JSON HAR)
#   - actions.js     the recorded UI steps (--target javascript)
#   - meta.json      lesson name, start URL, host, timestamp, channel
#
# Usage:  record_session.sh <lesson-name> <start-url>
# Env:
#   PW_CHANNEL   browser channel: chrome (default) | chromium | msedge
#   SAVE_HAR_GLOB   optional glob to limit captured requests (e.g. '**/api/**')
#   LESSONS_ROOT   base dir for lessons (default ~/.web-lessons)
set -euo pipefail

LESSON_NAME="${1:-}"
START_URL="${2:-}"
if [[ -z "$LESSON_NAME" || -z "$START_URL" ]]; then
  echo "usage: record_session.sh <lesson-name> <start-url>" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LESSONS_ROOT="${LESSONS_ROOT:-$HOME/.web-lessons}"
PROFILE_DIR="$LESSONS_ROOT/.browser-profile"
PW_CHANNEL="${PW_CHANNEL-chrome}"

# host = URL authority, sanitized for a directory name
HOST="$(printf '%s' "$START_URL" | sed -E 's#^[a-zA-Z]+://##; s#/.*$##; s#:[0-9]+$##')"
[[ -z "$HOST" ]] && HOST="site"
SAFE_NAME="$(printf '%s' "$LESSON_NAME" | tr -c 'A-Za-z0-9._-' '-')"
LESSON_DIR="$LESSONS_ROOT/$HOST/$SAFE_NAME"

mkdir -p "$LESSON_DIR" "$PROFILE_DIR"

# --- ensure the playwright npm package is available (browsers handled below) --
if [[ ! -d "$SCRIPT_DIR/node_modules/playwright" ]]; then
  echo "[record] installing playwright npm package (first run)..." >&2
  (cd "$SCRIPT_DIR" && npm install --no-audit --no-fund >&2)
fi
# Bundled chromium is only needed when NOT using a system channel.
if [[ -z "$PW_CHANNEL" ]]; then
  (cd "$SCRIPT_DIR" && npx --no-install playwright install chromium >&2) || true
fi

HAR_PATH="$LESSON_DIR/session.har"
ACTIONS_PATH="$LESSON_DIR/actions.js"
META_PATH="$LESSON_DIR/meta.json"

CREATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "$META_PATH" <<JSON
{
  "lesson": "$SAFE_NAME",
  "url": "$START_URL",
  "host": "$HOST",
  "channel": "${PW_CHANNEL:-chromium}",
  "created_at": "$CREATED_AT"
}
JSON

# Build codegen args.
args=(codegen
  --save-har="$HAR_PATH"
  --user-data-dir="$PROFILE_DIR"
  --target=javascript
  -o "$ACTIONS_PATH")
[[ -n "$PW_CHANNEL" ]] && args+=(--channel="$PW_CHANNEL")
[[ -n "${SAVE_HAR_GLOB:-}" ]] && args+=(--save-har-glob="$SAVE_HAR_GLOB")
args+=("$START_URL")

cat >&2 <<TXT

[record] Chrome is opening for lesson "$SAFE_NAME" ($HOST).
[record] Perform the action you want to teach, then CLOSE the window.
[record]   HAR     -> $HAR_PATH
[record]   actions -> $ACTIONS_PATH
[record]   profile -> $PROFILE_DIR (logins persist here)

TXT

(cd "$SCRIPT_DIR" && npx --no-install playwright "${args[@]}") || \
  (cd "$SCRIPT_DIR" && npx playwright "${args[@]}")

echo >&2
echo "[record] done. Lesson directory:" >&2
# stdout = the lesson dir, so callers can capture it
echo "$LESSON_DIR"
