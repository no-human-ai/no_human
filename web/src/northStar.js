// The north-star row for the Stats page (D1): the numbers a staff engineer
// actually trusts, derived from /api/metrics — NOT "completed tasks", which
// counts 0-token code reviews as deliveries. Pure so node --test can pin it.

import { fmtCost, fmtTokens, lifetimeCost, lifetimeTokens, pricingIsReal } from "./cost.js";

// tone: "good" | "bad" | "neutral" drives the semantic colour of the value.
//
// Colour is spent ONLY where a real threshold exists. It used to be handed out for
// "non-zero" (PRs merged) and withheld from a perfect score (Stagnation: 0), so four green
// tiles in a row taught the operator nothing — the colour carried no signal. Volume numbers
// (how many, how much) are neutral; quality numbers (did the gate hold, is the cache working)
// carry the colour.
export function northStarTiles(metrics, authMode) {
  if (!metrics) return [];
  const merged = metrics.prs_merged ?? 0;
  const opened = metrics.prs_opened ?? 0;
  const rPass = metrics.review_pass ?? 0;
  const rFail = metrics.review_fail ?? 0;
  const share = metrics.cache_economics?.creation_share;
  const dist = metrics.cache_read_share_dist;

  // Cost is the headline concern, so it is a VALUE, not a subtitle under a token count.
  //
  // It is computed from the three RAW buckets and divided by prs_MERGED. Two earlier models
  // were wrong: pricing the total burn at the fresh rate ($29.98), and splitting that total
  // by `creation_share` ($3.93) — a category error, because `tokens_per_pr` contains no
  // cache-creation while that share's universe is creation+read, AND it divides by prs_OPENED,
  // so a "per merged PR" label sat on a per-opened-PR number. The lifetime tile on the same
  // page divides this identical cost by nothing, so the two can no longer imply different rates.
  // Coder + reviewer + aux (every other named role — planner, utility, supervisor,
  // distill; the API rolls them into one aux triple). Those columns DID reach the
  // metrics feed (metrics.py aux_*_total) and lifetimeCost prices them as of the
  // cost-by-project work — the same nine buckets taskCost uses, so this tile and the
  // per-task surfaces cannot imply different totals.
  const lifetime = lifetimeCost(metrics);
  const costPerPr = merged > 0 && lifetime ? lifetime / merged : null;

  // Subscription (flat-fee) mode never renders a dollar figure here — the
  // API-rate estimate is not money that changed hands. `pricingIsReal` is
  // the single home of the api_key-vs-subscription rule (SCRUM re-home);
  // `authMode` absent/unrecognized falls to the token-led tile, same as
  // every other subscription-mode surface.
  const tokens = lifetimeTokens(metrics);
  const tokensPerPr = merged > 0 && tokens ? Math.round(tokens / merged) : null;
  const secondTile = pricingIsReal(authMode)
    ? {
        label: "Cost / merged PR",
        value: fmtCost(costPerPr),
        sub:
          costPerPr == null
            ? (merged === 0 ? "no merge yet" : "no token data yet")
            : `${fmtCost(lifetime)} spent · ${merged} merged`,
        tone: "neutral",
      }
    : {
        label: "Tokens / merged PR",
        value: fmtTokens(tokensPerPr),
        sub:
          tokensPerPr == null
            ? (merged === 0 ? "no merge yet" : "no token data yet")
            : `${fmtTokens(tokens)} used · ${merged} merged`,
        tone: "neutral",
      };

  return [
    {
      label: "PRs merged",
      value: String(merged),
      sub: `${opened} opened · human-merged`,
      // Zero shipped IS a threshold — the product is not delivering. But "more than zero"
      // is not a threshold for GOOD, and green-for-non-zero is what made the colour mean
      // nothing across four tiles.
      tone: merged === 0 ? "bad" : "neutral",
    },
    secondTile,
    {
      label: "Review gate",
      value: rPass + rFail > 0 ? `${rPass}/${rPass + rFail}` : "—",
      sub: `${rFail} blocked by the reviewer`,
      tone: rPass + rFail === 0 ? "neutral" : rFail > rPass ? "bad" : "good",
    },
    {
      label: "Cache reuse",
      value: share != null ? `${((1 - share) * 100).toFixed(0)}%` : "—",
      sub: share != null ? `${(share * 100).toFixed(1)}% rebuilt (full price)` : "no data",
      tone: share == null ? "neutral" : share > 0.5 ? "bad" : "good",
    },
    // The fleet tile above pools every attempt's tokens into one ratio, so a single
    // attempt heading for budget exhaustion can hide inside a healthy pooled average.
    // This is a DISTRIBUTION over per-attempt shares (metrics.py cache_read_share_dist,
    // itself computed by core.metrics.cache_read_share — no local arithmetic here) —
    // the earliest signal that one specific attempt, not the fleet, is in trouble.
    {
      label: "Cache read / attempt (p50)",
      value: dist?.p50 != null ? `${(dist.p50 * 100).toFixed(0)}%` : "—",
      sub:
        dist?.p90 != null
          ? `p90 ${(dist.p90 * 100).toFixed(0)}% · ${dist.attempts_measured} attempt(s) measured`
          : "no data",
      tone: "neutral",
    },
  ];
}
