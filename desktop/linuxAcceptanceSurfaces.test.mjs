// Unit tests for packaging/linuxAcceptanceSurfaces.mjs — the pure, testable
// step table behind packaging/linux-acceptance.mjs (docs/LINUX.md §4/§6).
//
// The bug this whole module exists to fix: the driver wrote a screenshot named
// "02-board.png" that was, in fact, the onboarding wizard's Welcome step (step
// 1 of 7) — a filename that lied about what was on screen, and nothing failed
// the job for it. These tests exercise the fix against a fake, Playwright-shaped
// `page` (no Electron/Playwright dependency needed to run them) and are written
// so that deleting any one proof/absent assertion in linuxAcceptanceSurfaces.mjs
// turns the corresponding test RED — a mutation gate, not prose.
import assert from "node:assert/strict";
import test from "node:test";
import {
  SURFACES, POST_WIZARD_SURFACES, SCREENSHOT_FILES, WIZARD, EMAIL,
  verifySurface, walkSurfaces, advanceThroughWizard, runPostCredentialWalk,
} from "../packaging/linuxAcceptanceSurfaces.mjs";

// ── A minimal fake of the duck-typed Playwright `page` surface that
// linuxAcceptanceSurfaces.mjs actually calls: getByRole/locator/getByPlaceholder,
// each returning a locator with isVisible/waitFor/click/fill/textContent/
// getAttribute. ─────────────────────────────────────────────────────────────
class Elem {
  constructor({ visible = true, text = "", attrs = {}, onClick } = {}) {
    this.visible = visible;
    this.text = text;
    this.attrs = attrs;
    this.onClick = onClick;
  }
}

function makeLoc(getEl, describe) {
  return {
    async isVisible() {
      const el = getEl();
      return !!el && !!el.visible;
    },
    async waitFor({ state }) {
      const el = getEl();
      const vis = !!el && !!el.visible;
      if (state === "visible" && !vis) throw new Error(`timeout: not visible: ${describe}`);
      if (state === "hidden" && vis) throw new Error(`timeout: not hidden: ${describe}`);
    },
    async click() {
      const el = getEl();
      if (!el) throw new Error(`click on missing element: ${describe}`);
      if (el.onClick) el.onClick();
    },
    async fill(v) {
      const el = getEl();
      if (!el) throw new Error(`fill on missing element: ${describe}`);
      el.value = v;
    },
    async textContent() {
      const el = getEl();
      return el ? el.text : null;
    },
    async getAttribute(name) {
      const el = getEl();
      return el ? (el.attrs[name] ?? null) : null;
    },
  };
}

// byLocator: { "<selector>": Elem }; byRole: [{ role, name: <string>, el: Elem }]
function fakePage(byLocator = {}, byRole = []) {
  return {
    locator(sel) {
      return makeLoc(() => byLocator[sel], sel);
    },
    getByRole(role, opts) {
      const entry = byRole.find((r) => r.role === role && (!opts?.name || opts.name.test(r.name)));
      return makeLoc(() => entry && entry.el, `role=${role} name=${opts?.name}`);
    },
    getByPlaceholder(ph) {
      return makeLoc(() => byLocator[`placeholder:${ph}`], `placeholder=${ph}`);
    },
    waitForTimeout: async () => {},
  };
}

// ── Structural pins on the SURFACES table itself ────────────────────────── //

test("every surface declares a non-empty proof and a .png filename", () => {
  for (const s of SURFACES) {
    assert.ok(Array.isArray(s.proof) && s.proof.length > 0, `${s.key} has no proof`);
    assert.match(s.file, /\.png$/, `${s.key} file is not a .png`);
    assert.ok(s.label && s.label.length > 0, `${s.key} has no label`);
  }
});

test("SCREENSHOT_FILES lists every surface's file in table order", () => {
  assert.deepEqual(SCREENSHOT_FILES, SURFACES.map((s) => s.file));
});

test("POST_WIZARD_SURFACES is exactly board -> settings -> stats, in order", () => {
  // Pins the exact gap the prior reviewer found: an inline `SURFACES.slice(2)`
  // in the driver was untested, so a mutation to `.slice(2, 3)` (dropping
  // Stats) would go undetected. This asserts both the length AND the key
  // order against the same exported array the driver imports and calls.
  assert.deepEqual(POST_WIZARD_SURFACES.map((s) => s.key), ["board-first-run", "settings", "stats"]);
  assert.equal(POST_WIZARD_SURFACES.length, SURFACES.length - 2);
});

test("Settings and Stats are opened by role + accessible name, never by full-text selector", () => {
  // This is the exact CI failure from the send-back: `button.nh-navrow:text-is(...)`
  // matches the row's full text content, which includes an aria-hidden icon
  // span (App.jsx NavRow) — so it can never match. getByRole's accessible name
  // excludes the aria-hidden icon.
  const settings = SURFACES.find((s) => s.key === "settings");
  const stats = SURFACES.find((s) => s.key === "stats");
  assert.equal(settings.open.role, "button");
  assert.match("Settings", settings.open.name);
  assert.equal(stats.open.role, "button");
  assert.match("Stats", stats.open.name);
  assert.doesNotMatch(JSON.stringify(SURFACES, (k, v) => (v instanceof RegExp ? v.toString() : v)), /text-is/);
  // The Stats nav row must be proven current — not merely that .stats-page
  // exists (which could in principle render behind an overlay).
  const currentCheck = stats.proof.find((p) => p.attr && p.attr.name === "aria-current");
  assert.ok(currentCheck, "stats surface has no aria-current proof");
  assert.equal(currentCheck.attr.value, "page");
  assert.equal(currentCheck.role, "button");
  assert.match("Stats", currentCheck.name);
});

// ── verifySurface: each surface fails when its own proof is absent ─────── //

function buildCredentialPage({ token = true, save = true } = {}) {
  const byLocator = {};
  if (token) byLocator["#token"] = new Elem();
  if (save) byLocator["#save"] = new Elem();
  return fakePage(byLocator);
}

test("credential-screen surface passes when #token and #save are both on screen", async () => {
  await verifySurface(buildCredentialPage(), SURFACES[0], { timeout: 50 });
});
test("credential-screen surface fails when #token is missing", async () => {
  await assert.rejects(verifySurface(buildCredentialPage({ token: false }), SURFACES[0], { timeout: 50 }), /credential-screen/);
});
test("credential-screen surface fails when #save is missing", async () => {
  await assert.rejects(verifySurface(buildCredentialPage({ save: false }), SURFACES[0], { timeout: 50 }), /credential-screen/);
});

function buildWelcomePage({ groupVisible = true } = {}) {
  const byRole = groupVisible ? [{ role: "group", name: "Step 1 of 7: Welcome", el: new Elem() }] : [];
  return fakePage({}, byRole);
}

test("onboarding-welcome surface passes when the Welcome step group is on screen", async () => {
  await verifySurface(buildWelcomePage(), SURFACES[1], { timeout: 50 });
});
test("onboarding-welcome surface fails when no step group is on screen", async () => {
  await assert.rejects(verifySurface(buildWelcomePage({ groupVisible: false }), SURFACES[1], { timeout: 50 }), /onboarding-welcome/);
});

function buildBoardPage({ sidenav = true, firstRunTitle = true, wizardVisible = false } = {}) {
  const byLocator = {};
  if (sidenav) byLocator['nav.nh-sidenav[aria-label="Primary"]'] = new Elem();
  if (firstRunTitle) byLocator["#first-run-title"] = new Elem({ text: "Your first task awaits" });
  const byRole = wizardVisible ? [{ role: "group", name: "Step 1 of 7: Welcome", el: new Elem() }] : [];
  return fakePage(byLocator, byRole);
}

test("board-first-run surface passes on a clean, post-wizard board", async () => {
  await verifySurface(buildBoardPage(), SURFACES[2], { timeout: 50 });
});
test("board-first-run surface fails when the sidebar is missing", async () => {
  await assert.rejects(verifySurface(buildBoardPage({ sidenav: false }), SURFACES[2], { timeout: 50 }), /board-first-run/);
});
test("board-first-run surface fails when the first-run title is missing", async () => {
  await assert.rejects(verifySurface(buildBoardPage({ firstRunTitle: false }), SURFACES[2], { timeout: 50 }), /board-first-run/);
});
test("board-first-run surface fails while a wizard step group is still on screen — the exact reported defect", async () => {
  // This is the regression test for the bug this task fixes: a page that still
  // shows the wizard (even if, hypothetically, board chrome also rendered)
  // must never pass the board proof. If the `absent` array were ever deleted
  // from the board surface, this test goes RED.
  await assert.rejects(
    verifySurface(buildBoardPage({ wizardVisible: true }), SURFACES[2], { timeout: 50 }),
    /board-first-run/,
  );
});
test("board-first-run surface fails on a page showing only the wizard (nothing board-shaped yet)", async () => {
  await assert.rejects(
    verifySurface(buildBoardPage({ sidenav: false, firstRunTitle: false, wizardVisible: true }), SURFACES[2], { timeout: 50 }),
    /board-first-run/,
  );
});

function buildSettingsPage({ dialogVisible = true } = {}) {
  const dialogSel = '[role="dialog"][aria-labelledby="settings-overlay-title"]';
  const byLocator = {};
  if (dialogVisible) byLocator[dialogSel] = new Elem();
  byLocator['[aria-label="Close settings"]'] = new Elem();
  const settingsBtn = new Elem();
  return fakePage(byLocator, [{ role: "button", name: "Settings", el: settingsBtn }]);
}

test("settings surface passes when the settings dialog is on screen after opening", async () => {
  await verifySurface(buildSettingsPage(), SURFACES[3], { timeout: 50 });
});
test("settings surface fails when the dialog never appears after opening", async () => {
  await assert.rejects(verifySurface(buildSettingsPage({ dialogVisible: false }), SURFACES[3], { timeout: 50 }), /"settings"/);
});

function buildStatsPage({ statsPageVisible = true, ariaCurrent = "page" } = {}) {
  const byLocator = {};
  if (statsPageVisible) byLocator[".stats-page"] = new Elem();
  const statsBtn = new Elem({ attrs: { "aria-current": ariaCurrent } });
  return fakePage(byLocator, [{ role: "button", name: "Stats", el: statsBtn }]);
}

test("stats surface passes when .stats-page is on screen and the Stats row is marked current", async () => {
  await verifySurface(buildStatsPage(), SURFACES[4], { timeout: 50 });
});
test("stats surface fails when .stats-page never renders", async () => {
  await assert.rejects(verifySurface(buildStatsPage({ statsPageVisible: false }), SURFACES[4], { timeout: 50 }), /"stats"/);
});
test("stats surface fails when the Stats nav row is not marked aria-current — .stats-page alone is not enough", async () => {
  // Pins the specific extra proof beyond ".stats-page exists": if this
  // aria-current check were ever deleted, this test goes RED even though a
  // bare ".stats-page" locator check would still pass.
  await assert.rejects(
    verifySurface(buildStatsPage({ ariaCurrent: "false" }), SURFACES[4], { timeout: 50 }),
    /"stats"/,
  );
});

// ── walkSurfaces: screenshots only after proof, in order, never on failure ── //

function buildPostWizardFullPage() {
  const sidenav = new Elem();
  const firstRunTitle = new Elem({ text: "Your first task awaits" });
  const dialog = new Elem({ visible: false });
  const statsPage = new Elem({ visible: false });
  const statsBtn = new Elem({ attrs: { "aria-current": "false" } });
  statsBtn.onClick = () => {
    statsPage.visible = true;
    statsBtn.attrs["aria-current"] = "page";
  };
  const settingsBtn = new Elem({ onClick: () => { dialog.visible = true; } });
  const closeBtn = new Elem({ onClick: () => { dialog.visible = false; } });
  const byLocator = {
    'nav.nh-sidenav[aria-label="Primary"]': sidenav,
    "#first-run-title": firstRunTitle,
    '[role="dialog"][aria-labelledby="settings-overlay-title"]': dialog,
    '[aria-label="Close settings"]': closeBtn,
    ".stats-page": statsPage,
  };
  const byRole = [
    { role: "button", name: "Settings", el: settingsBtn },
    { role: "button", name: "Stats", el: statsBtn },
  ];
  return fakePage(byLocator, byRole);
}

test("walkSurfaces writes board -> settings -> stats screenshots in order, one per surface", async () => {
  const page = buildPostWizardFullPage();
  const shotOrder = [];
  const shot = async (file) => { shotOrder.push(file); };
  const written = await walkSurfaces(page, POST_WIZARD_SURFACES, { timeout: 50, shot });
  assert.deepEqual(written, ["03-board-first-run.png", "04-settings.png", "05-stats.png"]);
  assert.deepEqual(shotOrder, written);
});

test("walkSurfaces never writes a screenshot for a surface whose proof never passed", async () => {
  // Break Settings: opening it never reveals the dialog. board-first-run
  // (surface 0 of this walk) must still get its screenshot; settings and
  // stats — which never get proven — must not.
  const byLocator = {
    'nav.nh-sidenav[aria-label="Primary"]': new Elem(),
    "#first-run-title": new Elem({ text: "Your first task awaits" }),
    '[aria-label="Close settings"]': new Elem(),
  };
  const brokenSettingsBtn = new Elem({ onClick: () => {} }); // dialog never appears
  const statsBtn = new Elem({ attrs: { "aria-current": "false" } });
  const brokenPage = fakePage(byLocator, [
    { role: "button", name: "Settings", el: brokenSettingsBtn },
    { role: "button", name: "Stats", el: statsBtn },
  ]);
  const shotOrder = [];
  const shot = async (file) => { shotOrder.push(file); };
  await assert.rejects(
    walkSurfaces(brokenPage, POST_WIZARD_SURFACES, { timeout: 50, shot }),
    /"settings"/,
  );
  assert.deepEqual(shotOrder, ["03-board-first-run.png"]);
});

// ── advanceThroughWizard: scripted step-machine fake ────────────────────── //

function scriptedWizardPage(steps) {
  let idx = 0;
  const log = [];
  const page = {
    getByPlaceholder(ph) {
      assert.equal(ph, WIZARD.emailField.placeholder);
      return {
        async isVisible() { return !!steps[idx].hasEmail; },
        async fill(v) { log.push(["fill", v]); steps[idx].filled = v; },
      };
    },
    getByRole(role, opts) {
      assert.equal(role, "button");
      const wantsFinish = opts.name.test("Enter no_human");
      const wantsContinue = opts.name.test("Continue");
      return {
        async isVisible() {
          if (wantsFinish) return !!steps[idx].isFinish;
          if (wantsContinue) return !steps[idx].isFinish;
          return false;
        },
        async click() {
          if (wantsFinish) { log.push(["click-finish", steps[idx].name]); return; }
          if (wantsContinue) {
            if (steps[idx].hasEmail && !steps[idx].filled) {
              throw new Error(`Continue clicked before the email field was filled on step "${steps[idx].name}"`);
            }
            log.push(["click-continue", steps[idx].name]);
            idx++;
          }
        },
      };
    },
    locator(sel) {
      assert.equal(sel, '[aria-label^="Step "]');
      return { async getAttribute() { return `Step ${idx + 1} of ${steps.length}: ${steps[idx]?.name ?? "?"}`; } };
    },
    waitForTimeout: async () => {},
  };
  return { page, log, currentIndex: () => idx };
}

test("advanceThroughWizard fills the email field before clicking Continue on the email step", async () => {
  const { page, log } = scriptedWizardPage([
    { name: "Welcome" },
    { name: "Email", hasEmail: true },
    { name: "Launch", isFinish: true },
  ]);
  await advanceThroughWizard(page, WIZARD, { hopDelay: 0 });
  const fillIdx = log.findIndex((e) => e[0] === "fill");
  const emailContinueIdx = log.findIndex((e) => e[0] === "click-continue" && e[1] === "Email");
  assert.ok(fillIdx !== -1, "email was never filled");
  assert.ok(emailContinueIdx !== -1, "Continue was never clicked on the Email step");
  assert.ok(fillIdx < emailContinueIdx, "email must be filled before Continue is clicked on that step");
  assert.equal(log[fillIdx][1], EMAIL);
});

test("advanceThroughWizard clicks the finish button and stops immediately", async () => {
  const { page, log } = scriptedWizardPage([
    { name: "Welcome" },
    { name: "Launch", isFinish: true },
  ]);
  await advanceThroughWizard(page, WIZARD, { hopDelay: 0 });
  assert.equal(log.filter((e) => e[0] === "click-finish").length, 1, "finish must be clicked exactly once");
  assert.equal(log[log.length - 1][0], "click-finish", "nothing should happen after finish is clicked");
});

test("advanceThroughWizard fails, naming the step it stalled on, when the finish button never appears", async () => {
  const { page } = scriptedWizardPage(
    [{ name: "Welcome" }, { name: "Repositories" }, { name: "Projects" }],
  );
  await assert.rejects(
    advanceThroughWizard(page, { ...WIZARD, maxHops: 2 }, { hopDelay: 0 }),
    (err) => {
      assert.match(err.message, /stalled/);
      assert.match(err.message, /Projects/);
      return true;
    },
  );
});

// ── runPostCredentialWalk: the composed, fail-closed sequence ──────────────
//
// packaging/linux-acceptance.mjs used to call walkSurfaces(win,
// POST_WIZARD_SURFACES, ...) directly at its own call site, with nothing
// exercising that specific composition — a reviewer found that truncating
// POST_WIZARD_SURFACES to `.slice(0,1)`, replacing the result with `const
// written = []`, or deleting the Welcome-step verify+shot each left the full
// 32-test unit suite green, and (because the driver's OK line was a fixed
// string) `written = []` would still print full success and exit 0. These
// tests drive the whole composed sequence — Welcome walk, wizard, the
// injected between-wizard callback, and the post-wizard walk — against one
// fake page, so each of those three mutations, made inside
// runPostCredentialWalk itself, makes the function throw (its own
// fail-closed check) instead of returning the wrong list, and these
// happy-path tests go RED. ────────────────────────────────────────────────

function buildFullRunPage(steps) {
  let idx = 0;
  const sidenav = new Elem();
  const firstRunTitle = new Elem({ text: "Your first task awaits" });
  const dialog = new Elem({ visible: false });
  const statsPage = new Elem({ visible: false });
  const statsBtn = new Elem({ attrs: { "aria-current": "false" } });
  statsBtn.onClick = () => { statsPage.visible = true; statsBtn.attrs["aria-current"] = "page"; };
  const settingsBtn = new Elem({ onClick: () => { dialog.visible = true; } });
  const closeBtn = new Elem({ onClick: () => { dialog.visible = false; } });
  const byLocator = {
    'nav.nh-sidenav[aria-label="Primary"]': sidenav,
    "#first-run-title": firstRunTitle,
    '[role="dialog"][aria-labelledby="settings-overlay-title"]': dialog,
    '[aria-label="Close settings"]': closeBtn,
    ".stats-page": statsPage,
  };

  const welcomeGroupLoc = {
    async isVisible() { return idx === 0 && steps.length > 0; },
    async waitFor({ state }) {
      const vis = idx === 0 && steps.length > 0;
      if (state === "visible" && !vis) throw new Error("timeout: welcome step group not visible");
      if (state === "hidden" && vis) throw new Error("timeout: welcome step group still visible");
    },
    async textContent() { return null; },
    async getAttribute() { return null; },
  };

  return {
    locator(sel) {
      if (sel === '[aria-label^="Step "]') {
        return { async getAttribute() { return idx < steps.length ? `Step ${idx + 1} of ${steps.length}: ${steps[idx].name}` : null; } };
      }
      return makeLoc(() => byLocator[sel], sel);
    },
    getByPlaceholder(ph) {
      assert.equal(ph, WIZARD.emailField.placeholder);
      return {
        async isVisible() { return idx < steps.length && !!steps[idx].hasEmail; },
        async fill(v) { steps[idx].filled = v; },
      };
    },
    getByRole(role, opts) {
      if (role === "group") {
        // Only the Welcome surface uses role="group" in this walk.
        return welcomeGroupLoc;
      }
      const name = opts && opts.name;
      if (role === "button" && name && name.test("Settings")) return makeLoc(() => settingsBtn, "Settings");
      if (role === "button" && name && name.test("Stats")) return makeLoc(() => statsBtn, "Stats");
      const wantsFinish = role === "button" && name && name.test("Enter no_human");
      const wantsContinue = role === "button" && name && name.test("Continue");
      if (wantsFinish || wantsContinue) {
        return {
          async isVisible() {
            if (idx >= steps.length) return false;
            if (wantsFinish) return !!steps[idx].isFinish;
            return !steps[idx].isFinish;
          },
          async click() {
            if (wantsFinish) return;
            if (steps[idx].hasEmail && !steps[idx].filled) {
              throw new Error(`Continue clicked before the email field was filled on step "${steps[idx].name}"`);
            }
            idx++;
          },
        };
      }
      return makeLoc(() => null, `role=${role} name=${name}`);
    },
    waitForTimeout: async () => {},
  };
}

test("runPostCredentialWalk (mode 'setup') walks Welcome -> board -> Settings -> Stats in order, screenshotting each, and runs betweenWizardAndSurfaces exactly once after the wizard and before the post-wizard walk", async () => {
  const page = buildFullRunPage([
    { name: "Welcome" },
    { name: "Email", hasEmail: true },
    { name: "Launch", isFinish: true },
  ]);
  const shotOrder = [];
  const shot = async (file) => { shotOrder.push(file); };
  const calls = [];
  const written = await runPostCredentialWalk(page, {
    mode: "setup",
    timeout: 50,
    shot,
    wizard: { ...WIZARD, maxHops: 12 },
    betweenWizardAndSurfaces: async () => { calls.push("between"); },
  });
  assert.deepEqual(written, [
    "02-onboarding-welcome.png", "03-board-first-run.png", "04-settings.png", "05-stats.png",
  ]);
  assert.deepEqual(shotOrder, written);
  assert.deepEqual(calls, ["between"]);
});

test("runPostCredentialWalk (mode 'board') skips the Welcome walk and the wizard, still walking board -> Settings -> Stats after the between-callback", async () => {
  const page = buildFullRunPage([]); // no wizard steps scripted — must never be touched in "board" mode
  const shotOrder = [];
  const shot = async (file) => { shotOrder.push(file); };
  const calls = [];
  const written = await runPostCredentialWalk(page, {
    mode: "board",
    timeout: 50,
    shot,
    betweenWizardAndSurfaces: async () => { calls.push("between"); },
  });
  assert.deepEqual(written, ["03-board-first-run.png", "04-settings.png", "05-stats.png"]);
  assert.deepEqual(shotOrder, written);
  assert.deepEqual(calls, ["between"]);
});

test("runPostCredentialWalk works with no betweenWizardAndSurfaces callback supplied", async () => {
  const page = buildFullRunPage([]);
  const written = await runPostCredentialWalk(page, { mode: "board", timeout: 50 });
  assert.deepEqual(written, ["03-board-first-run.png", "04-settings.png", "05-stats.png"]);
});
