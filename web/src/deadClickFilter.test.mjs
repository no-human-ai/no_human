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
import { DEAD_CLICK_IGNORE_SELECTORS, isDeadClickIgnored } from "./deadClickFilter.js";

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
