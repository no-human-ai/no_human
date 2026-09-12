import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { FUNNEL_STEPS, makeStepReporter } from "./onboardingFunnel.js";

// Task c4873934 REFILE — the onboarding funnel telemetry blind spot. This
// file pins onboardingFunnel.js's pure once-per-step dedup logic (unit tests,
// real module import — no server needed) plus the wiring INTO Onboarding.jsx
// (source-grep tests, since `node --test` has no React renderer — see
// onboardingWiring.test.mjs for the established idiom this file follows).

const here = fileURLToPath(new URL(".", import.meta.url));
const onboardingJsx = readFileSync(here + "Onboarding.jsx", "utf8");

test("FUNNEL_STEPS is the fixed 5-step wizard order", () => {
  assert.deepEqual(FUNNEL_STEPS, [
    "welcome", "repos", "projects", "integrations", "summary",
  ]);
});

test("each distinct step reports exactly once, in order visited", () => {
  const sent = [];
  const report = makeStepReporter((step) => { sent.push(step); });
  report("welcome");
  report("repos");
  assert.deepEqual(sent, ["welcome", "repos"]);
});

test("StrictMode double-invoke of the same step sends only once", () => {
  // React 18 StrictMode intentionally mounts, then re-invokes the same
  // effect body a second time in development — this is exactly the shape
  // `makeStepReporter`'s internal Set (not a boolean) exists to survive.
  const sent = [];
  const report = makeStepReporter((step) => { sent.push(step); });
  report("welcome");
  report("welcome");
  assert.deepEqual(sent, ["welcome"]);
});

test("a forward/back/forward walk reports each step once, not per visit", () => {
  const sent = [];
  const report = makeStepReporter((step) => { sent.push(step); });
  const walk = ["welcome", "repos", "projects", "repos", "welcome", "projects"];
  for (const step of walk) report(step);
  assert.deepEqual(sent, ["welcome", "repos", "projects"]);
});

test("an off-list step key is never sent", () => {
  const sent = [];
  const report = makeStepReporter((step) => { sent.push(step); });
  report("consent");           // the removed usage-insights step
  report("");
  report("Welcome");           // case must match exactly
  report(undefined);
  assert.deepEqual(sent, []);
});

test("all five real steps each report once, in FUNNEL_STEPS order", () => {
  const sent = [];
  const report = makeStepReporter((step) => { sent.push(step); });
  for (const step of FUNNEL_STEPS) report(step);
  for (const step of FUNNEL_STEPS) report(step);  // revisit every step again
  assert.deepEqual(sent, FUNNEL_STEPS);
});

test("a throwing send() never propagates out of the reporter", () => {
  const report = makeStepReporter(() => { throw new Error("network is down"); });
  assert.doesNotThrow(() => report("welcome"));
});

test("a send() that returns a rejecting promise never produces an unhandled rejection", async () => {
  const report = makeStepReporter((step) => Promise.reject(new Error("fetch failed")));
  assert.doesNotThrow(() => report("welcome"));
  // Give the microtask queue a turn; if the reporter didn't attach .catch()
  // this would surface as an unhandledRejection on the process.
  await new Promise((r) => setImmediate(r));
});

test("send() is not called again for a step once already reported, even after send() throws", () => {
  let calls = 0;
  const report = makeStepReporter((step) => { calls += 1; throw new Error("boom"); });
  report("welcome");
  report("welcome");
  assert.equal(calls, 1);
});

// --------------------------------------------------------------------------
// Wiring into Onboarding.jsx — source-grep, per onboardingWiring.test.mjs's
// established idiom (readFileSync + regex, no component mounting).
// --------------------------------------------------------------------------

test("Onboarding.jsx imports makeStepReporter from onboardingFunnel.js", () => {
  assert.match(
    onboardingJsx,
    /import\s*\{\s*makeStepReporter\s*\}\s*from\s*"\.\/onboardingFunnel\.js"/,
    "Onboarding.jsx must import makeStepReporter from the pure funnel module"
  );
});

test("the step reporter is created once in a ref, not recreated every render", () => {
  assert.match(
    onboardingJsx,
    /stepReporter\s*=\s*useRef\(null\)/,
    "the reporter's dedup state must survive across renders via useRef"
  );
  assert.match(
    onboardingJsx,
    /stepReporter\.current\s*=\s*makeStepReporter\(recordOnboardingStep\)/,
    "the ref must be lazily initialized with makeStepReporter(recordOnboardingStep)"
  );
});

test("the reporting effect depends on [i] and reports the current step's key", () => {
  const effectMatch = onboardingJsx.match(
    /useEffect\(\(\)\s*=>\s*\{\s*stepReporter\.current\(STEPS\[i\]\.key\);\s*\}\s*,\s*\[i\]\)\s*;/
  );
  assert.ok(
    effectMatch,
    "expected a useEffect keyed on [i] calling stepReporter.current(STEPS[i].key)"
  );
});

test("the step key is passed as STEPS[i].key, never a template literal or free text", () => {
  assert.doesNotMatch(
    onboardingJsx,
    /stepReporter\.current\(`[^`]*\$\{/,
    "the reported step must never be built from a template literal"
  );
});

test("Onboarding.jsx imports recordOnboardingStep from api.js", () => {
  assert.match(
    onboardingJsx,
    /recordOnboardingStep/,
  );
  // Confirm it comes from the same api.js import block as the wizard's other
  // network calls, not a stray ad-hoc fetch.
  const importBlock = onboardingJsx.match(/import\s*\{[\s\S]*?\}\s*from\s*"\.\/api\.js";/);
  assert.ok(importBlock, "expected a single api.js import block");
  assert.match(importBlock[0], /recordOnboardingStep/);
});
