// Dead-click ordering-race e2e check.
//
// Ties to web/src/deadClickFilter.js + web/src/telemetry.js's `before_send`.
// posthog-js 1.417.1 stamps a dead-click candidate's own click.timestamp in a
// window click listener, but stamps `_lastMutation` in a MutationObserver
// callback that only runs as a microtask AFTER React flushes the click's own
// re-render. With session replay on, rrweb's per-click overhead is 1-12ms —
// enough that the click's own re-render mutation can land a hair too late
// for posthog-js to credit it to that click, so a button that DID visibly
// re-render times out ~2.75s later as `$dead_click`. `before_send:
// deadClickBeforeSend` drops exactly that ordering-race artefact client-side;
// this harness proves it against a real Chromium + the actual built bundle
// (web/dist), not against a unit-test stub of posthog-js.
//
// Two passes of the SAME built bundle:
//   "filtered"   — the real dist, before_send wired as shipped.
//   "unfiltered" — dist served with a string-patch (`before_send:` ->
//                  `beforeSendOff:`) so posthog-js's own init() ignores it.
//                  This is the harness's proof of discriminating power: if
//                  the race were not real (or the harness never actually
//                  reached posthog's capture path), this control would also
//                  come back clean. It doesn't — see the RESULTS table.
// Two scenarios per pass:
//   "rerender-button" — the app's real theme toggle (.nh-theme-toggle),
//                        which synchronously re-renders on click.
//   "dead-button"      — a plain injected <button> wired to nothing, which
//                        never mutates the DOM — a genuine dead click, which
//                        must still be reported in BOTH passes.
//
// Known gap, not exercised here (see telemetry.js's header): capture_heatmaps
// runs its own DeadClicksAutocapture instance that feeds `$$heatmap` directly
// and never calls before_send, so it still carries this race unfiltered.
//
// Everything the browser can reach is a local mock (below) — no PostHog
// endpoint, real or otherwise, is ever contacted.
//
//   node e2e/dead-click-race.mjs   # needs `npm run build` first (drives web/dist)
import http from "node:http";
import zlib from "node:zlib";
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

const WEB_DIR = new URL("..", import.meta.url).pathname;
const DIST_DIR = path.join(WEB_DIR, "dist");
const PH_DIST_DIR = path.join(WEB_DIR, "node_modules/posthog-js/dist");

if (!fs.existsSync(DIST_DIR)) {
  console.error(`dead-click-race: ${DIST_DIR} does not exist — run \`npm run build\` first.`);
  process.exit(1);
}

// The one built chunk that contains the literal `before_send:` wiring —
// found by scanning dist/assets for it. Used for the A/B string-patch above.
const BEFORE_SEND_CHUNK = fs
  .readdirSync(path.join(DIST_DIR, "assets"))
  .find(
    (f) =>
      /^index-.*\.js$/.test(f) &&
      fs.readFileSync(path.join(DIST_DIR, "assets", f), "utf8").includes("before_send:"),
  );
if (!BEFORE_SEND_CHUNK) throw new Error("could not find the chunk containing before_send: in dist/assets");

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

function decodeEvents(raw, req) {
  let buf = raw;
  try {
    const enc = req.headers["content-encoding"] || "";
    if (enc.includes("gzip")) buf = zlib.gunzipSync(buf);
  } catch {
    /* fall through to raw */
  }
  let text = buf.toString("utf8");
  // application/x-www-form-urlencoded with a `data=<base64>` field (older
  // posthog-js beacon fallback).
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
    return [];
  }
  return Array.isArray(parsed) ? parsed : parsed.batch ? parsed.batch : [parsed];
}

function makeServer({ variant, captured, unmatched }) {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://localhost");
    const p = url.pathname;

    function json(obj) {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(obj));
    }

    // ── posthog: static assets (dead-clicks-autocapture.js, recorder.js, ...) ──
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

    // ── posthog: remote config — this is what actually gates session replay,
    // dead-click capture and heatmaps (posthog-js's RemoteConfig loader: GET
    // /array/<token>/config.js first — a <script> tag that would set
    // window._POSTHOG_REMOTE_CONFIG[token] itself if it wanted to preload —
    // and only when that sets nothing does it fall back to a JSON GET at
    // /array/<token>/config). /flags/ and /decide/ are kept too since a later
    // feature-flag reload hits them, but they do NOT gate replay/dead-clicks.
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
        // Advertise NO compression. posthog-js's "gzip-js" mode compresses
        // request bodies at the application layer WITHOUT setting the HTTP
        // Content-Encoding header, which decodeEvents() above has no way to
        // detect — advertising it here would make every captured event
        // silently undecodable (still a 200 on the wire, empty in the mock).
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

    // ── posthog: event capture endpoints ──
    if (req.method === "POST" && (p === "/ph/e/" || p === "/ph/i/v0/e/" || p === "/ph/s/" || p === "/ph/batch/")) {
      const raw = await readBody(req);
      for (const ev of decodeEvents(raw, req)) {
        if (ev && ev.event) captured.push({ path: p, event: ev.event, properties: ev.properties || {} });
      }
      json({ status: 1 });
      return;
    }
    if (p.startsWith("/ph/")) {
      unmatched.push(`PH UNMATCHED: ${req.method} ${p}`);
      json({ status: 1 });
      return;
    }

    // ── app API stubs — just enough for the board shell to mount ──
    if (p === "/api/config") {
      return json({
        telemetry: {
          enabled: true,
          posthog_publishable: "phc_e2e_test_key",
          posthog_host: `http://127.0.0.1:${req.socket.localPort}/ph`,
          instance_id: "e2e-dead-click-race",
        },
      });
    }
    if (p === "/api/version") return json({ version: "e2e-test", distName: "e2e", published: null });
    if (p === "/api/onboarding/status") return json({ completed: true });
    if (p === "/api/onboarding/deferred") return json({ deferred: [] });
    if (p === "/api/tasks") return json([]);
    if (p === "/api/projects") return json([]);
    if (p === "/api/auth/status") return json({ auth_mode: "api_key" });
    if (p === "/api/worker/status") return json({ running: true, inflight: 0, max_workers: 1 });
    if (p === "/api/queue/health") return json({});
    if (p.startsWith("/api/metrics/")) return json({});
    if (p.startsWith("/api/")) {
      unmatched.push(`API UNMATCHED: ${req.method} ${p}`);
      return json({});
    }

    // ── static dist, with the A/B patch applied to the one chunk that wires before_send ──
    let rel = p === "/" ? "/index.html" : p;
    let file = path.join(DIST_DIR, rel);
    if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST_DIR, "index.html");
    const ext = path.extname(file);
    let body = fs.readFileSync(file);
    if (variant === "unfiltered" && path.basename(file) === BEFORE_SEND_CHUNK) {
      body = Buffer.from(body.toString("utf8").replaceAll("before_send:", "beforeSendOff:"));
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
  const captured = [];
  const unmatched = [];
  const server = makeServer({ variant, captured, unmatched });
  const port = await listen(server);
  const base = `http://127.0.0.1:${port}`;

  const results = [];
  for (const scenario of ["rerender-button", "dead-button"]) {
    // posthog-js's bot-detection blocklist matches "headlesschrome" (and
    // navigator.webdriver) and silently drops every capture() call for it —
    // Playwright's default headless Chromium trips this. Give the context a
    // normal Chrome UA so the harness behaves like a real user session; this
    // is a test-environment shim, not a product behavior change.
    const ctx = await browser.newContext({
      userAgent:
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    });
    const page = await ctx.newPage();
    // Egress guard: nothing but our own 127.0.0.1 mock should ever be reachable.
    await page.route("**/*", (route) => {
      const u = new URL(route.request().url());
      if (u.hostname !== "127.0.0.1") {
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

    // Playwright's context `userAgent` option only patches navigator.userAgent
    // — navigator.userAgentData.brands still reports "HeadlessChrome" from the
    // underlying Chromium build, which also trips posthog-js's bot blocklist.
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

    await page.goto(base, { waitUntil: "load" });
    await page.waitForSelector(".nh-theme-toggle", { timeout: 15000 });
    // Let posthog finish init + the remote-config round trip before clicking.
    await page.waitForTimeout(1500);

    const beforeCount = captured.length;
    if (scenario === "rerender-button") {
      // The race window is only 1-2ms wide (rrweb's per-click microtask
      // overhead vs. the click listener's own stamp), so a single click
      // doesn't reproduce it deterministically — retry. Rapid-fire retries
      // would be WORSE, not better: posthog-js's `_lastMutation` is one
      // timestamp shared by the whole page, and it only credits a mutation
      // to a candidate when candidate.timestamp <= _lastMutation — so a
      // LATER click's own re-render can retroactively "resolve" an EARLIER
      // click's candidate and hide the race entirely. Each attempt is
      // therefore spaced past posthog's own ~2750ms absolute-timeout window
      // (mutation_threshold_ms default 2500 + margin) plus its ~1000ms
      // periodic-check cadence, so candidates stay independent. Stop as soon
      // as one attempt lands the race. 4 attempts measured flaky (5/6 clean
      // standalone runs, one miss on the unfiltered control); 8 measured
      // clean across 5/5 runs at ~4s/attempt — worth the extra wall-clock.
      for (let i = 0; i < 8; i++) {
        await page.click(".nh-theme-toggle");
        await page.waitForTimeout(4000);
        if (captured.slice(beforeCount).some((e) => e.event === "$dead_click")) break;
      }
    } else {
      await page.evaluate(() => {
        const btn = document.createElement("button");
        btn.id = "e2e-dead";
        btn.textContent = "dead";
        document.body.appendChild(btn);
      });
      await page.waitForTimeout(1200);
      await page.click("#e2e-dead");
    }
    // Past posthog's ~2750ms absolute timeout, its ~1000ms check cadence,
    // and its RequestQueue's flush_interval_ms (default 3000ms) that batches
    // an already-captured event before it hits the wire.
    await page.waitForTimeout(5500);
    // Best-effort nudge for anything still queued. NOTE (landing review of
    // 55916ece): in this app `window.posthog` is undefined — telemetry.js
    // initialises the module without assigning it to the window — so this
    // call is currently INERT and the 5500 ms wait above is what actually
    // guarantees delivery. It is kept as a no-cost belt for a build that
    // does expose the global; do not treat it as the flush.
    await page.evaluate(() => {
      window.posthog?._handle_unload?.();
    });
    await page.waitForTimeout(1500);

    const newEvents = captured.slice(beforeCount);
    const deadClicks = newEvents.filter((e) => e.event === "$dead_click");
    results.push({
      variant,
      scenario,
      deadClickSent: deadClicks.length > 0,
      deadClicks: deadClicks.map((e) => ({
        gap: e.properties.$dead_click_event_timestamp - e.properties.$dead_click_last_mutation_timestamp,
        delay: e.properties.$dead_click_mutation_delay_ms,
      })),
      consoleErrors: consoleErrors.slice(0, 5),
    });
    await ctx.close();
  }
  await new Promise((resolve) => server.close(resolve));
  return { results, unmatched: [...new Set(unmatched)] };
}

const browser = await chromium.launch();
try {
  const filtered = await runPass(browser, "filtered");
  const unfiltered = await runPass(browser, "unfiltered");

  if (filtered.unmatched.length) {
    console.log("=== unmatched/log (filtered pass) ===");
    filtered.unmatched.forEach((l) => console.log(l));
  }
  if (unfiltered.unmatched.length) {
    console.log("=== unmatched/log (unfiltered pass) ===");
    unfiltered.unmatched.forEach((l) => console.log(l));
  }

  console.log("\n=== RESULTS ===");
  console.log("variant     scenario           dead_click_sent  detail");
  for (const r of [...filtered.results, ...unfiltered.results]) {
    console.log(
      `${r.variant.padEnd(11)} ${r.scenario.padEnd(18)} ${String(r.deadClickSent).padEnd(16)} ${JSON.stringify(r.deadClicks)}`,
    );
    if (r.consoleErrors.length) console.log("   console errors:", r.consoleErrors);
  }

  const f = Object.fromEntries(filtered.results.map((r) => [r.scenario, r]));
  const u = Object.fromEntries(unfiltered.results.map((r) => [r.scenario, r]));

  const checks = [];
  function check(name, ok, detail) {
    checks.push({ name, ok });
    console.log(`${ok ? "PASS" : "FAIL"}: ${name}${detail ? " — " + detail : ""}`);
  }

  check(
    "filtered: re-rendering button does NOT send $dead_click",
    Boolean(f["rerender-button"]) && !f["rerender-button"].deadClickSent,
    JSON.stringify(f["rerender-button"]),
  );
  check(
    "filtered: genuinely dead control STILL sends $dead_click",
    Boolean(f["dead-button"]) && f["dead-button"].deadClickSent,
    JSON.stringify(f["dead-button"]),
  );
  check(
    "unfiltered (control): re-rendering button DOES send $dead_click " +
      "(proves the race is real and this harness actually reaches posthog's " +
      "capture path, rather than the filtered pass above being clean because " +
      "nothing was ever captured)",
    Boolean(u["rerender-button"]) && u["rerender-button"].deadClickSent,
    JSON.stringify(u["rerender-button"]),
  );

  const failed = checks.filter((c) => !c.ok);
  console.log(failed.length ? `\nFAIL — ${failed.length} check(s) failed` : "\nPASS — all checks passed");
  process.exit(failed.length ? 1 : 0);
} finally {
  await browser.close();
}
