// UpdatesPanel must seed from a retained fact, not just from the live push.
//
// Before this file, Settings' UpdatesPanel subscribed with a bare
// `desktop?.onUpdate?.(...)` and never pulled `getLastUpdate()`. Interleaving:
// the startup check pushes "available" BEFORE the board mounts, the board
// pulls the retained fact and shows the notice, the operator clicks "See
// details" — and Settings, having never pulled, renders the idle "Updates are
// checked once a day" card instead of the update the operator just clicked
// through to see. subscribeUpdates() (updateNotice.js) is the fix, shared by
// both surfaces; this file tests it directly (no DOM renderer needed — it
// takes a plain `setUpdate` function) and then checks lexically that
// Settings.jsx actually wires it in.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { subscribeUpdates } from "./updateNotice.js";

/** A tiny `useState`-alike: applies a value OR a functional updater against a
 *  box, exactly like React does, so subscribeUpdates can be driven without a
 *  renderer. */
function stateBox(initial = null) {
  let value = initial;
  const history = [];
  const setUpdate = (next) => {
    value = typeof next === "function" ? next(value) : next;
    history.push(value);
  };
  return { get: () => value, setUpdate, history };
}

test("a payload retained before mount is written into state", async () => {
  const box = stateBox(null);
  const payload = { mode: "available", latest: "9.9.1" };
  const desktop = {
    onUpdate: () => () => {},
    getLastUpdate: async () => payload,
  };
  subscribeUpdates({ desktop, setUpdate: box.setUpdate });
  // getLastUpdate() resolves on a microtask; let it settle.
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(box.get(), payload,
    "mutation: the getLastUpdate() pull was removed, or its result was discarded " +
    "(e.g. `.then(() => {})`) instead of written into state");
});

test("a live push already delivered wins over the later retained pull", async () => {
  const box = stateBox(null);
  const pushed = { mode: "failed", error: "x" };
  const retained = { mode: "available", latest: "9.9.1" };
  let deliverPush;
  const desktop = {
    onUpdate: (cb) => { deliverPush = cb; return () => {}; },
    // Resolves AFTER the push below fires: the push call is synchronous in
    // this test, and this promise only settles on a later macrotask turn
    // (setTimeout), so the push is observably first regardless of ordering.
    getLastUpdate: () => new Promise((resolve) => {
      setTimeout(() => resolve(retained), 0);
    }),
  };
  subscribeUpdates({ desktop, setUpdate: box.setUpdate });
  deliverPush(pushed);
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(box.get(), pushed,
    "mutation: the seed uses `setUpdate(payload)` instead of the functional " +
    "`setUpdate((cur) => cur ?? payload)` form, so a live push that already " +
    "landed gets clobbered by the later-resolving retained pull");
});

test("a retained up-to-date must not seed a \"Checked just now\" card", async () => {
  const box = stateBox(null);
  const desktop = {
    onUpdate: () => () => {},
    getLastUpdate: async () => ({ mode: "up-to-date" }),
  };
  subscribeUpdates({ desktop, setUpdate: box.setUpdate });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(box.get(), null,
    "mutation: subscribeUpdates seeds ANY retained mode (including up-to-date) " +
    "instead of only available/unavailable — an up-to-date fact retained hours " +
    "ago would render updateNotice()'s stale \"Checked just now.\" line");
});

test("a null retained fact leaves the panel idle", async () => {
  const box = stateBox(null);
  const desktop = { onUpdate: () => () => {}, getLastUpdate: async () => null };
  subscribeUpdates({ desktop, setUpdate: box.setUpdate });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(box.get(), null, "a null retained fact must not fabricate a payload");
});

test("a rejected getLastUpdate is swallowed", async () => {
  const box = stateBox(null);
  const desktop = { onUpdate: () => () => {}, getLastUpdate: async () => { throw new Error("boom"); } };
  assert.doesNotThrow(() => subscribeUpdates({ desktop, setUpdate: box.setUpdate }));
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(box.get(), null, "a rejected pull must not throw or set an error into update state");
});

test("the returned value unsubscribes the push", () => {
  let offCalled = false;
  const off = () => { offCalled = true; };
  const desktop = { onUpdate: () => off, getLastUpdate: async () => null };
  const returned = subscribeUpdates({ desktop, setUpdate: () => {} });
  assert.equal(returned, off, "subscribeUpdates must return onUpdate's unsubscribe");
  returned();
  assert.ok(offCalled);
});

const here = fileURLToPath(new URL(".", import.meta.url));
const settingsJsx = readFileSync(here + "Settings.jsx", "utf8");

test("Settings' UpdatesPanel wires the shared push+pull subscription", () => {
  const panelStart = settingsJsx.indexOf("function UpdatesPanel(");
  assert.ok(panelStart >= 0, "UpdatesPanel not found in Settings.jsx");
  const panelEnd = settingsJsx.indexOf("\nfunction ", panelStart + 1);
  const panelBody = settingsJsx.slice(panelStart, panelEnd > 0 ? panelEnd : undefined);
  assert.match(panelBody, /subscribeUpdates\(\{\s*desktop,\s*setUpdate\s*\}\)/,
    "mutation: UpdatesPanel no longer calls subscribeUpdates({ desktop, setUpdate }) — " +
    "it must not seed from getLastUpdate() only implicitly");
  assert.doesNotMatch(panelBody, /desktop\?\.onUpdate\?\.\(\(payload\)\s*=>\s*setUpdate\(payload\)\)/,
    "the panel must not go back to subscribing with a bare onUpdate that never pulls getLastUpdate()");
});
