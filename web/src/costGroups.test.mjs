import test from "node:test";
import assert from "node:assert/strict";
import { costByProject, totalCost, totalTokens, UNATTRIBUTED } from "./costGroups.js";
import { taskCost, taskBurn } from "./cost.js";
import { ledgerSummary } from "./nightLedger.js";

// Pure aggregation over the cost model — the kind of logic this repo tests directly
// (boardLanes, searchView, learningGroups all follow this shape).
//
// `taskCost` is now a pure read of `task.cost_usd` (the API prices per-model server-side —
// see core/cost.py, pinned in tests/test_pricing_usd.py and tests/test_api.py), so fixtures
// here set `cost_usd` directly rather than token buckets: this file tests the GROUPING/SUMMING
// logic in costGroups.js, not how a dollar figure gets computed (that guarantee, including
// "the reviewer's and aux tokens are included", now lives entirely server-side).

const task = (repo, costUsd) => ({ repo_name: repo, cost_usd: costUsd });

test("groups by repo and sums each project's cost", () => {
  const rows = costByProject([task("a", 1), task("b", 2), task("a", 3)]);
  assert.deepEqual(rows.map((r) => [r.project, r.tasks]), [["a", 2], ["b", 1]]);
  assert.equal(rows.find((r) => r.project === "a").cost, 4);
});

test("sorts by cost desc, then by name for a stable order", () => {
  const rows = costByProject([task("cheap", 1), task("dear", 9999), task("mid", 500)]);
  assert.deepEqual(rows.map((r) => r.project), ["dear", "mid", "cheap"]);
  // Equal cost -> alphabetical, so the list does not reshuffle between renders.
  const tied = costByProject([task("zeta", 100), task("alpha", 100)]);
  assert.deepEqual(tied.map((r) => r.project), ["alpha", "zeta"]);
});

// The old shape of this test constructed token buckets and checked that costByProject's total
// exceeded a coder-only figure — i.e. that the reviewer's/aux tokens were included in the
// price. That guarantee moved server-side with the pricing fix: core/cost.py's attempt_cost
// sums coder + reviewer + planner + utility + supervisor + distill for every attempt
// (tests/test_pricing_usd.py, tests/test_api.py). What remains testable here is that
// costByProject sums whatever cost_usd each task already carries, without dropping or
// double-counting a row.
test("sums whatever cost_usd each task already carries — no row dropped or double-counted", () => {
  const [row] = costByProject([task("a", 1), task("a", 2), task("a", 3)]);
  assert.equal(row.cost, 6);
  assert.equal(row.tasks, 3);
});

// This test used to assert the Stats rollup and the sidebar ledger priced the
// same tasks IDENTICALLY — both summed each task's lifetime `cost_usd`. That
// shared basis was itself the bug this file's sibling (nightLedger.js) was
// fixed for: closing/cancelling an old task bumps `updated_at` with no new
// spend, so summing lifetime `cost_usd` over "touched in the window" tasks
// swept the WHOLE stale total into "last 24h" (~3.5x inflation on a live
// board). The fix moved the ledger's cost onto the server's attempt-attributed
// `/api/metrics/window` figure (`spend`), a genuinely different basis
// (window-of-activity vs. lifetime-per-task) — so the two surfaces are now
// EXPECTED to disagree on the same task list. What must still hold: the
// ledger never falls back to re-deriving a total from the tasks' own
// `cost_usd` (that fallback would silently resurrect the bug), and it reports
// exactly whatever `spend` it was handed.
test("the ledger's window cost is NOT the Stats lifetime rollup — they are different bases by design", () => {
  const now = Date.now();
  const recent = (over) => ({
    ...over,
    updated_at: new Date(now - 3600e3).toISOString(),
    created_at: new Date(now - 7200e3).toISOString(),
    status: "done",
  });
  const tasks = [
    recent(task("a", 5.5)),
    recent(task("b", 3.0)),
  ];
  const rollup = totalCost(costByProject(tasks));
  assert.ok(rollup > 0, "fixture priced at zero — the assertion below would be vacuous");

  // No server figure yet (or an old daemon that 404s): the ledger must show
  // zero, NOT silently sum the tasks' own lifetime cost_usd like it used to.
  assert.equal(ledgerSummary(tasks, now).cost, 0);

  // With a server figure supplied, the ledger reports THAT value verbatim —
  // even when it is nowhere near the Stats lifetime rollup for the very same
  // tasks, because it is pricing a different thing (recent attempt activity).
  const windowSpend = { cost_usd: 0.42, tokens: 100 };
  assert.equal(ledgerSummary(tasks, now, undefined, windowSpend).cost, windowSpend.cost_usd);
  assert.notEqual(windowSpend.cost_usd, rollup);
});

test("groups case-insensitively, keeping the first spelling seen", () => {
  const rows = costByProject([task("no_human", 1), task("No_Human", 1)]);
  assert.equal(rows.length, 1, "the same repo in two casings split into two rows");
  assert.equal(rows[0].tasks, 2);
  assert.equal(rows[0].project, "no_human", "should display the spelling seen first");
});

test("keeps tasks with no repo instead of dropping them", () => {
  const rows = costByProject([task("a", 1), task(undefined, 2), task("   ", 3)]);
  const un = rows.find((r) => r.project === UNATTRIBUTED);
  assert.equal(un.tasks, 2, "tasks with a missing or blank repo were lost");
});

test("the rows always sum to the total", () => {
  const tasks = [task("a", 1.5), task("b", 2), task(null, 0.3)];
  const rows = costByProject(tasks);
  const expected = tasks.reduce((s, t) => s + taskCost(t), 0);
  // Floating point: compare within a cent rather than exactly.
  assert.ok(Math.abs(totalCost(rows) - expected) < 0.01);
});

// SCRUM re-home: `tokens` is the token-basis sibling of `cost` — same nine buckets taskBurn
// sums, kept per-row so the subscription-mode table can show a token column without a
// second sort (the rows stay ordered by `cost`, unchanged — see the test above).
test("each row carries a tokens field summing taskBurn over its tasks; totalTokens sums the rows", () => {
  const tok = (repo, tokens, costUsd = 0) => ({
    repo_name: repo, cost_usd: costUsd, total_tokens: tokens,
  });
  const rows = costByProject([tok("a", 1000), tok("a", 2000), tok("b", 500)]);
  const a = rows.find((r) => r.project === "a");
  const b = rows.find((r) => r.project === "b");
  assert.equal(a.tokens, 3000);
  assert.equal(b.tokens, 500);
  assert.equal(totalTokens(rows), 3500);
});

test("totalTokens on empty/missing input does not throw", () => {
  assert.equal(totalTokens([]), 0);
  assert.equal(totalTokens(undefined), 0);
});

test("row order stays keyed on cost, not tokens — a low-cost/high-token project does not jump the queue", () => {
  const tasks = [
    { repo_name: "high-cost-low-tokens", cost_usd: 100, total_tokens: 10 },
    { repo_name: "low-cost-high-tokens", cost_usd: 1, total_tokens: 999_999 },
  ];
  const rows = costByProject(tasks);
  assert.deepEqual(rows.map((r) => r.project), ["high-cost-low-tokens", "low-cost-high-tokens"]);
});

test("empty and missing input do not throw", () => {
  assert.deepEqual(costByProject([]), []);
  assert.deepEqual(costByProject(undefined), []);
  assert.equal(totalCost([]), 0);
  assert.equal(totalCost(undefined), 0);
});
