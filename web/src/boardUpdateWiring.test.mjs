// The board's own update wiring (App.jsx) has no DOM renderer in this suite,
// so these are lexical tests — each named for the ONE behaviour it protects,
// and each asserting the correct anchor is present (a positive control: the
// test cannot pass vacuously against a file that dropped the whole feature).
// App.jsx is otherwise read-only for this task (ec924d81's runtime behaviour
// must not change) — this file only reads it.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const appJsx = readFileSync(here + "App.jsx", "utf8");

/** Slice the source between two literal markers, failing loudly if either is
 *  missing — a missing START means the anchor itself was deleted (the
 *  positive control failing first, which is more informative than a
 *  downstream regex miss). */
function between(source, startMarker, endMarker, startFrom = 0) {
  const s = source.indexOf(startMarker, startFrom);
  assert.ok(s >= 0, `anchor not found: ${JSON.stringify(startMarker)}`);
  const e = source.indexOf(endMarker, s + startMarker.length);
  assert.ok(e >= 0, `end marker not found after anchor: ${JSON.stringify(endMarker)}`);
  return source.slice(s, e);
}

// The board's update-catch-up effect: `const off = d.onUpdate?.(...)` through
// its `getLastUpdate` pull and `return off;`.
const updateEffect = between(
  appJsx,
  "const d = window.nhDesktop;",
  "return off;",
);

test("the board pulls getLastUpdate on mount", () => {
  assert.match(updateEffect, /d\.getLastUpdate\?\.\(\)/,
    "mutation: the getLastUpdate() pull was dropped from the board's update effect — " +
    "a board that mounts after the startup check finished would show nothing");
});

test("the board writes the pulled payload into state, not just discards it", () => {
  const thenBody = between(updateEffect, ".then((payload) => {", "}).catch(");
  assert.match(thenBody, /setUpdate\(/,
    "mutation: the pulled payload is discarded (e.g. `.then(() => {})`) instead of written into state");
});

test("the pull uses the functional cur ?? payload form so a live push wins", () => {
  assert.match(updateEffect, /setUpdate\(\(cur\)\s*=>\s*cur\s*\?\?\s*payload\)/,
    "mutation: `setUpdate((cur) => cur ?? payload)` was replaced by `setUpdate(payload)` — " +
    "a live push that already landed would be clobbered by the later-resolving retained pull");
});

// onUpdateAction: from its declaration to the closing `};` of the arrow
// function (the "dismiss" branch is the last one before that close).
const onUpdateAction = between(
  appJsx,
  "const onUpdateAction = (action) => {",
  "\n  };",
);

test("the unavailable Dismiss is session-only and never persists a defer", () => {
  const dismissBlock = between(onUpdateAction, 'if (action === "dismiss") {', "\n    }");
  assert.match(dismissBlock, /setUpdateDismissed\(/, "positive control: the dismiss branch must exist and hide locally");
  assert.doesNotMatch(dismissBlock, /deferUpdate/,
    "mutation: deferUpdate was added to the unavailable build's Dismiss action — " +
    "an unsigned build has no persisted defer to reuse; Dismiss must stay session-only");
});

test("Later persists the defer through deferUpdate", () => {
  const laterBlock = between(onUpdateAction, 'if (action === "later") {', 'if (action === "downloads")');
  assert.match(laterBlock, /deferUpdate\?\.\(updateBar\.version\)/,
    "mutation: deferUpdate(updateBar.version) was removed from Later — " +
    "the board's own Later click would stop persisting the defer");
});

test("the board notice renders only on the board page", () => {
  assert.match(appJsx, /page === "board" && updateBar &&/,
    'mutation: the `page === "board" &&` gate was removed from the notice — ' +
    "it would then render outside the board page too");
});
