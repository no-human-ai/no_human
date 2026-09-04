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
