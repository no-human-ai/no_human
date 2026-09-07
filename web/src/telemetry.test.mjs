import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { initTelemetry, telemetryConsent, captureScreen, _resetForTests } from "./telemetry.js";

const SRC = dirname(fileURLToPath(import.meta.url));
const WEB = join(SRC, "..");
const REPO_ROOT = join(WEB, "..");
const read = (p) => readFileSync(join(SRC, p), "utf8");

function fakePosthogModule() {
  const calls = { init: [], register: [], capture: [], identify: [] };
  return {
    calls,
    module: {
      default: {
        init: (...a) => calls.init.push(a),
        register: (...a) => calls.register.push(a),
        capture: (...a) => calls.capture.push(a),
        identify: (...a) => calls.identify.push(a),
      },
    },
  };
}

// ── consent gate: no consent means posthog-js is NEVER imported ──────────────

test("no consent → importer never called, init returns null", async () => {
  _resetForTests();
  const imported = [];
  const importer = async (m) => { imported.push(m); return fakePosthogModule().module; };
  for (const cfg of [
    undefined,
    null,
    {},
    { telemetry: null },
    { telemetry: { enabled: false, posthog_publishable: "phc_x" } },
    { telemetry: { enabled: true } }, // consented but no client token
  ]) {
    assert.equal(await initTelemetry(cfg, { importer }), null);
  }
  assert.deepEqual(imported, []);
});

test("telemetryConsent needs enabled AND a client token", () => {
  assert.equal(telemetryConsent({ telemetry: { enabled: true } }), null);
  assert.equal(telemetryConsent({ telemetry: { posthog_publishable: "phc_x" } }), null);
  assert.deepEqual(
    telemetryConsent({ telemetry: { enabled: true, posthog_publishable: "phc_x", posthog_host: "https://us.i.posthog.com" } }),
    { key: "phc_x", host: "https://us.i.posthog.com", instanceId: "" },
  );
  assert.deepEqual(
    telemetryConsent({ telemetry: { enabled: true, posthog_publishable: "phc_x", posthog_host: "https://us.i.posthog.com", instance_id: "inst-uuid" } }),
    { key: "phc_x", host: "https://us.i.posthog.com", instanceId: "inst-uuid" },
  );
});

// ── consented init: exact posthog options (the privacy-load-bearing ones) ────

test("consent → posthog-js imported once, init gets the exact masking options", async () => {
  _resetForTests();
  const fake = fakePosthogModule();
  const imported = [];
  const importer = async (m) => { imported.push(m); return fake.module; };
  const cfg = {
    telemetry: {
      enabled: true, posthog_publishable: "phc_x",
      posthog_host: "https://us.i.posthog.com", instance_id: "inst-uuid",
    },
  };
  const client = await initTelemetry(cfg, { importer });
  assert.ok(client);
  assert.deepEqual(imported, ["posthog-js"]);
  assert.equal(fake.calls.init.length, 1);
  const [key, options] = fake.calls.init[0];
  assert.equal(key, "phc_x");
  assert.deepEqual(options, {
    api_host: "https://us.i.posthog.com",
    defaults: "2026-05-30",
    internal_or_test_user_hostname: null,
    autocapture: true,
    capture_pageview: true,
    capture_pageleave: true,
    capture_dead_clicks: true,
    capture_heatmaps: true,
    capture_performance: true,
    capture_exceptions: true,
    session_recording: { maskAllInputs: true, recordHeaders: true, recordBody: true },
    person_profiles: "always",
    bootstrap: { distinctID: "inst-uuid" },
  });
  // The two guarantees that survive the "everything on" posture:
  assert.equal(options.session_recording.maskAllInputs, true, "typed input is always masked");
  assert.equal(options.session_recording.maskTextSelector, undefined,
    "maskTextSelector matches zero elements in this UI and must not be configured");
  assert.equal(options.person_profiles, "always", "one person per install id");
  assert.equal(options.internal_or_test_user_hostname, null,
    "the board serves on 127.0.0.1 — posthog defaults would flag every real install $internal_or_test_user");

  // app_version + the installation id registered on every event.
  assert.equal(fake.calls.register.length, 1);
  const [registered] = fake.calls.register[0];
  assert.equal(typeof registered.app_version, "string");
  assert.ok(registered.app_version.length > 0);
  assert.equal(registered.instance_id, "inst-uuid");

  // the board never calls identify(): the bootstrap distinctID IS the person key.
  assert.deepEqual(fake.calls.identify, []);

  // screen views carry the lane NAME only
  captureScreen("board");
  assert.deepEqual(fake.calls.capture, [["screen_viewed", { screen: "board" }]]);
  _resetForTests();
});

test("no installation id in config → registered without a fabricated one", async () => {
  _resetForTests();
  const fake = fakePosthogModule();
  const importer = async () => fake.module;
  const cfg = {
    telemetry: { enabled: true, posthog_publishable: "phc_x", posthog_host: "https://us.i.posthog.com" },
  };
  const client = await initTelemetry(cfg, { importer });
  assert.ok(client);
  const [key, options] = fake.calls.init[0];
  assert.equal(key, "phc_x");
  assert.equal(options.bootstrap, undefined);
  const [registered] = fake.calls.register[0];
  assert.ok(!("instance_id" in registered), "no instance_id key when config has none");
  assert.equal(typeof registered.app_version, "string");
  assert.deepEqual(fake.calls.identify, []);
  _resetForTests();
});

test("captureScreen before init is a silent no-op", () => {
  _resetForTests();
  assert.doesNotThrow(() => captureScreen("stats"));
});

// ── no masking option may point at a selector that matches nothing ───────────
// A `*Selector`-named init option (top level or inside session_recording) that
// matches zero elements reads as protection and provides none — worse than no
// selector at all, since it implies coverage the config does not deliver.

async function unmatchedSelectors(initOptions) {
  const haystacks = [
    ...readdirSync(SRC).filter((f) => f.endsWith(".jsx")).map(read),
    readFileSync(join(WEB, "index.html"), "utf8"),
  ].join("\n");
  const selectorKeys = [];
  for (const [k, v] of Object.entries(initOptions)) {
    if (/Selector$/i.test(k) && typeof v === "string") selectorKeys.push(v);
    if (v && typeof v === "object") {
      for (const [k2, v2] of Object.entries(v)) {
        if (/Selector$/i.test(k2) && typeof v2 === "string") selectorKeys.push(v2);
      }
    }
  }
  const unmatched = [];
  for (const sel of selectorKeys) {
    const token = sel.startsWith(".") || sel.startsWith("#") ? sel.slice(1) : sel;
    if (!haystacks.includes(token)) unmatched.push(sel);
  }
  return unmatched;
}

test("no configured masking selector matches zero elements (positive control on the actual init)", async () => {
  _resetForTests();
  const fake = fakePosthogModule();
  const importer = async () => fake.module;
  const cfg = {
    telemetry: { enabled: true, posthog_publishable: "phc_x", posthog_host: "https://us.i.posthog.com" },
  };
  await initTelemetry(cfg, { importer });
  const [, options] = fake.calls.init[0];
  assert.deepEqual(await unmatchedSelectors(options), []);
  _resetForTests();
});

test("non-vacuity control: the helper DOES flag a selector that matches nothing", async () => {
  const unmatched = await unmatchedSelectors({ session_recording: { maskTextSelector: ".ph-mask" } });
  assert.deepEqual(unmatched, [".ph-mask"]);
});

// ── disclosure sweep: every capture() event kind must be published ───────────
// Prevents a new browser event from shipping undisclosed: whatever this file
// (or any other web/src file) calls posthog.capture(...) with must appear in
// the published event list in docs/configuration.md.

function walkSrcFiles(dir = SRC, prefix = "") {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const rel = prefix ? `${prefix}/${name}` : name;
    if (statSync(full).isDirectory()) out.push(...walkSrcFiles(full, rel));
    else out.push(rel);
  }
  return out;
}

function discoveredCaptureEvents() {
  const found = new Set();
  const CAPTURE_CALL = /\.capture\(\s*["']([\w.$-]+)["']/g;
  for (const rel of walkSrcFiles()) {
    if (!/\.(jsx?|mjs)$/.test(rel) || rel.endsWith(".test.mjs")) continue;
    const src = readFileSync(join(SRC, rel), "utf8");
    let m;
    while ((m = CAPTURE_CALL.exec(src))) found.add(m[1]);
  }
  return found;
}

test("every posthog.capture() event kind is documented in docs/configuration.md", () => {
  const events = discoveredCaptureEvents();
  assert.ok(events.size > 0, "the capture() sweep found nothing — regex or directory is broken");
  assert.ok(events.has("screen_viewed"), "sweep must find the browser screen_viewed event");
  const docs = readFileSync(join(REPO_ROOT, "docs", "configuration.md"), "utf8");
  const undisclosed = [...events].filter((name) => !docs.includes(name));
  assert.deepEqual(undisclosed, [],
    `capture() events not listed in docs/configuration.md: ${undisclosed}`);
});

// ── masking presence: the enumerated anchors carry ph-no-capture ─────────────
// A source-grep test: cheap, honest, and it fails the moment a refactor drops
// a masking class from one of the elements that render operator content.

test("SlideOver masks title, spec, diff and activity log", () => {
  const src = read("SlideOver.jsx");
  for (const anchor of [
    'className="so-title ph-no-capture"',
    'className="so-description ph-no-capture"',
    'className="so-criteria ph-no-capture"',
    'className="so-failure-reason ph-no-capture"',
    'className="so-diff-wrap ph-no-capture"',
  ]) {
    assert.ok(src.includes(anchor), `SlideOver.jsx missing masked anchor: ${anchor}`);
  }
  const feeds = src.match(/className="activity-feed ph-no-capture"/g) || [];
  // Three now: the primary digest (ActivityTab) plus the demoted raw-log
  // component's (ActivityLog) empty-state and main containers — all masked.
  assert.equal(feeds.length, 3, "every activity-feed container must be masked");
});

test("Backlog masks the ticket title/assignee block", () => {
  const src = read("Backlog.jsx");
  assert.ok(src.includes('className="flex min-w-0 flex-col gap-1 ph-no-capture"'));
});

test("TaskComposer masks the prompt surface, PR URL and repo row", () => {
  const src = read("TaskComposer.jsx");
  assert.ok(src.includes('sm:p-5 ph-no-capture"'), "prompt surface container");
  assert.ok(src.includes('className="mt-3 ph-no-capture"'), "PR URL container");
  assert.ok(src.includes('gap-2 ph-no-capture"'), "repository row container");
});

test("Board, TaskTable, Stats, App grill and Settings memory are masked", () => {
  assert.ok(read("Board.jsx").includes('"task-card ph-no-capture"'),
    "the whole board card must be masked");
  assert.ok(read("TaskTable.jsx").includes('<tbody className="ph-no-capture">'),
    "the whole table body must be masked");
  const stats = read("Stats.jsx");
  for (const id of ["cost-by-project", "repo-understanding", "session-search"]) {
    assert.ok(
      stats.includes(`className="stats-section ph-no-capture" data-testid="${id}"`),
      `Stats section ${id} must be masked`);
  }
  assert.ok(read("App.jsx").includes('className="grill-spec ph-no-capture"'),
    "the refined-spec block must be masked");
  const settings = read("Settings.jsx");
  assert.ok(settings.includes('className="memory-card ph-no-capture"'));
  assert.ok(settings.includes("memory-card retire-candidate-card ph-no-capture"));
  assert.ok(settings.includes("memory-card learning-card ph-no-capture"));
});

// ── DISCOVERY, not enumeration ───────────────────────────────────────────────
// The 2026-08-16 independent review found Board/TaskTable/Stats leaking task
// titles, repo names and PR URLs while the enumerated anchors above stayed
// green — a guard that enumerates cannot catch the surface nobody listed.
// This sweep DISCOVERS: any component file that renders a known operator-
// content field must carry a ph-no-capture (or ph-mask) annotation, and any
// NEW file that starts rendering one of these fields fails here until it is
// masked (or consciously added to the audited exemptions).

const CONTENT_FIELD_RENDER =
  /\{[a-zA-Z_$][\w$.]*\.(title|task_title|repo_name|pr_url|description|description_short|live_status|blocker_question|failure_reason|snippet|content|path|question|answer|error|test_cmd|summary|query|command|repo|name)\b/;

const SWEEP_EXEMPT = new Set(["telemetry.test.mjs"]);

test("every component rendering operator-content fields carries a mask", async () => {
  const { readdirSync } = await import("node:fs");
  const offenders = [];
  for (const f of readdirSync(SRC)) {
    if (!f.endsWith(".jsx") || SWEEP_EXEMPT.has(f)) continue;
    const src = read(f);
    if (CONTENT_FIELD_RENDER.test(src) &&
        !src.includes("ph-no-capture") && !src.includes("ph-mask")) {
      offenders.push(f);
    }
  }
  assert.deepEqual(offenders, [],
    `files render operator-content fields with no masking annotation: ${offenders}`);
});
