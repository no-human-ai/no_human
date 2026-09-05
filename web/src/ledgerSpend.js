// Mode-aware LAST 24H spend line (SCRUM-20). Subscription/enterprise profiles
// pay a flat fee — the dollar figure the API prices server-side (core/cost.py,
// core/pricing.py's per-model table) is an ESTIMATE of API-rate value, not
// money that changed hands, so tokens lead and the dollars are marked "est.".
// api_key (BYO) mode pays Anthropic per token for real, so the dollar figure
// IS the spend — same as today's line.
// Pure derivation, no fetching: the caller (the sidebar block) already has
// every number this needs from data it already fetches.
import { fmtTokens, fmtCost, pricingIsReal } from "./cost.js";

export function deriveSpendDisplay(tokensSpent, dollarEstimate, actualDollarSpent, authMode) {
  if (pricingIsReal(authMode)) {
    return { primary: fmtCost(actualDollarSpent), secondary: "spent", format: "dollars" };
  }
  // "subscription" and any absent/unrecognized value fall back to the
  // subscription behavior — OAuth is the default, never assume real dollars.
  return {
    primary: `${fmtTokens(tokensSpent)} tok`,
    secondary: `(~${fmtCost(dollarEstimate)} est.)`,
    format: "tokens",
  };
}

// Approximate cost per shipped PR for the window — only when both a shipped
// count and a window cost are actually available. Never invented from data
// the sidebar doesn't have (no new API call): callers show the shipped count
// alone when this returns null.
export function perShippedCost(shippedCount, windowCost) {
  if (!shippedCount || windowCost == null || !Number.isFinite(windowCost) || windowCost <= 0) {
    return null;
  }
  return windowCost / shippedCount;
}

// The per-PR figure, rendered — same tokens-lead/dollars-est. convention as
// deriveSpendDisplay above, applied to the per-shipped-PR figure instead of
// the window total. Returns null exactly when perShippedCost does (no
// shipped PRs, or no usable window cost yet) — callers show the shipped
// count alone in that case, same contract as today.
export function derivePerShippedDisplay(shippedCount, windowCost, tokensSpent, authMode) {
  const perPr = perShippedCost(shippedCount, windowCost);
  if (perPr == null) return null;
  if (pricingIsReal(authMode)) return `(~${fmtCost(perPr)}/PR)`;
  const perPrTokens = shippedCount > 0 && Number.isFinite(tokensSpent) && tokensSpent > 0
    ? tokensSpent / shippedCount : null;
  return perPrTokens != null
    ? `(~${fmtTokens(Math.round(perPrTokens))} tok/PR · ~${fmtCost(perPr)} est.)`
    : `(~${fmtCost(perPr)}/PR est.)`;
}
