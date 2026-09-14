import assert from "node:assert/strict";
import test from "node:test";
import { EMAIL, WIZARD, SURFACES, verifySurface, walkSurfaces, advanceThroughWizard }
  from "../packaging/linuxAcceptanceSurfaces.mjs";

function surfaceByKey(key) {
  const s = SURFACES.find((x) => x.key === key);
  assert.ok(s, `no surface named "${key}"`);
  return s;
}

function allSelectors(surface) {
  const out = [];
  if (surface.open) out.push(surface.open.selector);
  for (const p of surface.proof) out.push(p.selector);
  return out;
}

/** Duck-typed fake `page`: a static-per-call snapshot of what is "on screen"
 * (`present`), plus an optional `onClick` hook so a test can simulate a click
 * changing what is visible (settings/stats open+close, wizard hops). This
 * mirrors the real Playwright `Page`/`Locator` surface that
 * linuxAcceptanceSurfaces.mjs actually calls: `.locator(selector)`,
 * `.waitFor({state, timeout})`, `.isVisible()`, `.click()`, `.fill()`,
 * `.first()`, `.getAttribute()`, and `page.textContent("body")`. */
function fakePage({ present = [], bodyText = "", stepLabel = "", onClick } = {}) {
  const visible = new Set(present);
  const clicks = [];
  const filled = {};
  function loc(selector) {
    return {
      async waitFor({ state } = {}) {
        const has = visible.has(selector);
        if (state === "visible" && !has) throw new Error(`fakePage: never became visible: ${selector}`);
        if (state === "hidden" && has) throw new Error(`fakePage: never became hidden: ${selector}`);
      },
      async isVisible() { return visible.has(selector); },
      async click() {
        clicks.push(selector);
        if (onClick) onClick(selector, { visible, filled });
      },
      async fill(value) { filled[selector] = value; },
      first() { return this; },
      async getAttribute(name) { return name === "aria-label" ? stepLabel : null; },
    };
  }
  return {
    locator: loc,
    async textContent() { return bodyText; },
    _visible: visible,
    _clicks: clicks,
    _filled: filled,
  };
}

// ── the table itself ─────────────────────────────────────────────────────

test("every surface declares at least one proof selector and a distinct .png file", () => {
  const files = new Set();
  for (const s of SURFACES) {
    assert.ok(Array.isArray(s.proof) && s.proof.length > 0, `${s.key} has no proof`);
    assert.match(s.file, /\.png$/, `${s.key} has no .png file`);
    assert.ok(!files.has(s.file), `duplicate screenshot filename ${s.file}`);
    files.add(s.file);
  }
});

test("SURFACES is ordered credential-screen -> onboarding-welcome -> board-first-run -> settings -> stats, matching the numbered filenames", () => {
  assert.deepEqual(SURFACES.map((s) => s.key),
    ["credential-screen", "onboarding-welcome", "board-first-run", "settings", "stats"]);
  assert.deepEqual(SURFACES.map((s) => s.file),
    ["01-credential-screen.png", "02-onboarding-welcome.png", "03-board-first-run.png", "04-settings.png", "05-stats.png"]);
});

test("filenames name what they show: the wizard screen is never called board, the board screen is never called onboarding/welcome/wizard", () => {
  const welcome = surfaceByKey("onboarding-welcome");
  const board = surfaceByKey("board-first-run");
  assert.match(welcome.file, /welcome/i);
  assert.doesNotMatch(welcome.file, /\bboard\b/i);
  assert.match(board.file, /\bboard\b/i);
  assert.doesNotMatch(board.file, /welcome|wizard|onboarding/i);
});

test("the board surface's absent list names the wizard's step-1 stepper — the regression guard for the reported defect", () => {
  const board = surfaceByKey("board-first-run");
  const flat = (board.absent || []).map((a) => a.selector);
  assert.ok(flat.includes(WIZARD.entryStepperSelector),
    "board-first-run must assert the step-1 stepper is GONE, or a wizard screen can pass as the board again");
});

test("the acceptance email is a reserved-TLD (.invalid) address", () => {
  assert.match(EMAIL, /\.invalid$/);
});

// ── walkSurfaces: assert-then-screenshot ────────────────────────────────

test("walkSurfaces writes each screenshot only after its proof passes, in the given order", async () => {
  const board = surfaceByKey("board-first-run");
  const settings = surfaceByKey("settings");
  const stats = surfaceByKey("stats");
  const present = [
    ...board.proof.map((p) => p.selector),
    ...allSelectors(settings),
    ...allSelectors(stats),
  ];
  const page = fakePage({
    present,
    onClick(selector, ctx) {
      if (selector === settings.close.selector) ctx.visible.delete(settings.close.awaitHidden);
    },
  });
  const shots = [];
  const written = await walkSurfaces(page, [board, settings, stats], {
    shot: async (file) => { shots.push(file); },
    timeout: 50,
  });
  assert.deepEqual(shots, [board.file, settings.file, stats.file]);
  assert.deepEqual(written, shots);
});

test("the board surface fails the walk when only the wizard's step-1 stepper is on screen (the exact reported defect)", async () => {
  const board = surfaceByKey("board-first-run");
  // URL landed, but nothing beyond the wizard's entry stepper actually
  // rendered — the sidebar and first-run heading are both absent.
  const page = fakePage({ present: [WIZARD.entryStepperSelector] });
  const shots = [];
  await assert.rejects(
    () => walkSurfaces(page, [board], { shot: async (f) => shots.push(f), timeout: 50 }),
    /board-first-run/,
  );
  assert.deepEqual(shots, [], "no screenshot may be written for a surface whose proof did not pass");
});

test("the board surface fails via its absent-check even when its own proof is present (the entry stepper must be gone)", async () => {
  const board = surfaceByKey("board-first-run");
  const present = [...board.proof.map((p) => p.selector), WIZARD.entryStepperSelector];
  const page = fakePage({ present });
  const shots = [];
  await assert.rejects(
    () => walkSurfaces(page, [board], { shot: async (f) => shots.push(f), timeout: 50 }),
    /still present/,
  );
  assert.deepEqual(shots, []);
});

for (const key of ["board-first-run", "settings", "stats"]) {
  test(`walkSurfaces fails "${key}" when its own proof selector is absent (mutation gate)`, async () => {
    const surface = surfaceByKey(key);
    // Present everything EXCEPT the surface's own proof, so only the proof
    // check — not a missing `open` click or something unrelated — fails.
    const present = surface.open ? [surface.open.selector] : [];
    const page = fakePage({ present });
    const shots = [];
    await assert.rejects(
      () => walkSurfaces(page, [surface], { shot: async (f) => shots.push(f), timeout: 50 }),
      new RegExp(key),
    );
    assert.deepEqual(shots, []);
  });
}

test("walkSurfaces stops at the first failing surface — later surfaces are never opened or screenshotted", async () => {
  const board = surfaceByKey("board-first-run"); // proof absent -> throws
  const settings = surfaceByKey("settings");
  const page = fakePage({ present: allSelectors(settings) });
  const shots = [];
  await assert.rejects(
    () => walkSurfaces(page, [board, settings], { shot: async (f) => shots.push(f), timeout: 50 }));
  assert.deepEqual(shots, []);
  assert.ok(!page._clicks.includes(settings.open.selector), "settings must never be opened once board failed");
});

test("a screenshot is never written for a surface whose proof throws, even mid-list", async () => {
  const settings = surfaceByKey("settings"); // present, passes
  const stats = surfaceByKey("stats"); // absent, fails
  const page = fakePage({ present: allSelectors(settings) });
  const shots = [];
  await assert.rejects(
    () => walkSurfaces(page, [settings, stats], { shot: async (f) => shots.push(f), timeout: 50 }));
  assert.deepEqual(shots, [settings.file], "settings passed and was shot; stats must not be");
});

test("verifySurface clicks a surface's open control before checking its proof, and never touches its close control", async () => {
  const settings = surfaceByKey("settings");
  const page = fakePage({ present: allSelectors(settings) });
  await verifySurface(page, settings, { timeout: 50 });
  assert.ok(page._clicks.includes(settings.open.selector), "open control was not clicked");
  assert.ok(!page._clicks.includes(settings.close.selector), "verifySurface must not close — only walkSurfaces does, after the shot");
});

test("walkSurfaces clicks a surface's close control, and waits for its dialog to detach, after the screenshot", async () => {
  const settings = surfaceByKey("settings");
  const page = fakePage({
    present: allSelectors(settings),
    onClick(selector, ctx) {
      if (selector === settings.close.selector) ctx.visible.delete(settings.close.awaitHidden);
    },
  });
  const shots = [];
  await walkSurfaces(page, [settings], { shot: async (f) => shots.push(f), timeout: 50 });
  assert.deepEqual(shots, [settings.file]);
  assert.ok(page._clicks.includes(settings.close.selector));
});

// ── advanceThroughWizard ─────────────────────────────────────────────────

const noWait = async () => {};

test("advanceThroughWizard fills the email field with EMAIL before clicking Continue", async () => {
  const page = fakePage({
    present: [WIZARD.emailSelector, WIZARD.continueSelector],
    onClick(selector, ctx) {
      if (selector === WIZARD.continueSelector) {
        // Simulate landing on the last step after one Continue.
        ctx.visible.delete(WIZARD.emailSelector);
        ctx.visible.delete(WIZARD.continueSelector);
        ctx.visible.add(WIZARD.finishSelector);
      }
    },
  });
  await advanceThroughWizard(page, { wait: noWait });
  assert.equal(page._filled[WIZARD.emailSelector], EMAIL);
  assert.deepEqual(page._clicks, [WIZARD.continueSelector, WIZARD.finishSelector]);
});

test("advanceThroughWizard fails, naming the stalled step, when no forward control ever appears", async () => {
  const page = fakePage({
    present: [WIZARD.continueSelector], // Continue exists but never leads anywhere
    stepLabel: "Step 2 of 7: Email",
  });
  await assert.rejects(
    () => advanceThroughWizard(page, { wait: noWait }),
    (err) => {
      assert.match(err.message, /stalled on step/);
      assert.match(err.message, /Step 2 of 7: Email/);
      return true;
    },
  );
});

test("advanceThroughWizard clicks the finish button and stops — no Continue click, no email fill, once finish is visible", async () => {
  const page = fakePage({ present: [WIZARD.finishSelector] });
  await advanceThroughWizard(page, { wait: noWait });
  assert.deepEqual(page._clicks, [WIZARD.finishSelector]);
  assert.deepEqual(page._filled, {});
});
