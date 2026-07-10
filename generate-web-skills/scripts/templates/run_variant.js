// UI-replay runner for a generated web-action skill (self-contained).
//
// Launches a persistent Chrome context under a SKILL-LOCAL profile, injects the
// recorded session cookies from the embedded HAR (so the logged-in session
// carries over without depending on ~/.web-lessons), records video, then runs a
// variant module that exports a single async function:
//
//   module.exports = async (page, params) => { await page.goto(...); ... };
//
// Usage: node run_variant.js <variant.js> <video-out-dir> [<cookies.json>]
// Env:
//   PW_CHANNEL   chrome (default) | chromium | msedge   ("" => bundled chromium)
//   PW_PROFILE   persistent profile dir (default <skill>/.browser-profile)
//   PW_HEADLESS  1 to run headless (default headed)
//   PW_TIMEOUT   overall timeout ms (default 120000)
//   PW_PARAMS    JSON object passed as the variant's `params` argument
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

async function main() {
  const variantPath = process.argv[2];
  const outDir = process.argv[3];
  const cookiesPath = process.argv[4];
  if (!variantPath || !outDir) {
    console.error("usage: node run_variant.js <variant.js> <video-out-dir> [cookies.json]");
    process.exit(2);
  }

  const channelEnv = process.env.PW_CHANNEL;
  const channel = channelEnv === undefined ? "chrome" : channelEnv; // "" => chromium
  const profile =
    process.env.PW_PROFILE ||
    path.join(__dirname, "..", ".browser-profile");
  const headless = process.env.PW_HEADLESS === "1";
  const timeout = parseInt(process.env.PW_TIMEOUT || "120000", 10);
  const viewport = { width: 1280, height: 800 };

  let params = {};
  if (process.env.PW_PARAMS) {
    try {
      params = JSON.parse(process.env.PW_PARAMS);
    } catch (e) {
      console.error("[replay] PW_PARAMS is not valid JSON; ignoring");
    }
  }

  const launchOpts = { headless, viewport, recordVideo: { dir: outDir, size: viewport } };
  if (channel) launchOpts.channel = channel;

  const context = await chromium.launchPersistentContext(profile, launchOpts);
  context.setDefaultTimeout(timeout);

  if (cookiesPath && fs.existsSync(cookiesPath)) {
    try {
      const cookies = JSON.parse(fs.readFileSync(cookiesPath, "utf8"));
      if (Array.isArray(cookies) && cookies.length) {
        await context.addCookies(cookies);
        console.error(`[replay] injected ${cookies.length} session cookie(s)`);
      }
    } catch (e) {
      console.error("[replay] could not inject cookies:", e.message);
    }
  }

  const page = context.pages()[0] || (await context.newPage());
  const run = require(path.resolve(variantPath));
  if (typeof run !== "function") {
    await context.close();
    console.error("variant module must `module.exports = async (page, params) => {...}`");
    process.exit(2);
  }

  let failed = null;
  try {
    await run(page, params);
  } catch (err) {
    failed = err;
    console.error("[replay] variant threw:", err && err.message ? err.message : err);
  } finally {
    // closing the context flushes the video file to disk
    await context.close();
  }
  if (failed) process.exit(1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
