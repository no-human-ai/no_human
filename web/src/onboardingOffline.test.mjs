// Incident 2026-09-04: the operator walked the onboarding wizard while the
// backend process died mid-flow. Every step's fetch rejected and rendered its
// own raw "Failed to fetch" string — REPOSITORIES path-completion silently
// dead, DOCS chips, INTEGRATIONS list, no explanation, no way back. These
// tests pin `offlineRetry.js` (the pure logic: classification, banner
// view-model, fake-clock-driven probe) plus static-source guards on
// `Onboarding.jsx`, the same idiom `connectionBanner.test.mjs` /
// `wsReconnect.test.mjs` use for markup no jsdom/React renderer can mount.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { isNetworkError, offlineBanner, createServerProbe, PROBE_INTERVAL_MS } from "./offlineRetry.js";
import { detailMessage } from "./apiError.js";

const here = fileURLToPath(new URL(".", import.meta.url));
const onboardingSrc = readFileSync(here + "Onboarding.jsx", "utf8");
const stylesCss = readFileSync(here + "styles.css", "utf8");

/** A queue of {at, fn} timers a test advances by hand — no real clock. */
function fakeClock() {
  let now = 0;
  let nextId = 1;
  const pending = new Map();
  return {
    setTimeout(fn, ms) {
      const id = nextId++;
      pending.set(id, { at: now + ms, fn, ms });
      return id;
    },
    clearTimeout(id) {
      pending.delete(id);
    },
    advance(ms) {
      now += ms;
      const due = [...pending.entries()]
        .filter(([, t]) => t.at <= now)
        .sort((a, b) => a[1].at - b[1].at);
      for (const [id, t] of due) {
        pending.delete(id);
        t.fn();
      }
    },
    pendingCount() {
      return pending.size;
    },
  };
}

test("a network-level TypeError raises the wizard banner, never the exception text", () => {
  assert.equal(isNetworkError(new TypeError("Failed to fetch")), true);
  const banner = offlineBanner({ offline: true, probing: false });
  assert.ok(banner);
  assert.equal(banner.text, "The no_human server is not responding");
  assert.ok(!banner.text.includes("Failed to fetch"));
  assert.equal(offlineBanner({ offline: false }), null);
});

test("Onboarding.jsx routes every loader rejection through noteFetchFailure", () => {
  assert.match(onboardingSrc, /if \(!noteFetchFailure\(e\)\) setErr\(e\.message\)/);
  // No bare setErr(e.message) survives outside that one guarded line.
  const bareSetErr = [...onboardingSrc.matchAll(/\.catch\([^)]*=>\s*setErr\(e\.message\)\)/g)];
  assert.equal(bareSetErr.length, 0, "no .catch may call setErr(e.message) without the classifier");
  // No bare setIntegrations([]) fallback either — only the guarded form.
  assert.doesNotMatch(onboardingSrc, /\.catch\(\(\) => setIntegrations\(\[\]\)\)/);
  assert.match(onboardingSrc, /if \(!noteFetchFailure\(e\)\) setIntegrations\(\[\]\)/);
});

test("PathInput reports a network-level rejection to the wizard instead of dying silently", () => {
  // The autocomplete effect must never leave `suggestPaths(value)` as a bare
  // unhandled await — a network-level rejection has to route through the
  // `onNetworkError` prop (noteFetchFailure), not vanish as an unhandled
  // promise rejection.
  assert.match(onboardingSrc, /function PathInput\(\{[^}]*onNetworkError[^}]*\}\)/);
  assert.match(onboardingSrc, /try \{\s*const res = await suggestPaths\(value\);/);
  assert.match(onboardingSrc, /if \(live && !onNetworkError\?\.\(e\)\) setOpts\(\[\]\)/);
  // And the wizard must actually wire noteFetchFailure into it, not just
  // declare the prop.
  assert.match(onboardingSrc, /<PathInput value=\{root\}[\s\S]{0,400}?onNetworkError=\{noteFetchFailure\}/);
});

test("Generate wiki reports a network-level rejection to the wizard, not the raw exception text", () => {
  const wikiCatch = onboardingSrc.match(
    /const res = await generateDocs\(rp\);[\s\S]{0,800}?\}\s*\}\}>Generate wiki<\/button>/,
  )?.[0];
  assert.ok(wikiCatch, "Generate wiki's onClick handler must be present");
  assert.match(wikiCatch, /if \(!noteFetchFailure\(e\)\) \{/);
  // A network failure must not fall through to the bare, unguarded job-error write.
  assert.doesNotMatch(wikiCatch, /catch \(e\) \{\s*setWikiJobs\(\(s\) => nextJobState\(s, rp, \{ status: "failed", error: e\.message \}\)\);\s*\}/);
});

test("an HTTP failure is a step error, not an outage", () => {
  assert.equal(isNetworkError(new Error("GET /api/integrations/setup → 500")), false);
  const msg = detailMessage(
    { detail: [{ loc: ["body", "repo_path"], msg: "Field required" }] },
    "POST /api/x → 422",
  );
  assert.equal(isNetworkError(new Error(msg)), false);
  // The per-step error surface is untouched by this change.
  assert.match(onboardingSrc, /\{err && <div className="ob-error">\{err\}<\/div>\}/);
});

test("the probe retries every 3s and re-runs the step's fetches once it answers", async () => {
  const clock = fakeClock();
  let attempts = 0;
  let reconnected = 0;
  const delays = [];
  const p = createServerProbe({
    probe: () => {
      attempts += 1;
      return attempts <= 3 ? Promise.reject(new Error("down")) : Promise.resolve();
    },
    onReconnect: () => { reconnected += 1; },
    onStatus: () => {},
    setTimeout: (fn, ms) => { delays.push(ms); return clock.setTimeout(fn, ms); },
    clearTimeout: clock.clearTimeout,
  });
  p.start();

  for (let i = 0; i < 3; i++) {
    assert.equal(reconnected, 0, `must not reconnect before the 4th tick (i=${i})`);
    clock.advance(PROBE_INTERVAL_MS);
    await Promise.resolve();
    await Promise.resolve();
  }
  assert.equal(attempts, 3, "3 attempts must have rejected so far");
  assert.equal(reconnected, 0, "still not reconnected after the 3rd (rejecting) attempt");

  clock.advance(PROBE_INTERVAL_MS); // the 4th tick — the mock probe now resolves
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(attempts, 4);
  assert.equal(reconnected, 1, "onReconnect must fire exactly once after the probe answers");
  assert.equal(clock.pendingCount(), 0, "no timer must remain queued once reconnected");
  // Fixed cadence, not escalating: every scheduled delay is the same constant.
  for (const d of delays) assert.equal(d, PROBE_INTERVAL_MS);
});

test("Retry probes immediately and never stacks in-flight probes", async () => {
  const clock = fakeClock();
  let calls = 0;
  let resolveProbe;
  const p = createServerProbe({
    probe: () => { calls += 1; return new Promise((res) => { resolveProbe = res; }); },
    onReconnect: () => {},
    onStatus: () => {},
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  p.start();
  assert.equal(calls, 0, "start() must not probe before the first interval elapses");
  p.retryNow();
  assert.equal(calls, 1, "retryNow() before the tick issues exactly one probe");
  p.retryNow();
  assert.equal(calls, 1, "a second retryNow() while in flight issues none");
  resolveProbe();
  await Promise.resolve();
  p.stop();
});

test("stop() drops a late-resolving probe", async () => {
  const clock = fakeClock();
  let reconnected = 0;
  let resolveProbe;
  const p = createServerProbe({
    probe: () => new Promise((res) => { resolveProbe = res; }),
    onReconnect: () => { reconnected += 1; },
    onStatus: () => {},
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  p.start();
  p.retryNow();
  p.stop();
  resolveProbe();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(reconnected, 0, "a probe that resolves after stop() must never call onReconnect");
});

test("each rejection advances the fake clock by exactly PROBE_INTERVAL_MS (fixed interval, not escalating)", async () => {
  const clock = fakeClock();
  const delays = [];
  let attempts = 0;
  const p = createServerProbe({
    probe: () => { attempts += 1; return Promise.reject(new Error("down")); },
    onReconnect: () => {},
    onStatus: () => {},
    setTimeout: (fn, ms) => { delays.push(ms); return clock.setTimeout(fn, ms); },
    clearTimeout: clock.clearTimeout,
  });
  p.start();
  for (let i = 0; i < 4; i++) {
    clock.advance(PROBE_INTERVAL_MS);
    await Promise.resolve();
    await Promise.resolve();
  }
  assert.ok(attempts >= 4);
  for (const d of delays) assert.equal(d, PROBE_INTERVAL_MS);
  p.stop();
});

test("the banner is wizard-level, has a Retry control, and uses role=status not alert(", () => {
  assert.match(onboardingSrc, /offlineBanner\(\{ offline, probing \}\)/);
  assert.match(onboardingSrc, /className="ob-offline-retry"/);
  assert.match(onboardingSrc, /role=\{obBanner\.role\}/);
  assert.doesNotMatch(onboardingSrc, /alert\(/);
});

test("the reconnect re-runs the current step's loaders", () => {
  // All three loader effects' dep arrays must include reloadNonce, so a
  // reconnect re-fetches the current step's data instead of leaving it stale.
  const nonceDeps = [...onboardingSrc.matchAll(/\}, \[[^\]]*reloadNonce[^\]]*\]\);/g)];
  assert.equal(nonceDeps.length, 3, `expected 3 effects depending on reloadNonce, saw ${nonceDeps.length}`);
});

test("the offline banner styles exist and are themed", () => {
  const raw = stylesCss.replace(/\/\*[\s\S]*?\*\//g, "");
  const bannerBlocks = [...raw.matchAll(/\.ob-offline-banner[a-zA-Z-]*\s*\{([^}]*)\}/g)];
  assert.ok(bannerBlocks.length > 0, ".ob-offline-banner rule must exist");

  const definedInCss = new Set([...raw.matchAll(/(--[a-zA-Z0-9-]+)\s*:/g)].map((m) => m[1]));
  const lightBlock = raw.match(/\[data-theme="light"\]\s*\{([^}]*)\}/)?.[1];
  assert.ok(lightBlock, 'the [data-theme="light"] rule must exist');

  const readVars = new Set();
  for (const [, body] of bannerBlocks) {
    for (const m of body.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)\s*[,)]/g)) readVars.add(m[1]);
  }
  assert.ok(readVars.size > 0, "the banner rules must read at least one CSS var");
  for (const v of readVars) {
    assert.ok(definedInCss.has(v), `${v} is read by .ob-offline-banner but never defined`);
    assert.ok(lightBlock.includes(`${v}:`), `${v} is read by .ob-offline-banner but not overridden for the light theme`);
  }
});
