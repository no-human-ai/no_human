import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  TELEMETRY_CONSENT_QUESTION,
  CONSENT_YES_LABEL,
  CONSENT_NO_LABEL,
  shouldAskTelemetry,
  submitConsent,
} from "./onboardingConsent.js";
import * as consent from "./onboardingConsent.js";

// Onboarding never asked about telemetry, so the toggle sat buried in Settings
// and no real install ever showed up in a replay. This is the one-time consent
// step added after the existing 8 onboarding steps; usage insights default ON
// (opt-out), so the toggle arrives pre-set to Yes and an explicit No is
// persisted. No new write path: `submitConsent` only ever calls the
// `saveTelemetryConsent` it is handed (the existing PUT /api/telemetry/consent
// wrapper in api.js). As with
// onboardingNav.test.mjs, `node --test` has no React renderer, so the decision
// logic lives in this plain module and the JSX wiring is checked by reading
// Onboarding.jsx's source text (the ruleTextFull.test.mjs idiom).

// ── the decision logic ──────────────────────────────────────────────────────

test("the step not shown (null) writes nothing and leaves asked true", async () => {
  // null == the install was already asked before, so the insights step is not
  // rendered — nothing to record, and never re-nag.
  let called = false;
  const saveTelemetryConsent = async () => { called = true; };
  const result = await submitConsent(null, { saveTelemetryConsent });
  assert.equal(called, false, "a not-shown step must never call the consent endpoint");
  assert.equal(result.called, false);
  assert.equal(result.enabled, false);
  assert.equal(result.telemetryAsked, true, "still counts as asked — it must not re-nag");
});

test("an explicit No persists the opt-out (default is now ON) and marks asked", async () => {
  // Insights default ON: silence would leave telemetry enabled, so an explicit
  // No MUST be written to the server as false.
  const calls = [];
  const saveTelemetryConsent = async (enabled) => { calls.push(enabled); };
  const result = await submitConsent(false, { saveTelemetryConsent });
  assert.deepEqual(calls, [false], "No must persist the opt-out via the existing wrapper");
  assert.equal(result.enabled, false);
  assert.equal(result.called, true);
  assert.equal(result.telemetryAsked, true);
});

test("Yes calls the existing consent endpoint exactly once, with true", async () => {
  const calls = [];
  const saveTelemetryConsent = async (enabled) => { calls.push(enabled); };
  const result = await submitConsent(true, { saveTelemetryConsent });
  assert.deepEqual(calls, [true], "must reuse the existing wrapper, not a new write path");
  assert.equal(result.enabled, true);
  assert.equal(result.telemetryAsked, true);
});

test("a consent failure does not block the launch, and does not lose the Yes forever", async () => {
  const saveTelemetryConsent = async () => { throw new Error("network down"); };
  let errored;
  const result = await submitConsent(true, {
    saveTelemetryConsent,
    onError: (e) => { errored = e; },
  });
  assert.equal(result.enabled, false);
  assert.ok(errored, "the failure must be surfaced, not swallowed silently");
  // The write failed (a possibly-transient fault) — it must NOT be recorded
  // as "asked". If it were, shouldAskTelemetry() would return false forever:
  // the user said Yes, telemetry stayed off, and they would never be offered
  // the choice again. Leaving telemetryAsked false here means the complete
  // payload omits the key, so the next launch asks again instead of silently
  // losing the Yes (see onboarding_complete's sticky patch in app.py).
  assert.equal(result.telemetryAsked, false, "a failed write must not be recorded as asked — re-ask next launch");
});

test("shouldAskTelemetry re-asks after a failed Yes, since it was never actually recorded", () => {
  // Mirrors the shape onboarding_complete persists when telemetryAsked was
  // false: the "telemetry_asked" key is simply absent from onboarding state.
  assert.equal(shouldAskTelemetry({ completed: true }), true);
});

test("an install that was already asked never sees the step again", () => {
  assert.equal(shouldAskTelemetry({ telemetry_asked: true }), false);
  assert.equal(shouldAskTelemetry({ completed: true, telemetry_asked: true }), false);
});

test("an install that has never been asked sees the step", () => {
  assert.equal(shouldAskTelemetry({ completed: true }), true);
  assert.equal(shouldAskTelemetry({ completed: false }), true);
  assert.equal(shouldAskTelemetry(null), true);
  assert.equal(shouldAskTelemetry(undefined), true);
});

// ── the copy: byte-identical twin of config.py's contract ──────────────────
// tests/test_telemetry.py pins the Python side of this same equality, so a
// comment edit there and a copy edit here cannot silently part ways.

test("the question names exactly what is collected and nothing more", () => {
  assert.match(TELEMETRY_CONSENT_QUESTION, /anonymous usage events/);
  assert.match(TELEMETRY_CONSENT_QUESTION, /masked screen recordings/);
  assert.match(
    TELEMETRY_CONSENT_QUESTION,
    /never code, prompts, titles, paths or tokens/,
    "must mirror config.py's own telemetry-block comment, not a paraphrase",
  );
});

test("the removed Settings-hint constant does not creep back", () => {
  // TELEMETRY_CONSENT_SETTINGS_HINT ("Settings > Usage insights") was removed
  // with the onboarding step + Settings pane it named (operator, 2026-08-26);
  // config.yaml `telemetry.enabled: false` is the opt-out. It must not return.
  assert.equal(consent.TELEMETRY_CONSENT_SETTINGS_HINT, undefined);
});

test("no dark patterns in the button copy", () => {
  // Plainly-worded yes/no — not "Accept"/"Maybe later", not a checkbox label.
  assert.equal(CONSENT_NO_LABEL, "No");
  assert.doesNotMatch(CONSENT_YES_LABEL, /accept|agree|allow all/i);
});

// ── the wiring: the consent STEP is REMOVED from the wizard (2026-08-26) ────
// Telemetry is on by default and no longer asked about. The onboardingConsent.js
// MODULE is retained — its copy constants are the byte-identical twin of
// config.py's, pinned by tests/test_telemetry.py, and the privacy policy still
// names usage insights — but Onboarding.jsx must render no step, ask no consent,
// and mention usage insights nowhere. These tests guard that the step stays out.

const here = fileURLToPath(new URL(".", import.meta.url));
const jsx = readFileSync(here + "Onboarding.jsx", "utf8");
const api = readFileSync(here + "api.js", "utf8");

test("the 5 base steps are untouched and nothing is appended after summary", () => {
  const base = jsx.match(/const BASE_STEPS = \[([\s\S]*?)\n\];/);
  assert.ok(base, "the base-step list must still exist as its own array");
  const keys = [...base[1].matchAll(/key: "(\w+)"/g)].map((m) => m[1]);
  assert.deepEqual(
    keys,
    // "history" + "rules" (the AI-learnings walk) left the wizard 2026-08-30 —
    // that work now lives in Settings, nudged by the Settings "!" badge.
    // "docs" (Repo docs & wiki) left the wizard 2026-09-04 — the wiki is now
    // enqueued automatically in the background at Launch.
    ["welcome", "repos", "projects", "integrations", "summary"],
    "the existing steps must not be reordered or renamed",
  );
  assert.match(jsx, /const STEPS = BASE_STEPS;/,
    "STEPS must be exactly BASE_STEPS — no conditional insights append");
  assert.doesNotMatch(jsx, /INSIGHTS_STEP/, "the INSIGHTS_STEP literal must be gone");
});

test("Onboarding takes no askTelemetry prop and holds no consent state", () => {
  assert.match(
    jsx,
    /export default function Onboarding\(\{ onComplete \}\)/,
    "Onboarding must take only onComplete — the askTelemetry gate is gone",
  );
  assert.doesNotMatch(jsx, /const \[consent, setConsent\]/,
    "the consent state belonged to the removed step and must not remain");
});

test("no usage-insights step or copy is rendered anywhere in the wizard", () => {
  assert.doesNotMatch(jsx, /step\.key === "insights"/, "no insights step block may render");
  assert.doesNotMatch(jsx, /Usage insights/, "the wizard UI must not mention Usage insights");
  assert.doesNotMatch(jsx, /TELEMETRY_CONSENT_QUESTION/, "the consent question must not be printed");
  assert.doesNotMatch(jsx, /CONSENT_YES_LABEL|CONSENT_NO_LABEL/, "the Yes/No consent buttons must be gone");
});

test("the wizard no longer imports or calls the consent write path", () => {
  assert.doesNotMatch(jsx, /submitConsent/, "the removed step's submitConsent call must be gone");
  assert.doesNotMatch(jsx, /saveTelemetryConsent/, "the wizard must not call the consent wrapper anymore");
  // The endpoint itself is untouched — still defined once in api.js for the
  // retained Settings-side / hosted uses, never duplicated.
  const hits = [...api.matchAll(/\/api\/telemetry\/consent/g)];
  assert.equal(hits.length, 1, "api.js must still define the consent endpoint exactly once");
});

test("finish() completes onboarding without a telemetry_asked field", () => {
  const start = jsx.indexOf("async function finish()");
  assert.ok(start > 0);
  const end = jsx.indexOf("\n  }\n", start);
  const body = jsx.slice(start, end);
  assert.match(body, /completeOnboarding\(/, "finish() must still complete onboarding");
  assert.doesNotMatch(body, /telemetry_asked/,
    "no telemetry_asked is written — the wizard never asks, so it never records an answer");
  assert.doesNotMatch(body, /submitConsent/, "finish() must not resolve any consent");
});

// Regression guard for the AI-learnings step removal (2026-08-30). The build
// and the .mjs suite BOTH pass on a dangling reference to deleted state — a
// bare undeclared identifier is a runtime ReferenceError, not a build error,
// and these tests read source rather than rendering the summary step. A real
// user hit exactly that: `chosenRules.size` survived in the "Launch" summary
// after `const [chosenRules] = useState(...)` was deleted, crashing the final
// step. This asserts none of the removed AI-learnings symbols remain in CODE
// (comments are stripped first — one mentions two of them by name on purpose).
test("no removed AI-learnings symbol is still referenced in Onboarding.jsx code", () => {
  const code = jsx
    .replace(/\/\*[\s\S]*?\*\//g, "")   // block comments
    .replace(/^\s*\/\/.*$/gm, "");      // line comments
  const removed = [
    "chosenRules", "proposals", "scanHistory", "toggleRule", "scanPhase",
    "extractHistory", "analyzeHistory", "confirmRules", "scanSummary",
    "groupProposalsByProject", "historyPromiseRef",
  ];
  const survivors = removed.filter((s) => new RegExp(`\\b${s}\\b`).test(code));
  assert.deepEqual(
    survivors, [],
    `these symbols were deleted with the AI-learnings steps but are still `
    + `referenced in code (a ReferenceError at runtime): ${survivors.join(", ")}`,
  );
});
