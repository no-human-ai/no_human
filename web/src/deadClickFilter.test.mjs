// PostHog triage 2026-09-09: 26 of ~40 real-user dead clicks in the prior 3
// days were <select class="new-task-select"> in Settings → Models, 4 more on
// <input> — opening a native dropdown or focusing a field mutates nothing, so
// posthog-js's heuristic flagged every one as a dead click and buried the
// real dead controls. These tests pin the executable spec for the fix:
// DEAD_CLICK_IGNORE_SELECTORS (handed to posthog-js) and isDeadClickIgnored
// (the same rule as a pure, testable predicate), in the fake-element style of
// keepFocusInDialog.test.mjs.
import test from "node:test";
import assert from "node:assert/strict";
import {
  DEAD_CLICK_IGNORE_SELECTORS,
  isDeadClickIgnored,
  deadClickBeforeSend,
} from "./deadClickFilter.js";

function el(tag, opts = {}) {
  const attrs = opts.attributes || {};
  const node = {
    tagName: tag.toUpperCase(),
    parentElement: opts.parentElement ?? null,
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    querySelector: () => opts.wraps ?? null,
  };
  if ("control" in opts) node.control = opts.control;
  if ("htmlFor" in opts) node.htmlFor = opts.htmlFor;
  return node;
}

// ── AC 1: native form controls are silenced ─────────────────────────────────

test("native form controls are not dead clicks", () => {
  // The exact triage case: <select class="new-task-select">.
  assert.equal(isDeadClickIgnored(el("select", { attributes: { class: "new-task-select" } })), true);
  assert.equal(isDeadClickIgnored(el("input")), true);
  assert.equal(isDeadClickIgnored(el("input", { attributes: { type: "checkbox" } })), true);
  assert.equal(isDeadClickIgnored(el("input", { attributes: { type: "radio" } })), true);
  assert.equal(isDeadClickIgnored(el("textarea")), true);
  assert.equal(isDeadClickIgnored(el("option")), true);
});

test("real dead controls are still captured", () => {
  assert.equal(isDeadClickIgnored(el("button")), false);
  assert.equal(isDeadClickIgnored(el("a")), false);
  assert.equal(isDeadClickIgnored(el("div", { attributes: { role: "button" } })), false);
  assert.equal(isDeadClickIgnored(el("div")), false);
  assert.equal(isDeadClickIgnored(el("span")), false);
});

test("a label only counts when it labels something", () => {
  // Associated via the DOM's own `control` resolution.
  assert.equal(isDeadClickIgnored(el("label", { control: el("input") })), true);
  // A dangling `for` (points at a missing id, `control` resolves to null):
  // the mere presence of the `for` attribute is enough per the intake Q&A.
  assert.equal(
    isDeadClickIgnored(el("label", { attributes: { for: "missing-id" }, control: null })),
    true,
  );
  // Wraps an <input> — no `for`, no `control` property on the fake.
  assert.equal(isDeadClickIgnored(el("label", { wraps: el("input") })), true);
  // A bare label: no `for`, no `control`, wraps nothing — a heading wearing a
  // <label> tag, not a control.
  assert.equal(isDeadClickIgnored(el("label")), false);
});

test("the nearest interactive ancestor decides", () => {
  const svgInButton = el("svg", { parentElement: el("button") });
  assert.equal(isDeadClickIgnored(svgInButton), false);
  const spanInButton = el("span", { parentElement: el("button") });
  assert.equal(isDeadClickIgnored(spanInButton), false);

  const spanInSelect = el("span", { parentElement: el("select") });
  assert.equal(isDeadClickIgnored(spanInSelect), true);

  // Divergence from the flat CSS list, which has no early exit: a <button>
  // that IS the click target wins over an associated <label> ancestor.
  const buttonInLabel = el("button", {
    parentElement: el("label", { control: el("input") }),
  });
  assert.equal(isDeadClickIgnored(buttonInLabel), false);
});

test("deep and malformed trees are safe", () => {
  let leaf = el("div", { parentElement: null });
  for (let i = 1; i < 500; i++) {
    leaf = el("div", { parentElement: leaf });
  }
  assert.equal(isDeadClickIgnored(leaf), false);

  const cyclic = el("div");
  cyclic.parentElement = cyclic;
  assert.equal(isDeadClickIgnored(cyclic), false);

  assert.doesNotThrow(() => isDeadClickIgnored(null));
  assert.doesNotThrow(() => isDeadClickIgnored(undefined));
  assert.doesNotThrow(() => isDeadClickIgnored({}));
  assert.equal(isDeadClickIgnored(null), false);
  assert.equal(isDeadClickIgnored(undefined), false);
  assert.equal(isDeadClickIgnored({}), false);
});

test("the selector list and the predicate agree", () => {
  for (const sel of ["select", "input", "textarea", "option", "label[for]", "label:has(input, select, textarea)"]) {
    assert.ok(DEAD_CLICK_IGNORE_SELECTORS.includes(sel), `missing selector: ${sel}`);
  }
  for (const sel of ["button", "a", '[role="button"]']) {
    assert.ok(!DEAD_CLICK_IGNORE_SELECTORS.includes(sel), `unexpectedly ignoring: ${sel}`);
  }
});

// ── dead-click ordering-race filter ─────────────────────────────────────────
// Measured 2026-09-09 in real Chromium with posthog-js 1.417.1 and replay on:
// button dead clicks the ignorelist above deliberately leaves alone are the
// module's own mutation-vs-click stamp ordering race, not real dead controls.
// deadClickBeforeSend (wired as posthog's before_send in telemetry.js) drops
// exactly that artefact and nothing else. See deadClickFilter.js's header.

const EVENT_TS = 1_700_000_000_000;

function deadClick(props) {
  return { event: "$dead_click", properties: { ...props }, timestamp: "2026-09-09T00:00:00.000Z" };
}

test("drops the ordering-race artefact at gaps of 0, 3 and 12 ms", () => {
  for (const gap of [0, 3, 12]) {
    const ev = deadClick({
      $dead_click_event_timestamp: EVENT_TS,
      $dead_click_last_mutation_timestamp: EVENT_TS - gap,
    });
    assert.equal(deadClickBeforeSend(ev), null, `gap ${gap}ms should be dropped`);
  }
});

test("keeps a genuine mutation timeout (3600 ms gap)", () => {
  const ev = deadClick({
    $dead_click_event_timestamp: EVENT_TS,
    $dead_click_last_mutation_timestamp: EVENT_TS - 3600,
  });
  assert.equal(deadClickBeforeSend(ev), ev);
});

test("keeps a dead click with no last-mutation stamp", () => {
  const missing = deadClick({ $dead_click_event_timestamp: EVENT_TS });
  assert.equal(deadClickBeforeSend(missing), missing);

  const undef = deadClick({
    $dead_click_event_timestamp: EVENT_TS,
    $dead_click_last_mutation_timestamp: undefined,
  });
  assert.equal(deadClickBeforeSend(undef), undef);
});

test("keeps a dead click that carries a mutation delay", () => {
  const withDelay = deadClick({
    $dead_click_event_timestamp: EVENT_TS,
    $dead_click_last_mutation_timestamp: EVENT_TS,
    $dead_click_mutation_delay_ms: 2590,
  });
  assert.equal(deadClickBeforeSend(withDelay), withDelay);

  // A present-but-zero delay is still "present" — only an ABSENT delay
  // (== null) is eligible for the race-window drop.
  const zeroDelay = deadClick({
    $dead_click_event_timestamp: EVENT_TS,
    $dead_click_last_mutation_timestamp: EVENT_TS,
    $dead_click_mutation_delay_ms: 0,
  });
  assert.equal(deadClickBeforeSend(zeroDelay), zeroDelay);
});

test("keeps a negative gap, and respects the [0, 100] ms boundary", () => {
  const negative = deadClick({
    $dead_click_event_timestamp: EVENT_TS,
    $dead_click_last_mutation_timestamp: EVENT_TS + 5, // last mutation AFTER the event stamp
  });
  assert.equal(deadClickBeforeSend(negative), negative);

  const at101 = deadClick({
    $dead_click_event_timestamp: EVENT_TS,
    $dead_click_last_mutation_timestamp: EVENT_TS - 101,
  });
  assert.equal(deadClickBeforeSend(at101), at101);

  const at100 = deadClick({
    $dead_click_event_timestamp: EVENT_TS,
    $dead_click_last_mutation_timestamp: EVENT_TS - 100,
  });
  assert.equal(deadClickBeforeSend(at100), null);
});

test("leaves non-dead-click events untouched", () => {
  const autocapture = { event: "$autocapture", properties: {} };
  assert.equal(deadClickBeforeSend(autocapture), autocapture);

  // Race-shaped properties under the WRONG event name must not match.
  const deadSwipe = {
    event: "$dead_swipe",
    properties: {
      $dead_swipe_event_timestamp: EVENT_TS,
      $dead_swipe_last_mutation_timestamp: EVENT_TS,
    },
  };
  assert.equal(deadClickBeforeSend(deadSwipe), deadSwipe);

  const heatmap = { event: "$$heatmap", properties: {} };
  assert.equal(deadClickBeforeSend(heatmap), heatmap);

  const screenViewed = { event: "screen_viewed", properties: { screen: "board" } };
  assert.equal(deadClickBeforeSend(screenViewed), screenViewed);

  const pageview = { event: "$pageview", properties: {} };
  assert.equal(deadClickBeforeSend(pageview), pageview);
});

test("is total: null/undefined/garbage in, same value out", () => {
  assert.equal(deadClickBeforeSend(null), null);
  assert.equal(deadClickBeforeSend(undefined), undefined);

  const empty = {};
  assert.equal(deadClickBeforeSend(empty), empty);

  const noProps = { event: "$dead_click" };
  assert.equal(deadClickBeforeSend(noProps), noProps);

  const throwsOnProperties = {
    event: "$dead_click",
    get properties() {
      throw new Error("boom");
    },
  };
  assert.doesNotThrow(() => deadClickBeforeSend(throwsOnProperties));
  assert.equal(deadClickBeforeSend(throwsOnProperties), throwsOnProperties);
});

test("does not mutate the event", () => {
  const ev = deadClick({
    $dead_click_event_timestamp: EVENT_TS,
    $dead_click_last_mutation_timestamp: EVENT_TS - 3600,
  });
  const before = JSON.parse(JSON.stringify(ev));
  deadClickBeforeSend(ev);
  assert.deepEqual(ev, before);
});
