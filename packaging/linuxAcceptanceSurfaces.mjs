// Pure step table + assert-then-screenshot walker for Lane A of Linux
// acceptance (packaging/linux-acceptance.mjs), split out so it can be driven
// by a fake `page` in desktop/linuxAcceptanceSurfaces.test.mjs without ever
// importing Electron/Playwright here.
//
// The defect this fixes: the driver used to screenshot "02-board.png" right
// after the credential save, which is really onboarding step 1 of 7 (the
// WELCOME step) — a screenshot of the wrong screen, named as if it were the
// right one. The fix is assert-then-screenshot: a filename is only ever
// written once a DOM proof for what it claims to show has been observed, so
// a screenshot can never again outrun what it names.
//
// web/src/Onboarding.jsx / onboardingNav.js are the source of truth for the
// selectors below — see stepButtonLabel() for the stepper's accessible name
// and BASE_STEPS for the 7-step title list (Welcome/Email/Repositories/
// Projects/Integrations/Community/Launch).

// RFC 2606 reserved .invalid TLD — guaranteed undeliverable, never a real
// address. Email registration is not wired to any real delivery in this repo
// (src/no_human/email/send.py) and Onboarding.jsx's guard() never rethrows,
// so this is safe to submit in CI.
export const EMAIL = "linux-acceptance@nohuman.invalid";

// Everything the wizard walk needs, expressed as plain selectors so both the
// real Playwright `page` and a fake duck-typed `page` in tests can resolve
// them the same way, through one method: `page.locator(selector)`.
export const WIZARD = Object.freeze({
  emailSelector: 'input[placeholder="you@example.com"]',
  // The one primary "Continue" button rendered on every non-final step
  // (Onboarding.jsx: `<button className="ob-btn" onClick={advance} …>Continue</button>`).
  continueSelector: 'button.ob-btn:text-is("Continue")',
  // The terminal launch control — "Enter no_human" or, when a repo was
  // proven ready, "Create your first task in <repo>". Both variants carry the
  // same `ob-btn-go` class, unique to the final step, so matching on the
  // class avoids depending on which of the two dynamic labels renders.
  finishSelector: "button.ob-btn-go",
  // The current step's accessible-name landing spot (Onboarding.jsx:
  // `aria-label={\`Step ${i + 1} of ${STEPS.length}: ${step.title}\`}`) — read
  // only for the "stalled on step N" diagnosis, never asserted on.
  stepLabelSelector: '[role="group"][aria-label^="Step "]',
  // The step-1 (Welcome) stepper button's accessible name (onboardingNav.js
  // stepButtonLabel): "Welcome, step 1 of 7, <state>". Prefix-matched so it is
  // found regardless of state (current/completed/not started) — this is the
  // screenshot the reported defect actually produced, and the thing that must
  // be GONE once the board is reached.
  entryStepperSelector: 'button[aria-label^="Welcome, step 1 of 7"]',
  // Generous bound: 7 steps, at most one hop per step plus slack for a
  // disabled-Continue retry on the email step.
  maxHops: 12,
});

// One entry per screenshot the driver writes, in the order it writes them.
// `proof` must all be visible, `absent` must all be gone, before `file` is
// written — see verifySurface()/walkSurfaces() below. `open`/`close` are click
// descriptors, not functions, so this table stays plain, introspectable data.
export const SURFACES = Object.freeze([
  {
    key: "credential-screen",
    file: "01-credential-screen.png",
    label: "the credential screen (token.html) on first run",
    open: null,
    proof: [
      { selector: "#token", desc: "credential input" },
      { selector: "#save", desc: "save button" },
    ],
    absent: [],
    close: null,
  },
  {
    key: "onboarding-welcome",
    file: "02-onboarding-welcome.png",
    label: "the onboarding wizard's Welcome step (step 1 of 7)",
    open: null, // already on screen — the wizard opens here right after save
    proof: [
      { selector: WIZARD.entryStepperSelector, desc: "step-1 stepper (Welcome)" },
    ],
    absent: [],
    close: null,
  },
  {
    key: "board-first-run",
    file: "03-board-first-run.png",
    label: "the task board with zero tasks, reached past onboarding",
    open: null, // the wizard's finish() lands here; nothing to click
    proof: [
      { selector: 'nav.nh-sidenav[aria-label="Primary"]', desc: "primary sidebar" },
      { selector: "#first-run-title", desc: "first-run board heading" },
    ],
    // The regression guard for the reported defect: if the wizard's step-1
    // stepper is still on screen, this is NOT the board — it is exactly the
    // defect being fixed, and this must fail rather than screenshot it.
    absent: [
      { selector: WIZARD.entryStepperSelector, desc: "step-1 stepper (must be gone once onboarding is complete)" },
    ],
    close: null,
  },
  {
    key: "settings",
    file: "04-settings.png",
    label: "the Settings overlay",
    open: { selector: 'button.nh-navrow:text-is("Settings")' },
    proof: [
      { selector: '[role="dialog"][aria-labelledby="settings-overlay-title"]', desc: "settings dialog" },
    ],
    absent: [],
    close: {
      selector: '[aria-label="Close settings"]',
      awaitHidden: '[role="dialog"][aria-labelledby="settings-overlay-title"]',
    },
  },
  {
    key: "stats",
    file: "05-stats.png",
    label: "the Stats page",
    open: { selector: 'button.nh-navrow:text-is("Stats")' },
    proof: [
      { selector: ".stats-page", desc: "stats page container" },
      // .stats-page alone could in principle render behind a still-open
      // overlay; the nav row actually switching to aria-current proves page
      // state changed, not just that the element exists somewhere in the DOM.
      { selector: 'button.nh-navrow[aria-current="page"]:text-is("Stats")', desc: "Stats nav row marked current" },
    ],
    absent: [],
    close: null,
  },
]);

async function locatorVisible(page, selector, timeout) {
  await page.locator(selector).waitFor({ state: "visible", timeout });
}

async function locatorHidden(page, selector, timeout) {
  await page.locator(selector).waitFor({ state: "hidden", timeout });
}

async function surfaceError(page, surface, entry, reason) {
  const body = await page.textContent("body").catch(() => "");
  return new Error(
    `surface "${surface.key}" (${surface.label}): "${entry.desc}" [${entry.selector}] ${reason}\n`
    + `page text:\n${(body || "").trim().slice(0, 2000)}`
  );
}

/** Runs `surface.open` if present, then asserts every `proof` selector is
 * visible and every `absent` selector is gone. Never takes a screenshot. */
export async function verifySurface(page, surface, { timeout = 15000 } = {}) {
  if (surface.open) {
    await page.locator(surface.open.selector).click();
  }
  for (const p of surface.proof) {
    try {
      await locatorVisible(page, p.selector, timeout);
    } catch {
      throw await surfaceError(page, surface, p, "did not become visible");
    }
  }
  for (const a of surface.absent || []) {
    try {
      await locatorHidden(page, a.selector, timeout);
    } catch {
      throw await surfaceError(page, surface, a, "was still present (expected gone)");
    }
  }
}

/** Walks `surfaces` in order: verify, THEN screenshot via `shot(file)`, then
 * `close` if the surface declares one. Returns the ordered list of files
 * written — a screenshot is written only for a surface whose proof passed. */
export async function walkSurfaces(page, surfaces, { shot, timeout } = {}) {
  const written = [];
  for (const surface of surfaces) {
    await verifySurface(page, surface, { timeout });
    await shot(surface.file);
    written.push(surface.file);
    if (surface.close) {
      await page.locator(surface.close.selector).click();
      if (surface.close.awaitHidden) {
        await locatorHidden(page, surface.close.awaitHidden, timeout);
      }
    }
  }
  return written;
}

const defaultWait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Bounded hop loop past the onboarding wizard: fills the (required, gating)
 * email field the moment it is visible, clicks the terminal launch control
 * the moment IT is visible, otherwise clicks Continue. Never clicks Continue
 * while the email field is unfilled and visible — Continue is disabled by
 * `continueBlocked` until the address is well-formed, and Playwright's click
 * waits out its own actionability timeout on a disabled control, which is
 * exactly the stall this ordering avoids. */
export async function advanceThroughWizard(page, { timeout = 15000, hopDelayMs = 150, wait = defaultWait } = {}) {
  for (let hop = 0; hop < WIZARD.maxHops; hop++) {
    const finish = page.locator(WIZARD.finishSelector);
    if (await finish.isVisible().catch(() => false)) {
      await finish.click();
      return;
    }
    const email = page.locator(WIZARD.emailSelector);
    if (await email.isVisible().catch(() => false)) {
      await email.fill(EMAIL);
      await wait(hopDelayMs);
    }
    await page.locator(WIZARD.continueSelector).click();
    await wait(hopDelayMs);
  }
  const label = await page.locator(WIZARD.stepLabelSelector).first().getAttribute("aria-label").catch(() => null);
  throw new Error(
    `advanceThroughWizard: stalled on step: ${label || "unknown"} — no forward control appeared within ${WIZARD.maxHops} hops`
  );
}
