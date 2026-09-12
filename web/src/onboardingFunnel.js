// Pure onboarding-funnel step reporter. No React import: `node --test` has no
// renderer, and this module needs none — it is deliberately kept side-effect
// free and framework-agnostic so it can be unit tested directly.
//
// `FUNNEL_STEPS` mirrors `Onboarding.jsx`'s `BASE_STEPS` keys (welcome, repos,
// projects, integrations, summary) and the server's `_WIZARD_STEPS`
// (api/app.py) / `telemetry.ONBOARDING_STEPS` — kept as its own literal list
// (this module cannot import a .jsx file) and pinned equal to both by
// onboardingFunnel.test.mjs and tests/test_onboarding_funnel_telemetry.py.
export const FUNNEL_STEPS = ["welcome", "repos", "projects", "integrations", "summary"];

/**
 * Build a StrictMode-safe step reporter: `send(step)` fires at most once per
 * distinct step key for the lifetime of the returned reporter (a `Set`, not a
 * boolean, so every step reports exactly once across a forward/back/forward
 * walk through the wizard, not just the first one ever seen). An off-list key
 * is silently ignored (the server 422s it anyway; this just avoids a wasted
 * request), and any error `send` throws or rejects with is swallowed — a
 * telemetry hiccup must never surface in, or block, the onboarding UI.
 *
 * `send` is expected to be fire-and-forget (e.g. `recordOnboardingStep` in
 * api.js), but the reporter tolerates a throwing/rejecting `send` either way.
 */
export function makeStepReporter(send) {
  const reported = new Set();
  return function reportStep(step) {
    if (!FUNNEL_STEPS.includes(step)) return;
    if (reported.has(step)) return;
    reported.add(step);
    try {
      const result = send(step);
      if (result && typeof result.catch === "function") {
        result.catch(() => {});
      }
    } catch {
      // swallow — a telemetry failure must never affect onboarding itself
    }
  };
}
