// The "Community" step (operator, 2026-09-12): every first-run user is
// offered the Discord invite once, in the wizard they're already looking at.
// Two paths, both pinned in one run:
//   A) opens it — a real anchor, target=_blank, to the one URL the app
//      carries in source (web/src/community.js), routed to a real OS-browser
//      popup, and the wizard tab itself never navigates away.
//   B) ignores it — Continue is never gated by this step; Launch renders
//      exactly as it does for every other install.
// No widget, no iframe, no request to any discord.* host — this asserts that
// too. Mocked API, no :8420.
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { DISCORD_INVITE_URL } from "../src/community.js";

const DIST = new URL("../dist", import.meta.url).pathname;
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
const srv = http.createServer((q, r) => {
  const u = q.url.split("?")[0];
  let f = path.join(DIST, u === "/" ? "index.html" : u);
  if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(DIST, "index.html");
  r.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream" });
  r.end(fs.readFileSync(f));
});
await new Promise((r) => srv.listen(4645, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

// Routes are registered on the CONTEXT, not the page: a target="_blank" click
// opens a new Page in the same context, and a page-scoped route would leave
// that popup's navigation completely unmocked — which is exactly how the
// first version of this test accidentally let a popup travel over the real
// network to Discord's live redirect (discord.gg -> discord.com/invite/...)
// instead of asserting anything about what THIS APP requested.
async function mockApi(ctx, page) {
  const allHits = []; // every request to a discord.* host, from any page in ctx
  ctx.on("request", (req) => {
    let host;
    try { host = new URL(req.url()).hostname; } catch { return; }
    if (!/(^|\.)discord\.(gg|com)/.test(host)) return;
    // A popup's very first navigation request fires before Playwright has a
    // Frame to hand back — frame() throws in that exact window. That failure
    // IS the signal: only a brand-new page (the popup) can produce it, so it
    // reliably means "not the app tab" without racing to read frame().page().
    let fromApp = false;
    try { fromApp = req.frame().page() === page; } catch { fromApp = false; }
    allHits.push({ url: req.url(), fromApp });
  });
  await ctx.route("**/api/**", (route) => {
    const u = route.request().url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/onboarding/status")) return j({ completed: false });
    if (u.includes("/api/repos/discover")) return j({
      repos: [], roots_scanned: [], roots: [], roots_missing: [], roots_refused: [],
      refused: [], home_direct: 0, total_found: 0, limit: 200, capped: false,
      walk_truncated: false, note: "", elapsed_ms: 1,
    });
    if (u.includes("/api/tasks")) return j([]);
    if (u.includes("/api/onboarding/readiness")) return j({ total: 0, usable: 0, first_usable: null, needs_proving: [] });
    return j({});
  });
  // The invite domain must never actually be reached — not from the app page
  // (no widget, no render-time fetch, no prefetch) and not even from the
  // popup: aborting BEFORE the request leaves the browser means the assertion
  // on its URL is the literal href we clicked, never whatever Discord's own
  // live redirect chain happens to answer with today.
  await ctx.route("https://discord.gg/**", (route) => route.abort());
  await ctx.route("https://discord.com/**", (route) => route.abort());
  return allHits;
}

const browser = await chromium.launch();

// ── Path A: a first-run user opens the invite ───────────────────────────
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const discordHits = await mockApi(ctx, page);

  await page.goto("http://127.0.0.1:4645/", { waitUntil: "networkidle" });
  await page.waitForTimeout(400);

  const communityStep = page.getByRole("button", { name: /^Community, step 5 of 6/ });
  check("the step indicator exposes a 'Community' step button", await communityStep.isVisible().catch(() => false));
  await communityStep.click();
  await page.waitForTimeout(300);

  check("the Community step's headline renders",
    await page.getByRole("heading", { name: /There is a Discord/ }).isVisible().catch(() => false));

  const link = page.locator("a.ob-btn-ghost", { hasText: "Open the invite" });
  check("the invite anchor is visible", await link.isVisible().catch(() => false));
  const href = await link.getAttribute("href");
  check("the anchor's href is exactly the app's one constant", href === DISCORD_INVITE_URL, `href=${href}`);
  check("the anchor opens in a new tab", (await link.getAttribute("target")) === "_blank");
  const rel = (await link.getAttribute("rel")) || "";
  check("the anchor carries noreferrer noopener", /noreferrer/.test(rel) && /noopener/.test(rel), `rel=${rel}`);

  check("no <iframe> is present anywhere on the step", (await page.locator("iframe").count()) === 0);
  check("before clicking, nothing has touched a discord.* host", discordHits.length === 0, JSON.stringify(discordHits));

  const [popup] = await Promise.all([
    ctx.waitForEvent("page"),
    link.click(),
  ]);
  await page.waitForTimeout(300); // aborted nav never commits; give the abort a beat to land

  const popupHits = discordHits.filter((h) => !h.fromApp);
  check("clicking the invite attempted exactly one request, to the exact invite URL",
    popupHits.length === 1 && popupHits[0].url === DISCORD_INVITE_URL,
    JSON.stringify(popupHits));
  await popup.close().catch(() => {});
  check("the wizard tab itself never navigated away", page.url() === "http://127.0.0.1:4645/", `page url=${page.url()}`);

  const appHits = discordHits.filter((h) => h.fromApp);
  check("the app page itself never made a request to a discord.* host", appHits.length === 0, JSON.stringify(appHits));
  check("no page errors on the Community step (open path)", errors.length === 0, errors[0] || "");

  await ctx.close();
}

// ── Path B: a first-run user ignores it and still reaches Launch ────────
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const discordHits = await mockApi(ctx, page);

  await page.goto("http://127.0.0.1:4645/", { waitUntil: "networkidle" });
  await page.waitForTimeout(400);

  const communityStep = page.getByRole("button", { name: /^Community, step 5 of 6/ });
  await communityStep.click();
  await page.waitForTimeout(300);

  const continueBtn = page.getByRole("button", { name: "Continue" });
  check("Continue is enabled on the Community step — nothing here gates it",
    await continueBtn.isEnabled().catch(() => false));
  await continueBtn.click();
  await page.waitForTimeout(300);

  check("ignoring the step still reaches the Launch headline",
    await page.getByRole("heading", { name: /^(Ready\.|Almost ready\.|Checking…)$/ }).isVisible().catch(() => false));
  check("the terminal button renders as usual",
    await page.getByRole("button", { name: /Enter no_human|Create your first task/ }).isVisible().catch(() => false));

  check("the page never made a request to a discord.* host (ignore path)", discordHits.length === 0, JSON.stringify(discordHits));
  check("no page errors on the Community step (ignore path)", errors.length === 0, errors[0] || "");

  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
