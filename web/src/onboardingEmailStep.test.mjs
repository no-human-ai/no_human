// Pins the wiring of the required email step INTO Onboarding.jsx: step order,
// the one client-side gate (continueBlocked), the Continue button staying
// textually unchanged, the advance()/guard() failure contract, and that the
// new step's markup uses only existing `ob-*` classes (DESIGN.md: no new CSS,
// no inline styles, no hex literals). Same static-source-sweep idiom as
// onboardingOffline.test.mjs / onboardingRepoPath.test.mjs — Onboarding.jsx
// isn't mountable here (no jsdom/React renderer in this suite), so the
// contract is pinned against the source text directly.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const src = readFileSync(here + "Onboarding.jsx", "utf8");

function stepKeysInOrder(source) {
  const m = source.match(/const BASE_STEPS = \[([\s\S]*?)\n\];/);
  assert.ok(m, "BASE_STEPS array not found");
  const body = m[1];
  const keys = [...body.matchAll(/key:\s*"([a-z]+)"/g)].map((x) => x[1]);
  return keys;
}

test("BASE_STEPS gains exactly one new required step, right after welcome", () => {
  const keys = stepKeysInOrder(src);
  assert.deepEqual(keys, ["welcome", "email", "repos", "projects", "integrations", "summary"]);
});

test("the email step is not skippable: no skip/remind-me-later affordance (button/link) in its block", () => {
  const block = src.match(/\{step\.key === "email" && \(([\s\S]*?)\n {10}\)\}/);
  assert.ok(block, "email step render block not found");
  const text = block[1];
  // No clickable affordance at all besides the input itself — no <button>,
  // no onClick, anywhere in this step's block. (Prose like "can't be
  // skipped" is fine; a *control* that lets you skip is not.)
  assert.ok(!/<button/i.test(text), "must not offer any button in the email step");
  assert.ok(!/onClick/.test(text), "must not offer any clickable affordance in the email step");
  assert.ok(!/remind me later/i.test(text));
});

test("the email step block uses only existing ob-* classes, no new CSS, no inline styles, no hex literals", () => {
  const block = src.match(/\{step\.key === "email" && \(([\s\S]*?)\n {10}\)\}/);
  assert.ok(block);
  const text = block[1];
  const ALLOWED = new Set(["ob-h2", "ob-note", "ob-row", "ob-input", "ob-btn", "ob-btn-ghost", "ob-faint", "ph-no-capture"]);
  const classAttrs = [...text.matchAll(/className="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(classAttrs.length > 0, "expected at least one className in the email step block");
  for (const attr of classAttrs) {
    for (const cls of attr.split(/\s+/)) {
      assert.ok(ALLOWED.has(cls), `unexpected class "${cls}" — DESIGN.md restricts this step to existing ob-* classes`);
    }
  }
  assert.ok(!/style=\{\{/.test(text), "no inline styles in the new block");
  assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(text), "no hex color literals in the new block");
  assert.ok(!/\brgb\(|\bhsl\(/.test(text));
});

test("the email input carries ph-no-capture (DOM masking) in addition to the network-layer exclusion", () => {
  const block = src.match(/\{step\.key === "email" && \(([\s\S]*?)\n {10}\)\}/)[1];
  assert.ok(/type="email"[\s\S]*?className="[^"]*ph-no-capture[^"]*"/.test(block) ||
    /className="[^"]*ph-no-capture[^"]*"[\s\S]*?type="email"/.test(block));
});

test("continueBlocked folds in the email gate beside the existing projects gate — one mechanism, not two", () => {
  assert.match(src, /const emailBlockMsg = step\.key === "email" \? emailBlocksContinue\(email\) : null;/);
  assert.match(src, /const continueBlocked = projectsBlockMsg !== null \|\| emailBlockMsg !== null;/);
});

test("the Continue button wiring is textually unchanged", () => {
  assert.match(
    src,
    /<button className="ob-btn" onClick=\{advance\} disabled=\{forwardDisabled\(navState\) \|\| continueBlocked\}>Continue<\/button>/,
  );
});

test("advance() runs the email submission through guard() and still calls next() unconditionally — no new catch, no lockout", () => {
  const m = src.match(/async function advance\(\) \{([\s\S]*?)\n  \}/);
  assert.ok(m, "advance() not found");
  const body = m[1];
  assert.match(body, /if \(step\.key === "email"\) \{[\s\S]*?await guard\(\(\) => submitEmail\(email, \{ registerOnboardingEmail \}\)\);[\s\S]*?\}/);
  // next() must still be the last call in the try block, unconditional —
  // exactly mirroring the pre-existing "repos" branch's shape.
  const tryBody = body.match(/try \{([\s\S]*?)\} finally/)[1];
  assert.ok(/next\(\);\s*$/.test(tryBody.trim()), "next() must be called unconditionally, last, in the try block");
});

test("guard()/noteFetchFailure are untouched: guard still returns nothing and swallows via setErr/setOffline only", () => {
  assert.match(
    src,
    /async function guard\(fn\) \{\s*setBusy\(true\); setErr\(null\);\s*try \{ await fn\(\); \} catch \(e\) \{ if \(!noteFetchFailure\(e\)\) setErr\(e\.message\); \} finally \{ setBusy\(false\); \}\s*\}/,
  );
});

test("no new error-display surface was added for the email step — the wizard's one ob-error banner is unchanged", () => {
  const occurrences = src.match(/ob-error/g) || [];
  // Exactly the pre-existing single {err && <div className="ob-error">...} surface.
  assert.equal(occurrences.length, 1, "expected exactly one ob-error usage, unchanged");
});

test("registerOnboardingEmail and onboardingEmail.js helpers are imported, existing imports untouched", () => {
  assert.match(src, /registerOnboardingEmail,?\s*\n\} from "\.\/api\.js";/);
  assert.match(src, /import \{ emailBlocksContinue, submitEmail, EMAIL_REJECT_MESSAGE \} from "\.\/onboardingEmail\.js";/);
});

// ── stepper-jump / minimal-skip bypass: both completion paths must register ──

test("finish() and startMinimal() both refuse to complete without a registered email", () => {
  const helper = src.match(/async function ensureEmailRegistered\(\) \{([\s\S]*?)\n  \}/);
  assert.ok(helper, "ensureEmailRegistered() not found");
  assert.match(helper[1], /if \(emailBlocksContinue\(email\) !== null\) throw new Error\(EMAIL_REJECT_MESSAGE\);/);
  assert.match(helper[1], /await submitEmail\(email, \{ registerOnboardingEmail \}\);/);

  const finishBody = src.slice(src.indexOf("async function finish()"), src.indexOf("\n  }\n", src.indexOf("async function finish()")));
  assert.match(finishBody, /await ensureEmailRegistered\(\);/, "finish() must require the address before launching");
  // Refused before anything is created, same as the unbound-projects check.
  assert.ok(finishBody.indexOf("ensureEmailRegistered()") < finishBody.indexOf("createProject("));

  const startMinimalBody = src.slice(src.indexOf("async function startMinimal()"), src.indexOf("\n  }\n", src.indexOf("async function startMinimal()")));
  assert.match(startMinimalBody, /await ensureEmailRegistered\(\);/, "startMinimal() must require the address too — it is a second completion path (the Repositories step's Skip-setup shortcut) that bypasses the Email step just like the stepper jump does");
  assert.ok(startMinimalBody.indexOf("ensureEmailRegistered()") < startMinimalBody.indexOf("completeOnboarding("));
});
