// Session-replay DOM/rrweb leak e2e check.
//
// Sibling to replay-body-leak.mjs, but a DIFFERENT capture channel: this
// file drives a real Chromium against the real built bundle (web/dist) with
// the real posthog-js recorder chunk loaded, and inspects the ACTUAL decoded
// bytes of the DOM/rrweb snapshot fields the browser sent to a mock PostHog
// ingestion endpoint — not a source-text assertion that a class name is
// present somewhere.
//
// The bug: `/api/queue/health`'s real response shape (core/health.py
// QueueHealth.as_dict) carries `paused_profile`, a user-chosen auth-profile
// name (`nh auth use <profile>`), whenever `paused_reason === "quota"` — a
// routine pause state, not an edge case. web/src/drainChip.js used to render
// that name verbatim into the sidebar PausedIndicator's `title` attribute.
// PostHog session replay's DOM/rrweb mutation-snapshot capture recorded that
// attribute regardless of the SEPARATE network-body masking
// (web/src/replayScrub.js / web/src/telemetry.js's
// maskCapturedNetworkRequestFn) — that mechanism governs HTTP request/
// response bodies only and has zero effect on rendered DOM content.
// `.ph-no-capture` (rrweb's own block-selector) is the mechanism that DOES
// govern this channel; drainChip.js's fix instead removes the name at the
// source, since the operator does not need it to act on a quota pause.
//
// This harness is deliberately scoped to the DOM/rrweb channel ONLY (see
// `domHaystack` below, built from decompressed rrweb snapshot fields alone,
// never the outer network-capture body text) — NOT "every captured byte
// across both channels". The network-capture channel for this exact field
// is a separate, already-in-flight concern (task 0c7cc4b2, replay-body-leak.mjs's
// check 5); coupling this harness to that channel would make it pass or fail
// for reasons that have nothing to do with the DOM fix this file verifies,
// and web/e2e/replay-body-leak.mjs / web/src/replayScrub.js / web/src/telemetry.js
// are explicitly out of scope for this change.
//
// Three things are checked against the real captured, decompressed bytes:
//   1. vacuity guard  — the harness actually captured $snapshot replay
//                        events at all (a clean result on a harness that
//                        captured nothing would be meaningless).
//   2. DOM liveness   — the paused indicator's VISIBLE text (which tells the
//                        operator work is paused and when it resumes) DOES
//                        reach the decompressed DOM/rrweb channel. Without
//                        this, check 3 passing could just mean the harness
//                        never captured this part of the DOM at all.
//   3. no profile leak — the user-chosen auth-profile name sentinel is
//                        ABSENT from every decompressed DOM/rrweb snapshot
//                        field, while check 2 proves the surrounding element
//                        (and its informative text) was captured live.
//
// Served directly on 127.0.0.1 (no TEST_HOST / --host-resolver-rules trick):
// posthog-js's built-in localhost network-capture guard (lazy-recorder.js:
// `B=["localhost","127.0.0.1"]`) suppresses network BODY/header capture only
// — it has no bearing on DOM/rrweb snapshot capture, which is what this file
// tests, so there is nothing here for that guard to interfere with.
//
//   node e2e/replay-dom-leak.mjs   # needs `npm run build` first (drives web/dist)
import http from "node:http";
import zlib from "node:zlib";
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

const WEB_DIR = new URL("..", import.meta.url).pathname;
const DIST_DIR = path.join(WEB_DIR, "dist");
const PH_DIST_DIR = path.join(WEB_DIR, "node_modules/posthog-js/dist");

if (!fs.existsSync(DIST_DIR)) {
  console.error(`replay-dom-leak: ${DIST_DIR} does not exist — run \`npm run build\` first.`);
  process.exit(1);
}

// Own sentinel, distinct from replay-body-leak.mjs's SENTINEL_QUOTA_PROFILE —
// separate harnesses, separate mock servers, no reason to share one, and a
// distinct value makes it unambiguous which suite's evidence a captured byte
// came from if anyone ever greps a saved payload.
const SENTINEL_PROFILE = "zzqq-dom-profile-canary-8c71";
// The prefix of pausedPresentation("quota", ...)'s `text` (drainChip.js) —
// what tells the operator work is paused and when it resumes. Deliberately
// NOT asserting the exact suffix (a formatted HH:MM), which is incidental
// here; the identifying part (the profile name) is what this fix removes,
// not the "paused / resumes" information itself.
const VISIBLE_TEXT = "Paused — quota resets";

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

// Same reversal as replay-body-leak.mjs's inflateBinaryGzipString: posthog-js's
// recorder chunk gzip-compresses individual rrweb snapshot fields (a
// FullSnapshot's whole `data`, or a Mutation/StyleSheetRule
// IncrementalSnapshot's `texts`/`attributes`/`removes`/`adds`) and encodes
// the compressed bytes as a JS "binary string" (one UTF-16 code unit per raw
// byte, via `String.fromCharCode`) — NOT base64 — before they reach a
// "$snapshot" event's `properties.$snapshot_data`. `Buffer.from(s, "binary")`
// (Node's latin1 alias) reverses that byte-for-byte before `zlib.gunzipSync`.
function inflateBinaryGzipString(s) {
  if (typeof s !== "string" || s.length === 0) return undefined;
  try {
    return zlib.gunzipSync(Buffer.from(s, "binary")).toString("utf8");
  } catch {
    return undefined;
  }
}

// Decompresses one rrweb event's `cv: "2024-10"`-marked field(s) into plain
// text. Returns "" for events that aren't compressed (most — e.g. the
// "rrweb/network@1" Plugin-type events that carry HTTP bodies are never in
// the compression-eligible set: only FullSnapshot and Mutation/
// StyleSheetRule IncrementalSnapshot events are, and only those two carry
// actual page DOM content).
function decompressRrwebEvent(ev) {
  if (!ev || ev.cv !== "2024-10" || ev.data == null) return "";
  const pieces = [];
  if (typeof ev.data === "string") {
    const out = inflateBinaryGzipString(ev.data);
    if (out !== undefined) pieces.push(out);
  } else if (typeof ev.data === "object") {
    for (const key of ["texts", "attributes", "removes", "adds"]) {
      const out = inflateBinaryGzipString(ev.data[key]);
      if (out !== undefined) pieces.push(out);
    }
  }
  return pieces.join("\n");
}

// Returns:
//   - domText: EVERY decompressed rrweb DOM/rrweb snapshot field this POST
//     carried, joined — the actual DOM-channel haystack this file's checks
//     use. Deliberately excludes the outer network-capture body text (that's
//     replay-body-leak.mjs's channel, not this one).
//   - events: best-effort parsed top-level PostHog events (for the vacuity
//     guard: proving $snapshot events were actually captured).
function decode(raw, req) {
  let buf = raw;
  try {
    const enc = req.headers["content-encoding"] || "";
    // posthog-js's default pre-`/decide`-response compression scheme is
    // "gzip-js" (module.js: `this.compression=i.disable_compression?void
    // 0:Hn.GZipJS` at client construction, before any decide round-trip
    // completes): it gzips the WHOLE POST body itself and sends it as raw
    // bytes with NO Content-Encoding header and no `?compression=` query
    // param (that query param is reserved for the "base64" scheme only —
    // `ia=(t,e,i,r)=>...i===Hn.GZipJS?Wo(t,"compression"):t` strips it for
    // GZipJS). The very first POST this harness observes always lands
    // before the decide response disables compression, so a header-only
    // check misses it entirely; sniff the gzip magic number instead, same
    // as a real PostHog capture endpoint does.
    if (enc.includes("gzip") || (buf.length >= 2 && buf[0] === 0x1f && buf[1] === 0x8b)) {
      buf = zlib.gunzipSync(buf);
    }
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
    return { domText: "", events: [] };
  }
  const events = Array.isArray(parsed) ? parsed : parsed.batch ? parsed.batch : [parsed];
  const inner = [];
  for (const ev of events) {
    const rrwebEvents =
      ev && ev.properties && Array.isArray(ev.properties.$snapshot_data) ? ev.properties.$snapshot_data : [];
    for (const re of rrwebEvents) {
      const d = decompressRrwebEvent(re);
      if (d) inner.push(d);
    }
  }
  return { domText: inner.join("\n"), events };
}

function makeServer({ domTexts, capturedEvents, unmatched }) {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://localhost");
    const p = url.pathname;

    function json(obj) {
      // Explicit Content-Length, not chunked transfer-encoding — posthog-js's
      // fetch response-body capture refuses to read chunked bodies at all
      // (see replay-body-leak.mjs's identical comment).
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
      const { domText, events } = decode(raw, req);
      domTexts.push(domText);
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
          posthog_host: `http://127.0.0.1:${req.socket.localPort}/ph`,
          instance_id: "e2e-replay-dom-leak",
        },
      });
    }
    if (p === "/api/version") return json({ version: "e2e-test", dist_name: "e2e", published: null });
    if (p === "/api/onboarding/status") return json({ completed: true });
    if (p === "/api/onboarding/deferred") return json({ deferred: [] });
    if (p === "/api/tasks") return json([]);
    if (p === "/api/projects") return json([]);
    if (p === "/api/auth/status") return json({ auth_mode: "api_key" });
    if (p === "/api/worker/status") {
      return json({ running: true, inflight: 0, max_workers: 1 });
    }
    // The finding this file exists to verify: real /api/queue/health shape
    // (core/health.py QueueHealth.as_dict) with paused_reason: "quota", which
    // carries paused_profile whenever that reason fires — a routine state.
    if (p === "/api/queue/health") {
      return json({
        open_tasks: 0, at_gate: 0, completed_in_window: 0, window_minutes: 30,
        stuck: false, stuck_reason: "", eta_minutes: null,
        workers_busy: 0, max_workers: 1, queue_depth: 0, est_drain_seconds: null,
        paused: true, paused_reason: "quota", paused_until: "2026-01-01T00:00:00Z",
        paused_profile: SENTINEL_PROFILE,
      });
    }
    if (p.startsWith("/api/metrics/")) return json({});
    if (p === "/api/fs/suggest") return json({ suggestions: [], prefix: "" });
    if (p.startsWith("/api/")) {
      unmatched.push(`API UNMATCHED: ${req.method} ${p}`);
      return json({});
    }

    // ── static dist, served as-is — no A/B patching in this harness ──
    let rel = p === "/" ? "/index.html" : p;
    let file = path.join(DIST_DIR, rel);
    if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST_DIR, "index.html");
    const ext = path.extname(file);
    const body = fs.readFileSync(file);
    res.writeHead(200, { "content-type": MIME[ext] || "application/octet-stream" });
    res.end(body);
  });
}

async function listen(server) {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server.address().port;
}

async function runPass(browser) {
  const domTexts = [];
  const capturedEvents = [];
  const unmatched = [];
  const server = makeServer({ domTexts, capturedEvents, unmatched });
  const port = await listen(server);
  const base = `http://127.0.0.1:${port}`;

  // Same bot-detection UA shim as replay-body-leak.mjs/dead-click-race.mjs —
  // posthog-js's "headlesschrome" blocklist would otherwise silently drop
  // every capture() call (not just replay).
  const ctx = await browser.newContext({
    userAgent:
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
  });
  const page = await ctx.newPage();
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

  await page.goto(base + "/", { waitUntil: "networkidle" });
  await page.waitForTimeout(500);

  // Session recording is wired by the LAZILY-LOADED recorder chunk (fetched
  // async from /ph/static/... only after the config round-trip decides to
  // record). Its initial FullSnapshot captures whatever DOM is present the
  // moment it finishes loading; a generous head start here just means "by
  // the time the recorder is ready, the paused indicator has certainly
  // already rendered" — App.jsx's first /api/queue/health poll fires on
  // mount, well before this wait ends.
  await page.waitForTimeout(5000);

  // Positive control: the sidebar's paused indicator (App.jsx's
  // `<PausedIndicator {...queueHealth} />`, always rendered — not gated
  // behind any Settings navigation — whenever `queueHealth.paused` and not
  // `stuck`) must actually be visible, or the checks below would be
  // meaningless.
  const indicator = page.getByRole("status").filter({ hasText: "Paused" });
  await indicator.first().waitFor({ state: "visible", timeout: 5000 });

  // Force at least one real DOM mutation for rrweb to record. An idle page
  // (no clicks, no navigation) never accumulates enough Mutation/
  // IncrementalSnapshot volume to cross posthog-js's session-recording
  // buffer's flush threshold within any reasonable wait — confirmed
  // empirically: waiting 20s+ past load on a page that never mutates still
  // produces zero `$snapshot` events, while replay-body-leak.mjs's sibling
  // harness (which types into a field and clicks Scan, generating many DOM
  // mutations) reliably gets its first snapshot POST out. Opening and
  // closing the sidebar's Settings overlay (a real, already-wired
  // interaction — App.jsx's `.nh-settings-row` → `openSettings()`, closed
  // via Settings.jsx's own Escape handler) is a minimal, already-existing
  // way to generate that mutation volume without adding new API surface:
  // any request it fires that this mock server doesn't explicitly stub
  // falls through to the `/api/*` catch-all's `json({})` above.
  await page.click(".nh-settings-row");
  await page.waitForTimeout(500);
  await page.keyboard.press("Escape");

  // Past posthog's flush_interval_ms (default 3000ms) so the FullSnapshot
  // (and any Mutation records) are actually on the wire before inspection.
  await page.waitForTimeout(6000);
  await page.evaluate(() => {
    window.posthog?._handle_unload?.();
  });
  await page.waitForTimeout(1500);

  await ctx.close();
  await new Promise((resolve) => server.close(resolve));
  return {
    domTexts,
    capturedEvents,
    unmatched: [...new Set(unmatched)],
    consoleErrors: consoleErrors.slice(0, 5),
  };
}

const browser = await chromium.launch();
try {
  const result = await runPass(browser);

  if (process.env.DIAG) {
    console.log(`=== DIAG: ${result.capturedEvents.length} events, ${result.domTexts.length} POST bodies ===`);
    result.capturedEvents.forEach((e, i) => console.log(`  event[${i}]: ${e.event}`));
    fs.writeFileSync("/tmp/domtexts-replay-dom-leak.log", result.domTexts.join("\n\n=====\n\n"));
  }
  if (result.unmatched.length) {
    console.log("=== unmatched/log ===");
    result.unmatched.forEach((l) => console.log(l));
  }
  if (result.consoleErrors.length) console.log("console errors:", result.consoleErrors);

  // domHaystack: every decompressed DOM/rrweb snapshot field across every
  // captured POST — the DOM/rrweb capture channel only (see decode()'s
  // header comment), never the outer network-capture body text.
  const domHaystack = result.domTexts.join("\n\x00\n");

  const checks = [];
  function check(name, ok, detail) {
    checks.push({ name, ok });
    console.log(`${ok ? "PASS" : "FAIL"}: ${name}${detail ? " — " + detail : ""}`);
  }

  // 1. vacuity guard
  const snapshotCount = result.capturedEvents.filter((e) => e.event === "$snapshot").length;
  check(
    "vacuity guard: the harness actually captured $snapshot session-replay events",
    snapshotCount > 0,
    `captured ${snapshotCount} $snapshot event(s) across ${result.domTexts.length} POST bodies`,
  );

  // 2. DOM-channel liveness — the informative, non-identifying part of the
  // paused indicator ("paused" / "resumes when") DOES reach the decompressed
  // DOM/rrweb channel, so check 3's absence can't just mean this region of
  // the DOM was never captured at all.
  check(
    "DOM-channel liveness: the visible paused text reaches the decompressed DOM/rrweb snapshot data",
    domHaystack.includes(VISIBLE_TEXT),
  );

  // 3. the actual fix: the user-chosen auth-profile name must be absent from
  // EVERY decompressed DOM/rrweb snapshot field — verified by capturing a
  // real payload and decompressing it, not by asserting a class name is in
  // the source.
  check(
    "the user-chosen auth-profile name is absent from every decompressed DOM/rrweb snapshot field",
    !domHaystack.includes(SENTINEL_PROFILE),
  );

  const failed = checks.filter((c) => !c.ok);
  console.log(failed.length ? `\nFAIL — ${failed.length} check(s) failed` : "\nPASS — all checks passed");
  process.exit(failed.length ? 1 : 0);
} finally {
  await browser.close();
}
