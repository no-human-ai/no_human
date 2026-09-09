// Opt-OUT usage telemetry + masked session replay (PostHog), default ON.
//
// The privacy contract this file enforces, matching the published policy:
//  - CONSENT FIRST: without `telemetry.enabled` AND a publishable client
//    token in /api/config, initTelemetry returns before `posthog-js` is even
//    IMPORTED (dynamic import) — no consent means the module never loads,
//    no cookies, no recorder, zero bytes to PostHog.
//  - ALL CHANNELS ON: autocapture, `$pageview` / `$pageleave`, dead clicks,
//    heatmaps, performance and exceptions are all enabled (operator decision
//    2026-09-03). The app still sends its own `screen_viewed` event carrying
//    a lane NAME only, alongside these PostHog-internal channels. The
//    `$el_text` autocapture stamps on every click/change/submit — here that
//    would be task titles, repo names, file paths, PR text — is bounded by
//    the `ph-no-capture` blocks below, not by the channel being off. See the
//    published event list in docs/configuration.md. Dead-click capture stays
//    on. Native form controls (select/input/textarea/option/associated
//    label) are ON the ignorelist in ./deadClickFilter.js, so posthog never
//    even queues them as dead-click candidates — a 2026-09-09 PostHog triage
//    found opening a dropdown or focusing a field was flagged as a dead
//    click 26+4 out of ~40 times, burying real ones.
//    The button dead clicks the ignorelist deliberately leaves alone turned
//    out to be a second, unrelated problem: posthog-js 1.417.1's own
//    mutation-vs-click stamp ordering race flags a synchronously
//    re-rendering React button as dead (mechanism and measurements in
//    deadClickFilter.js's header comment). `before_send: deadClickBeforeSend`
//    below — posthog-js's own sanctioned init option — drops a `$dead_click`
//    client-side only when it carries no `$dead_click_mutation_delay_ms` and
//    its event-minus-last-mutation gap sits in [0, 100] ms; every other
//    event passes through unchanged. The window is a heuristic: a genuinely
//    dead control clicked within 100 ms of any unrelated page-wide mutation
//    is dropped as well (measured in the harness at gaps of 43 and 49 ms),
//    because the stamp posthog exposes is page-wide — most reachably after
//    a preceding interaction that re-renders. Genuine dead controls measured
//    1216-1420 ms in the harness and 2590/3191 ms live, so the trade is 12x
//    to 32x of margin against that false negative.
//    Not done, and deliberately so: no `*_threshold_ms` change (that would
//    move the detector's global sensitivity, not fix the ordering race) and
//    no `.ph-no-deadclick` on any button (that would silence the very
//    buttons the race is measured on).
//    Known gap: `capture_heatmaps: true` runs its OWN `DeadClicksAutocapture`
//    instance (posthog-js's heatmaps.ts) whose `__onCapture` feeds the
//    `$$heatmap` buffer directly, never `capture('$dead_click')` — so the
//    heatmaps dead-click layer is NOT filtered by `before_send` and still
//    carries this race. Not fixed here.
//  - MASKED REPLAY: recordings capture the app's OWN interface only. All
//    inputs are masked, and PostHog's own `.ph-no-capture` (whole-block) is
//    hand-applied to every element that renders operator content: task
//    titles, specs/descriptions, diffs, activity logs, the composer,
//    backlog ticket titles. Never your code. (No blanket text-mask selector
//    is configured: `maskTextSelector` would match zero elements in this UI,
//    so it is deliberately unset — pinned by web/src/telemetry.test.mjs.)
//    Replay network request/response headers and bodies ARE recorded and
//    are NOT masked — see docs/configuration.md.
//  - ONE IDENTIFIER: events are tagged with the same anonymous `instance_id`
//    as the server channel (registered below), not PostHog's own generated
//    device id. `person_profiles: "always"` keys one person per install id;
//    still no human identity, no `identify()` call.
import { fetchVersion } from "./api.js";
import { DEAD_CLICK_IGNORE_SELECTORS, deadClickBeforeSend } from "./deadClickFilter.js";

let posthog = null; // the initialized client, or null when not consented
let started = false;

export function telemetryConsent(cfg) {
  const t = cfg?.telemetry;
  if (!t?.enabled) return null;
  // `posthog_publishable` is the ONLY accepted field: it is named so
  // /api/config's secret-scrubber does not mask it. No *_token/*_key
  // fallbacks — those names ARE scrubbed to a "●●● set" literal by the
  // config echo, so a fallback would init PostHog with the mask string.
  const key = t.posthog_publishable;
  if (!key) return null;
  return { key, host: t.posthog_host, instanceId: String(t.instance_id || "") };
}

// `importer` is injectable so tests can prove "no consent => posthog-js never
// imported" without shipping a module loader mock.
export async function initTelemetry(cfg, { importer } = {}) {
  if (started) return posthog;
  const consent = telemetryConsent(cfg);
  if (!consent) return null; // returns BEFORE any posthog-js import
  started = true;
  try {
    // The literal specifier lets vite split posthog-js into a lazy chunk that
    // is only ever fetched on this consented path.
    const load = importer || (() => import("posthog-js"));
    const mod = await load("posthog-js");
    const client = mod.default ?? mod.posthog ?? mod;
    client.init(consent.key, {
      api_host: consent.host,
      defaults: "2026-05-30",
      // posthog-js's "2026-05-30" defaults turn on
      // internal_or_test_user_hostname=/^(localhost|127\.0\.0\.1)$/. This board
      // ALWAYS serves on 127.0.0.1, so every real install was being stamped
      // $internal_or_test_user=true and hidden by PostHog's internal-user
      // filter - disable the hostname heuristic entirely.
      internal_or_test_user_hostname: null,
      // Everything PostHog offers is on (operator, 2026-09-03). Operator
      // content is still kept out of autocapture and replay pixels by the
      // hand-applied `ph-no-capture` blocks (posthog-js skips any element
      // with a ph-no-capture ancestor) and by maskAllInputs. Replay network
      // bodies are NOT masked — docs/configuration.md says so.
      autocapture: true,
      capture_pageview: true,
      capture_pageleave: true,
      capture_dead_clicks: { css_selector_ignorelist: DEAD_CLICK_IGNORE_SELECTORS },
      // Drops the mutation-vs-click ordering-race artefact (see the header
      // comment above and deadClickFilter.js) client-side, before the event
      // is batched. Does not reach capture_heatmaps's own dead-click layer.
      before_send: deadClickBeforeSend,
      capture_heatmaps: true,
      capture_performance: true,
      capture_exceptions: true,
      session_recording: { maskAllInputs: true, recordHeaders: true, recordBody: true },
      person_profiles: "always",
      ...(consent.instanceId ? { bootstrap: { distinctID: consent.instanceId } } : {}),
    });
    // Stamp every event with the running app version (server's /api/version —
    // the same string `nh --version` prints). Best-effort: unknown stays unknown.
    let version = "unknown";
    try {
      version = (await fetchVersion())?.version || "unknown";
    } catch {
      /* best-effort */
    }
    client.register({
      app_version: version,
      ...(consent.instanceId ? { instance_id: consent.instanceId } : {}),
    });
    posthog = client;
    return posthog;
  } catch {
    return null; // telemetry must never break the board
  }
}

// Screen-view events: lane NAMES only (board/backlog/done/failed/stats/about),
// never content. A no-op until initTelemetry has run with consent.
export function captureScreen(screen) {
  try {
    posthog?.capture("screen_viewed", { screen });
  } catch {
    /* never break navigation */
  }
}

// Test seam: reset module state between node:test cases.
export function _resetForTests() {
  posthog = null;
  started = false;
}
