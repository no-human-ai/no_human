// Onboarding-funnel telemetry wiring inside Onboarding.jsx — previously ZERO
// coverage (per review: `step-viewed`/`auth/verify`/`recordOnboardingStep`/
// `verifyAuthLive` had no test anywhere in web/src/*.test.mjs or web/e2e/).
//
// Like onboardingWiring.test.mjs, `node --test` has no React renderer, so
// this reads the .jsx source rather than mounting the component. What it
// guards is the shape that makes the funnel data trustworthy rather than
// inflated or silently broken:
//   - each wizard step reports itself AT MOST ONCE per session (the `viewedSteps`
//     ref dedup) — without it, StrictMode's double-invoked effects or a
//     back-then-forward re-entry would double-count a step.
//   - the live-credential probe fires AT MOST ONCE per session, and ONLY at
//     the summary step — firing it on every step-render would spend the
//     operator's own credential quota repeatedly for no reason.
//   - both calls are fire-and-forget (`.catch(() => {})`) — a telemetry
//     hiccup must never surface as a wizard error or block advancing.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const onboardingJsx = readFileSync(here + "Onboarding.jsx", "utf8");

test("Onboarding imports both funnel-telemetry calls from api.js", () => {
  assert.match(
    onboardingJsx,
    /import\s*\{[^}]*\brecordOnboardingStep\b[^}]*\}\s*from\s*["']\.\/api\.js["']/,
    "recordOnboardingStep must be imported from api.js"
  );
  assert.match(
    onboardingJsx,
    /import\s*\{[^}]*\bverifyAuthLive\b[^}]*\}\s*from\s*["']\.\/api\.js["']/,
    "verifyAuthLive must be imported from api.js"
  );
});

test("a per-session dedup ref exists for both the step-viewed and auth-verify calls", () => {
  assert.match(
    onboardingJsx,
    /const\s+viewedSteps\s*=\s*useRef\(\s*new Set\(\)\s*\)/,
    "viewedSteps must be a ref (survives re-renders, not state) seeded with an empty Set"
  );
  assert.match(
    onboardingJsx,
    /const\s+authVerified\s*=\s*useRef\(\s*false\s*\)/,
    "authVerified must be a ref seeded false — a live probe fired from state would re-run on every render that toggles it"
  );
});

test("recordOnboardingStep fires once per step key, guarded by viewedSteps, and never throws into the wizard", () => {
  const effectMatch = onboardingJsx.match(
    /useEffect\(\(\)\s*=>\s*\{\s*if\s*\(viewedSteps\.current\.has\(step\.key\)\)[\s\S]*?\}\s*,\s*\[step\.key\]\)\s*;/
  );
  assert.ok(effectMatch, "the step-viewed effect (guarded on viewedSteps.current.has(step.key)) was not found");
  const block = effectMatch[0];
  assert.match(
    block,
    /viewedSteps\.current\.add\(step\.key\)/,
    "the step must be marked seen BEFORE (or regardless of) the call, so a slow/failed request can't re-fire on the next render"
  );
  assert.match(
    block,
    /recordOnboardingStep\(step\.key\)\.catch\(\(\)\s*=>\s*\{\s*\}\)/,
    "recordOnboardingStep must be called fire-and-forget — a rejection must be swallowed, not surfaced to the wizard"
  );
});

test("verifyAuthLive fires only at the summary step, only once per session, and never throws into the wizard", () => {
  const effectMatch = onboardingJsx.match(
    /useEffect\(\(\)\s*=>\s*\{\s*if\s*\(step\.key\s*!==\s*["']summary["']\)[\s\S]*?\}\s*,\s*\[step\.key\]\)\s*;/
  );
  assert.ok(effectMatch, "the summary-step auth-verify effect was not found");
  const block = effectMatch[0];
  assert.match(
    block,
    /if\s*\(authVerified\.current\)\s*return\s*;/,
    "a second visit to the summary step within the same session must not re-fire the probe"
  );
  assert.match(
    block,
    /authVerified\.current\s*=\s*true\s*;/,
    "the guard must be set before (or regardless of) the call, so a slow/failed request can't re-fire on the next render"
  );
  assert.match(
    block,
    /verifyAuthLive\(\)\.catch\(\(\)\s*=>\s*\{\s*\}\)/,
    "verifyAuthLive must be called fire-and-forget — a rejection must be swallowed, not surfaced to the wizard"
  );
});

test("the auth-verify probe is not wired to any step other than summary", () => {
  // Guards against a regression where verifyAuthLive gets called from some
  // OTHER effect/handler (e.g. on the repos or integrations step) in
  // addition to the summary-step one above — which would spend the
  // credential's quota far more than once per session.
  const calls = [...onboardingJsx.matchAll(/verifyAuthLive\(\)/g)];
  assert.equal(calls.length, 1, "verifyAuthLive() must be called from exactly one call site");
});

test("the step-viewed telemetry is not wired to any handler other than the per-render effect", () => {
  const calls = [...onboardingJsx.matchAll(/recordOnboardingStep\(/g)];
  assert.equal(calls.length, 1, "recordOnboardingStep(...) must be called from exactly one call site");
});
