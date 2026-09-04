import test from "node:test";
import assert from "node:assert/strict";
import {
  fmtTokens, totalBurn, fmtCost, lifetimeCost, taskCost, taskBurn,
  pricingIsReal, lifetimeTokens,
} from "./cost.js";

// NOTE ON WHAT MOVED: this file used to pin the dollar ARITHMETIC (a flat rate applied to
// three token buckets). That arithmetic no longer lives in JS — it moved server-side to
// `core/pricing.py`'s per-model `usd_cost`, pinned in `tests/test_pricing_usd.py`, and the API
// wiring that calls it per attempt/task is pinned in `tests/test_api.py`. What remains here is
// what this file still does: FORMAT a dollar figure the API already computed, and sum token
// buckets for display. `costOf`/`estimateCost` no longer exist in `cost.js` — see
// `costNoRateInJs.test.mjs` for the guard that keeps them from coming back.

test("totalBurn is every token bucket — cache-read (the hidden 90%) AND cache-creation", () => {
  // Named buckets: the burn a card shows and the cost it is priced at must have the SAME
  // basis. They didn't — the Token Usage tile read "169.87M · est. $73.58", pricing 6M
  // cache-creation tokens the count never showed.
  assert.equal(totalBurn({ used: 121_500, read: 33_000_000 }), 33_121_500);
  assert.equal(totalBurn({ used: 121_500, creation: 6_000, read: 33_000_000 }), 33_127_500);
  assert.equal(totalBurn({}), 0);
  assert.equal(totalBurn(null), 0);
  assert.equal(totalBurn({ used: 500 }), 500);
});

test("fmtTokens", () => {
  assert.equal(fmtTokens(33_121_500), "33.12M");
  assert.equal(fmtTokens(null), "—");
});

test("fmtCost formats, and says nothing when there is nothing to say", () => {
  assert.equal(fmtCost(0), "—");
  assert.equal(fmtCost(null), "—");
  assert.equal(fmtCost(undefined), "—");
  assert.equal(fmtCost(0.004), "<$0.01");
  assert.equal(fmtCost(12.345), "$12.35");
});

// taskCost/lifetimeCost are now pure readers of API fields — no bucket, no rate. The dollar
// figures themselves (Claude vs. Codex, mixed-model attempts, the fallback label) are pinned
// in `tests/test_pricing_usd.py` and `tests/test_api.py`; these tests only pin that the board
// renders the number the API sent, verbatim, for both backends.

test("taskCost renders the API's cost_usd for a Claude attempt — same $3.30 the old flat-rate JS produced", () => {
  // Regression pin matching tests/test_pricing_usd.py::test_claude_regression_pin: 1M fresh +
  // 1M cache-read on claude-sonnet-5 = $3.30. The API computed this now; the board just reads it.
  const task = { cost_usd: 3.30, cost_model: "claude-sonnet-5" };
  assert.equal(taskCost(task), 3.30);
  assert.equal(fmtCost(taskCost(task)), "$3.30");
});

test("taskCost renders the API's cost_usd for a Codex attempt — priced at Codex's own rate, not Claude's", () => {
  // gpt-5.3-codex is $1.75/Mtok vs Claude's $3/Mtok for the same tokens_used; this is exactly
  // the bug this file used to have (it priced every backend at the Claude rate).
  const task = { cost_usd: 1.75, cost_model: "gpt-5.3-codex" };
  assert.equal(taskCost(task), 1.75);
  assert.equal(fmtCost(taskCost(task)), "$1.75");
});

test("taskCost is 0 for a missing task or a task with no cost field, never a computed guess", () => {
  assert.equal(taskCost(null), 0);
  assert.equal(taskCost(undefined), 0);
  assert.equal(taskCost({}), 0);
});

test("lifetimeCost renders /api/metrics's cost_usd_total verbatim", () => {
  assert.equal(lifetimeCost({ cost_usd_total: 9.0, cost_model_total: "mixed" }), 9.0);
  assert.equal(fmtCost(lifetimeCost({ cost_usd_total: 9.0 })), "$9.00");
});

test("lifetimeCost is null when the install has no attempts yet — distinct from spent $0", () => {
  assert.equal(lifetimeCost({}), null);
  assert.equal(lifetimeCost(null), null);
  assert.equal(lifetimeCost({ cost_usd_total: null }), null);
});

test("lifetimeCost is 0 (not null) when the API says attempts spent nothing", () => {
  assert.equal(lifetimeCost({ cost_usd_total: 0 }), 0);
});

// The per-task and lifetime surfaces sit on the SAME page (the Token Usage tile and the
// north-star "Cost / merged PR" tile above the task table and the per-project rollup); both now
// read a field the API priced the same way (core/cost.py), so they cannot drift the way they
// did when each assembled its own buckets in JS.
test("taskCost and lifetimeCost both just forward the API's number — no local recomputation", () => {
  assert.equal(taskCost({ cost_usd: 5.5 }), lifetimeCost({ cost_usd_total: 5.5 }));
});

test("taskBurn counts every bucket a task's cost can be priced from", () => {
  // A surface showing both must not price tokens its own count omits — the Token Usage tile
  // read "1.00M · est. $9.00" while the price covered 3M.
  const only = (field) => ({ [field]: 1_000_000 });
  for (const field of ["total_tokens", "total_review_tokens", "total_aux_tokens"]) {
    assert.equal(taskBurn(only(field)), 1_000_000, `${field} missing from taskBurn`);
  }
  for (const field of ["total_cache_read", "total_review_cache_read", "total_aux_cache_read"]) {
    assert.equal(taskBurn(only(field)), 1_000_000, `${field} missing from taskBurn`);
  }
  for (const field of ["total_cache_creation", "total_review_cache_creation", "total_aux_cache_creation"]) {
    assert.equal(taskBurn(only(field)), 1_000_000, `${field} missing from taskBurn`);
  }
  assert.equal(taskBurn(null), 0);
});

// SCRUM re-home: the single home of the api_key-vs-subscription rule. Only `api_key` (BYO)
// pays Anthropic per token for real — every other/absent/garbage value must fall to the
// subscription (token-led) behavior, never assume real dollars.
test("pricingIsReal is true only for api_key — subscription, absent, and garbage all fall to token display", () => {
  assert.equal(pricingIsReal("api_key"), true);
  assert.equal(pricingIsReal("subscription"), false);
  assert.equal(pricingIsReal(undefined), false);
  assert.equal(pricingIsReal(null), false);
  assert.equal(pricingIsReal("SUBSCRIPTION"), false);   // case-sensitive, no normalization guess
  assert.equal(pricingIsReal("api-key"), false);         // near-miss spelling is not a match
  assert.equal(pricingIsReal(""), false);
});

test("lifetimeTokens renders /api/metrics's tokens_total verbatim — the token-basis sibling of lifetimeCost", () => {
  assert.equal(lifetimeTokens({ tokens_total: 12_345 }), 12_345);
  assert.equal(lifetimeTokens({ tokens_total: 0 }), 0, "0 tokens spent is honest, not absence");
});

test("lifetimeTokens is null when the payload lacks the key — distinct from 0 tokens spent", () => {
  assert.equal(lifetimeTokens({}), null);
  assert.equal(lifetimeTokens(null), null);
  assert.equal(lifetimeTokens(undefined), null);
});
