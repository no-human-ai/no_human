// a11y guard for the onboarding flow's icon-only remove buttons.
// The "✕" remove-project control in the Projects step renders only glyph
// content; without an accessible name a screen reader announces nothing
// meaningful. This drives the real flow in a browser and asserts it carries an
// aria-label. Mocked API, no :8420. (The Docs step's ListEditor and its
// remove-entry ✕ were retired in B4; the Docs step itself left the wizard on
// the operator's 2026-09-04 ruling — wiki generation is now enqueued
// automatically at Launch — so the walk now anchors on the Integrations step
// heading instead.)
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
await new Promise((r) => srv.listen(4640, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
// Onboarding renders only when status is NOT completed.
await page.route("**/api/**", (route) => {
  const u = route.request().url();
  const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  if (u.includes("/api/onboarding/status")) return j({ completed: false });
  if (u.includes("/api/repos/discover")) {
    const now = Math.floor(Date.now() / 1000);
    return j({
      repos: [
        { path: "/Users/me/git/alpha-svc", name: "alpha-svc", is_git: true, branch: "main", dirty: false, dirty_scan: "complete", ecosystem: "python", mtime: now - 3600 },
        { path: "/Users/me/git/beta-web", name: "beta-web", is_git: true, branch: "main", dirty: false, dirty_scan: "complete", ecosystem: "node", mtime: now - 7200 },
      ],
      roots_scanned: ["/Users/me/git"], roots: ["/Users/me/git"],
      roots_missing: [], roots_refused: [], refused: [], home_direct: 0,
      total_found: 2, limit: 200, capped: false, walk_truncated: false, note: "", elapsed_ms: 3,
    });
  }
  if (u.includes("/api/tasks")) return j([]);
  return j({});
});
await page.goto("http://127.0.0.1:4640/", { waitUntil: "networkidle" });
await page.waitForTimeout(400);

const cont = () => page.getByRole("button", { name: /^Continue$/ }).click();

// Repos step: the recently-worked-on cards each carry an icon-ish "Add" button
// whose only text is "Add". Without an accessible name naming the repo, a
// screen reader announces "Add" three times with no way to tell them apart.
// Walk to the repos step and assert every Add button names its repo.
for (let hop = 0; hop < 6; hop++) {
  if (await page.getByRole("heading", { name: /Which repositories do you work on/i })
      .isVisible().catch(() => false)) break;
  await cont(); await page.waitForTimeout(250);
}
await page.waitForTimeout(300);
const addButtons = page.getByRole("button", { name: /^(Add|Remove) / });
const addCount = await addButtons.count();
check("recently-worked-on cards rendered with Add buttons", addCount >= 2, `saw ${addCount}`);
let namedOk = addCount >= 2;
for (const name of ["alpha-svc", "beta-web"]) {
  const named = page.getByRole("button", { name: `Add ${name}` });
  const ok = await named.isVisible().catch(() => false);
  if (!ok) namedOk = false;
}
check("every Add button has an accessible name \"Add <repo name>\"", namedOk,
  `expected "Add alpha-svc" and "Add beta-web"`);

// Tick a repo before moving on: a project seeded with no repos now blocks
// Continue (spec §3 B2 validate-at-the-step), which would otherwise trap this
// walk on the Projects step. Ticking one keeps the added project non-empty.
await page.getByRole("button", { name: "Add alpha-svc" }).click();
await page.waitForTimeout(150);

// Walk Continues until the Projects step's heading renders (bounded): the
// suite used to hardcode 3 Continues for a welcome->team->repos->projects
// order, and the step list has changed shape once already (the team step is
// gone) — counting steps is exactly what went stale (2026-08-19 fix).
for (let hop = 0; hop < 6; hop++) {
  if (await page.getByRole("heading", { name: /Group repos into projects/i })
      .isVisible().catch(() => false)) break;
  await cont(); await page.waitForTimeout(250);
}

// Projects step: add a project, then the remove-project ✕ appears.
const NAME = "metrics-core";
const projInput = page.getByPlaceholder(/Project name/i);
check("reached the Projects step",
  await projInput.waitFor({ state: "visible", timeout: 5000 }).then(() => true).catch(() => false));
await projInput.fill(NAME);
await projInput.press("Enter");
await page.waitForTimeout(300);
const removeProj = page.getByRole("button", { name: `Remove project ${NAME}` });
check("remove-project button has a descriptive aria-label",
  await removeProj.isVisible().catch(() => false),
  `looked for aria-label "Remove project ${NAME}"`);

// The Docs step left the wizard 2026-09-04 (wiki generation is enqueued
// automatically at Launch); one Continue from Projects now lands directly on
// Integrations. Assert that step's heading rendered so the walk is anchored.
await cont(); await page.waitForTimeout(300);
const integrationsHeading = page.getByRole("heading", { name: /Connect your tools/i });
check("reached the Integrations step",
  await integrationsHeading.isVisible().catch(() => false));

check("no page errors during onboarding", errors.length === 0, errors[0] || "");

await ctx.close();
await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
