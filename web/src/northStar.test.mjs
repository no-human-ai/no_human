import test from "node:test";
import assert from "node:assert/strict";
import { northStarTiles } from "./northStar.js";

test("empty metrics yields no tiles", () => {
  assert.deepEqual(northStarTiles(null), []);
});

test("zero merged PRs reads as bad; a non-zero count is NOT 'good'", () => {
  const t0 = northStarTiles({ prs_merged: 0, prs_opened: 3, review_pass: 4, review_fail: 15 });
  const merged = t0.find((t) => t.label === "PRs merged");
  assert.equal(merged.value, "0");
  assert.equal(merged.tone, "bad");   // nothing has shipped — a real alarm
  const review = t0.find((t) => t.label === "Review gate");
  assert.equal(review.value, "4/19");
  assert.equal(review.tone, "bad");   // more blocked than passed
  // CHANGED (UI_AUDIT M9): "2 merged" used to read GREEN, i.e. green meant "non-zero".
  // With four tiles green at once the colour stopped carrying any signal, so a raw count
  // with no threshold for "good" is now neutral.
  const t1 = northStarTiles({ prs_merged: 2, prs_opened: 2 });
  assert.equal(t1.find((t) => t.label === "PRs merged").tone, "neutral");
});

test("cost/PR only shows once something merged; cache share inverts", () => {
  // The API prices the lifetime figure server-side now (core/metrics.py's cost_usd_total,
  // core/cost.py's attempts_cost) — this tile just divides that number by prs_merged, so the
  // fixture sets cost_usd_total directly rather than reconstructing it from token buckets.
  // api_key mode: this is the only mode where the dollar tile renders at all (SCRUM re-home).
  const t = northStarTiles({
    prs_merged: 2, cost_usd_total: 6.00, cost_model_total: "claude-sonnet-5",
    cache_economics: { creation_share: 0.0335 },
  }, "api_key");
  const cost = t.find((x) => x.label === "Cost / merged PR");
  assert.equal(cost.value, "$3.00");
  assert.match(cost.sub, /\$6\.00 spent · 2 merged/);
  const cache = t.find((x) => x.label === "Cache reuse");
  assert.equal(cache.value, "97%");   // 1 - 0.0335
  assert.equal(cache.tone, "good");
  // no merge → a dash, not a misleading number
  const t2 = northStarTiles({ prs_merged: 0, cost_usd_total: 5000 }, "api_key");
  assert.equal(t2.find((x) => x.label === "Cost / merged PR").value, "—");
});

// M1 — the cost lie. A JS-side `estimateCost(tokens_per_pr)` used to price a TOTAL burn at
// the fresh rate. That arithmetic is gone; the API prices the lifetime total server-side
// (core/cost.py, per-model, summed across every attempt) and this tile only divides by
// prs_merged, so tokens_per_pr (a per-OPENED-PR, cache-creation-excluding figure) cannot
// leak into the cost shown here even by accident.
test("the cost per merged PR is the LIFETIME cost over merged PRs — not tokens_per_pr", () => {
  const tiles = northStarTiles({
    prs_merged: 13, prs_opened: 17, tokens_per_pr: 9_992_527,
    cost_usd_total: 73.58, cost_model_total: "claude-sonnet-5",
  }, "api_key");
  const cost = tiles.find((t) => t.label === "Cost / merged PR");
  assert.equal(cost.value, "$5.66");            // NOT $3.93 (split model), NOT $29.98 (fresh-rate)
  assert.match(cost.sub, /\$73\.58 spent · 13 merged/);
});

test("no token data → the cost tile says so rather than guessing", () => {
  const tiles = northStarTiles({ prs_merged: 13 }, "api_key");
  assert.equal(tiles.find((t) => t.label === "Cost / merged PR").value, "—");
});

test("no measured cache split → the cost tile says so rather than guessing", () => {
  const tiles = northStarTiles({ prs_merged: 13, tokens_per_pr: 9_992_527 }, "api_key");
  const cost = tiles.find((t) => t.label === "Cost / merged PR");
  assert.equal(cost.value, "—");
});

// M9 — the tone system. Colour must mean "measured against a threshold", or it means nothing.
test("volume tiles are neutral; only tiles with a real threshold carry colour", () => {
  const tiles = northStarTiles({
    prs_merged: 13, prs_opened: 17, tokens_per_pr: 1_000_000,
    review_pass: 42, review_fail: 17,
    cache_economics: { creation_share: 0.03 },
  }, "api_key");
  const tone = (label) => tiles.find((t) => t.label === label)?.tone;
  assert.equal(tone("PRs merged"), "neutral", "a raw count has no threshold — green meant 'non-zero'");
  assert.equal(tone("Cost / merged PR"), "neutral", "cost is volume, not quality");
  assert.equal(tone("Review gate"), "good");
  assert.equal(tone("Cache reuse"), "good");
});

// THE ASSERTION WHOSE ABSENCE LET THE PAGE LIE. The "Cost / merged PR" tile and the
// "Token Usage" tile show the same burn from the same buckets; if they ever assemble those
// buckets separately again, their implied $/token drifts apart (it did: $29.98 vs $55.54,
// then $3.93 vs $55.54, then $68.50 vs $73.58). They are ONE identity now.
test("the per-PR tile and the lifetime tile derive from the same cost — they cannot disagree", async () => {
  const { lifetimeCost, fmtCost } = await import("./cost.js");
  const metrics = {
    prs_merged: 13, prs_opened: 17,
    cost_usd_total: 73.58, cost_model_total: "claude-sonnet-5",
  };
  const tiles = northStarTiles(metrics, "api_key");
  const perPr = tiles.find((t) => t.label === "Cost / merged PR");
  const lifetime = lifetimeCost(metrics);          // what the Token Usage tile renders
  assert.equal(fmtCost(lifetime), "$73.58");
  assert.equal(perPr.value, fmtCost(lifetime / metrics.prs_merged));
  assert.match(perPr.sub, /\$73\.58 spent · 13 merged/);
});

test("no cost_usd_total (an un-restarted server or an install with no attempts) → BOTH surfaces say '—', not two numbers", async () => {
  const { lifetimeCost, fmtCost } = await import("./cost.js");
  const metrics = { prs_merged: 13 };
  assert.equal(lifetimeCost(metrics), null);
  assert.equal(fmtCost(lifetimeCost(metrics)), "—");
  assert.equal(
    northStarTiles(metrics, "api_key").find((t) => t.label === "Cost / merged PR").value,
    "—",
  );
});

// The per-attempt distribution is a SIBLING tile of the fleet "Cache reuse" tile, not
// a replacement — the earliest signal a single attempt (not the whole fleet) is heading
// for budget exhaustion. Neither label may say "reuse"; that word belongs to the fleet tile.
test("cache-read-share distribution is its own tile, distinct from the fleet 'Cache reuse' tile", () => {
  const tiles = northStarTiles({
    cache_economics: { creation_share: 0.03 },
    cache_read_share_dist: { p50: 0.9, p90: 0.99, attempts_measured: 3 },
  });
  const fleet = tiles.find((t) => t.label === "Cache reuse");
  const perAttempt = tiles.find((t) => t.label !== "Cache reuse" && /cache/i.test(t.label));
  assert.ok(fleet, "fleet tile must still be present");
  assert.ok(perAttempt, "a distinct per-attempt tile must be present");
  assert.notEqual(fleet.label, perAttempt.label);
  assert.doesNotMatch(perAttempt.label.toLowerCase(), /reuse/);
  assert.equal(fleet.value, "97%");            // 1 - 0.03, unaffected by the new tile
  assert.equal(perAttempt.value, "90%");       // p50
  assert.match(perAttempt.sub, /99%/);         // p90 surfaced too
  assert.match(perAttempt.sub, /3/);           // attempts_measured surfaced too
});

test("cache-read-share distribution absent → the tile reads '—', no crash", () => {
  const tiles = northStarTiles({ prs_merged: 1 });
  const perAttempt = tiles.find((t) => t.label !== "Cache reuse" && /cache/i.test(t.label));
  assert.ok(perAttempt);
  assert.equal(perAttempt.value, "—");
  assert.equal(perAttempt.sub, "no data");
});

// SCRUM re-home: subscription (flat-fee) mode — the default, and any absent/unrecognized
// authMode — must never render a dollar figure for the second tile. Only api_key (BYO) mode
// gets "Cost / merged PR"; every other mode gets "Tokens / merged PR" with no `$` anywhere.
test("subscription mode (and no authMode at all) renders tokens, not dollars, for the 2nd tile", () => {
  const metrics = {
    prs_merged: 2, tokens_total: 500_000, cost_usd_total: 6.00,
    cost_model_total: "claude-sonnet-5",
  };
  for (const authMode of [undefined, null, "subscription", "bogus"]) {
    const tiles = northStarTiles(metrics, authMode);
    assert.equal(tiles.find((t) => t.label === "Cost / merged PR"), undefined,
      `authMode=${authMode} must not render a dollar-labelled tile`);
    const tokenTile = tiles.find((t) => t.label === "Tokens / merged PR");
    assert.ok(tokenTile, `authMode=${authMode} must render the token tile`);
    assert.equal(tokenTile.value, "250.0k");
    assert.match(tokenTile.sub, /500\.0k used · 2 merged/);
    assert.doesNotMatch(tokenTile.value + tokenTile.sub, /\$/, "no dollar figure anywhere");
  }
});

test("api_key mode still renders the dollar tile, with fmtCost output", () => {
  const tiles = northStarTiles({
    prs_merged: 2, tokens_total: 500_000, cost_usd_total: 6.00,
    cost_model_total: "claude-sonnet-5",
  }, "api_key");
  assert.equal(tiles.find((t) => t.label === "Tokens / merged PR"), undefined);
  const cost = tiles.find((t) => t.label === "Cost / merged PR");
  assert.equal(cost.value, "$3.00");
});

test("subscription mode: no merge yet vs no token data yet are distinguished", () => {
  const noMerge = northStarTiles({ prs_merged: 0, tokens_total: 500_000 });
  assert.equal(noMerge.find((t) => t.label === "Tokens / merged PR").sub, "no merge yet");
  const noTokens = northStarTiles({ prs_merged: 5 });
  assert.equal(noTokens.find((t) => t.label === "Tokens / merged PR").sub, "no token data yet");
});
