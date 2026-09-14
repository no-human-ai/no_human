// Session-replay network-body leak e2e check.
//
// Ties to web/src/replayScrub.js + web/src/telemetry.js's
// `maskCapturedNetworkRequestFn`. The bug this guards: `GET /api/profiles`
// (and dozens of siblings) return a repo's NAME and absolute filesystem
// PATH, and PostHog session replay was configured with `recordBody: true` /
// `recordHeaders: true` — with only 3 onboarding paths excluded and every
// other endpoint's body passed through to PostHog UNMASKED. `.ph-no-capture`
// (used on the scanned-repo list below) protects the rendered DOM only; it
// has zero effect on the separate network-capture channel this file drives.
//
// This is NOT a unit test of replayScrub.js's pure functions (see
// web/src/replayScrub.test.mjs for that). This drives a real Chromium
// against the real built bundle (web/dist) with the real posthog-js
// recorder chunk loaded, types a sentinel path into the real "New Project"
// scan-root field, clicks the real "Scan" button (GET
// /api/repos/discover?root=<sentinel>, a `tier: "redact"` endpoint whose
// response also carries the sentinel as repo name+path), and then inspects
// the ACTUAL decoded bytes of the POST bodies the browser sent to the mock
// PostHog ingestion endpoint — not a JS assertion against an exclusion
// array.
//
// Four things are checked against the real captured bytes:
//   1. vacuity guard      — the harness actually captured $snapshot replay
//                            events at all (a clean result on a harness that
//                            captured nothing would be meaningless).
//   2. redact-tier leak    — the sentinel repo path/name (planted in
//                            /api/repos/discover's response, and in the scan
//                            field's outgoing query string) is ABSENT.
//   3. default-deny        — a sentinel planted in a POST body/response for
//                            an endpoint that is NOT in api.js at all (so it
//                            can never appear in replayScrub.js's
//                            classification map, however careful the sweep
//                            is) is ALSO absent. This is the live proof of
//                            "an API path nobody considered must not be
//                            captured by default" — allowlist, not denylist.
//   4. allow-tier passthrough — a marker planted in /api/queue/health's
//                            response (on REPLAY_BODY_ALLOWLIST) IS present.
//                            Without this, checks 2/3 passing could just
//                            mean the harness never captures ANY body.
//
// A second, "control" pass of the SAME dist bundle string-patches
// `maskCapturedNetworkRequestFn:` out of the one chunk that wires it, so
// posthog-js's own init() never calls it. That pass must leak the sentinel —
// proof this harness has discriminating power (mirrors dead-click-race.mjs's
// before_send/beforeSendOff A/B).
//
//   node e2e/replay-body-leak.mjs   # needs `npm run build` first (drives web/dist)
import http from "node:http";
import zlib from "node:zlib";
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

const WEB_DIR = new URL("..", import.meta.url).pathname;
const DIST_DIR = path.join(WEB_DIR, "dist");
const PH_DIST_DIR = path.join(WEB_DIR, "node_modules/posthog-js/dist");

if (!fs.existsSync(DIST_DIR)) {
  console.error(`replay-body-leak: ${DIST_DIR} does not exist — run \`npm run build\` first.`);
  process.exit(1);
}

// The one built chunk that contains the literal `maskCapturedNetworkRequestFn:`
// wiring — found by scanning dist/assets for it, same technique as
// dead-click-race.mjs's BEFORE_SEND_CHUNK.
const MASK_FN_CHUNK = fs
  .readdirSync(path.join(DIST_DIR, "assets"))
  .find(
    (f) =>
      /^index-.*\.js$/.test(f) &&
      fs
        .readFileSync(path.join(DIST_DIR, "assets", f), "utf8")
        .includes("maskCapturedNetworkRequestFn:"),
  );
if (!MASK_FN_CHUNK) {
  throw new Error("could not find the chunk containing maskCapturedNetworkRequestFn: in dist/assets");
}

// ── sentinels — never anything a real repo would be named/paths would be ──
// posthog-js hard-codes network *body/header* capture OFF whenever the
// PAGE's own origin hostname is literally "localhost" or "127.0.0.1"
// (lazy-recorder.js: `B=["localhost","127.0.0.1"]`, `if(!B.includes(i.hostname)
// || this._forceAllowLocalhostNetworkCapture)` gates whether the
// "rrweb/network@1" plugin is even registered) — a built-in dev-safety
// guard, independent of maskCapturedNetworkRequestFn. This app's own
// telemetry.js notes it "ALWAYS serves on 127.0.0.1" for the common local
// case, so that default guard already suppresses this exact leak vector for
// that case — but the app is also reachable over a LAN IP/hostname or a
// tunnel, where the guard does NOT apply and the leak (absent this fix) is
// live. To exercise the code path this fix actually protects, navigate via
// a non-blocklisted hostname that Chromium resolves straight back to the
// mock server's 127.0.0.1 loopback via --host-resolver-rules, rather than
// literal "127.0.0.1" — no real DNS/network access required.
const TEST_HOST = "replay-body-leak.e2e.test";

// Deliberately avoids posthog-js's own BUILT-IN, unrelated body-content
// denylist (lazy-recorder.js scans captured bodies/headers/URLs for the
// literal substrings "auth", "credential", "password", "secret", "token" and
// redacts the WHOLE body on a match, replacing it with a fixed
// "[SessionRecording] Response body redacted as might contain: <word>"
// message — independent of maskCapturedNetworkRequestFn). An earlier
// version of this sentinel used a "secret-repos" path segment, which
// collided with that built-in scan: it got redacted even in the "unmasked"
// ablation pass (mask function string-patched off), which defeated the
// ablation's whole purpose of proving this harness can actually observe an
// unredacted leak when the app's OWN fix is absent.
const SENTINEL_PATH = "/Users/e2e-sentinel-user/local-repos/zzqq-leak-canary-7f2a";
const SENTINEL_NAME = "zzqq-leak-canary-reponame-7f2a";
const SENTINEL_UNLISTED = "zzqq-unlisted-endpoint-canary-9f3c1b";
const ALLOWLIST_MARKER = "zzqq-allowlist-passthrough-marker-7a1d";
// Never a real api.js call site (the drift test in replayScrub.test.mjs
// guarantees every real one IS classified) — this is the "endpoint nobody
// considered" default-deny proof.
const UNLISTED_PATH = "/api/zzqq-never-enumerated-endpoint";

const MIME = {
  ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
  ".map": "application/json", ".ico": "image/x-icon", ".woff2": "font/woff2",
};

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
  });
}

// Returns both the decoded raw TEXT (for byte-level substring inspection —
// the point of this harness) and best-effort parsed events (for the vacuity
// guard: proving $snapshot events were actually captured).
function decode(raw, req) {
  let buf = raw;
  try {
    const enc = req.headers["content-encoding"] || "";
    if (enc.includes("gzip")) buf = zlib.gunzipSync(buf);
  } catch {
    /* fall through to raw */
  }
  let text = buf.toString("utf8");
  if (text.startsWith("data=")) {
    try {
      const b64 = decodeURIComponent(text.slice(5));
      text = Buffer.from(b64, "base64").toString("utf8");
    } catch {
      /* keep text */
    }
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { text, events: [] };
  }
  const events = Array.isArray(parsed) ? parsed : parsed.batch ? parsed.batch : [parsed];
  return { text, events };
}

function makeServer({ variant, rawTexts, capturedEvents, unmatched }) {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://localhost");
    const p = url.pathname;

    function json(obj) {
      // Explicit Content-Length, not Node's default chunked
      // transfer-encoding: posthog-js's fetch response-body capture
      // refuses to read chunked bodies at all (substitutes the literal
      // string "Chunked Transfer-Encoding is not supported" instead of the
      // real body) — a real app server sending a small known-length JSON
      // body would never trigger that, so chunking here would just be a
      // mock-fidelity gap masking whether allow-tier passthrough actually
      // captures real bytes.
      const body = Buffer.from(JSON.stringify(obj));
      res.writeHead(200, { "content-type": "application/json", "content-length": body.length });
      res.end(body);
    }

    if (p.startsWith("/ph/static/")) {
      const file = path.join(PH_DIST_DIR, path.basename(p));
      if (fs.existsSync(file)) {
        res.writeHead(200, { "content-type": "text/javascript" });
        res.end(fs.readFileSync(file));
      } else {
        unmatched.push(`STATIC 404: ${p}`);
        res.writeHead(404);
        res.end("not found");
      }
      return;
    }

    // Remote config — see dead-click-race.mjs for why config.js is answered
    // empty (forces the JSON fallback) and why supportedCompression is [].
    if (/^\/ph\/array\/[^/]+\/config\.js$/.test(p)) {
      res.writeHead(200, { "content-type": "text/javascript" });
      res.end("// e2e: intentionally empty — forces the JSON fallback below\n");
      return;
    }
    if (/^\/ph\/array\/[^/]+\/config$/.test(p) || p.startsWith("/ph/flags/") || p.startsWith("/ph/decide/")) {
      await readBody(req);
      json({
        config: { enable_collect_everything: true },
        toolbarParams: {},
        isAuthenticated: false,
        supportedCompression: [],
        featureFlags: {},
        sessionRecording: {
          endpoint: "/s/",
          consoleLogRecordingEnabled: true,
          sampleRate: null,
          linkedFlag: null,
          minimumDurationMilliseconds: null,
          networkPayloadCapture: { recordHeaders: true, recordBody: true },
        },
        capturePerformance: true,
        autocaptureExceptions: false,
        captureDeadClicks: true,
        heatmaps: true,
        siteApps: [],
      });
      return;
    }

    if (req.method === "POST" && (p === "/ph/e/" || p === "/ph/i/v0/e/" || p === "/ph/s/" || p === "/ph/batch/")) {
      const raw = await readBody(req);
      const { text, events } = decode(raw, req);
      rawTexts.push(text);
      for (const ev of events) {
        if (ev && ev.event) capturedEvents.push({ path: p, event: ev.event });
      }
      json({ status: 1 });
      return;
    }
    if (p.startsWith("/ph/")) {
      unmatched.push(`PH UNMATCHED: ${req.method} ${p}`);
      json({ status: 1 });
      return;
    }

    // ── app API stubs ──
    if (p === "/api/config") {
      return json({
        telemetry: {
          enabled: true,
          posthog_publishable: "phc_e2e_test_key",
          // Same-origin as the page (TEST_HOST, not literal 127.0.0.1): once
          // the page navigates via TEST_HOST to dodge posthog-js's built-in
          // localhost network-capture guard (see TEST_HOST's comment above),
          // a 127.0.0.1 api_host would make every /ph/* request cross-origin
          // and get CORS-blocked by the mock (no ACAO header) — same-origin
          // is also what a real deployment looks like (app and PostHog proxy
          // served from the same host the browser navigated to).
          posthog_host: `http://${TEST_HOST}:${req.socket.localPort}/ph`,
          instance_id: "e2e-replay-body-leak",
        },
      });
    }
    if (p === "/api/version") return json({ version: "e2e-test", distName: "e2e", published: null });
    if (p === "/api/onboarding/status") return json({ completed: true });
    if (p === "/api/onboarding/deferred") return json({ deferred: [] });
    if (p === "/api/tasks") return json([]);
    if (p === "/api/projects") return json([]);
    if (p === "/api/auth/status") return json({ auth_mode: "api_key" });
    // REDACT-tier (moved off the allowlist: watcher_error/worker_error/
    // health_error can embed raw exception text, which can contain
    // filesystem paths — see replayScrub.js's classification comment).
    if (p === "/api/worker/status") {
      return json({ running: true, inflight: 0, max_workers: 1 });
    }
    // ALLOWLIST-tier (REPLAY_BODY_ALLOWLIST) — carries a marker with no path
    // or name in it, so the assertion below is purely "does tier-2 body
    // passthrough actually work", not accidentally also a path leak.
    if (p === "/api/queue/health") return json({ marker: ALLOWLIST_MARKER });
    if (p.startsWith("/api/metrics/")) return json({});
    if (p === "/api/fs/suggest") return json({ suggestions: [], prefix: "" });
    // REDACT-tier — the original bug's own endpoint family: filesystem paths
    // and repo names in the response body, PLUS the sentinel round-trips
    // through the outgoing query string (?root=<sentinel>) too.
    if (p === "/api/repos/discover") {
      return json({ repos: [{ path: SENTINEL_PATH, name: SENTINEL_NAME, ecosystem: "node" }] });
    }
    // Never enumerated in api.js at all — proves default-deny holds for an
    // endpoint the classification map/sweep can never have an entry for.
    if (p === UNLISTED_PATH) {
      const raw = await readBody(req).catch(() => Buffer.alloc(0));
      return json({ echo: raw.toString("utf8") || SENTINEL_UNLISTED, leaked: SENTINEL_UNLISTED });
    }
    if (p.startsWith("/api/")) {
      unmatched.push(`API UNMATCHED: ${req.method} ${p}`);
      return json({});
    }

    // ── static dist, with the A/B patch applied to the chunk that wires
    // maskCapturedNetworkRequestFn ──
    let rel = p === "/" ? "/index.html" : p;
    let file = path.join(DIST_DIR, rel);
    if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST_DIR, "index.html");
    const ext = path.extname(file);
    let body = fs.readFileSync(file);
    if (variant === "unmasked" && path.basename(file) === MASK_FN_CHUNK) {
      body = Buffer.from(
        body.toString("utf8").replaceAll("maskCapturedNetworkRequestFn:", "maskCapturedNetworkRequestFnOFF:"),
      );
    }
    res.writeHead(200, { "content-type": MIME[ext] || "application/octet-stream" });
    res.end(body);
  });
}

async function listen(server) {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server.address().port;
}

async function runPass(browser, variant) {
  const rawTexts = [];
  const capturedEvents = [];
  const unmatched = [];
  const server = makeServer({ variant, rawTexts, capturedEvents, unmatched });
  const port = await listen(server);
  const base = `http://${TEST_HOST}:${port}`;

  // Same bot-detection UA shim as dead-click-race.mjs — posthog-js's
  // "headlesschrome" blocklist would otherwise silently drop every capture().
  const ctx = await browser.newContext({
    userAgent:
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
  });
  const page = await ctx.newPage();
  // Playwright's context `userAgent` option only patches navigator.userAgent
  // — navigator.userAgentData.brands still reports "HeadlessChrome" from the
  // underlying Chromium build, which trips posthog-js's bot-detection
  // blocklist and silently drops EVERY capture() call (not just replay —
  // all event types), same as dead-click-race.mjs's own shim.
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => false });
    if (navigator.userAgentData) {
      Object.defineProperty(navigator, "userAgentData", {
        get: () => ({
          brands: [
            { brand: "Not_A Brand", version: "24" },
            { brand: "Chromium", version: "128" },
            { brand: "Google Chrome", version: "128" },
          ],
          mobile: false,
          platform: "macOS",
        }),
      });
    }
  });
  await page.route("**/*", (route) => {
    const u = new URL(route.request().url());
    if (u.hostname !== "127.0.0.1" && u.hostname !== TEST_HOST) {
      unmatched.push(`EGRESS BLOCKED: ${route.request().url()}`);
      return route.abort();
    }
    return route.continue();
  });
  const consoleErrors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => consoleErrors.push(String(err)));

  await page.goto(base + "/", { waitUntil: "networkidle" });
  await page.waitForTimeout(500);

  // Session recording's network-capture (recordBody/recordHeaders) is wired
  // by monkey-patching window.fetch/XHR inside posthog-js's LAZILY-LOADED
  // recorder chunk (fetched async from /ph/static/... only after the
  // config round-trip decides to record). Any fetch that fires before that
  // patch lands can never be captured — retroactively, no matter how long
  // we wait afterward, because the request already completed unwrapped.
  // The app's own /api/queue/health poll (App.jsx, setInterval 10000ms,
  // same poll() as /api/worker/status) fires once immediately on mount —
  // almost certainly before the patch is installed — so the harness must
  // not treat "a" queue/health response as proof of readiness; it needs the
  // *second* one. Give the async recorder chunk a generous head start
  // before touching anything else.
  const queueHealthHits = [];
  page.on("response", (r) => {
    if (r.url().includes("/api/queue/health")) queueHealthHits.push(Date.now());
  });
  await page.waitForTimeout(5000);

  // ── real UI drive: Settings → (default) Projects pane → New Project →
  // type the sentinel into the scan-root field → Scan. This is the exact
  // shape of the original bug: a repo path chosen on the user's machine,
  // typed into the real UI, sent to a real `tier: "redact"` endpoint. Fired
  // only after the 5s head start above, so the fetch wrapper is installed
  // by the time this one-shot request goes out (unlike queue/health, a
  // user-triggered Scan click never repeats on its own — there is no later
  // occurrence to fall back on, so this one has to land after the patch). ──
  await page.getByRole("button", { name: /^Settings$/ }).click();
  await page.waitForTimeout(300);
  await page.getByRole("button", { name: /New Project/ }).click();
  await page.waitForTimeout(200);
  const scanInput = page.getByPlaceholder("Scan root, e.g. ~/git");
  await scanInput.fill(SENTINEL_PATH);
  const discoverResp = page.waitForResponse((r) => r.url().includes("/api/repos/discover"), { timeout: 5000 });
  await page.getByRole("button", { name: /^Scan$/ }).click();
  await discoverResp.catch(() => {});
  await page.waitForTimeout(300);

  // ── default-deny live proof: a real browser fetch (still inside the same
  // page/posthog instance — the mechanism under test is the network-capture
  // interception, which is global, not tied to how the request was
  // triggered) to a path that literally does not exist in api.js, so it can
  // never be added to replayScrub.js's classification map. ──
  await page.evaluate(
    async ({ p, sentinel }) => {
      try {
        await fetch(p, { method: "POST", body: JSON.stringify({ leak: sentinel }) });
      } catch {
        /* best-effort — the assertion is about what reaches PostHog, not this call succeeding */
      }
    },
    { p: UNLISTED_PATH, sentinel: SENTINEL_UNLISTED },
  );

  // queue/health polls every 10s (App.jsx). Wait for the SECOND hit
  // specifically — the first predates the fetch wrapper (see above) and is
  // not evidence that allow-tier passthrough capture is actually wired up.
  const deadline = Date.now() + 16000;
  while (queueHealthHits.length < 2 && Date.now() < deadline) {
    await page.waitForTimeout(250);
  }

  // Past posthog's flush_interval_ms (default 3000ms) so anything captured
  // above is actually on the wire before we inspect it.
  await page.waitForTimeout(4000);
  await page.evaluate(() => {
    window.posthog?._handle_unload?.();
  });
  await page.waitForTimeout(1500);

  await ctx.close();
  await new Promise((resolve) => server.close(resolve));
  return {
    rawTexts,
    capturedEvents,
    unmatched: [...new Set(unmatched)],
    consoleErrors: consoleErrors.slice(0, 5),
  };
}

const browser = await chromium.launch({
  args: [`--host-resolver-rules=MAP ${TEST_HOST} 127.0.0.1`],
});
try {
  const masked = await runPass(browser, "masked");
  const unmasked = await runPass(browser, "unmasked");

  for (const [label, r] of [["masked", masked], ["unmasked", unmasked]]) {
    if (process.env.DIAG) {
      console.log(`=== DIAG ${label}: ${r.capturedEvents.length} events, ${r.rawTexts.length} POST bodies ===`);
      r.capturedEvents.forEach((e, i) => console.log(`  event[${i}]: ${e.event}`));
      fs.writeFileSync(`/tmp/rawtexts-${label}.log`, r.rawTexts.join("\n\n=====\n\n"));
    }
    if (r.unmatched.length) {
      console.log(`=== unmatched/log (${label} pass) ===`);
      r.unmatched.forEach((l) => console.log(l));
    }
    if (r.consoleErrors.length) console.log(`console errors (${label}):`, r.consoleErrors);
  }

  const haystack = (r) => r.rawTexts.join("\n\x00\n");
  const mHay = haystack(masked);
  const uHay = haystack(unmasked);

  const checks = [];
  function check(name, ok, detail) {
    checks.push({ name, ok });
    console.log(`${ok ? "PASS" : "FAIL"}: ${name}${detail ? " — " + detail : ""}`);
  }

  // 1. vacuity guard
  const snapshotCount = masked.capturedEvents.filter((e) => e.event === "$snapshot").length;
  check(
    "vacuity guard: the harness actually captured $snapshot session-replay events",
    snapshotCount > 0,
    `captured ${snapshotCount} $snapshot event(s) across ${masked.rawTexts.length} POST bodies`,
  );

  // 2. redact-tier: the planted repo path/name must not reach PostHog
  check("masked: sentinel repo PATH is absent from every captured byte", !mHay.includes(SENTINEL_PATH));
  check("masked: sentinel repo NAME is absent from every captured byte", !mHay.includes(SENTINEL_NAME));

  // 3. default-deny for an endpoint nobody enumerated
  check(
    "masked: default-deny — sentinel for an endpoint NOT in api.js's classification map is absent",
    !mHay.includes(SENTINEL_UNLISTED),
  );

  // 4. allow-tier passthrough actually captures real bytes (rules out "1-3
  // pass because nothing is ever captured")
  check(
    "masked: allowlisted /api/queue/health marker IS present (tier-2 passthrough proven live, not just by omission)",
    mHay.includes(ALLOWLIST_MARKER),
  );

  // Control: same bundle, masking mechanism string-patched away — must leak.
  check(
    "control (masking removed): sentinel repo PATH DOES leak — proves this harness has discriminating power",
    uHay.includes(SENTINEL_PATH),
  );

  const failed = checks.filter((c) => !c.ok);
  console.log(failed.length ? `\nFAIL — ${failed.length} check(s) failed` : "\nPASS — all checks passed");
  process.exit(failed.length ? 1 : 0);
} finally {
  await browser.close();
}
