// Repeatable full-screen visual sweep — the "everything looks good" gate.
//
// Walks every screen in both themes at desktop + mobile widths, screenshots
// each to e2e/sweep-shots/, and — like e2e/live-flows.mjs — HARD-FAILS on any
// page error (a blank page can pass `vite build` and `node --test`; only a real
// render catches it). READ-ONLY: every non-GET request is aborted.
//
// Run against a live board:  node e2e/sweep.mjs   (BASE defaults to :8420;
// override with SWEEP_BASE). Writes e2e/sweep-shots/sweep-report.md.
import { chromium } from "playwright";
import fs from "node:fs";

const BASE = process.env.SWEEP_BASE || "http://127.0.0.1:8420";
const OUT = new URL("./sweep-shots", import.meta.url).pathname;
fs.mkdirSync(OUT, { recursive: true });

const pageErrors = [];
const shots = [];
const notes = [];

const b = await chromium.launch();
const ctx = await b.newContext({ viewport: { width: 1440, height: 950 } });
const page = await ctx.newPage();
// Hard-fail only on uncaught exceptions (a blank/broken render), like
// live-flows.mjs. Console errors (benign 404s/warnings) are not the signal.
page.on("pageerror", (e) => pageErrors.push(`${page.url()} :: ${e.message}`));
await page.route("**/*", (r) => (r.request().method() === "GET" ? r.continue() : r.abort()));

const setTheme = (t) => page.evaluate((x) => document.documentElement.setAttribute("data-theme", x), t);
async function shot(name) {
  await page.waitForTimeout(350);
  const file = `${name}.png`;
  await page.screenshot({ path: `${OUT}/${file}`, fullPage: true });
  shots.push(file);
}
async function click(sel, ms = 2500) {
  try { await page.click(sel, { timeout: ms }); await page.waitForTimeout(400); return true; }
  catch { return false; }
}

await page.goto(BASE, { waitUntil: "networkidle" });
await page.waitForTimeout(700);

const nav = (label) => click(`.nh-sidebar >> text="${label}"`).catch(() => click(`text="${label}"`));
async function escAll() { for (let i = 0; i < 3; i++) { await page.keyboard.press("Escape"); await page.waitForTimeout(150); } }

for (const theme of ["dark", "light"]) {
  await setTheme(theme);
  await escAll();
  // Board
  await nav("In progress"); await shot(`board-${theme}`);
  // A task's drawer via the Done table — SYSTEM tab (+ expand an agent) + tabs
  if (await click("button:has-text('Done')")) {
    await shot(`done-${theme}`);
    if (await click("tbody tr") && await page.locator(".slideover").count()) {
      await click(".slideover >> text=SYSTEM"); await shot(`drawer-system-${theme}`);
      await click(".fx-role-row, .fx-head.clickable"); await shot(`drawer-system-open-${theme}`);
      for (const t of ["ACTIVITY", "DETAILS", "SPEC", "REVIEW", "DIFF"]) {
        if (await click(`.slideover >> text=${t}`)) await shot(`drawer-${t.toLowerCase()}-${theme}`);
      }
    } else { notes.push(`${theme}: could not open a done task's drawer`); }
    await escAll();
  }
  // Failed table
  if (await click("button:has-text('Failed')")) { await shot(`failed-${theme}`); await escAll(); }
  // Composer
  if (await click("text=+ NEW TASK")) { await shot(`composer-${theme}`); await escAll(); }
  // Stats
  await nav("Stats"); await shot(`stats-${theme}`);
  // Settings — every tab (expand a project card on the Projects tab)
  await nav("Settings"); await page.waitForTimeout(300);
  for (const [label, key] of [["Projects", "projects"], ["Rules", "rules"], ["Skills", "skills"],
    ["Memories", "learnings"], ["Integrations", "integrations"], ["Config", "config"]]) {
    if (await click(`.settings-tab:has-text('${label}')`)) {
      if (key === "projects") await click(".settings-page .project-card, .project-card");
      await shot(`settings-${key}-${theme}`);
    }
  }
  await escAll();
}

// Mobile pass (dark) — a couple of key screens for responsive regressions.
await ctx.close();
const mctx = await b.newContext({ viewport: { width: 390, height: 780 } });
const mp = await mctx.newPage();
mp.on("pageerror", (e) => pageErrors.push(`[390] ${mp.url()} :: ${e.message}`));
await mp.route("**/*", (r) => (r.request().method() === "GET" ? r.continue() : r.abort()));
await mp.goto(BASE, { waitUntil: "networkidle" });
await mp.waitForTimeout(600);
await mp.screenshot({ path: `${OUT}/board-390-dark.png`, fullPage: true }); shots.push("board-390-dark.png");
try { await mp.click("text=Settings", { timeout: 2000 }); await mp.waitForTimeout(400);
  await mp.screenshot({ path: `${OUT}/settings-390-dark.png`, fullPage: true }); shots.push("settings-390-dark.png"); } catch {}
await b.close();

// Report
const report = [
  "# Visual sweep report", "",
  `Base: ${BASE}`, `Shots: ${shots.length}`, `Page errors: ${pageErrors.length}`, "",
  "## Screens", ...shots.map((s) => `- ${s}`), "",
  ...(notes.length ? ["## Notes", ...notes.map((n) => `- ${n}`), ""] : []),
  ...(pageErrors.length ? ["## PAGE ERRORS (fail)", ...pageErrors.map((e) => `- ${e}`)] : ["No page errors. ✓"]),
].join("\n");
fs.writeFileSync(`${OUT}/sweep-report.md`, report + "\n");
console.log(`sweep: ${shots.length} shots, ${pageErrors.length} page errors`);
if (pageErrors.length) { console.error("PAGE ERRORS:\n" + pageErrors.join("\n")); process.exit(1); }
console.log("SWEEP CLEAN");
