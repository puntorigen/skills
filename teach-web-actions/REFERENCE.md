# Teach Web Actions — Reference

Background for [SKILL.md](SKILL.md): the artifacts each phase writes, the HAR
shape, how `process_har.py` decides what to keep, how parameter knobs and the
auth surface are detected, the `lesson.json` schema, the UI-replay contract,
and video conversion.

## Lesson directory layout

Everything for one lesson lives under `~/.web-lessons/<host>/<lesson-name>/`
(override the root with `LESSONS_ROOT`). Nothing here belongs in a git repo.

```
~/.web-lessons/
├── .browser-profile/            # persistent Chrome profile (logins persist)
└── www.example-air.com/
    └── flight-availability/
        ├── meta.json            # lesson name, start URL, host, channel, time
        ├── session.har          # all captured network activity (Phase 1)
        ├── actions.js           # recorded UI steps, --target javascript (Phase 1)
        ├── lesson.json          # distilled endpoints/knobs/auth (Phase 2)
        ├── LESSON.md            # human-readable distillation (Phase 2)
        ├── variant.js           # your adapted flow for UI replay (Phase 3, you write)
        └── runs/<timestamp>/
            ├── video/*.webm     # raw Playwright recording
            └── proof.mp4        # converted proof (Phase 3)
```

## Phase 1 internals (recording)

`record_session.sh` runs:

```bash
npx playwright codegen \
  --save-har="$LESSON_DIR/session.har" \
  --user-data-dir="$LESSONS_ROOT/.browser-profile" \
  --target=javascript \
  -o "$LESSON_DIR/actions.js" \
  [--channel=chrome] [--save-har-glob='**/api/**'] \
  "$START_URL"
```

- **`--user-data-dir`** gives a persistent, dedicated profile. Chrome 136+
  refuses to let automation attach to the *default* profile, so a separate one
  is mandatory; the upside is that a login done once during teaching is reused
  on the next recording and on UI replay.
- **`--save-har`** records every request/response (headers, cookies, bodies,
  timings) as a HAR (a JSON schema). `--save-har-glob` (via `SAVE_HAR_GLOB`)
  narrows capture to matching URLs, e.g. only `**/api/**`.
- **`--target=javascript`** makes `actions.js` a plain runnable script (not a
  `@playwright/test` file), so the UI steps are easy to lift into `variant.js`.
- The HAR is flushed when the user **closes the browser** — always wait.

## HAR anatomy

The parser cares about `log.entries[]`, each roughly:

```json
{
  "startedDateTime": "2026-07-01T18:00:00.000Z",
  "time": 123.4,
  "_resourceType": "xhr",
  "request": {
    "method": "GET",
    "url": "https://host/api/search?from=LAX&to=JFK&date=2026-08-14",
    "headers": [{ "name": "Authorization", "value": "Bearer ..." }],
    "queryString": [{ "name": "from", "value": "LAX" }],
    "cookies": [{ "name": "session", "value": "..." }],
    "postData": { "mimeType": "application/json", "text": "{...}" }
  },
  "response": {
    "status": 200,
    "content": { "mimeType": "application/json", "size": 812, "text": "{...}",
                 "encoding": "base64" }
  }
}
```

`_resourceType` is Playwright's classification (`xhr`, `fetch`, `document`,
`script`, `image`, ...). `response.content.text` may be base64 (`encoding`) or
omitted for large/binary bodies.

## What `process_har.py` keeps vs. drops

Goal: surface the **API calls that carry the action**, not page chrome.

Dropped:
- **Noise hosts** — analytics/telemetry/ads (Google Analytics/GTM, Segment,
  Sentry, Datadog, New Relic, FullStory, Hotjar, Mixpanel, Amplitude,
  Intercom, LaunchDarkly, Clarity, ...). Matched by substring in `NOISE_HOSTS`.
- **Static assets** — by `_resourceType` (`stylesheet`, `script`, `image`,
  `font`, `media`, `manifest`, `websocket`, ...), by file extension, or by
  response mime (`image/*`, `font/*`, css, js).

Kept: `xhr`, `fetch`, and `document` requests to non-noise hosts. Entries are
grouped into **endpoints** by `METHOD host path-template`.

### Path templating

To group `/api/orders/123` and `/api/orders/456` together, each path segment
that looks like an id is replaced with `{id}`: integers, UUIDs, and long hex
strings (≥16 chars). So both become `GET host/api/orders/{id}`.

### Endpoint ordering

Endpoints are sorted so the useful ones surface first: non-mutating before
mutating, then more parameter candidates, then higher call count. The
data-bearing search/availability call therefore tends to rank at the top.

## Parameter-knob detection

A knob is a value the agent can change to make a variation. For every query
param and every scalar in a JSON request body (flattened to dotted paths), a
candidate is recorded when **either**:

- the **value** matches a pattern:
  - `date` — `YYYY-MM-DD[THH:MM]` or `D/M/Y`
  - `code` — three uppercase letters (IATA airport, currency, state)
  - `uuid` — canonical UUID
  - `number` — integer or decimal
- or the **name** matches a knob pattern (`KNOB_NAME_RE`): dates, from/to,
  origin/dest, depart/arrive/return, check-in/out, city/airport/station,
  lat/lon, page/offset/limit/cursor/size, sort/order, q/query/search/term,
  filter/category/type/status, id/code/currency, passengers/adults/children,
  qty/amount/price/min/max.

Each candidate records `location` (`query` | `body`), `name` (param or dotted
body path), `kind`, and a redacted `sample`. These are exactly the fields to
substitute for an API-replay variation.

## Auth surface and redaction

Credential **values are never copied** into `lesson.json` / `LESSON.md`:

- Header names in `AUTH_HEADER_NAMES` (authorization, x-api-key, x-auth-token,
  x-csrf-token, ...) are kept as names; the value becomes `"<scheme> <redacted>"`
  (e.g. `Bearer <redacted>`).
- Any field whose **name** matches `SECRET_KEY_RE` (password, token, secret,
  api_key, refresh, client_secret, signature, csrf, otp, card, cvv, ...) is
  replaced with `<redacted>` in bodies, query samples, and examples.
- Cookie **names** are collected (from `request.cookies[]` and the `Cookie`
  header) into the auth surface; cookie values are never stored.

`lesson.json`/`LESSON.md` therefore tell you *what a replay needs* without
leaking the credentials. The live values remain only in `session.har` and the
browser profile — read them from there at call time, never print them.

## `lesson.json` schema

```json
{
  "lesson": "flight-availability",
  "source_url": "https://www.example-air.com",
  "host": "www.example-air.com",
  "recorded_at": "2026-07-01T18:00:00Z",
  "counts": { "total_requests": 214, "kept": 9, "endpoints": 4 },
  "auth_surface": {
    "cookies_seen": ["session", "csrftoken"],
    "auth_headers_seen": ["authorization"]
  },
  "endpoints": [
    {
      "id": "GET www.example-air.com/api/search",
      "action_guess": "search search",
      "method": "GET",
      "host": "www.example-air.com",
      "path_template": "/api/search",
      "count": 2,
      "mutating": false,
      "param_candidates": [
        { "location": "query", "name": "from", "kind": "code", "sample": "LAX" },
        { "location": "query", "name": "to", "kind": "code", "sample": "JFK" },
        { "location": "query", "name": "date", "kind": "date", "sample": "2026-08-14" }
      ],
      "auth": { "cookies_required": ["session"], "headers": ["authorization"] },
      "examples": [
        {
          "url": "https://.../api/search?from=LAX&to=JFK&date=2026-08-14",
          "query": { "from": "LAX", "to": "JFK", "date": "2026-08-14" },
          "started": "2026-07-01T18:00:03Z",
          "request_headers": { "accept": "application/json" },
          "response": {
            "status": 200, "mime": "application/json", "size": 812,
            "json_keys": ["flights", "currency"],
            "json_sample": "{\"flights\":[...]}"
          }
        }
      ]
    }
  ]
}
```

`mutating` is `true` for POST/PUT/PATCH/DELETE — the replay-confirmation gate.
`examples` is capped (`--max-examples`, default 3) and response bodies are
truncated (`--body-chars`, default 1200).

## API-replay recipe

1. Read `lesson.json`; pick the endpoint whose response held the data.
2. Choose the `param_candidates` to change (new date, new origin/destination).
3. Read the matching cookies/auth headers from `session.har` for that host **at
   call time** — do not print them.
4. Issue the request with `curl` or python `requests`, substituting params.
5. Parse the response and answer the user.

Only do this autonomously for idempotent reads (GET/search). For `mutating`
endpoints, confirm with the user first.

## UI-replay contract and video

`variant.js` exports one async function; the runner supplies `page`:

```js
module.exports = async (page) => {
  await page.goto("https://www.example-air.com");
  await page.getByLabel("From").fill("SFO");
  await page.getByRole("button", { name: "Search" }).click();
  await page.waitForSelector(".results");
};
```

`run_variant.js` calls `chromium.launchPersistentContext(profile, {
recordVideo: { dir } })` (reusing the teaching profile, so logins persist),
runs the function, and closes the context — which **flushes the `.webm`**.
Env: `PW_CHANNEL` (default `chrome`; `""` = bundled chromium), `PW_PROFILE`,
`PW_HEADLESS=1`, `PW_TIMEOUT` (ms).

`replay_ui.sh` then converts to mp4:

```bash
ffmpeg -y -i video/*.webm -movflags +faststart -pix_fmt yuv420p proof.mp4
```

`-pix_fmt yuv420p` keeps the mp4 broadly playable; `+faststart` moves the moov
atom to the front for streaming. Multiple `.webm` segments (multi-page flows)
are concatenated in filename order. Feed `proof.mp4` to the **review-mp4** skill
to describe/verify the flow.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `codegen` errors about the browser channel | System Chrome missing. Set `PW_CHANNEL=chromium` (bundled) or install Chrome. |
| Nothing captured / empty HAR | Browser closed before any request, or `SAVE_HAR_GLOB` too narrow. |
| `lesson.json` has 0 endpoints | The action used only static/websocket traffic, or all hosts were noise-filtered. Re-record; inspect `session.har`. Consider removing a `SAVE_HAR_GLOB`. |
| The data call isn't listed | It may be a `document` navigation or on a filtered host; check `session.har` directly for the URL that returned the result. |
| UI replay records no video | The variant never opened/navigated a page, or it threw before `goto`. Check the runner's stderr. |
| Replay times out on a selector | Selectors from `actions.js` may be brittle; prefer role/label locators and add `waitForSelector`. |
| Chrome profile "already in use" | A codegen/replay session is still open on the same profile. Close it first. |
