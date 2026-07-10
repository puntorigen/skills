// Per-user login capture for a generated web-action skill (self-contained).
//
// Opens a headed browser at a service's login page, lets the user sign in with
// THEIR OWN account, then captures that session (cookies + any auth headers) to
// a per-host HAR under ../data/user-auth.<host-slug>.har. Replay overlays this
// on top of the shipped session.har so each user acts as themselves.
//
// The captured file stays local (gitignored) and is never printed. Values are
// written to disk only; nothing is echoed to the terminal.
//
// Usage: node setup_login.js <host> <login-url> <out-har> [label]
// Env:
//   PW_CHANNEL   chrome (default) | chromium | msedge | "" (bundled chromium)
//   PW_SETUP_PROFILE  persistent profile dir (default <skill>/.setup-profile/<host>)
const fs = require("fs");
const path = require("path");
const readline = require("readline");
const { chromium } = require("playwright");

const AUTH_HEADER_NAMES = new Set([
  "authorization", "x-api-key", "x-auth-token", "x-access-token",
  "x-csrf-token", "x-xsrf-token", "x-csrftoken", "api-key", "apikey",
  "x-amz-security-token",
]);

function hostSlug(host) {
  return (host || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "host";
}

function hostMatches(cookieDomain, host) {
  const d = (cookieDomain || "").replace(/^\./, "").toLowerCase();
  const h = (host || "").toLowerCase();
  if (!d || !h) return false;
  return d === h || h.endsWith("." + d) || d.endsWith("." + h);
}

function waitForEnter(promptText) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stderr });
    rl.question(promptText, () => { rl.close(); resolve(); });
  });
}

async function main() {
  const [host, loginUrl, outHar, label] = process.argv.slice(2);
  if (!host || !loginUrl || !outHar) {
    console.error("usage: node setup_login.js <host> <login-url> <out-har> [label]");
    process.exit(2);
  }

  const channelEnv = process.env.PW_CHANNEL;
  const channel = channelEnv === undefined ? "chrome" : channelEnv; // "" => chromium
  const profile =
    process.env.PW_SETUP_PROFILE ||
    path.join(__dirname, "..", ".setup-profile", hostSlug(host));

  const launchOpts = { headless: false, viewport: { width: 1280, height: 800 } };
  if (channel) launchOpts.channel = channel;

  const context = await chromium.launchPersistentContext(profile, launchOpts);

  // capture auth headers seen on requests to the target host (values not logged)
  const capturedHeaders = {}; // lower name -> { name, value }
  context.on("request", (req) => {
    try {
      const u = new URL(req.url());
      if (!hostMatches(u.hostname, host)) return;
      const hs = req.headers();
      for (const [k, v] of Object.entries(hs)) {
        if (AUTH_HEADER_NAMES.has(k.toLowerCase()) && v) {
          capturedHeaders[k.toLowerCase()] = { name: k, value: v };
        }
      }
    } catch (e) {
      /* ignore malformed URLs */
    }
  });

  const page = context.pages()[0] || (await context.newPage());
  const who = label ? `${label} (${host})` : host;
  console.error(`\n[setup] Log in to ${who} in the browser window.`);
  try {
    await page.goto(loginUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  } catch (e) {
    console.error(`[setup] could not open ${loginUrl}: ${e.message}`);
  }

  await waitForEnter(`[setup] When you have finished logging in to ${who}, press Enter here... `);

  // read cookies from the live context (works even if no XHR fired)
  let allCookies = [];
  try {
    allCookies = await context.cookies();
  } catch (e) {
    console.error("[setup] could not read cookies:", e.message);
  }
  const cookies = allCookies.filter((c) => hostMatches(c.domain, host));

  const cookieHeader = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  const headers = [];
  if (cookieHeader) headers.push({ name: "Cookie", value: cookieHeader });
  for (const h of Object.values(capturedHeaders)) {
    headers.push({ name: h.name, value: h.value });
  }

  const entry = {
    startedDateTime: new Date().toISOString(),
    time: 0,
    request: {
      method: "GET",
      url: `https://${host}/`,
      httpVersion: "HTTP/1.1",
      headers,
      cookies: cookies.map((c) => ({ name: c.name, value: c.value })),
      queryString: [],
      headersSize: -1,
      bodySize: -1,
    },
    response: {
      status: 200, statusText: "", httpVersion: "HTTP/1.1", headers: [],
      cookies: [], content: { size: 0, mimeType: "" }, redirectURL: "",
      headersSize: -1, bodySize: -1,
    },
    cache: {},
    timings: { send: 0, wait: 0, receive: 0 },
  };
  const har = {
    log: {
      version: "1.2",
      creator: { name: "teach-web-actions-setup", version: "1.0" },
      entries: [entry],
    },
  };

  fs.mkdirSync(path.dirname(outHar), { recursive: true });
  fs.writeFileSync(outHar, JSON.stringify(har));
  await context.close();

  const nCookies = cookies.length;
  const nHeaders = Object.keys(capturedHeaders).length;
  if (nCookies === 0 && nHeaders === 0) {
    console.error(`[setup] WARNING: captured no cookies or auth headers for ${host}. ` +
      `Make sure you completed login before pressing Enter, then re-run setup.`);
    process.exit(3);
  }
  console.error(`[setup] captured ${nCookies} cookie(s)` +
    (nHeaders ? ` + ${nHeaders} auth header(s)` : "") + ` for ${host} -> saved locally`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
