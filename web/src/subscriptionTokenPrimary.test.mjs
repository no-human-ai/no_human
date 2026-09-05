import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { cardFacts } from "./cardFacts.js";
import { chipsFor } from "./slideOverSummary.js";
import { derivePerShippedDisplay } from "./ledgerSpend.js";
import { taskBurn, taskCost } from "./cost.js";

// Completes 2b62483a (tokens-not-dollars): the Stats table and the sidebar
// spend tile already lead with tokens for a subscription user
// (pricingIsReal(authMode) / deriveSpendDisplay) — this closes the same gap
// on the board CARD, the task DRAWER, and the per-PR figure.
//
// Like sidebarNav.test.mjs and answerLane.test.mjs:133, no jsdom/React
// renderer is wired into this project's `node --test` harness (web/node_modules
// is not installed), so there is nothing to mount and no DOM to assert on
// directly. The render-path assertions below are made on the PURE
// DERIVATIONS that actually produce the rendered strings (cardFacts,
// chipsFor, derivePerShippedDisplay), PLUS static source-wiring assertions
// proving those derivations are fed `authMode` from the mounted component
// tree (App -> Board -> Lane -> TaskCard, and App -> Board -> SlideOver ->
// TaskSummary). This is a deliberate, documented deviation from "assert the
// DOM" — a browser-level DOM check belongs in web/e2e/.

// Fixture: total_tokens 500_000 + total_cache_read 2_000_000 = 2,500,000 tok
// (taskBurn), cost_usd 0.81 (taskCost) — same shape slideOverSummary.test.mjs
// already uses for its cost-chip fixture.
function makeTask() {
  return {
    title: "T",
    repo_name: "app",
    attempt_count: 2,
    total_tokens: 500_000,
    total_cache_read: 2_000_000,
    cost_usd: 0.81,
  };
}

test("board card leads with tokens for subscription and undefined authMode", () => {
  const task = makeTask();
  for (const authMode of ["subscription", undefined, null]) {
    const f = cardFacts(task, { cost: taskCost(task), tokens: taskBurn(task), authMode });
    assert.match(f.metaLine, /2\.50M tok/, `authMode=${authMode}`);
    assert.match(f.metaLine, /~\$0\.81 est\./, `authMode=${authMode}`);
    assert.doesNotMatch(f.metaLine, /·\s*\$0\.81/, `authMode=${authMode} must not show a naked dollar as primary`);
  }
});

test("board card keeps dollars for api_key — no regression", () => {
  const task = makeTask();
  const f = cardFacts(task, { cost: taskCost(task), tokens: taskBurn(task), authMode: "api_key" });
  assert.equal(f.metaLine, "app · att 2 · $0.81");
  assert.doesNotMatch(f.metaLine, /tok/);
  assert.doesNotMatch(f.metaLine, /est\./);
});

test("drawer cost chip leads with tokens for subscription/undefined", () => {
  const task = makeTask();
  for (const authMode of ["subscription", undefined]) {
    const chips = chipsFor(task, authMode);
    assert.equal(chips[0].label, "2.50M tok", `authMode=${authMode}`);
    assert.equal(chips[0].sub, "~$0.81 est.", `authMode=${authMode}`);
    // Only cost + attempts fire on this fixture (no wall_seconds/pr_url) — the
    // chip order among whichever keys ARE present must still follow the
    // canonical ["cost","time","attempts","pr"] sequence.
    const canonical = ["cost", "time", "attempts", "pr"];
    const keys = chips.map((c) => c.key);
    assert.deepEqual(keys, canonical.filter((k) => keys.includes(k)), `authMode=${authMode}`);
  }
});

test("drawer cost chip keeps dollars for api_key", () => {
  const task = makeTask();
  const chips = chipsFor(task, "api_key");
  assert.equal(chips[0].label, "$0.81");
  assert.match(chips[0].sub, /tok$/);
});

test("per-PR figure leads with tokens for subscription/undefined", () => {
  const sub = derivePerShippedDisplay(3, 2.43, 3_000_000, "subscription");
  assert.match(sub, /1\.00M tok\/PR/);
  assert.match(sub, /~\$0\.81 est\./);

  const apiKey = derivePerShippedDisplay(3, 2.43, 3_000_000, "api_key");
  assert.equal(apiKey, "(~$0.81/PR)");

  assert.equal(derivePerShippedDisplay(0, 2.43, 3_000_000, "subscription"), null);
  assert.equal(derivePerShippedDisplay(3, null, 3_000_000, "subscription"), null);
  assert.equal(derivePerShippedDisplay(3, 0, 3_000_000, "subscription"), null);
});

// Static source-shape assertions: no renderer is wired into `node --test`
// (see the header comment above), so the wiring itself is pinned by regex —
// deleting/miswiring the prop chain fails this test even though every
// pure-derivation test above stays green.
test("Board threads authMode from App into cardFacts, SlideOver and TaskCard", () => {
  const here = fileURLToPath(new URL(".", import.meta.url));
  const appJsx = readFileSync(here + "App.jsx", "utf8");
  const boardJsx = readFileSync(here + "Board.jsx", "utf8");
  const slideOverJsx = readFileSync(here + "SlideOver.jsx", "utf8");

  // App.jsx: <Board ... authMode={authMode} ... />
  assert.match(appJsx, /<Board[\s\S]{0,400}authMode=\{authMode\}/,
    "App.jsx must pass authMode to <Board>");

  // Board.jsx: authMode threaded through Board -> Lane -> TaskCard signatures,
  // and fed into cardFacts alongside cost + tokens.
  assert.match(boardJsx, /function Board\(\{[^)]*authMode[^)]*\}\)/,
    "Board(...) signature must accept authMode");
  assert.match(boardJsx, /function Lane\(\{[^)]*authMode[^)]*\}\)/,
    "Lane(...) signature must accept authMode");
  // TaskCard's own signature has a `nowMs = Date.now()` default with a literal
  // `)` inside it, so this one can't use the same "[^)]*" trick as the others.
  assert.match(boardJsx, /function TaskCard\(\{[\s\S]{0,300}?authMode[\s\S]{0,10}?\}\)/,
    "TaskCard(...) signature must accept authMode");
  assert.match(
    boardJsx,
    /cardFacts\(task, \{ cost: taskCost\(task\), tokens: taskBurn\(task\), authMode \}\)/,
    "TaskCard must call cardFacts with cost, tokens AND authMode",
  );
  assert.match(boardJsx, /<SlideOver[\s\S]{0,2000}authMode=\{authMode\}/,
    "Board.jsx must pass authMode to <SlideOver>");

  // SlideOver.jsx: authMode threaded into TaskSummary -> chipsFor.
  assert.match(slideOverJsx, /function SlideOver\(\{[^)]*authMode[^)]*\}\)/,
    "SlideOver(...) signature must accept authMode");
  assert.match(slideOverJsx, /function TaskSummary\(\{[^)]*authMode[^)]*\}\)/,
    "TaskSummary(...) signature must accept authMode");
  assert.match(slideOverJsx, /chipsFor\(task, authMode\)/,
    "TaskSummary must call chipsFor(task, authMode)");

  // The tokens-vs-dollars RULE stays in pricingIsReal(authMode) (cost.js) —
  // none of the three changed files may re-derive `=== "api_key"` locally.
  for (const [name, src] of [["App.jsx", appJsx], ["Board.jsx", boardJsx], ["SlideOver.jsx", slideOverJsx]]) {
    assert.doesNotMatch(src, /authMode\s*===\s*["']api_key["']/,
      `${name} must not re-derive the api_key check locally — use pricingIsReal(authMode)`);
  }
});
