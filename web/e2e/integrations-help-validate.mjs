// Onboarding "Connect your tools" step: per-field help catalogue (C1) and
// fail-closed live validation (C2). A real user could not tell what "Linear
// team key" meant, and a green check meant "a key was typed", not "the
// connection works". Mocked API, serves web/dist, no :8420.
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
await new Promise((r) => srv.listen(4649, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

// Shaped like GET /api/integrations/setup (integrations/__init__.py setup_specs),
// with the help/help_url the server now emits per field and the `verified` flag
// C2 added.
const linearSpec = ({ enabled = false, configured = false, verified = false, teamKey = "" } = {}) => ({
  name: "linear", kind: "issue_tracker", enable_field: "enabled",
  enabled, enable_default: false, configured, detail: configured ? "team ENG" : "not configured",
  verified,
  // Field VALUES mirror the block, exactly as setup_specs emits them — so the
  // card's isOn (draftFrom → effectiveEnabled) agrees with the top-level flags.
  fields: [
    { name: "enabled", label: "Enabled", kind: "bool", value: enabled,
      help: "Turn polling on or off for this integration.", help_url: "https://example.com/docs" },
    { name: "team_key", label: "Team key", kind: "text", value: teamKey,
      help: "The prefix of your issue ids — ENG in ENG-123. Linear → Settings → Teams.",
      help_url: "https://linear.app/settings/teams" },
    { name: "default_repo", label: "Run tasks in repo", kind: "repo_select", value: "",
      options: ["/repos/proj1", "/repos/proj2"],
      help: "The repository the coder works in when an issue from this tracker is pulled in.",
      help_url: "https://example.com/docs" },
  ],
  secrets: [{ env_var: "LINEAR_API_KEY", set: true }],
  secret_note: "",
});

async function gotoIntegrations(page, { savedSpec, testResponse } = {}) {
  await page.route("**/api/**", (route) => {
    const req = route.request();
    const u = req.url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/onboarding/status")) return j({ completed: false });
    if (u.includes("/api/repos/discover")) return j({ repos: [], roots_scanned: [], roots_refused: [], walk_truncated: false, capped: false, note: "" });
    if (u.includes("/api/onboarding/readiness")) return j({ total: 0, usable: 0, first_usable: null, needs_proving: [] });
    if (u.includes("/api/onboarding/history/extract")) return j({ available: false });
    if (u.includes("/api/integrations/linear/setup") && req.method() === "PUT")
      return j(savedSpec || linearSpec({ configured: true }));
    if (u.includes("/api/integrations/linear/test")) return j(testResponse || {});
    if (u.includes("/api/integrations/setup")) return j({ integrations: [linearSpec()] });
    if (u.includes("/api/tasks")) return j([]);
    return j({});
  });
  await page.goto("http://127.0.0.1:4649/", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  // The Email step (required, not skippable) sits between Welcome and
  // Repositories, and its Continue is `disabled` until the field holds a
  // well-formed address — a bare click would sit on Playwright's enabled
  // actionability check for 30s and then TimeoutError. Go THROUGH the step
  // the way a user does: whenever the address field is on screen, type into
  // it, then Continue (same idiom as onboarding-consent-step.mjs /
  // onboarding-summary-counts.mjs).
  const EMAIL = "walker@example.com";
  const cont = async () => {
    const field = page.getByPlaceholder("you@example.com");
    if (await field.isVisible().catch(() => false)) {
      await field.fill(EMAIL);
      await page.waitForTimeout(100);
    }
    await page.getByRole("button", { name: /^Continue$/ }).click();
    await page.waitForTimeout(150);
  };
  // welcome -> email -> repos -> projects -> integrations. Hop until the
  // integrations step is actually on screen rather than a fixed hop count —
  // the step list (src/onboardingSteps.js) has changed shape more than once.
  for (let hop = 0; hop < 8; hop++) {
    if (await page.getByRole("checkbox", { name: /Enable Linear/ }).isVisible().catch(() => false)) break;
    await cont();
  }
  // Turn Linear on so its fields (and their hints) render.
  await page.getByRole("checkbox", { name: /Enable Linear/ }).check();
  await page.waitForTimeout(150);
}

const chipText = (page) => page.locator(".ob-integration-card .integration-chip").first().innerText();

const browser = await chromium.launch();

// ── Scenario 1: C1 — the field help catalogue ──────────────────────────────
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await gotoIntegrations(page);

  const toggle = page.getByRole("button", { name: /How to find this/ }).first();
  check("team_key shows a 'How to find this' hint toggle", await toggle.isVisible().catch(() => false));
  check("hint starts collapsed", (await toggle.getAttribute("aria-expanded")) === "false");
  await toggle.click();
  await page.waitForTimeout(100);
  check("hint expands", (await toggle.getAttribute("aria-expanded")) === "true");
  const hintText = await page.locator("#hint-linear-team_key").innerText().catch(() => "");
  check("expanded hint explains the team key with ENG-123", hintText.includes("ENG-123"), `got "${hintText}"`);
  check("expanded hint links out to the vendor page", hintText.includes("Open linear.app"), `got "${hintText}"`);
  // C3: "Run tasks in repo" is a select over registered repos, not free text,
  // and the step explains GitHub/GitLab come from the repo profile.
  const repoSelect = page.locator("select#ob-int-linear-default_repo");
  check("default_repo renders as a <select>", await repoSelect.count() === 1);
  const optCount = await repoSelect.locator("option").count().catch(() => 0);
  check("the select lists the registered repos (+ a 'none' option)", optCount === 3, `options=${optCount}`);
  check("the step explains GitHub/GitLab/CI come from the repo profile",
    await page.getByText(/configured per repository from its profile/).isVisible().catch(() => false));
  check("no page errors (C1)", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario 2: C2 — Save runs the test; a PASS shows "Ready" ───────────────
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await gotoIntegrations(page, {
    savedSpec: linearSpec({ enabled: true, configured: true, teamKey: "ENG" }),
    testResponse: { healthy: true, detail: "connected as amit · team ENG found" },
  });
  await page.getByLabel("Team key").fill("ENG");
  await page.waitForTimeout(100);
  await page.getByRole("button", { name: /^Save$/ }).click();
  await page.waitForTimeout(300);
  check("a passing test shows the '✓ Connected' verdict", await page.getByText(/✓ Connected/).isVisible().catch(() => false));
  check("the chip reads Ready only after a passing test", (await chipText(page)) === "Ready", `chip="${await chipText(page).catch(() => "?")}"`);
  check("the passing detail is shown", await page.getByText(/team ENG found/).isVisible().catch(() => false));
  check("no page errors (C2 pass)", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario 3: C2 fail-closed — a FAILING test never shows "Ready" ─────────
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await gotoIntegrations(page, {
    savedSpec: linearSpec({ enabled: true, configured: true, teamKey: "ENG" }),
    testResponse: { healthy: false, detail: "401 — key rejected" },
  });
  await page.getByLabel("Team key").fill("ENG");
  await page.waitForTimeout(100);
  await page.getByRole("button", { name: /^Save$/ }).click();
  await page.waitForTimeout(300);
  check("a failing test shows '✗ Not verified'", await page.getByText(/✗ Not verified/).isVisible().catch(() => false));
  check("the chip reads 'Saved — not verified', NOT Ready", (await chipText(page)) === "Saved — not verified",
    `chip="${await chipText(page).catch(() => "?")}"`);
  check("the rejection reason is shown", await page.getByText(/401 — key rejected/).isVisible().catch(() => false));
  check("no page errors (C2 fail)", errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
