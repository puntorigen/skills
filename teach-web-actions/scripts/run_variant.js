// Runner for a teach-web-actions UI variant.
//
// Launches a persistent Chrome context (reusing the teaching profile so logins
// carry over), records video, then runs a variant module that exports a single
// async function taking `page`:
//
//   module.exports = async (page) => { await page.goto(...); ... };
//
// Usage: node run_variant.js <variant.js> <video-out-dir>
// Env:
//   PW_CHANNEL   chrome (default) | chromium | msedge   ("" => bundled chromium)
//   PW_PROFILE   persistent profile dir (default ~/.web-lessons/.browser-profile)
//   PW_HEADLESS  1 to run headless (default headed)
//   PW_TIMEOUT   overall timeout ms (default 120000)
const path = require("path");
const os = require("os");
const { chromium } = require("playwright");

async function main() {
  const variantPath = process.argv[2];
  const outDir = process.argv[3];
  if (!variantPath || !outDir) {
    console.error("usage: node run_variant.js <variant.js> <video-out-dir>");
    process.exit(2);
  }

  const channelEnv = process.env.PW_CHANNEL;
  const channel = channelEnv === undefined ? "chrome" : channelEnv; // "" => chromium
  const profile =
    process.env.PW_PROFILE || path.join(os.homedir(), ".web-lessons", ".browser-profile");
  const headless = process.env.PW_HEADLESS === "1";
  const timeout = parseInt(process.env.PW_TIMEOUT || "120000", 10);
  const viewport = { width: 1280, height: 800 };

  const launchOpts = {
    headless,
    viewport,
    recordVideo: { dir: outDir, size: viewport },
  };
  if (channel) launchOpts.channel = channel;

  const context = await chromium.launchPersistentContext(profile, launchOpts);
  context.setDefaultTimeout(timeout);
  const page = context.pages()[0] || (await context.newPage());

  const run = require(path.resolve(variantPath));
  if (typeof run !== "function") {
    await context.close();
    console.error("variant module must `module.exports = async (page) => {...}`");
    process.exit(2);
  }

  let failed = null;
  try {
    await run(page);
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
