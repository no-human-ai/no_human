// Settings > Models (model picker part 3 of 3): one row per role, fed by
// GET /api/models, writes going through PUT /api/config/models only. Drives
// the real UI against a mocked models API (never touches :8420 or the real
// ~/.no_human/config.yaml). Mirrors settings-account.mjs's structure.
//
// Constraint §6d (reviewer role-backend): the reviewer row also carries an
// explicit backend/model override, disclosed via
// [data-testid="reviewer-backend-disclosure"], posted through the SAME PUT
// under a `role_backends` key. Scenarios 6-9 below drive that control.
//
// `node models-pane.mjs --evidence-server` (instead of running the suite)
// serves `web/dist` plus an in-process, stateful fixture API on
// 127.0.0.1:5173 and stays alive — this is what `.no_human/ui_evidence.json`
// drives for this task's visual proof, since the live :8420 board predates
// the `backend` block on GET /api/models.
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const DIST = new URL("../dist", import.meta.url).pathname;
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };

// Fixture ids/notes are test-only strings, not literals copied from the
// production catalog — the point is that the pane renders whatever the
// server sends, not a hardcoded five.
const VENDOR_PIN_NOTE = "only Claude ids may run this role.";
const DISABLED_REASON = "'gpt-5-codex' cannot be set as llm.primary_model: only the Claude backend reads that key (worker.backend).";
const COST_NOTE = "The operator's 2026-08-11 A/B reverted this role from claude-opus-5 back to claude-opus-4-8.";
const LOCAL_UNAVAILABLE_REASON = "no local server configured";

function basePayload(over = {}) {
  return {
    roles: [
      {
        role: "coder", key: "primary_model", current: "claude-sonnet-5", default: "claude-sonnet-5",
        note: "", cost_note: "",
        options: [
          { id: "claude-sonnet-5", price_class: { label: "medium" }, is_default: true, note: "", requires_backend: false, disabled_reason: "" },
          { id: "gpt-5-codex", price_class: { label: "medium" }, is_default: false, note: "", requires_backend: true, disabled_reason: DISABLED_REASON },
        ],
      },
      {
        role: "reviewer", key: "review_model", current: "claude-opus-4-8", default: "claude-opus-4-8",
        note: VENDOR_PIN_NOTE, cost_note: COST_NOTE,
        // Constraint §6d: the server's own effective-backend answer, copied
        // verbatim onto the row — this fixture starts on the default.
        backend: { backend: "claude", model: "claude-opus-4-8", is_default: true },
        options: [
          { id: "claude-opus-4-8", price_class: { label: "high" }, is_default: true, note: VENDOR_PIN_NOTE, requires_backend: false, disabled_reason: "" },
          { id: "claude-opus-5", price_class: { label: "high" }, is_default: false, note: VENDOR_PIN_NOTE, requires_backend: false, disabled_reason: "" },
        ],
      },
      {
        role: "planner", key: "planner_model", current: "claude-opus-5", default: "claude-opus-5",
        note: VENDOR_PIN_NOTE, cost_note: "",
        options: [{ id: "claude-opus-5", price_class: { label: "high" }, is_default: true, note: VENDOR_PIN_NOTE, requires_backend: false, disabled_reason: "" }],
      },
      {
        role: "supervisor", key: "supervisor_model", current: "claude-sonnet-5", default: "claude-sonnet-5",
        note: VENDOR_PIN_NOTE, cost_note: "",
        options: [{ id: "claude-sonnet-5", price_class: { label: "medium" }, is_default: true, note: VENDOR_PIN_NOTE, requires_backend: false, disabled_reason: "" }],
      },
      {
        role: "utility", key: "utility_model", current: "claude-haiku-4-5", default: "claude-haiku-4-5",
        note: VENDOR_PIN_NOTE, cost_note: "",
        options: [{ id: "claude-haiku-4-5", price_class: { label: "low" }, is_default: true, note: VENDOR_PIN_NOTE, requires_backend: false, disabled_reason: "" }],
      },
    ],
    restart_required: false,
    ...over,
  };
}

// GET /api/coder-backend fixture — the reviewer backend picker's own option
// list (backendPanelView.js), independent of the five model-id rows above.
// One entry is unavailable so s9 can exercise the disabled/title path.
function coderBackendPayload(over = {}) {
  return {
    current: "claude",
    default: "claude",
    options: [
      { id: "claude", available: true, reason: "" },
      { id: "codex", available: true, reason: "" },
      { id: "local", available: false, reason: LOCAL_UNAVAILABLE_REASON },
    ],
    restart_required: false,
    ...over,
  };
}

const EVIDENCE_MODE = process.argv.includes("--evidence-server");

if (EVIDENCE_MODE) {
  await runEvidenceServer();
} else {
  await runE2ESuite();
}

// ─────────────────────────────────────────────────────────────────────────
// e2e suite: a static dist server on :4671, a headless browser, 9 scenarios.
// ─────────────────────────────────────────────────────────────────────────
async function runE2ESuite() {
  const srv = http.createServer((q, r) => {
    const u = q.url.split("?")[0];
    let f = path.join(DIST, u === "/" ? "index.html" : u);
    if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(DIST, "index.html");
    r.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream" });
    r.end(fs.readFileSync(f));
  });
  await new Promise((r) => srv.listen(4671, r));

  const failures = [];
  const check = (n, ok, d = "") => {
    console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
    if (!ok) failures.push(n);
  };

  const browser = await chromium.launch();

  // Open Settings and switch to the Models section. `page.route("**/api/**")`
  // must already be installed. Returns the page.
  async function openModels(page) {
    await page.goto("http://127.0.0.1:4671/", { waitUntil: "networkidle" });
    await page.waitForTimeout(300);
    await page.getByRole("button", { name: /^Settings$/ }).click();
    await page.waitForTimeout(300);
    await page.getByRole("button", { name: /^Models$/ }).click();
    await page.waitForTimeout(300);
  }

  function commonRoutes(j) {
    return (route) => {
      const u = route.request().url();
      if (u.includes("/api/onboarding")) return j({ completed: true });
      if (u.includes("/api/tasks")) return j([]);
      if (u.includes("/api/projects")) return j([]);
      return j({});
    };
  }

  // ── Scenario 1: initial render — 5 rows, chips, defaults, disabled option ────
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.route("**/api/**", (route) => {
      const u = route.request().url();
      const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
      if (u.includes("/api/models") && route.request().method() === "GET") return j(basePayload());
      return commonRoutes(j)(route);
    });
    await openModels(page);

    // Scoped to `.models-rows` — CoderBackendRow renders its own
    // `.models-row`-classed div OUTSIDE that wrapper, as a sibling of it,
    // whenever its own (unrelated) endpoint is reachable at all. WorkersRow
    // no longer renders in the Models pane at all (task 05a9cee0, re-home) —
    // it moved to its own top-level Settings → Workers section.
    const rows = page.locator(".models-rows > .models-row");
    check("[s1] exactly five rows are rendered", (await rows.count()) === 5, String(await rows.count()));

    // `> .auth-label` (direct child) — the reviewer row's override controls
    // add their OWN nested `.auth-label`s one level deeper, inside
    // `.reviewer-backend-override`, which a descendant selector would also
    // match.
    const labels = await page.locator(".models-rows > .models-row > .auth-label").allInnerTexts();
    check("[s1] rows are labelled Coder/Reviewer/Planner/Supervisor/Utility in order",
      labels.every((t, i) => t.startsWith(["Coder", "Reviewer", "Planner", "Supervisor", "Utility"][i])),
      JSON.stringify(labels));

    const chips = await page.locator(".models-rows > .models-row > .integration-chip").allInnerTexts();
    check("[s1] every row shows a price-class chip", chips.length === 5 && chips.every(Boolean), JSON.stringify(chips));

    const defaults = await page.locator(".models-rows > .models-row .models-default code").allInnerTexts();
    check("[s1] every row shows its default id", JSON.stringify(defaults) ===
      JSON.stringify(["claude-sonnet-5", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]),
      JSON.stringify(defaults));

    const coderSelect = rows.nth(0).locator("select");
    const gptOption = coderSelect.locator('option[value="gpt-5-codex"]');
    // Playwright's isDisabled() locator assertion does not treat <option> as a
    // form control, so it reads the DOM property directly instead.
    check("[s1] the coder's requires_backend option is disabled",
      await gptOption.evaluate((el) => el.disabled) === true);
    check("[s1] the disabled option's title is the server's disabled_reason",
      (await gptOption.getAttribute("title")) === DISABLED_REASON,
      await gptOption.getAttribute("title"));

    check("[s1] the reviewer row shows the reviewer cost_note",
      (await rows.nth(1).innerText()).includes(COST_NOTE));
    check("[s1] the coder row shows no cost_note", !(await rows.nth(0).innerText()).includes("A/B"));

    check("[s1] no restart banner on a fresh load",
      !(await page.locator(".nh-alarm.auth-alarm").isVisible().catch(() => false)));
    check("[s1] no page errors", errors.length === 0, errors[0] || "");
    await ctx.close();
  }

  // ── Scenario 2: change reviewer, Save, restart banner, PUT body scoped ───────
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    let putBody = null;
    await page.route("**/api/**", (route) => {
      const req = route.request(); const u = req.url();
      const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
      if (u.includes("/api/models") && req.method() === "GET") return j(basePayload());
      if (u.includes("/api/config/models") && req.method() === "PUT") {
        putBody = JSON.parse(req.postData() || "{}");
        return j(basePayload({ restart_required: true, roles: basePayload().roles.map((r) =>
          r.role === "reviewer" ? { ...r, current: "claude-opus-5" } : r) }));
      }
      return commonRoutes(j)(route);
    });
    await openModels(page);

    const rows = page.locator(".models-rows > .models-row");
    // `[aria-label="Reviewer"]` — the reviewer row now also has an override
    // `<select>` (aria-label "Reviewer backend override"); an unqualified
    // `select` locator would match both and violate strict mode.
    await rows.nth(1).locator('select[aria-label="Reviewer"]').selectOption("claude-opus-5");
    await page.locator('[data-testid="models-save"]').click();
    await page.waitForTimeout(400);

    check("[s2] the PUT body contains ONLY the changed key",
      putBody && Object.keys(putBody).length === 1 && putBody.review_model === "claude-opus-5",
      JSON.stringify(putBody));
    check("[s2] the restart banner is shown after a restart_required response",
      await page.locator(".nh-alarm.auth-alarm").isVisible().catch(() => false));
    check("[s2] the restart banner names the restart command",
      (await page.locator(".nh-alarm.auth-alarm").innerText().catch(() => "")).includes("nh stop && nh start"));
    check("[s2] no page errors", errors.length === 0, errors[0] || "");
    await ctx.close();
  }

  // ── Scenario 3: Reset to defaults ─────────────────────────────────────────────
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const drifted = basePayload({
      roles: basePayload().roles.map((r) =>
        r.role === "reviewer" ? { ...r, current: "claude-opus-5" } : r),
    });
    let putBody = null;
    await page.route("**/api/**", (route) => {
      const req = route.request(); const u = req.url();
      const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
      if (u.includes("/api/models") && req.method() === "GET") return j(drifted);
      if (u.includes("/api/config/models") && req.method() === "PUT") {
        putBody = JSON.parse(req.postData() || "{}");
        return j(basePayload({ restart_required: false }));
      }
      return commonRoutes(j)(route);
    });
    await openModels(page);
    await page.getByRole("button", { name: /^Reset to defaults$/ }).click();
    await page.waitForTimeout(400);

    check("[s3] the PUT body equals the payload's own defaults for the drifted role",
      putBody && Object.keys(putBody).length === 1 && putBody.review_model === "claude-opus-4-8",
      JSON.stringify(putBody));

    // `> .auth-label > select` — the five primary role selects only; the
    // reviewer override's own `<select>` is nested one level deeper.
    const values = await page.locator(".models-rows > .models-row > .auth-label > select").evaluateAll((els) => els.map((e) => e.value));
    check("[s3] every select shows its default after Reset",
      JSON.stringify(values) === JSON.stringify(["claude-sonnet-5", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]),
      JSON.stringify(values));
    check("[s3] no page errors", errors.length === 0, errors[0] || "");
    await ctx.close();
  }

  // ── Scenario 4: a 422 refusal reverts every pending edit, shows the detail ────
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const DETAIL = "'gpt-5.4' has no published price; refusing to run an unpriced model.";
    await page.route("**/api/**", (route) => {
      const req = route.request(); const u = req.url();
      const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
      if (u.includes("/api/models") && req.method() === "GET") return j(basePayload());
      if (u.includes("/api/config/models") && req.method() === "PUT") return j({ detail: DETAIL }, 422);
      return commonRoutes(j)(route);
    });
    await openModels(page);

    const rows = page.locator(".models-rows > .models-row");
    await rows.nth(1).locator('select[aria-label="Reviewer"]').selectOption("claude-opus-5");
    await page.locator('[data-testid="models-save"]').click();
    await page.waitForTimeout(400);

    check("[s4] the server's refusal detail is rendered verbatim",
      (await page.locator(".settings-error").innerText().catch(() => "")).includes(DETAIL),
      await page.locator(".settings-error").innerText().catch(() => "(none)"));
    const reviewerValue = await rows.nth(1).locator('select[aria-label="Reviewer"]').inputValue();
    check("[s4] the reviewer select reverted to `current`, not the failed pending edit",
      reviewerValue === "claude-opus-4-8", reviewerValue);
    check("[s4] no page errors", errors.length === 0, errors[0] || "");
    await ctx.close();
  }

  // ── Scenario 5: endpoint absent — degrades to an 'unavailable' note ──────────
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.route("**/api/**", (route) => {
      const u = route.request().url();
      const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
      if (u.includes("/api/models") && route.request().method() === "GET") {
        return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
      }
      return commonRoutes(j)(route);
    });
    await openModels(page);
    const noteVisible = (await page.locator(".settings-empty").innerText().catch(() => "")).match(/unavailable/i) != null;
    check("[s5] the panel degrades to an 'unavailable' note when the endpoint is absent", noteVisible);
    check("[s5] with the unavailable note shown, no rows are rendered",
      noteVisible && (await page.locator(".models-rows > .models-row").count()) === 0);
    check("[s5] no page errors", errors.length === 0, errors[0] || "");
    await ctx.close();
  }

  // ── Scenario 6: reviewer backend on default — disclosure, no override ───────
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.route("**/api/**", (route) => {
      const req = route.request(); const u = req.url();
      const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
      if (u.includes("/api/models") && req.method() === "GET") return j(basePayload());
      if (u.includes("/api/coder-backend") && req.method() === "GET") return j(coderBackendPayload());
      return commonRoutes(j)(route);
    });
    await openModels(page);

    const disclosure = page.locator('[data-testid="reviewer-backend-disclosure"]');
    const disclosureText = await disclosure.innerText();
    check("[s6] the disclosure reads 'default' with the default reviewer id",
      disclosureText.includes("default") && disclosureText.includes("claude-opus-4-8"),
      disclosureText);
    check("[s6] the disclosure does not claim an override on a fresh default load",
      !disclosureText.includes("overrides"), disclosureText);

    const overrideValue = await page.locator('select[aria-label="Reviewer backend override"]').inputValue();
    check("[s6] the override select is on the 'default' option", overrideValue === "", overrideValue);
    check("[s6] no pending line is shown before any edit",
      (await page.locator('[data-testid="reviewer-backend-pending"]').count()) === 0);
    check("[s6] no page errors", errors.length === 0, errors[0] || "");
    await ctx.close();
  }

  // ── Scenario 7: choose a non-default reviewer backend and Save ──────────────
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    let putBody = null;
    await page.route("**/api/**", (route) => {
      const req = route.request(); const u = req.url();
      const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
      if (u.includes("/api/models") && req.method() === "GET") return j(basePayload());
      if (u.includes("/api/coder-backend") && req.method() === "GET") return j(coderBackendPayload());
      if (u.includes("/api/config/models") && req.method() === "PUT") {
        putBody = JSON.parse(req.postData() || "{}");
        return j(basePayload({
          roles: basePayload().roles.map((r) =>
            r.role === "reviewer" ? { ...r, backend: { backend: "codex", model: "gpt-5-codex", is_default: false } } : r),
        }));
      }
      return commonRoutes(j)(route);
    });
    await openModels(page);

    await page.locator('select[aria-label="Reviewer backend override"]').selectOption("codex");
    await page.locator('input[aria-label="Reviewer model"]').fill("gpt-5-codex");

    const pending = page.locator('[data-testid="reviewer-backend-pending"]');
    const pendingText = await pending.innerText();
    check("[s7] a pending line appears with the unsaved choice",
      pendingText.includes("codex") && pendingText.includes("gpt-5-codex"), pendingText);
    const disclosureBeforeSave = await page.locator('[data-testid="reviewer-backend-disclosure"]').innerText();
    check("[s7] the disclosure still shows the SAVED default before Save",
      disclosureBeforeSave.includes("default") && !disclosureBeforeSave.includes("overrides"),
      disclosureBeforeSave);

    await page.locator('[data-testid="models-save"]').click();
    await page.waitForTimeout(400);

    check("[s7] the PUT body is exactly the role_backends choice, no model-id keys",
      putBody && JSON.stringify(putBody) === JSON.stringify({ role_backends: { reviewer: { backend: "codex", model: "gpt-5-codex" } } }),
      JSON.stringify(putBody));

    const disclosureAfterSave = await page.locator('[data-testid="reviewer-backend-disclosure"]').innerText();
    check("[s7] the disclosure now shows the chosen backend/model",
      disclosureAfterSave.includes("codex") && disclosureAfterSave.includes("gpt-5-codex") && disclosureAfterSave.includes("overrides"),
      disclosureAfterSave);
    check("[s7] the pending line is gone after Save", (await pending.count()) === 0);
    check("[s7] no page errors", errors.length === 0, errors[0] || "");
    await ctx.close();
  }

  // ── Scenario 8: clear a chosen reviewer backend back to default ─────────────
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const chosen = basePayload({
      roles: basePayload().roles.map((r) =>
        r.role === "reviewer" ? { ...r, backend: { backend: "codex", model: "gpt-5-codex", is_default: false } } : r),
    });
    let putBody = null;
    await page.route("**/api/**", (route) => {
      const req = route.request(); const u = req.url();
      const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
      if (u.includes("/api/models") && req.method() === "GET") return j(chosen);
      if (u.includes("/api/coder-backend") && req.method() === "GET") return j(coderBackendPayload());
      if (u.includes("/api/config/models") && req.method() === "PUT") {
        putBody = JSON.parse(req.postData() || "{}");
        return j(basePayload());
      }
      return commonRoutes(j)(route);
    });
    await openModels(page);

    const disclosureBefore = await page.locator('[data-testid="reviewer-backend-disclosure"]').innerText();
    check("[s8] starts from the chosen (non-default) state", disclosureBefore.includes("codex"), disclosureBefore);

    await page.locator('select[aria-label="Reviewer backend override"]').selectOption("");
    const pending = page.locator('[data-testid="reviewer-backend-pending"]');
    const pendingText = await pending.innerText();
    check("[s8] picking 'default' shows a pending 'default' line", pendingText.includes("default"), pendingText);

    await page.locator('[data-testid="models-save"]').click();
    await page.waitForTimeout(400);

    check("[s8] the PUT body clears the reviewer role_backends entry",
      putBody && JSON.stringify(putBody) === JSON.stringify({ role_backends: { reviewer: null } }),
      JSON.stringify(putBody));

    const disclosureAfter = await page.locator('[data-testid="reviewer-backend-disclosure"]').innerText();
    check("[s8] the disclosure reverts to 'default' after Save",
      disclosureAfter.includes("default") && !disclosureAfter.includes("overrides"), disclosureAfter);
    check("[s8] no page errors", errors.length === 0, errors[0] || "");
    await ctx.close();
  }

  // ── Scenario 9: an unavailable backend option is disabled, Save gated on model id ──
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.route("**/api/**", (route) => {
      const req = route.request(); const u = req.url();
      const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
      if (u.includes("/api/models") && req.method() === "GET") return j(basePayload());
      if (u.includes("/api/coder-backend") && req.method() === "GET") return j(coderBackendPayload());
      return commonRoutes(j)(route);
    });
    await openModels(page);

    const override = page.locator('select[aria-label="Reviewer backend override"]');
    const localOption = override.locator('option[value="local"]');
    check("[s9] the unavailable backend option is disabled",
      (await localOption.evaluate((el) => el.disabled)) === true);
    check("[s9] the disabled option's title is the server's reason",
      (await localOption.getAttribute("title")) === LOCAL_UNAVAILABLE_REASON,
      await localOption.getAttribute("title"));

    await override.selectOption("codex");
    const save = page.locator('[data-testid="models-save"]');
    check("[s9] Save stays disabled while the reviewer model id is blank", await save.isDisabled());

    await page.locator('input[aria-label="Reviewer model"]').fill("gpt-5-codex");
    check("[s9] Save becomes enabled once a model id is provided", !(await save.isDisabled()));
    check("[s9] no page errors", errors.length === 0, errors[0] || "");
    await ctx.close();
  }

  await browser.close();
  srv.close();
  console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
  process.exit(failures.length ? 1 : 0);
}

// ─────────────────────────────────────────────────────────────────────────
// --evidence-server: web/dist + a stateful fixture API on 127.0.0.1:5173,
// for `.no_human/ui_evidence.json` to drive. Never touches the real
// ~/.no_human/config.yaml or the live :8420 board.
// ─────────────────────────────────────────────────────────────────────────
async function runEvidenceServer() {
  const modelsState = basePayload();
  const backendState = coderBackendPayload();

  function applyModelsPut(body) {
    if (!body || typeof body !== "object") return modelsState;
    for (const [key, value] of Object.entries(body)) {
      if (key === "role_backends") {
        const reviewerRole = modelsState.roles.find((r) => r.role === "reviewer");
        if (!reviewerRole) continue;
        if (value && value.reviewer === null) {
          reviewerRole.backend = { backend: "claude", model: reviewerRole.default, is_default: true };
        } else if (value && value.reviewer && typeof value.reviewer === "object") {
          reviewerRole.backend = {
            backend: value.reviewer.backend,
            model: value.reviewer.model,
            is_default: false,
          };
        }
        continue;
      }
      const role = modelsState.roles.find((r) => r.key === key);
      if (role) role.current = value;
    }
    return modelsState;
  }

  function sendJson(res, body, status = 200) {
    const data = JSON.stringify(body);
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(data);
  }

  function serveStatic(req, res) {
    const u = req.url.split("?")[0];
    let f = path.join(DIST, u === "/" ? "index.html" : u);
    if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(DIST, "index.html");
    res.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream" });
    res.end(fs.readFileSync(f));
  }

  const server = http.createServer((req, res) => {
    const u = req.url.split("?")[0];
    if (u === "/ws") { res.writeHead(404); res.end(); return; }
    if (!u.startsWith("/api/")) { serveStatic(req, res); return; }

    if (u === "/api/models" && req.method === "GET") return sendJson(res, modelsState);
    if (u === "/api/coder-backend" && req.method === "GET") return sendJson(res, backendState);
    if (u === "/api/onboarding/status" && req.method === "GET") return sendJson(res, { completed: true });
    if (u === "/api/onboarding/deferred" && req.method === "GET") return sendJson(res, { deferred: [] });
    if (u === "/api/tasks" && req.method === "GET") return sendJson(res, []);
    if (u === "/api/projects" && req.method === "GET") return sendJson(res, []);
    if (u === "/api/config/models" && req.method === "PUT") {
      let raw = "";
      req.on("data", (chunk) => { raw += chunk; });
      req.on("end", () => {
        let body = {};
        try { body = JSON.parse(raw || "{}"); } catch { body = {}; }
        sendJson(res, applyModelsPut(body));
      });
      return;
    }
    return sendJson(res, {});
  });

  await new Promise((resolve) => {
    server.once("error", (err) => {
      console.error(`--evidence-server: failed to bind 127.0.0.1:5173 — ${err.message}`);
      process.exit(1);
    });
    server.listen(5173, "127.0.0.1", resolve);
  });
  console.log("--evidence-server: serving web/dist + fixture API on http://127.0.0.1:5173");
  // Stay alive — an external process (the walk probe, or the operator) reads
  // and eventually kills this one; nothing here ever resolves on its own.
  await new Promise(() => {});
}
