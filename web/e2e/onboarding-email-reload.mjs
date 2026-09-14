// Reload-after-Email-step reload bug (2026-09-14): completing the Email step,
// then reloading the page, permanently stranded the user in onboarding.
// `email` is local React state — it resets to "" on remount — and BOTH
// completion paths ("Enter no_human" on Launch, and the Repositories step's
// "Skip setup — open the board" shortcut) call `ensureEmailRegistered()`,
// which used to refuse on that empty local state alone, never asking the
// server whether an address was already on file. The fix: `GET
// /api/onboarding/status` now reports a derived `email_registered` boolean
// (never the address itself — see `_ONBOARDING_STATUS_REDACTED_FIELDS` in
// app.py), and both completion paths consult it before refusing.
//
// This suite drives the BUILT bundle against a stateful mock of the onboarding
// API (no :8420) and asserts, end to end:
//   AC1 — reload -> jump straight to Launch -> "Enter no_human" completes.
//   AC2 — reload -> jump straight to Repositories -> "Skip setup" completes.
//   AC3 — no address anywhere (fresh install, nothing typed) still refuses,
//         VISIBLY (role="alert"), and lands the user back on the Email step.
//   AC4 — the stepper never marks Email "done" while completion would refuse,
//         and does mark it done once an address is actually on file.
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const DIST = new URL("../dist", import.meta.url).pathname;
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
const srv = http.createServer((q, r) => {
  const u = q.url.split("?")[0];
  let f = path.join(DIST, u === "/" ? "index.html" : u);
  if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(DIST, "index.html");
  r.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream" });
  r.end(fs.readFileSync(f));
});
await new Promise((r) => srv.listen(4646, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const browser = await chromium.launch();

// A fresh, stateful mock of just the routes this walk touches. `state.email`
// tracks only WHETHER an address is registered, mirroring the server's own
// redaction contract — the mock never has an address to leak either.
function installRoutes(page, state) {
  return page.route("**/api/**", (route) => {
    const req = route.request();
    const u = req.url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/onboarding/status")) {
      return j({ completed: state.completed, email_registered: state.emailRegistered });
    }
    if (u.includes("/api/onboarding/email") && req.method() === "POST") {
      state.emailRegistered = true;
      state.hits.add("email");
      return j({ ok: true });
    }
    if (u.includes("/api/onboarding/complete") && req.method() === "POST") {
      state.completed = true;
      state.hits.add("complete");
      return j({ ok: true, onboarding: { completed: true, deferred: [] } });
    }
    if (u.includes("/api/repos/discover")) {
      const now = Math.floor(Date.now() / 1000);
      return j({
        repos: [
          { path: "/Users/me/git/alpha-svc", name: "alpha-svc", is_git: true, branch: "main", dirty: false, dirty_scan: "complete", ecosystem: "python", mtime: now - 3600 },
        ],
        roots_scanned: ["/Users/me/git"], roots: ["/Users/me/git"],
        roots_missing: [], roots_refused: [], refused: [], home_direct: 0,
        total_found: 1, limit: 200, capped: false, walk_truncated: false, note: "", elapsed_ms: 3,
      });
    }
    if (u.includes("/api/onboarding/readiness")) return j({});
    if (u.includes("/api/tasks")) return j([]);
    return j({});
  });
}

const EMAIL = "reload-walker@example.com";

// Fills the Email step's field and clicks Continue whenever it is on screen;
// otherwise just clicks Continue. Same idiom as onboarding-minimal-path.mjs /
// onboarding-a11y.mjs — the Email step's Continue is `disabled` until the
// field holds a well-formed address, so a bare click would sit on
// Playwright's actionability wait.
async function cont(page) {
  const field = page.getByPlaceholder("you@example.com");
  if (await field.isVisible().catch(() => false)) {
    await field.fill(EMAIL);
    await page.waitForTimeout(100);
  }
  await page.getByRole("button", { name: /^Continue$/ }).click();
}

async function jumpTo(page, title) {
  await page.getByRole("button", { name: new RegExp(`^${title}, step \\d+ of \\d+`) }).click();
  await page.waitForTimeout(250);
}

async function fetchStatus(page) {
  return page.evaluate(() => fetch("/api/onboarding/status").then((r) => r.json()));
}

// ── AC1: reload after the Email step, then Launch → "Enter no_human" ───────
async function ac1() {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const state = { emailRegistered: false, completed: false, hits: new Set() };
  await installRoutes(page, state);

  await page.goto("http://127.0.0.1:4646/", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);

  await cont(page); // Welcome -> Email
  await cont(page); // fills EMAIL, Continue submits it, Email -> Repos
  await page.waitForTimeout(200);
  check("[AC1] the email registration request was sent before reload", state.hits.has("email"));

  // The reload: a fresh mount, `email` local state wiped back to "". The
  // server still has the address on file (state.emailRegistered stays true —
  // untouched by remounting the page).
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(400);

  // Jump straight to Launch — the stepper lets any step reach any other,
  // exactly the bypass the bug report's "reload then jump" scenario needs
  // (no re-visiting Email, no re-typing the address).
  await jumpTo(page, "Launch");
  check("[AC1] reached the Launch step by jumping via the stepper",
    await page.getByRole("button", { name: /^(Enter no_human|Create your first task)/ }).isVisible().catch(() => false));

  const launchBtn = page.getByRole("button", { name: /^Enter no_human$/ });
  await launchBtn.click();
  await page.waitForTimeout(400);

  check("[AC1] POST /api/onboarding/complete was issued", state.hits.has("complete"));
  const status = await fetchStatus(page);
  check("[AC1] GET /api/onboarding/status now reports completed: true", status.completed === true,
    JSON.stringify(status));
  check("[AC1] no visible refusal was raised", !(await page.locator('[role="alert"]').isVisible().catch(() => false)));
  check("[AC1] no page errors", errors.length === 0, errors[0] || "");

  await ctx.close();
}

// ── AC2: reload after the Email step, then Repositories → "Skip setup" ─────
async function ac2() {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const state = { emailRegistered: false, completed: false, hits: new Set() };
  await installRoutes(page, state);

  await page.goto("http://127.0.0.1:4646/", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);

  await cont(page); // Welcome -> Email
  await cont(page); // registers EMAIL, Email -> Repos
  await page.waitForTimeout(200);
  check("[AC2] the email registration request was sent before reload", state.hits.has("email"));

  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(400);

  await jumpTo(page, "Repositories");
  check("[AC2] reached the Repositories step by jumping via the stepper",
    await page.getByRole("heading", { name: /Which repositories do you work on/i }).isVisible().catch(() => false));

  await page.getByRole("button", { name: "Add alpha-svc" }).click();
  await page.waitForTimeout(200);
  const skipBtn = page.getByRole("button", { name: /^Skip setup — open the board$/ });
  check("[AC2] 'Skip setup — open the board' is available once a repo is ticked",
    await skipBtn.isVisible().catch(() => false));
  await skipBtn.click();
  await page.waitForTimeout(400);

  check("[AC2] POST /api/onboarding/complete was issued", state.hits.has("complete"));
  const status = await fetchStatus(page);
  check("[AC2] GET /api/onboarding/status now reports completed: true", status.completed === true,
    JSON.stringify(status));
  check("[AC2] no visible refusal was raised", !(await page.locator('[role="alert"]').isVisible().catch(() => false)));
  check("[AC2] no page errors", errors.length === 0, errors[0] || "");

  await ctx.close();
}

// ── AC3: no address anywhere — still refuses, VISIBLY, and lands on Email ──
async function ac3() {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const state = { emailRegistered: false, completed: false, hits: new Set() };
  await installRoutes(page, state);

  await page.goto("http://127.0.0.1:4646/", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);

  // Never visit the Email step at all — jump straight from Welcome to Launch,
  // the same stepper bypass finish()/ensureEmailRegistered()'s comment names
  // explicitly. Neither this mount nor the server has ever seen an address.
  await jumpTo(page, "Launch");
  const launchBtn = page.getByRole("button", { name: /^Enter no_human$/ });
  check("[AC3] reached the Launch step with the button visible",
    await launchBtn.isVisible().catch(() => false));
  await launchBtn.click();
  await page.waitForTimeout(400);

  check("[AC3] completion was refused: no POST /api/onboarding/complete", !state.hits.has("complete"));
  const alert = page.locator('[role="alert"]');
  check("[AC3] the refusal is shown to the user via role=\"alert\", not only thrown",
    await alert.isVisible().catch(() => false));
  const alertText = await alert.textContent().catch(() => "");
  check("[AC3] the visible message explains an address is required",
    /email/i.test(alertText || ""), alertText || "(none)");
  check("[AC3] the wizard lands back on the Email step",
    await page.getByRole("heading", { name: /One email address for this install/i }).isVisible().catch(() => false));
  const status = await fetchStatus(page);
  check("[AC3] the server still reports nothing completed", status.completed === false, JSON.stringify(status));
  check("[AC3] no page errors", errors.length === 0, errors[0] || "");

  await ctx.close();
}

// ── AC4: the stepper never lies about Email being done ─────────────────────
async function ac4() {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const state = { emailRegistered: false, completed: false, hits: new Set() };
  await installRoutes(page, state);

  await page.goto("http://127.0.0.1:4646/", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);

  // Negative control: jump PAST Email without ever registering an address.
  await jumpTo(page, "Repositories");
  const emailStepUnsatisfied = page.getByRole("button", { name: /^Email, step \d+ of \d+, completed$/ });
  check("[AC4] Email's stepper dot is NOT 'completed' while completion would still refuse",
    !(await emailStepUnsatisfied.isVisible().catch(() => false)));
  const emailStepNotStarted = page.getByRole("button", { name: /^Email, step \d+ of \d+, not started$/ });
  check("[AC4] Email's stepper dot instead reads 'not started'",
    await emailStepNotStarted.isVisible().catch(() => false));

  // Positive control: actually register the address, then jump forward again.
  await jumpTo(page, "Email");
  await cont(page); // fills EMAIL, registers it, Email -> Repos
  await page.waitForTimeout(200);
  check("[AC4] the email registration request was sent", state.hits.has("email"));
  await jumpTo(page, "Projects");
  const emailStepSatisfied = page.getByRole("button", { name: /^Email, step \d+ of \d+, completed$/ });
  check("[AC4] Email's stepper dot reads 'completed' once an address is actually on file",
    await emailStepSatisfied.isVisible().catch(() => false));

  check("[AC4] no page errors", errors.length === 0, errors[0] || "");

  await ctx.close();
}

await ac1();
await ac2();
await ac3();
await ac4();

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
