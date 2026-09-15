// Pure, testable step table for packaging/linux-acceptance.mjs (docs/LINUX.md
// §4/§6). The bug this fixes: the driver named a screenshot "02-board.png" that
// was, in fact, step 1 of 7 of the onboarding wizard — a screenshot alone never
// proved what it claimed. Everything here is duck-typed over a Playwright-shaped
// `page` (getByRole/locator/getByPlaceholder, each returning a locator with
// isVisible/waitFor/click/fill/textContent/getAttribute) so `desktop/` unit
// tests can drive it against a fake without a Playwright dependency, and the
// driver runs the very same functions against the real Electron window.
//
// The one rule that makes a filename trustworthy: PROVE, then SCREENSHOT.
// verifySurface() never takes a screenshot; walkSurfaces() only calls its
// `shot` callback after verifySurface() has resolved. A surface whose proof
// never appears can never have its filename written — that ordering, not
// prose, is what makes 03-board-first-run.png retire 02-board.png's defect.

// RFC 2606 reserved TLD — never resolves, never delivers, safe to hardcode in
// a file that ends up in CI logs.
export const EMAIL = "linux-acceptance@nohuman.invalid";

// The onboarding walk (web/src/Onboarding.jsx). Steps: welcome, email, repos,
// projects, integrations, discord, summary (STEPS in Onboarding.jsx) — titled
// Welcome / Email / Repositories / Projects / Integrations / Community /
// Launch. Continue is disabled until the email step holds a well-formed
// address (onboardingEmail.js), so a bare click would sit on Playwright's 30s
// actionability wait — the trap web/e2e/onboarding-minimal-path.mjs already
// documents. With a throwaway HOME (zero discovered repos, zero projects)
// nothing else gates Continue, and finish() completes onboarding without
// changing the URL.
export const WIZARD = {
  emailField: { placeholder: "you@example.com" },
  continueName: /^Continue$/,
  // "Enter no_human" with zero repos, or "Create your first task in <repo>"
  // if a repo were ever bound — Onboarding.jsx's finish button label.
  finishName: /^Enter no_human$|^Create your first task in /,
  maxHops: 12,
};

// One entry per screenshot the driver writes, in the order it writes them.
// `proof`/`absent` are locator descriptors (see resolveLocator below);
// `open`/`close` are single descriptors clicked before/after the surface is
// verified. A filename can only ever be written for a surface whose `proof`
// held true.
export const SURFACES = [
  {
    key: "credential-screen",
    file: "01-credential-screen.png",
    open: null,
    proof: [{ locator: "#token" }, { locator: "#save" }],
    absent: [],
    label: "the credential screen (token.html) on first run",
  },
  {
    key: "onboarding-welcome",
    file: "02-onboarding-welcome.png",
    open: null,
    // role="group" aria-label="Step 1 of 7: Welcome" (Onboarding.jsx) — this
    // is the exact element the reported defect screenshotted and mislabeled.
    proof: [{ role: "group", name: /^Step 1 of \d+: Welcome$/ }],
    absent: [],
    label: "the onboarding wizard's Welcome step (step 1 of 7) — NOT the board",
  },
  {
    key: "board-first-run",
    file: "03-board-first-run.png",
    open: null,
    proof: [
      { locator: 'nav.nh-sidenav[aria-label="Primary"]' },
      { locator: "#first-run-title", text: /Your first task/ },
    ],
    // The negative assertion that makes the reported defect impossible to
    // reproduce green: the board surface cannot pass while any wizard step
    // group is still on screen.
    absent: [{ role: "group", name: /^Step \d+ of \d+:/ }],
    label: "the task board reached after onboarding completes (zero tasks, first-run empty state)",
  },
  {
    key: "settings",
    file: "04-settings.png",
    // Role + accessible name, not `button.nh-navrow:text-is("Settings")` — the
    // row's full text content is icon + "Settings" (App.jsx NavRow renders an
    // aria-hidden icon span alongside the label span), so a full-text-content
    // (equality) selector can never match. The accessible name excludes the
    // aria-hidden icon, which is why web/e2e/live-flows.mjs, mobile-nav.mjs,
    // models-pane.mjs, and replay-body-leak.mjs click it as
    // getByRole("button", { name: /^Settings$/ }). Two other walks
    // (web/e2e/form-order.mjs:176, web/e2e/sweep.mjs:91) instead use
    // Playwright's `text=Settings` engine, a substring/contains match rather
    // than an equality match — it still finds the row despite the icon span
    // for the same underlying reason (it isn't comparing full text content
    // for equality), but it is not getByRole.
    open: { role: "button", name: /^Settings$/ },
    proof: [{ locator: '[role="dialog"][aria-labelledby="settings-overlay-title"]' }],
    absent: [],
    close: { locator: '[aria-label="Close settings"]' },
    label: "the Settings overlay",
  },
  {
    key: "stats",
    file: "05-stats.png",
    open: { role: "button", name: /^Stats$/ },
    proof: [
      { locator: ".stats-page" },
      // .stats-page alone could in principle render behind an overlay; the
      // Stats nav row's aria-current proves the page state actually switched
      // (App.jsx NavRow sets aria-current="page" only for page-nav rows).
      { role: "button", name: /^Stats$/, attr: { name: "aria-current", value: "page" } },
    ],
    absent: [],
    label: "the Stats page",
  },
];

// The exact set the driver walks after the wizard completes. Named and
// exported — rather than the driver re-deriving `SURFACES.slice(2)` inline —
// so a regression in either the table's order/length OR the driver's call
// site is caught by an executable assertion instead of by prose review.
export const POST_WIZARD_SURFACES = SURFACES.slice(2);

export const SCREENSHOT_FILES = SURFACES.map((s) => s.file);

function describeLocator(descriptor) {
  if (descriptor.role) return `role=${descriptor.role} name=${descriptor.name}`;
  return descriptor.locator;
}

function resolveLocator(page, descriptor) {
  if (descriptor.role) {
    return page.getByRole(descriptor.role, descriptor.name ? { name: descriptor.name } : undefined);
  }
  return page.locator(descriptor.locator);
}

async function checkProof(page, descriptor, timeout) {
  const loc = resolveLocator(page, descriptor);
  await loc.waitFor({ state: "visible", timeout });
  if (descriptor.text) {
    const content = await loc.textContent();
    if (!descriptor.text.test(content || "")) {
      throw new Error(`expected text matching ${descriptor.text} at ${describeLocator(descriptor)}, got ${JSON.stringify(content)}`);
    }
  }
  if (descriptor.attr) {
    const val = await loc.getAttribute(descriptor.attr.name);
    if (val !== descriptor.attr.value) {
      throw new Error(`expected ${descriptor.attr.name}="${descriptor.attr.value}" at ${describeLocator(descriptor)}, got ${JSON.stringify(val)}`);
    }
  }
}

async function checkAbsent(page, descriptor, timeout) {
  const loc = resolveLocator(page, descriptor);
  await loc.waitFor({ state: "hidden", timeout });
}

/**
 * Assert every proof/absent descriptor for `surface`, clicking `surface.open`
 * first if present. Never takes a screenshot — that is walkSurfaces()'s job,
 * and only after this resolves. Throws naming the surface key, its label, the
 * failing selector, and (best-effort) a slice of the page's body text — the
 * same diagnosis pattern packaging/linux-acceptance.mjs already uses for the
 * credential-screen check.
 */
export async function verifySurface(page, surface, opts = {}) {
  const timeout = opts.timeout ?? 15000;
  try {
    if (surface.open) {
      await resolveLocator(page, surface.open).click();
    }
    for (const descriptor of surface.proof || []) {
      await checkProof(page, descriptor, timeout);
    }
    for (const descriptor of surface.absent || []) {
      await checkAbsent(page, descriptor, timeout);
    }
  } catch (e) {
    const body = await page.locator("body").textContent().catch(() => "");
    const msg = e && e.message ? e.message : String(e);
    throw new Error(`surface "${surface.key}" (${surface.label}) failed: ${msg}\n`
      + `page text:\n${(body || "").trim().slice(0, 2000)}`);
  }
}

/**
 * Walk `surfaces` in order: verify, then screenshot via `opts.shot(file)`,
 * then close (if the surface declares one). Returns the ordered list of
 * filenames written. A screenshot for surfaces[i] is only ever written after
 * verifySurface(surfaces[i]) resolves — this ordering IS the filename-honesty
 * mechanism.
 */
export async function walkSurfaces(page, surfaces, opts = {}) {
  const timeout = opts.timeout ?? 15000;
  const written = [];
  for (const surface of surfaces) {
    await verifySurface(page, surface, { timeout });
    if (opts.shot) await opts.shot(surface.file);
    written.push(surface.file);
    if (surface.close) {
      const closeLoc = resolveLocator(page, surface.close);
      await closeLoc.click();
      await closeLoc.waitFor({ state: "hidden", timeout }).catch(() => {});
    }
  }
  return written;
}

/**
 * The whole post-credential walk: (in "setup" mode only) prove and screenshot
 * the wizard's Welcome step, then drive the wizard to completion; then, after
 * `opts.betweenWizardAndSurfaces()` (the driver's non-page checks — GET
 * /api/tasks, the nh-in-shell class, the reaped/live process list, the
 * credential-file/db-file checks — which must run between "the wizard
 * finished" and "walk Settings/Stats" to match the CI-green run this
 * function was extracted from), walk POST_WIZARD_SURFACES (board, Settings,
 * Stats).
 *
 * Extracted out of packaging/linux-acceptance.mjs so this composition — not
 * just verifySurface/walkSurfaces/advanceThroughWizard individually — is
 * exercised by a unit test against a fake page. Ends with a FAIL-CLOSED
 * check: the list of files actually written is compared against a list
 * derived independently from the untouched, module-level SURFACES /
 * POST_WIZARD_SURFACES tables (never from whatever a mutated call above
 * happened to produce). Any of the following — truncating the post-wizard
 * walk, swapping its result for an empty/partial array, or skipping the
 * Welcome-step walk — makes `written` disagree with `expected` and throws,
 * rather than returning a short or empty list that a caller could still
 * report as full success.
 */
export async function runPostCredentialWalk(page, opts = {}) {
  const mode = opts.mode ?? "setup";
  const timeout = opts.timeout ?? 15000;
  const shot = opts.shot;
  const wizard = opts.wizard ?? WIZARD;

  const written = [];
  if (mode === "setup") {
    // This is the screenshot the reported defect mislabeled "the board": it
    // is honestly the wizard's Welcome step (step 1 of 7), proven by the
    // step group's own aria-label before the file is written.
    written.push(...(await walkSurfaces(page, [SURFACES[1]], { timeout, shot })));
    // Drive the wizard to completion — email + Continue, zero repos/projects
    // means nothing else gates — landing on the board without a URL change.
    await advanceThroughWizard(page, wizard, { timeout });
  }

  if (opts.betweenWizardAndSurfaces) await opts.betweenWizardAndSurfaces();

  // Walk board -> Settings -> Stats, proving each surface's own on-screen
  // content before writing its screenshot.
  written.push(...(await walkSurfaces(page, POST_WIZARD_SURFACES, { timeout, shot })));

  const expected = [
    ...(mode === "setup" ? [SURFACES[1].file] : []),
    ...POST_WIZARD_SURFACES.map((s) => s.file),
  ];
  if (JSON.stringify(written) !== JSON.stringify(expected)) {
    throw new Error(`runPostCredentialWalk wrote ${JSON.stringify(written)}, `
      + `expected ${JSON.stringify(expected)} (derived from SURFACES/POST_WIZARD_SURFACES) — `
      + `refusing to report success for a walk that did not write what it claims to have written`);
  }
  return written;
}

async function isVisible(locator) {
  try {
    return await locator.isVisible();
  } catch {
    return false;
  }
}

async function currentStepLabel(page) {
  try {
    return await page.locator('[aria-label^="Step "]').getAttribute("aria-label");
  } catch {
    return null;
  }
}

/**
 * Drive the wizard from wherever it currently is to completion: fill the
 * email field the moment it is visible (BEFORE clicking Continue — Continue
 * is disabled on a malformed/empty address, and clicking a disabled button
 * would sit on Playwright's actionability wait), click the finish button the
 * moment it is visible and return, otherwise click Continue. Bounded by
 * `wizard.maxHops`; on exhaustion throws naming the step it stalled on
 * (read from the step group's own aria-label) rather than a bare timeout.
 */
export async function advanceThroughWizard(page, wizard = WIZARD, opts = {}) {
  const emailField = page.getByPlaceholder(wizard.emailField.placeholder);
  const finishBtn = page.getByRole("button", { name: wizard.finishName });
  const continueBtn = page.getByRole("button", { name: wizard.continueName });
  for (let hop = 0; hop < wizard.maxHops; hop++) {
    if (await isVisible(finishBtn)) {
      await finishBtn.click();
      return;
    }
    if (await isVisible(emailField)) {
      await emailField.fill(EMAIL);
    }
    await continueBtn.click();
    if (typeof page.waitForTimeout === "function") await page.waitForTimeout(opts.hopDelay ?? 150);
  }
  const label = await currentStepLabel(page);
  throw new Error(`advanceThroughWizard stalled on step: ${label ?? "unknown"}`);
}
