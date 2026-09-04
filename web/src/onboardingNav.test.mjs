import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { backDisabled, backDisabledReason, forwardDisabled, canJumpTo, stepButtonLabel } from "./onboardingNav.js";

// BUG (first external DMG tester): "the back button doesn't work" on the repos
// step of onboarding.
//
// Back was `disabled={i === 0 || busy}`, and `busy` is a single flag that
// Onboarding's `guard()` raises around EVERY awaited call in the wizard —
// including the background repository scan that fires the moment the repos step
// opens. So Back was not dead, it was disabled for the length of a scan the user
// never asked for, with nothing saying so. See onboardingNav.js for why leaving
// that scan is free (read-only GET, already re-runnable from the step's own
// "Re-scan" button) and why the terminal launch is the one call that keeps the
// lock.
//
// Two layers are guarded here, because either alone can be green while the bug is
// live: the predicates themselves, and the fact that Onboarding.jsx actually uses
// them. This project's `node --test` harness has no React renderer (see the note
// at the top of onboardingIntegrations.test.mjs), so the wiring half is a source
// assertion.

const LAST = 4; // STEPS.length - 1 in Onboarding.jsx (Docs step left the wizard 2026-09-04; AI-history/rules steps left 2026-08-30; team step left 2026-08-09); asserted against the real list below.

// ── the predicates ─────────────────────────────────────────────────────────

test("Back is live during a background scan on the repos step", () => {
  // index 2 === the repos step, busy === the discovery scan in flight.
  assert.equal(
    backDisabled({ index: 2, lastIndex: LAST, busy: true }),
    false,
    "Back must stay usable while the repo scan runs — that scan writes nothing",
  );
});

test("Back is live during every mid-wizard awaited call, not only the scan", () => {
  for (let index = 1; index < LAST; index++) {
    assert.equal(
      backDisabled({ index, lastIndex: LAST, busy: true }),
      false,
      `Back must stay usable on step ${index} while an abandonable call is in flight`,
    );
  }
});

test("Back is still off on the first step, where there is nowhere to go", () => {
  assert.equal(backDisabled({ index: 0, lastIndex: LAST, busy: false }), true);
  assert.equal(backDisabled({ index: 0, lastIndex: LAST, busy: true }), true);
});

test("Back is locked during the terminal launch, and says why", () => {
  const launching = { index: LAST, lastIndex: LAST, busy: true };
  assert.equal(backDisabled(launching), true,
    "the final launch writes config and completes onboarding — it is not abandonable");
  assert.match(backDisabledReason(launching), /can't be interrupted/i,
    "a disabled Back must explain itself rather than read as dead");
});

test("a Back that is merely on step 0 offers no reason (nothing to explain)", () => {
  assert.equal(backDisabledReason({ index: 0, lastIndex: LAST, busy: true }), "");
});

test("no reason is offered while Back is live — a tooltip on a working button is noise", () => {
  assert.equal(backDisabledReason({ index: 2, lastIndex: LAST, busy: true }), "");
  assert.equal(backDisabledReason({ index: LAST, lastIndex: LAST, busy: false }), "");
});

test("Continue never traps the user behind a background call", () => {
  // The trap this closes: if Back leaves the repos step mid-scan but Continue is
  // still gated on `busy`, the user cannot get back to the step they just left.
  for (let index = 0; index < LAST; index++) {
    assert.equal(
      forwardDisabled({ index, lastIndex: LAST, busy: true }),
      false,
      `Continue must stay usable on step ${index} while an abandonable call runs`,
    );
  }
});

test("the launch button keeps its lock while launching (unchanged behaviour)", () => {
  assert.equal(forwardDisabled({ index: LAST, lastIndex: LAST, busy: true }), true);
  assert.equal(forwardDisabled({ index: LAST, lastIndex: LAST, busy: false }), false);
});

// ── clickable step indicator (spec §3 B2) ──────────────────────────────────

test("any step is reachable from any other — no step gates another", () => {
  assert.equal(canJumpTo({ from: 6, to: 1, busy: false }), true);
  assert.equal(canJumpTo({ from: 0, to: LAST, busy: false }), true);
});

test("a jump is refused only while an awaited call is in flight", () => {
  assert.equal(canJumpTo({ from: 6, to: 1, busy: true }), false);
});

test("the step button's accessible name carries title, position and state", () => {
  const step = { key: "projects", title: "Projects" };
  assert.equal(stepButtonLabel(step, 2, 0, 8), "Projects, step 3 of 8, not started");
  assert.equal(stepButtonLabel(step, 2, 2, 8), "Projects, step 3 of 8, current");
  assert.equal(stepButtonLabel(step, 2, 5, 8), "Projects, step 3 of 8, completed");
});

// ── the wiring: the predicates are what the buttons actually use ────────────

const here = fileURLToPath(new URL(".", import.meta.url));
const src = readFileSync(here + "Onboarding.jsx", "utf8");

const NAV = (() => {
  const start = src.indexOf('<div className="ob-nav">');
  assert.ok(start > 0, "the wizard's .ob-nav block must exist");
  const end = src.indexOf("</div>\n      </div>", start);
  assert.ok(end > start, "could not bound the .ob-nav block");
  return src.slice(start, end);
})();

test("the wizard's STEPS list really has lastIndex 4, so these cases are the real ones", () => {
  const steps = [...src.matchAll(/\{ key: "\w+",\s+title:/g)];
  assert.equal(steps.length - 1, LAST,
    "STEPS changed length — update LAST in this test so the launch case still tests the LAST step");
});

test("the step indicator renders BUTTONS that jump via setI, gated on canJumpTo", () => {
  // Regression guard: the indicator was <div>s that only reflected state. spec §3
  // B2 makes each a real button so a user can jump instead of clicking Back.
  assert.match(src, /className=\{`ob-step[\s\S]*?onClick=\{\(\) => \{ if \(canJumpTo\(/,
    "each step must be a button whose onClick jumps only when canJumpTo allows it");
  assert.match(src, /aria-label=\{stepButtonLabel\(s, idx, i, STEPS\.length\)\}/,
    "the button's accessible name must come from the tested label helper");
  assert.match(src, /onKeyDown=\{onStepKeyDown\}/,
    "the stepper must handle ArrowLeft/ArrowRight for roving focus");
});

test("Back's disabled rule comes from backDisabled, not from a raw `busy`", () => {
  assert.match(NAV, /disabled=\{backDisabled\(navState\)\}/,
    "the Back button must delegate to the tested predicate");
  assert.doesNotMatch(NAV, /disabled=\{i === 0 \|\| busy\}/,
    "the original defect: Back gated on the wizard-wide busy flag");
});

test("Back carries the explanation when it is locked", () => {
  assert.match(NAV, /title=\{backDisabledReason\(navState\)/,
    "a locked Back must be able to say why");
});

test("the explanation is RENDERED, not only a title= nobody can see", () => {
  // Browsers suppress the native tooltip on a disabled control, so a title
  // attribute alone would be an explanation that never reaches anyone.
  assert.match(
    NAV,
    /\{backDisabledReason\(navState\) && \(\s*<span className="ob-nav-note"[\s\S]*?\{backDisabledReason\(navState\)\}<\/span>/,
    "the reason must be rendered on the page next to the locked Back button",
  );
  const css = readFileSync(here + "styles.css", "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  assert.match(css, /\n\.ob-nav-note\s*\{/,
    "the rendered reason needs a style, or it lands unstyled in the nav row");
});

test("both forward controls delegate to forwardDisabled", () => {
  // The launch button uses forwardDisabled alone; Continue additionally carries
  // the Projects-step gate (spec §3 B2: `|| continueBlocked`) — but it still
  // delegates to the tested predicate, never to a raw `busy`.
  assert.match(NAV, /disabled=\{forwardDisabled\(navState\)\}>\s*\n?\s*\{busy \? "Launching…"/,
    "the terminal launch button uses forwardDisabled alone");
  assert.match(NAV, /disabled=\{forwardDisabled\(navState\) \|\| continueBlocked\}>Continue<\/button>/,
    "Continue delegates to forwardDisabled and the tested projects gate");
  assert.doesNotMatch(NAV, /onClick=\{next\} disabled=\{busy\}/,
    "Continue must not be gated on the wizard-wide busy flag either");
});

test("navState feeds the predicates the wizard's real step bounds", () => {
  assert.match(
    src,
    /const navState = \{ index: i, lastIndex: STEPS\.length - 1, busy \};/,
    "navState must be derived from the live step index and STEPS length",
  );
});
