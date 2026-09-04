// Token formatting + cost DISPLAY (W2.5, then the per-model pricing fix). One home so the
// board, the drawer and Stats all say the same number — spend must be visible where approval
// decisions happen, not only on an aggregate page.
//
// THIS FILE NO LONGER COMPUTES A DOLLAR FIGURE. It used to (see the two repair stories below,
// kept as history): a flat `costOf` priced every attempt at one hardcoded Anthropic rate
// ($3/1K fresh, $0.3/1K cache-read), which was wrong for any Codex/OpenAI attempt the moment
// `core/pricing.py` gained per-model OpenAI rows — a `gpt-5.3-codex` attempt ($1.75/$14
// published) rendered on the board as if it had been billed at Sonnet's $3/$15. Fixing that in
// JS a third time (a second price table, or a defaulted-fallback rate baked into a `??`) would
// just move the drift somewhere else, so the fix moves pricing server-side: `core/cost.py`'s
// `attempt_cost` /
// `attempts_cost` price every attempt at ITS OWN recorded model via `core/pricing.py`, and the
// API sends the result as `cost_usd` (+ `cost_model`, naming what priced it) on every
// attempt/task/metrics payload. `taskCost` and `lifetimeCost` below now only READ those fields
// — no rate, no per-bucket arithmetic, nothing to drift a second time. The Python side of this
// split is pinned in `tests/test_pricing_usd.py`; the API wiring in `tests/test_api.py`.
//
// Cost HISTORY, kept because the failure modes it documents are still the reason the buckets
// below are named rather than positional, even though the arithmetic that used them is gone:
// the burn a card shows and the cost it is priced at must have the SAME basis — they didn't
// (the Token Usage tile once read "169.87M · est. $73.58", pricing 6M cache-creation tokens
// the count never showed), and a single-argument `estimateCost(tokens, cacheRead = 0)` let a
// one-argument call silently price a TOTAL burn at the fresh rate (how a Stats tile once
// claimed $29.98 per merged PR).

export function fmtTokens(n) {
  if (n == null) return "—";
  if (n < 1000) return `${n}`;
  if (n < 1000000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1000000).toFixed(2)}M`;
}

/**
 * Every token bucket, summed — the number a burn meter shows.
 *
 * Cache-read is 90%+ of real spend (C1): summing only tokens_used under-reported a 33M-token
 * task as "121.5k tok". Cache-CREATION was the bucket still missing, so the burn a surface
 * displayed and the cost it priced had different bases (the Token Usage tile read
 * "169.87M · est. $73.58" — a price for 6M tokens the count never showed).
 *
 * Named buckets: they cannot be transposed or silently dropped.
 */
export function totalBurn(buckets) {
  const { used = 0, creation = 0, read = 0 } = buckets || {};
  return (used || 0) + (creation || 0) + (read || 0);
}

/** Format a dollar figure — or say nothing, which beats saying a wrong number. */
export function fmtCost(dollars) {
  if (dollars == null || !Number.isFinite(dollars) || dollars === 0) return "—";
  if (dollars < 0.01) return "<$0.01";
  return `$${dollars.toFixed(2)}`;
}

/**
 * One task's token burn across the same nine buckets the API's `cost_usd` prices — so a
 * surface showing both cannot show a price for tokens its own count never included.
 */
export function taskBurn(task) {
  if (!task) return 0;
  return (
    totalBurn({ used: task.total_tokens, creation: task.total_cache_creation, read: task.total_cache_read })
    + totalBurn({
      used: task.total_review_tokens,
      creation: task.total_review_cache_creation,
      read: task.total_review_cache_read,
    })
    + totalBurn({
      used: task.total_aux_tokens,
      creation: task.total_aux_cache_creation,
      read: task.total_aux_cache_read,
    })
  );
}

/**
 * One task's cost — read straight off the API. Priced server-side by `core/cost.py`'s
 * `attempt_cost`/`attempts_cost`, each attempt at its OWN recorded model
 * (`core/pricing.py`'s per-model table), summed across coder + reviewer + aux. `task.cost_model`
 * names what priced it: a model id, `"mixed"` when the task's attempts used more than one, or
 * `pricing.FALLBACK_PRICE_NAME` when nothing priced could be resolved — never a rate computed
 * here. `0` for a task with no attempts yet or no cost field (an older payload shape), same as
 * before this file stopped computing.
 */
export function taskCost(task) {
  return task?.cost_usd ?? 0;
}

/**
 * The lifetime cost, from `/api/metrics`'s `cost_usd_total` — the SINGLE source both the
 * "Cost / merged PR" tile and the "Token Usage" tile read. `null` when the install has no
 * attempts yet (distinct from "attempts spent $0"), so both tiles say "—" together rather than
 * disagreeing.
 */
export function lifetimeCost(metrics) {
  return metrics?.cost_usd_total ?? null;
}

/**
 * Only `api_key` (BYO) mode pays Anthropic per token for real; `subscription`
 * (the default, and any absent/unrecognized value — OAuth is the fallback,
 * never assume real dollars) pays a flat fee, so a dollar figure there is an
 * API-rate ESTIMATE, not money that changed hands. The single home of this
 * rule (SCRUM re-home) — every surface that must choose tokens vs. dollars
 * reads this instead of re-deriving the `=== "api_key"` check.
 */
export function pricingIsReal(authMode) {
  return authMode === "api_key";
}

/**
 * Lifetime tokens from `/api/metrics`'s `tokens_total` — the token-basis
 * sibling of `lifetimeCost`, reading the SAME nine buckets `cost_usd_total`
 * prices. `null` on an older payload that lacks the key (distinct from "0
 * tokens spent"), so a subscription-mode tile can say "no token data yet"
 * instead of a false zero.
 */
export function lifetimeTokens(metrics) {
  return metrics?.tokens_total ?? null;
}
