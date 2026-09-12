// Regression gate for the "Recently worked on" card layout bug: once a card
// is Added and profiled, the name/path used to collapse to a ~40px sliver
// ("tiny-…", "/pri…") and the status line wrapped one word per line, because
// the single card sat in an auto-fill ~210px grid column and the Prove panel
// claimed the `auto` track at max-content width. This suite adds + profiles
// the one recent repo at 1440x900 (dark, the default theme) and measures the
// actual DOM geometry, not just presence. Mocked API, no :8420.
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const DIST = new URL("../dist", import.meta.url).pathname;
const OUT = new URL("./shots", import.meta.url).pathname;
fs.mkdirSync(OUT, { recursive: true });
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
const srv = http.createServer((q, r) => {
  const u = q.url.split("?")[0];
  let f = path.join(DIST, u === "/" ? "index.html" : u);
  if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(DIST, "index.html");
  r.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream" });
  r.end(fs.readFileSync(f));
});
await new Promise((r) => srv.listen(4652, r));

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

const REPO_NAME = "tiny-proj-fixture";
const REPO_PATH = "/private/var/folders/tmp/tiny-proj-fixture";

await page.route("**/api/**", (route) => {
  const req = route.request();
  const u = req.url();
  const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  if (u.includes("/api/onboarding/status")) return j({ completed: false });
  if (u.includes("/api/repos/discover")) {
    const now = Math.floor(Date.now() / 1000);
    return j({
      repos: [
        { path: REPO_PATH, name: REPO_NAME, is_git: true, branch: "main", dirty: false, dirty_scan: "complete", ecosystem: "node", mtime: now - 600 },
      ],
      roots_scanned: ["/private/var/folders/tmp"], roots: ["/private/var/folders/tmp"],
      roots_missing: [], roots_refused: [], refused: [], home_direct: 0,
      total_found: 1, limit: 200, capped: false, walk_truncated: false, note: "", elapsed_ms: 3,
    });
  }
  if (u.includes("/api/onboarding/repos/onboard")) {
    return j({ ecosystem: "node", test_cmd: "npm test", test_proven: false, is_usable: false });
  }
  if (u.includes("/api/onboarding/deferred")) return j({ deferred: [] });
  if (u.includes("/api/tasks")) return j([]);
  return j({});
});
await page.goto("http://127.0.0.1:4652/", { waitUntil: "networkidle" });
await page.waitForTimeout(400);

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
};
for (let hop = 0; hop < 6; hop++) {
  if (await page.getByRole("heading", { name: /Which repositories do you work on/i })
      .isVisible().catch(() => false)) break;
  await cont(); await page.waitForTimeout(200);
}

await page.getByRole("button", { name: `Add ${REPO_NAME}` }).click();
await page.waitForTimeout(200);
await page.getByRole("button", { name: /^Profile 1 repo here$/ }).click();
await page.waitForSelector(".ob-repo-card .ob-prove");
await page.waitForTimeout(200);

const nameEl = page.locator(".ob-repo-card-name").first();
const pathEl = page.locator(".ob-repo-card-path").first();
const statusEl = page.locator(".ob-repo-card .ob-repo-status").first();
const proveEl = page.locator(".ob-repo-card .ob-prove").first();

// 1. Full name renders, nothing clipped.
const nameOverflow = await nameEl.evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth, text: el.textContent }));
check("repo name is not clipped (scrollWidth <= clientWidth)", nameOverflow.scroll <= nameOverflow.client + 1,
  `scrollWidth=${nameOverflow.scroll} clientWidth=${nameOverflow.client}`);
check("repo name text is the full mocked name", nameOverflow.text === REPO_NAME, `got "${nameOverflow.text}"`);

// 2. Left column is not a ~40px sliver (the exact reported regression).
const nameBox = await nameEl.boundingBox();
check("name column width is well above the ~40px collapse", nameBox && nameBox.width > 200,
  `width=${nameBox?.width}`);

// 3. Status renders on one line.
const statusRects = await statusEl.evaluate((el) => el.getClientRects().length);
const statusLine = await statusEl.evaluate((el) => ({
  height: el.offsetHeight,
  lineHeight: parseFloat(getComputedStyle(el).lineHeight),
  text: el.textContent,
}));
check("status has a single client rect (one line, no per-word wrap)", statusRects === 1, `rects=${statusRects}`);
check("status height is under two line-heights", statusLine.height < 2 * statusLine.lineHeight,
  `height=${statusLine.height} lineHeight=${statusLine.lineHeight}`);
check("status reads as one readable line", statusLine.text.includes("node") && statusLine.text.includes("npm test"),
  `text="${statusLine.text}"`);

// 4. Path stays CSS end-ellipsis, not JS mid-truncated.
const pathStyle = await pathEl.evaluate((el) => ({
  textOverflow: getComputedStyle(el).textOverflow,
  whiteSpace: getComputedStyle(el).whiteSpace,
  text: el.textContent,
}));
check("path keeps CSS end-ellipsis (text-overflow: ellipsis)", pathStyle.textOverflow === "ellipsis",
  `text-overflow=${pathStyle.textOverflow}`);
check("path keeps single-line white-space: nowrap", pathStyle.whiteSpace === "nowrap", `white-space=${pathStyle.whiteSpace}`);
check("path textContent is untouched by JS truncation (no inserted …)", pathStyle.text === REPO_PATH, `got "${pathStyle.text}"`);

// 5. Prove block sits below the status (stacked, per the intake decision).
const statusBox = await statusEl.boundingBox();
const proveBox = await proveEl.boundingBox();
check("Prove block sits below the status line", statusBox && proveBox && proveBox.y >= statusBox.y + statusBox.height - 1,
  `statusBox=${JSON.stringify(statusBox)} proveBox=${JSON.stringify(proveBox)}`);

check("no page errors", errors.length === 0, errors[0] || "");

const cardEl = page.locator(".ob-repo-card").first();
await cardEl.screenshot({ path: `${OUT}/onboarding-recent-card-after.png` }).catch(() => {});

await ctx.close();
await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
