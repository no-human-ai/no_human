// The wizard's own step keys, in order. This literal is NOT itself the
// enforcement mechanism — nothing at build time keeps it, Onboarding.jsx's
// BASE_STEPS, api/app.py's _WIZARD_STEPS, and telemetry.py's ONBOARDING_STEPS
// in sync automatically. The one thing that does enforce agreement is
// tests/test_onboarding_funnel_telemetry.py::test_step_key_matches_the_wizards_own_list
// and web/src/onboardingFunnel.test.mjs's derivation test, both of which parse
// BASE_STEPS out of Onboarding.jsx's own source at test time and fail if this
// list (or the server-side ones) drifts from it.
export const FUNNEL_STEPS = [
  "welcome", "email", "repos", "projects", "integrations", "discord", "summary",
];

// Wraps a `send` function (e.g. recordOnboardingStep from api.js) with
// once-per-step dedup so a step is reported at most once per app session,
// regardless of how many times the wizard revisits it (forward/back
// navigation, or React 18 StrictMode's intentional double-invoke of effects
// in development). Off-list keys are silently ignored. `send` failures
// (thrown, or a rejected promise) never propagate — this is a best-effort
// telemetry signal, never allowed to break the wizard.
export function makeStepReporter(send) {
  const sent = new Set();
  return function report(step) {
    if (typeof step !== "string" || !FUNNEL_STEPS.includes(step)) return;
    if (sent.has(step)) return;
    sent.add(step);
    try {
      const result = send(step);
      if (result && typeof result.catch === "function") {
        result.catch(() => {});
      }
    } catch {
      // fail open — telemetry must never break the wizard
    }
  };
}
