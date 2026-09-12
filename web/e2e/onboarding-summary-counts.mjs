// Regression guard for the launch-summary count bug (real-user walk,
// 2026-08-15): after registering exactly one repo, "Repos" read 0 while
// "Repos with a proven test command" read "0 of 1" for the SAME repo — one
// row read the wizard's local tick state, the other read the server's
// readiness payload. Both must now agree, sourced from one place
// (summaryRepoCounts, see src/onboardingSummary.js). Mocked API, no :8420.
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
await new Promise((r) => srv.listen(4643, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const ONE_REPO = { path: "/home/user/proj", name: "proj", is_git: true, branch: "main", detached: false, dirty: false, dirty_scan: "clean" };

// Reads the "<span>label</span><b>value</b>" row inside .ob-summary, matched
// by the span's EXACT text — "Repos" must not also match "Repos with a
// proven test command" via substring.
async function rowValue(page, label) {
  const li = page.locator(".ob-summary li").filter({ has: page.locator("span", { hasText: new RegExp(`^${label}$`) }) });
  return li.locator("b").innerText();
}

async function runScenario(browser, { name, repos, readiness, tick, expectFix }) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/**", (route) => {
    const req = route.request();
    const u = req.url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/onboarding/status")) return j({ completed: false });
    if (u.includes("/api/repos/discover")) return j({ repos, roots_scanned: [], roots_refused: [], walk_truncated: false, capped: false, note: "" });
    if (u.includes("/api/onboarding/repos/onboard")) return j({ path: repos[0]?.path, ecosystem: "node", is_usable: false, test_proven: false });
    if (u.includes("/api/onboarding/readiness")) return j(readiness);
    if (u.includes("/api/onboarding/history/extract")) return j({ available: false });
    if (u.includes("/api/integrations/setup")) return j({ integrations: [] });
    if (u.includes("/api/tasks")) return j([]);
    return j({});
  });
  await page.goto("http://127.0.0.1:4643/", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);

  // The Email step (required, not skippable) sits between Welcome and
  // Repositories, and its Continue is `disabled` until the field holds a
  // well-formed address — a bare click would sit on Playwright's enabled
  // actionability check for 30s. Go THROUGH the step the way a user does:
  // whenever the address field is on screen, type into it, then Continue.
  const EMAIL = "walker@example.com";
  const cont = async () => {
    const field = page.getByPlaceholder("you@example.com");
    if (await field.isVisible().catch(() => false)) {
      await field.fill(EMAIL);
      await page.waitForTimeout(100);
    }
    await page.getByRole("button", { name: /^Continue$/ }).click();
    await page.waitForTimeout(200);
  };

  // welcome -> repos. Hop until the Repositories step is actually on screen
  // rather than counting hops: the Email step now sits between the two, and a
  // fixed single hop would land on it (same idiom as
  // onboarding-recent-card-layout.mjs).
  for (let hop = 0; hop < 6; hop++) {
    if (await page.getByRole("heading", { name: /Which repositories do you work on/i })
        .isVisible().catch(() => false)) break;
    await cont();
  }
  await page.waitForTimeout(300); // let the repos-step discovery scan land.

  if (tick && repos.length) {
    await page.locator(".ob-repo").first().locator('input[type="checkbox"]').check();
    await page.waitForTimeout(150);
  }

  // repos -> projects -> docs -> integrations -> history -> rules -> summary.
  for (let i = 0; i < 6; i++) await cont();

  const label = page.getByText("Repos with a proven test command");
  check(`[${name}] reached the Launch summary`, await label.isVisible().catch(() => false));

  const reposValue = await rowValue(page, "Repos").catch(() => null);
  const provenValue = await rowValue(page, "Repos with a proven test command").catch(() => null);

  check(`[${name}] Repos row = "${repos.length ? "1" : "0"}"`, reposValue === (readiness.total ? String(readiness.total) : "0"),
    `got "${reposValue}"`);
  // m6: with no registered repos the proven row is an em dash, not "0 of 0".
  const expectedProven = readiness.total ? `${readiness.usable} of ${readiness.total}` : "—";
  check(`[${name}] proven row = "${expectedProven}"`, provenValue === expectedProven,
    `got "${provenValue}"`);
  if (readiness.total) {
    check(`[${name}] both rows agree on the repo count`,
      reposValue !== null && provenValue !== null && reposValue === provenValue.split(" of ")[1],
      `Repos="${reposValue}" vs proven="${provenValue}"`);
  }
  // Launch-card readiness rows (spec §3 B2): each unmet step shows a "Fix →"
  // that jumps to it. A repo-less run has a "Fix →" for Repositories; a ticked
  // run has none.
  const fixButtons = page.getByRole("button", { name: /^Fix →$/ });
  const fixCount = await fixButtons.count();
  check(`[${name}] readiness Fix rows = ${expectFix}`, fixCount === expectFix, `saw ${fixCount}`);
  if (expectFix > 0) {
    await fixButtons.first().click();
    await page.waitForTimeout(200);
    check(`[${name}] Fix → jumped to the Repositories step`,
      await page.getByRole("heading", { name: /Which repositories do you work on/i }).isVisible().catch(() => false));
  }
  check(`[${name}] no page errors`, errors.length === 0, errors[0] || "");

  await ctx.close();
}

const browser = await chromium.launch();

// Scenario 1: repo registered AND ticked in this mount.
await runScenario(browser, {
  name: "registered-and-ticked",
  repos: [ONE_REPO],
  readiness: { total: 1, usable: 0, first_usable: null, needs_proving: [ONE_REPO.path] },
  tick: true,
  expectFix: 0,
});

// Scenario 2 (the regression itself): repo persisted server-side (readiness
// says total:1) but NOT ticked in this mount — e.g. a reload/re-run. Before
// the fix, "Repos" read selectedRepos.size === 0 here while the proven row
// still read the server's "0 of 1".
await runScenario(browser, {
  name: "registered-but-not-ticked",
  repos: [ONE_REPO],
  readiness: { total: 1, usable: 0, first_usable: null, needs_proving: [ONE_REPO.path] },
  tick: false,
  expectFix: 1,
});

// Scenario 3: zero-repo control.
await runScenario(browser, {
  name: "zero-repo-control",
  repos: [],
  readiness: { total: 0, usable: 0, first_usable: null, needs_proving: [] },
  tick: false,
  expectFix: 1,
});

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
