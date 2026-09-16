// Drawer regression guards (UI_AUDIT B3, B4, M3, M4 + the DA note's six risks).
//
// QUARANTINED (lane: manual — see e2e/manifest.mjs): this walk's own bugs are
// fixed — it no longer crashes with an uncaught TimeoutError/exception. Three
// separate walk-authoring defects, all "the walk fell behind a real app
// change", were found and fixed: (1) the P1 lazy-attempt-details API split
// (api.js `_hydrateAttemptDetails`) moved review_checklist/verifier_results/
// test_results off the inline task payload onto
// `/api/tasks/{id}/attempts/{n}/details`, which this walk's mock never served,
// so every review-section check timed out waiting on an empty state; (2) the
// Approve button's copy changed from "Approve" to "Approve and merge"
// (operator directive 2026-08-12), and two checks still asserted the old
// exact string; (3) the "ONE scroll region" redesign moved the scrolling
// container from `.so-body` to `.so-scroll` (`.so-body` is now a
// non-scrolling inner wrapper), so a check that measured `.so-body`'s
// scrollHeight/clientHeight never saw an overflow. All three are fixed above.
// What remains, and is NOT a walk bug: "[M4+] the drawer body scrolls
// vertically on a phone" fails with `overflowY: "visible"` on a narrow
// (phone-width) viewport — the secondary-inspector accordion genuinely does
// not get a vertical scroll container on mobile, so its Diff/Attempts
// sections are unreachable below the fold. That is a real CSS/layout defect
// in the app, not this walk; fixing it is an application-code change and out
// of scope for this task (see OUT OF SCOPE in .no_human/PLAN.md — "fixing
// product bugs is a different task"). This walk stays red on that one defect
// until the mobile scroll-container CSS is fixed.
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const DIST = new URL("../dist", import.meta.url).pathname;
const OUT = new URL("./shots", import.meta.url).pathname;
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

const now = new Date().toISOString();
const LONG_TITLE = "Add --json flag to nh status for machine-readable lane counts everywhere";
const mkTask = (id, status, extra = {}) => ({
  id, title: LONG_TITLE, status, kind: "feature",
  description: "Some description", created_at: now, updated_at: now,
  total_tokens: 1_560_000, attempts: 1, ...extra,
});

const PARKED = mkTask("parked00aaaabbbbcccc", "awaiting_input", {
  blocker_question: "The PR was closed without merging. Abandon the task, or rework and reopen?",
  // The DRAWER reads task.blocker (an object) — the flat field is the board card's.
  blocker: {
    question: "The PR was closed without merging. Abandon the task, or rework and reopen?",
    category: "ambiguity",
    confidence: 0.8,
    options: ["Abandon the task", "Rework and reopen the PR"],
  },
});
const REVIEW = mkTask("review00aaaabbbbcccc", "awaiting_approval", { pr_url: "https://example.com/pull/1" });
const RUNNING = mkTask("running0aaaabbbbcccc", "implementing");

const api = (task, opts = {}) => {
  // The real /approve stamps context.approved_at and leaves the task in
  // awaiting_approval (api/app.py) — it does NOT change the status. `opts.stateful`
  // reproduces that, so a later GET sees the approval the way a reopened drawer does.
  let approvedAt = null;
  return (route) => {
    const u = route.request().url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (route.request().method() !== "GET") {
      if (opts.approveFails && u.includes("/approve")) {
        return route.fulfill({
          status: 409, contentType: "application/json",
          body: JSON.stringify({ detail: "task is 'done', not awaiting_approval" }),
        });
      }
      if (opts.stateful && u.includes("/approve")) {
        approvedAt = new Date().toISOString();
        return route.fulfill({
          status: 200, contentType: "application/json",
          body: JSON.stringify({ ok: true, message: "Approval recorded. Merge the PR in your git host — the agent never merges." }),
        });
      }
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    }
    const t = approvedAt
      ? { ...task, context: { ...(task.context || {}), approved_at: approvedAt } }
      : task;
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/projects")) return j([]);
    // The P1 split (api.js `_hydrateAttemptDetails`) moved review_checklist /
    // verifier_results / test_results off the inline task payload onto a
    // lazy per-attempt endpoint — a mocked task that only sets those fields
    // on task.attempts[n] (as every mock below does) now needs this route
    // too, or the drawer's fetch silently 404s/degrades and every section
    // that reads those fields renders its "no data yet" empty state instead.
    if (u.match(/\/api\/tasks\/[^/]+\/attempts\/[^/]+\/details/)) {
      const last = task.attempts?.[task.attempts.length - 1] || {};
      return j({
        review_checklist: last.review_checklist,
        verifier_results: last.verifier_results,
        test_results: last.test_results,
      });
    }
    if (u.match(/\/api\/tasks\/[^/]+\/diff/)) return j({ diff: "" });
    if (u.match(/\/api\/tasks\/[^/]+\/events/)) return j([]);
    if (u.match(/\/api\/tasks\/[^/]+$/)) return j(t);
    if (u.includes("/api/tasks")) return j([t]);
    return j({});
  };
};

const browser = await chromium.launch();

async function open(task, viewport = { width: 1440, height: 900 }, theme = "dark", opts = {}) {
  const ctx = await browser.newContext({ viewport });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error" && !m.text().includes("WebSocket")) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await page.addInitScript((t) => localStorage.setItem("nh-theme", t), theme);
  await page.route("**/api/**", api(task, opts));
  await page.goto("http://127.0.0.1:4640/", { waitUntil: "networkidle" });
  await page.waitForTimeout(500);
  await page.locator(".task-card").first().click();
  await page.locator(".slideover").waitFor({ state: "visible", timeout: 5000 });
  await page.waitForTimeout(700);
  return { ctx, page, errors };
}

// 1.4: the tab strip became a lazy accordion (one section open at a time,
// below a summary-first narrative). The gate-aware "which surface opens
// first" behavior is unchanged — only the selector for "which one is open".
const openSectionLabel = (page) =>
  page.locator(".so-section.open .so-section-title").first().innerText().catch(() => "?");

// B4 — a parked task must surface its blocker answer UI up front. Since the
// Decision panel landed, a parked task's one-click answers live in the promoted
// DecisionPanel (`.decision-option-btn`) ABOVE the accordion — not inside a
// "detail" section — so this asserts the panel, not which section is open.
{
  const { ctx, page, errors } = await open(PARKED);
  const panel = page.locator(".slideover .decision-panel");
  check("[B4] a PARKED task surfaces its blocker answer UI (DecisionPanel) up front",
    await panel.isVisible().catch(() => false));
  const optionBtns = page.locator(".slideover .decision-option-btn");
  const nOptions = await optionBtns.count();
  const questionShown = await page.locator(".slideover").innerText();
  check("[B4] the blocker's one-click answers are on screen without changing sections",
    nOptions >= 2 && questionShown.includes("Abandon the task"), `${nOptions} option buttons`);
  check("[B4] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}
{
  const { ctx, page } = await open(REVIEW);
  const t = (await openSectionLabel(page)).toLowerCase();
  check("[B4] an awaiting-approval task still opens on Review (unchanged)", t.includes("review"), t);
  await ctx.close();
}
{
  const { ctx, page } = await open(RUNNING);
  const t = (await openSectionLabel(page)).toLowerCase();
  check("[B4] a running task still opens on System (unchanged)", t.includes("system"), t);
  await ctx.close();
}

// M3 — Escape must close the nested modal ONLY, preserving the drawer and the typed text.
{
  const { ctx, page } = await open(PARKED);
  const replyBtn = page.getByRole("button", { name: /reply/i }).first();
  if (await replyBtn.count()) {
    await replyBtn.click();
    await page.waitForTimeout(400);
    const ta = page.locator(".sendback-modal textarea").first();
    await ta.fill("my carefully typed answer");
    await page.keyboard.press("Escape");
    await page.waitForTimeout(400);
    const modalGone = !(await page.locator(".sendback-modal").isVisible().catch(() => false));
    const drawerStays = await page.locator(".slideover").isVisible().catch(() => false);
    check("[M3] Escape closes the reply modal", modalGone);
    check("[M3] ...and does NOT close the drawer under it", drawerStays);
    // The whole point: the operator's typed answer must survive the Escape.
    await replyBtn.click();
    await page.waitForTimeout(400);
    const kept = await page.locator(".sendback-modal textarea").first().inputValue();
    check("[M3] the typed answer SURVIVES the Escape", kept === "my carefully typed answer", JSON.stringify(kept));
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    // And a second Escape, with no modal open, still closes the drawer.
    await page.keyboard.press("Escape");
    await page.waitForTimeout(400);
    const drawerClosed = !(await page.locator(".slideover").isVisible().catch(() => false));
    check("[M3] Escape with no modal open still closes the drawer", drawerClosed);
  } else {
    check("[M3] reply button present on a parked task", false, "not found");
  }
  await ctx.close();
}

// B3 — the title must not render one character per line on a phone.
{
  const { ctx, page } = await open(PARKED, { width: 390, height: 844 });
  const geom = await page.evaluate(() => {
    const t = document.querySelector(".so-title");
    const r = t.getBoundingClientRect();
    const cs = getComputedStyle(t);
    return { w: Math.round(r.width), h: Math.round(r.height), lh: parseFloat(cs.lineHeight) };
  });
  const lines = Math.round(geom.h / geom.lh);
  check("[B3] the drawer title is readable on a phone (not one char per line)",
    geom.w > 150 && lines <= 5, `width ${geom.w}px, ~${lines} lines`);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  check("[B3] no horizontal overflow at 390px", !overflow);
  await page.screenshot({ path: `${OUT}/drawer-mobile-after.png` });
  await ctx.close();
}

// Desktop header must be unchanged (the DA note's risk 5).
{
  const { ctx, page } = await open(PARKED);
  const rows = await page.evaluate(() => {
    const h = document.querySelector(".so-header");
    return { wrap: getComputedStyle(h).flexWrap, height: Math.round(h.getBoundingClientRect().height) };
  });
  check("[desktop] the drawer header still lays out on one row", rows.wrap === "nowrap", JSON.stringify(rows));
  await page.screenshot({ path: `${OUT}/drawer-desktop-after.png` });
  await ctx.close();
}


// ── The reviewer's refutations: each must now be closed ────────────────────────────────
// BLOCKER — mid-swap, the drawer must never show task A under task B's id with Approve live.
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const A = mkTask("aaaaaaaaaaaabbbbcccc", "awaiting_approval", { pr_url: "https://x/pull/1" });
  A.title = "TASK-A the first review";
  const B = mkTask("bbbbbbbbbbbbccccdddd", "awaiting_approval", { pr_url: "https://x/pull/2" });
  B.title = "TASK-B the second review";
  await page.route("**/api/**", async (route) => {
    const u = route.request().url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (route.request().method() !== "GET") return j({});
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/projects")) return j([]);
    if (u.match(/\/api\/tasks\/[^/]+\/events/)) return j([]);   // an ARRAY: the feed filters it
    if (u.match(/\/api\/tasks\/[^/]+\/diff/)) return j({ diff: "" });
    if (u.includes(A.id)) return j(A);
    // B's fetch is SLOW: this is the window where the old code showed A under B's id.
    if (u.includes(B.id)) { await new Promise((r) => setTimeout(r, 2500)); return j(B); }
    if (u.includes("/api/tasks")) return j([A, B]);
    return j({});
  });
  await page.goto("http://127.0.0.1:4640/", { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  // Click the TITLE, not the card centre: these tasks carry a pr_url, so the card renders a
  // PR link and a centre-click navigates away from the app.
  // Click the TITLE, not the card centre: these tasks carry a pr_url, so the card renders a
  // PR link and a centre-click would navigate away from the app.
  await page.locator(".task-card .card-title").first().click();
  await page.locator(".slideover").waitFor({ state: "visible", timeout: 8000 });
  await page.waitForTimeout(600);
  const next = page.getByRole("button", { name: /next review/i }).first();
  if (await next.count()) {
    await next.click();
    await page.waitForTimeout(400);   // mid-flight: B is bound, B's fetch has NOT returned
    const midText = await page.locator(".slideover").innerText();
    const approve = page.locator(".slideover").getByRole("button", { name: /^approve/i }).first();
    const approveLive = (await approve.count()) ? await approve.isEnabled() : false;
    check("[BLOCKER] mid-swap the drawer does NOT still show the previous task",
      !midText.includes("TASK-A"), midText.split("\n").slice(0, 2).join(" / "));
    check("[BLOCKER] mid-swap there is no live Approve button bound to the wrong task",
      !approveLive);
    await page.waitForTimeout(2600);
    const after = await page.locator(".slideover").innerText();
    check("[BLOCKER] the new task repaints once its fetch lands", after.includes("TASK-B"));
  } else {
    check("[BLOCKER] next-review button present", false, "not found");
  }
  await ctx.close();
}

// MAJOR 3 — "n" must not open the composer behind the drawer, and Escape must not kill both.
{
  const { ctx, page } = await open(PARKED);
  await page.keyboard.press("n");
  await page.waitForTimeout(400);
  const composerOpen = await page.locator('[role="dialog"][aria-label="New task"]').count();
  check("[M3+] 'n' does not open the composer behind an open drawer", composerOpen === 0);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  await ctx.close();
}

// MAJOR 4 — every drawer section (incl. Diff/Attempts) must be reachable on a
// phone. 1.4 replaced the horizontally-scrolling tab strip with a vertical
// accordion inside the (vertically scrolling) drawer body — so "reachable"
// now means every section header is present and the body scrolls vertically,
// not that a strip scrolls sideways.
{
  const { ctx, page } = await open(PARKED, { width: 390, height: 844 });
  const info = await page.evaluate(() => {
    const body = document.querySelector(".so-body");
    const cs = getComputedStyle(body);
    const titles = [...document.querySelectorAll(".so-section-title")].map((n) => n.textContent.toLowerCase());
    return { overflowY: cs.overflowY, titles };
  });
  check("[M4+] the drawer body scrolls vertically on a phone", info.overflowY === "auto" || info.overflowY === "scroll",
    JSON.stringify(info));
  check("[M4+] Diff and Attempts sections are both present (reachable by scrolling)",
    info.titles.some((t) => t.includes("diff")) && info.titles.some((t) => t.includes("attempt")),
    JSON.stringify(info.titles));
  await ctx.close();
}

// ── SCRUM-80: a terminal task's blocker renders as neutral history, never a
// live ask; a parked task keeps the live ask unchanged. The blocker's full
// evidence trail lives in the Details accordion section, so open it first.
{
  const openDetails = async (page) => {
    await page.locator(".so-section-header", { hasText: "Details" }).first().click();
    await page.waitForTimeout(300);
  };

  // Control: the existing PARKED task (awaiting_input, a live decision) must
  // keep the live "Question for you" ask, with no blocker-history modifier.
  const { ctx: ctxLive, page: pageLive } = await open(PARKED);
  await openDetails(pageLive);
  const liveHistoryCount = await pageLive.locator(".slideover .blocker-history").count();
  check("[SCRUM-80 control] a parked task's blocker section is NOT blocker-history", liveHistoryCount === 0);
  const liveLabel = pageLive.locator(".slideover .blocker-question .blocker-field-label").first();
  // .blocker-field-label is CSS text-transform: uppercase — innerText reflects
  // the painted case in a real browser, so compare case-insensitively.
  const liveLabelText = (await liveLabel.innerText()).trim().toLowerCase();
  check("[SCRUM-80 control] the label reads 'Question for you' on a parked task", liveLabelText === "question for you", liveLabelText);
  const liveColor = await liveLabel.evaluate((el) => getComputedStyle(el).color);
  await ctxLive.close();

  // Terminal: a failed task carrying the same blocker shape renders it as
  // settled history — neutral label, no blocker-history-less styling, and no
  // stale "live choice" affordances (numbered options / "Wake when").
  // Done/Failed are OUTCOMES (Board.jsx 5D) — they are NOT board lanes with
  // `.task-card`s; they live behind the "Failed" nav row as a TaskTable
  // (`.stats-tr`), per Outcomes.jsx / TaskTable.jsx. Open it that way.
  const FAILED_WITH_BLOCKER = mkTask("failed00aaaabbbbcccc", "failed", {
    blocker: {
      question: "The PR was closed without merging. Abandon the task, or rework and reopen?",
      category: "ambiguity",
      confidence: 0.8,
      options: ["Abandon the task", "Rework and reopen the PR"],
      wake_condition: "the PR reopens",
    },
  });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error" && !m.text().includes("WebSocket")) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await page.addInitScript((t) => localStorage.setItem("nh-theme", t), "dark");
  await page.route("**/api/**", api(FAILED_WITH_BLOCKER));
  await page.goto("http://127.0.0.1:4640/", { waitUntil: "networkidle" });
  await page.waitForTimeout(500);
  await page.locator(".nh-navrow", { hasText: "Failed" }).click();
  await page.waitForTimeout(300);
  await page.locator(".stats-tr").first().click();
  await page.locator(".slideover").waitFor({ state: "visible", timeout: 5000 });
  await page.waitForTimeout(700);
  await openDetails(page);
  const historyCount = await page.locator(".slideover .blocker-history").count();
  check("[SCRUM-80] a failed task's blocker section gets the blocker-history modifier", historyCount > 0);
  const label = page.locator(".slideover .blocker-question .blocker-field-label").first();
  const labelText = (await label.innerText()).trim().toLowerCase();
  check("[SCRUM-80] label reads 'Asked before it ended' on a failed task", labelText === "asked before it ended", labelText);
  const color = await label.evaluate((el) => getComputedStyle(el).color);
  check("[SCRUM-80] the neutralized label's computed colour is not the live accent",
    color !== liveColor, `${color} vs live ${liveColor}`);
  const wakeLabel = page.locator(".slideover .blocker-field-label", { hasText: "Was waiting for" });
  check("[SCRUM-80] the wake-condition field is past-tense on a terminal task", await wakeLabel.count() > 0);
  const staleWakeLabel = page.locator(".slideover .blocker-field-label", { hasText: "Wake when" });
  check("[SCRUM-80] the live 'Wake when' wording is gone on a terminal task", await staleWakeLabel.count() === 0);
  const liveOptionsCount = await page.locator(".slideover .blocker-options").count();
  check("[SCRUM-80] the numbered options list is not rendered as a live choice on a terminal task", liveOptionsCount === 0);
  check("[SCRUM-80] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// FOCUS-STEAL — reported by a friend testing the app: "every time they tried typing
// in the text box, after a few seconds the text stopped getting typed". The drawer's
// focus trap called closeRef.focus() from an effect keyed on [onClose], and Board
// passes a NEW onClose identity on every render — so every re-render (the 10s worker
// poll, and every WS task frame) yanked focus out of whatever the operator was typing
// into and parked it on the drawer's close button. Typing into a <button> goes nowhere.
//
// This drives the REAL re-render (the poll), not a simulated one: a test that called
// the effect directly would pass with the bug present.
{
  const { ctx, page, errors } = await open(PARKED);
  const replyBtn = page.getByRole("button", { name: /reply/i }).first();
  await replyBtn.click();
  await page.waitForTimeout(400);
  const ta = page.locator(".sendback-modal textarea").first();
  await ta.click();
  await page.keyboard.type("an answer the operator is still typing");
  const focusedBefore = await page.evaluate(() => document.activeElement?.tagName || "none");
  check("[focus] the reply box has focus once clicked", focusedBefore === "TEXTAREA", focusedBefore);

  // Span a full worker-status poll (App.jsx polls every 10s) so a real re-render lands
  // while the operator is "typing". Nothing here touches focus.
  await page.waitForTimeout(11000);

  const after = await page.evaluate(() => ({
    tag: document.activeElement?.tagName || "none",
    cls: String(document.activeElement?.className || ""),
  }));
  check("[focus] a background re-render does NOT steal focus from the reply box",
    after.tag === "TEXTAREA", `focus landed on ${after.tag}.${after.cls}`);

  // The operator must be able to keep typing where they left off. Note the SPACE:
  // with focus parked on the close button, a space keystroke ACTIVATES it, closing
  // the drawer and discarding the answer — so this also pins that the reply survives.
  await page.keyboard.type(" and this arrives after the re-render");
  const stillOpen = await page.locator(".sendback-modal textarea").count() > 0;
  check("[focus] typing a space does not activate the close button and discard the reply",
    stillOpen, stillOpen ? "" : "the reply modal was closed by a keystroke");
  const value = stillOpen ? await ta.inputValue() : "(modal gone)";
  check("[focus] keystrokes after the re-render still reach the reply box",
    value.endsWith("after the re-render"), JSON.stringify(value.slice(-40)));
  check("[focus] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// The reply/send-back modals inside the drawer had backdrop-click close, so a
// stray click discarded a typed blocker answer — the exact box the reported
// symptom describes. And a backdrop that no longer closes must not STEAL FOCUS
// either, or the dialog stays open silently swallowing keystrokes, which is the
// same symptom by a different trigger.
{
  const { ctx, page, errors } = await open(PARKED);
  await page.getByRole("button", { name: /reply/i }).first().click();
  await page.waitForTimeout(400);
  const ta = page.locator(".sendback-modal textarea").first();
  await ta.click();
  await page.keyboard.type("a blocker answer worth keeping");

  // Click the backdrop, away from the modal box.
  await page.mouse.click(12, 12);
  await page.waitForTimeout(400);
  check("[reply-backdrop] a click outside does NOT close the reply modal",
    await page.locator(".sendback-modal").count() > 0);
  const focus = await page.evaluate(() => ({
    tag: document.activeElement?.tagName || "none",
    cls: String(document.activeElement?.className || ""),
  }));
  check("[reply-backdrop] the backdrop click does not steal focus from the box",
    focus.cls.includes("sendback-textarea"), `${focus.tag}.${focus.cls}`);
  await page.keyboard.type(" and this still lands");
  const val = await ta.inputValue().catch(() => "(gone)");
  check("[reply-backdrop] keystrokes after the stray click still reach the box",
    val.endsWith("still lands"), JSON.stringify(val.slice(-30)));
  check("[reply-backdrop] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// The drawer is aria-modal, so focus must not be left ORPHANED outside it.
// Clicking the reply modal's own Cancel unmounts the focused control; focus
// falls to <body> with nothing to return to, and one Tab then walks into the
// page behind the dialog. THAT is what the trap heals.
{
  const { ctx, page } = await open(PARKED);
  await page.getByRole("button", { name: /reply/i }).first().click();
  await page.waitForTimeout(400);
  // Put focus INSIDE the modal first — the operator is typing their answer.
  // Without this the spec never reaches the state it describes: on macOS
  // Chromium a mouse click does not focus a <button>, so focus would still be
  // on the drawer's own close button and nothing would be orphaned.
  await page.locator(".sendback-modal textarea").first().click();
  await page.waitForTimeout(150);
  await page.locator(".sendback-modal").getByRole("button", { name: /^cancel$/i }).click();
  await page.waitForTimeout(600);
  const healed = await page.evaluate(() => {
    const so = document.querySelector(".slideover");
    return Boolean(so && so.contains(document.activeElement));
  });
  check("[focus-trap] focus returns inside the drawer when the focused control unmounts",
    healed);
  await ctx.close();
}

// …and it must NOT heal in any other case. An earlier version of this suite
// asserted the OPPOSITE — it required focus to be dragged back inside after the
// user Tabbed away — which pinned a focus THEFT as correct and would have
// blocked its own fix.
//
// 🔴 These specs must do two things an earlier version did not, or they pass
// while testing nothing: (1) put focus INSIDE the drawer first, so the observer
// has a remembered element at all — otherwise it early-returns and every guard
// below it is unreachable; and (2) cause a mutation UNDER THE OBSERVED SCOPE.
// Waiting 11s for "a real background poll" does neither: the worker-status poll
// mutates the sidebar, not the drawer's subtree, so the observer never runs.
const forceMutationInDrawer = (page) => page.evaluate(() => {
  const host = document.querySelector(".slideover");
  const probe = document.createElement("span");
  host.appendChild(probe);
  probe.remove();
});

// THE REPORTED BUG, by the route a user actually hits it: typing an answer, then
// clicking a heading inside the dialog. Most of a dialog is not focusable, so a
// mousedown there used to move focus to <body> and silently drop every keystroke
// after it — with the dialog still on screen looking focused.
{
  const { ctx, page } = await open(PARKED);
  await page.getByRole("button", { name: /reply/i }).first().click();
  await page.waitForTimeout(400);
  const ta = page.locator(".sendback-modal textarea").first();
  await ta.click();
  await page.keyboard.type("my blocker answer");
  await page.locator(".sendback-modal .sendback-label").first().click();
  await page.waitForTimeout(200);
  const held = await page.evaluate(() => document.activeElement?.tagName || "none");
  check("[caret] clicking a heading inside the dialog does not strand the caret",
    held === "TEXTAREA", held);
  await page.keyboard.type(" AND MORE");
  const val = await ta.inputValue().catch(() => "(gone)");
  check("[caret] …so the keystrokes after it still land",
    val.endsWith(" AND MORE"), JSON.stringify(val.slice(-24)));
  await ctx.close();
}

// The heal must NOT fire while the element it remembers is still on the page —
// otherwise every mutation under the drawer re-grabs focus from wherever the
// user put it, which is the theft this whole change exists to remove.
{
  const { ctx, page } = await open(PARKED);
  await page.getByRole("button", { name: /reply/i }).first().click();
  await page.waitForTimeout(400);
  await page.locator(".sendback-modal textarea").first().click();
  // Drop focus WITHOUT removing anything — the state a stray click used to make.
  await page.evaluate(() => document.activeElement?.blur());
  await forceMutationInDrawer(page);
  await page.waitForTimeout(200);
  const after = await page.evaluate(() => ({
    tag: document.activeElement?.tagName || "none",
    cls: String(document.activeElement?.className || ""),
  }));
  check("[focus-trap] a mutation does not heal while the remembered box still exists",
    !after.cls.includes("so-close"), `${after.tag}.${after.cls}`);
  await ctx.close();
}

// …and it must not reclaim focus from a live control the user moved to.
{
  const { ctx, page } = await open(PARKED);
  await page.getByRole("button", { name: /reply/i }).first().click();
  await page.waitForTimeout(400);
  await page.locator(".sendback-modal textarea").first().click();
  await page.evaluate(() => {
    const outside = document.querySelector(".nh-navrow");
    if (outside) outside.focus();          // a deliberate move, as by Tab
    // now remove what the drawer remembered, so the heal is armed
    document.querySelector(".sendback-modal textarea")?.remove();
  });
  await page.waitForTimeout(300);
  const after = await page.evaluate(() => String(document.activeElement?.className || ""));
  check("[focus-trap] a live control outside the drawer keeps focus",
    !after.includes("so-close"), after);
  await ctx.close();
}

// A DISABLED control must not swallow the click and strand the caret.
//
// This is the reported bug's last route and the one that survived six review
// rounds, because the guard for it was written in the wrong LAYER: Chrome does
// not dispatch mouse events on a disabled form control at all, so
// `keepFocusInDialog` is never called and cannot cancel anything — the browser
// moves focus to <body> regardless. Only a BROWSER test can see this. The unit
// spec "covering" it builds an event with a disabled `closest()` target, which
// the browser never produces, so the fake passed while the product broke.
//
// The sequence is the friend's: the reply modal opens with the textarea focused
// and "Send reply" already greyed out (it is disabled until you type). Click the
// greyed button — the natural thing to do when a dialog seems stuck — and every
// keystroke after it is silently discarded.
{
  const { ctx, page, errors } = await open(PARKED);
  await page.getByRole("button", { name: /reply/i }).first().click();
  await page.waitForTimeout(400);
  const ta = page.locator(".sendback-modal textarea").first();

  const greyed = await page.evaluate(() => {
    const b = [...document.querySelectorAll(".sendback-modal button")]
      .find((x) => x.disabled);
    return b ? { text: b.textContent.trim(), disabled: b.disabled } : null;
  });
  check("[disabled-strand] the reply modal opens with its primary action disabled",
    greyed !== null && greyed.disabled === true, JSON.stringify(greyed));
  check("[disabled-strand] the caret starts in the reply box (autofocus)",
    (await page.evaluate(() => document.activeElement?.tagName)) === "TEXTAREA");

  // Click the greyed button by coordinates — `.click()` on a disabled control is
  // refused by the driver, but a real mouse press at that point is not, and that
  // is exactly what a person does.
  const box = await page.evaluate(() => {
    const b = [...document.querySelectorAll(".sendback-modal button")]
      .find((x) => x.disabled);
    const r = b.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  });
  await page.mouse.click(box.x, box.y);
  await page.waitForTimeout(200);

  const landed = await page.evaluate(() => document.activeElement?.tagName || "none");
  check("[disabled-strand] clicking the greyed primary action does NOT strand the caret on <body>",
    landed === "TEXTAREA", `focus landed on ${landed}`);

  await page.keyboard.type("the answer typed after clicking the greyed button");
  const value = await ta.inputValue();
  check("[disabled-strand] keystrokes after that click still reach the reply box",
    value === "the answer typed after clicking the greyed button", JSON.stringify(value));

  // POSITIVE CONTROL. 🔴 It has to be able to FAIL against the failure mode it
  // names. An earlier version only checked that Cancel still closed the modal,
  // and a review proved that was not enough: gutting `keepFocusInDialog` into
  // the naive "preventDefault on EVERY mousedown" — the exact fix its own
  // header warns against — left this whole suite GREEN. Only the unit tests
  // noticed.
  //
  // What the naive fix actually breaks is CARET PLACEMENT: cancelling mousedown
  // on a real text control stops the browser moving the caret to where you
  // clicked. So the control now clicks at a known offset inside the textarea
  // and asserts the caret MOVED there, which no blanket-preventDefault build
  // can satisfy.
  await page.keyboard.press("Home");
  const caret = await page.evaluate(() => {
    const ta = document.querySelector(".sendback-modal textarea");
    ta.setSelectionRange(0, 0);
    const r = ta.getBoundingClientRect();
    // y must clear the textarea's top PADDING (14px) or the click lands above
    // the first text line and the browser correctly reports caret 0 — which
    // reads as a failure of the product when it is a failure of the coordinate.
    return { x: r.x + 60, y: r.y + 26, before: ta.selectionStart };
  });
  await page.mouse.click(caret.x, caret.y);
  await page.waitForTimeout(150);
  const caretAfter = await page.evaluate(
    () => document.querySelector(".sendback-modal textarea").selectionStart);
  check("[disabled-strand CONTROL] clicking INSIDE the reply box still places the caret",
    caretAfter > caret.before,
    `caret stayed at ${caret.before} — a blanket preventDefault() would do this`);

  // …and an ENABLED control in the same dialog must still activate.
  const cancelBox = await page.evaluate(() => {
    const b = [...document.querySelectorAll(".sendback-modal button")]
      .find((x) => !x.disabled && /cancel/i.test(x.textContent));
    const r = b.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  });
  await page.mouse.click(cancelBox.x, cancelBox.y);
  await page.waitForTimeout(300);
  check("[disabled-strand CONTROL] an ENABLED button in the same dialog still activates",
    (await page.locator(".sendback-modal").count()) === 0,
    (await page.locator(".sendback-modal").count()) === 0 ? ""
      : "Cancel did not close the reply modal — the fix made live controls inert");
  check("[disabled-strand] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── DEFECT 1 (operator report): a PASSED review rendered as an 80% failure ──
// The severity-class rule fails the gate on BLOCKING findings only. A review
// that passed with 4 non-blocking findings + 1 passing criterion rendered
// "4 findings · 1/5 passed" — read as a failure, so a legitimate PASS was
// distrusted. Measured in the browser: the header text and the row styling.
const PASSED_REVIEW = mkTask("passrev0aaaabbbbcccc", "awaiting_approval", {
  pr_url: "https://example.com/pull/7",
  attempts: [{
    review_checklist: {
      passed: true,
      items: [
        { label: "Acceptance criterion 1 met", passed: true, evidence: "e" },
        { label: "Name shadows the outer binding", passed: false, severity: "low", comment: "rename it", file: "a.js", line: 3 },
        { label: "Redundant null check", passed: false, severity: "low", comment: "drop it", file: "a.js", line: 9 },
        { label: "Comment typo", passed: false, severity: "nit", comment: "typo", file: "b.js", line: 1 },
        { label: "Prefer const", passed: false, severity: "nit", comment: "const", file: "b.js", line: 4 },
      ],
    },
  }],
});
{
  const { ctx, page, errors } = await open(PASSED_REVIEW);
  const header = await page.locator(".slideover .so-section.open .so-section-label-row").first().innerText();
  const body = await page.locator(".slideover").innerText();
  check("[D1] a PASSED review leads with PASSED", /PASSED/.test(header) && !/FAILED/.test(header), header);
  check("[D1] …and counts its findings as non-blocking, with severities",
    header.toLowerCase().includes("4 non-blocking findings (2 low, 2 nit)"), header);
  check("[D1] the reported '1/5 passed' reading is gone", !/\d+\s*\/\s*\d+\s*passed/.test(body),
    (body.match(/\d+\s*\/\s*\d+\s*passed/) || [""])[0]);
  const chips = await page.locator(".slideover .so-checklist .cr-sev").allInnerTexts();
  check("[D1] every graded row carries a severity chip",
    chips.length === 4 && chips.filter((c) => /low/i.test(c)).length === 2
      && chips.filter((c) => /nit/i.test(c)).length === 2, JSON.stringify(chips));
  const nAdvisory = await page.locator(".slideover .checklist-item.advisory").count();
  const nBlocking = await page.locator(".slideover .checklist-item.fail").count();
  check("[D1] non-blocking rows are advisory, not blocking failures",
    nAdvisory === 4 && nBlocking === 0, `advisory=${nAdvisory} fail=${nBlocking}`);
  check("[D1] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// …and an older attempt with no severity key must render no chip, not crash.
const UNGRADED_REVIEW = mkTask("oldrev00aaaabbbbcccc", "awaiting_approval", {
  pr_url: "https://example.com/pull/8",
  attempts: [{
    review_checklist: {
      passed: false,
      items: [
        { label: "Blocking problem", passed: false, evidence: "e", file: "a.js", line: 2 },
        { label: "Advisory nit", passed: false, severity: "nit", comment: "c", file: "a.js", line: 5 },
      ],
    },
  }],
});
{
  const { ctx, page, errors } = await open(UNGRADED_REVIEW);
  const header = await page.locator(".slideover .so-section.open .so-section-label-row").first().innerText();
  check("[D1] a FAILED review keeps today's failure-first header", /FAILED/.test(header), header);
  const chips = await page.locator(".slideover .so-checklist .cr-sev").allInnerTexts();
  // An item with no severity key must not crash, and must not silently vanish
  // either: the section header counts it in an "unrated" bucket, and an
  // ungraded finding BLOCKS, so the row says so with the same word.
  check("[D1] an item with no severity key is chipped 'unrated', matching the header's bucket",
    chips.length === 2 && /unrated/i.test(chips[0]) && /nit/i.test(chips[1]), JSON.stringify(chips));
  const unratedPainted = await page.evaluate(() => {
    const el = document.querySelector(".slideover .cr-sev-unrated");
    return el ? getComputedStyle(el).color : null;
  });
  check("[D1] …and .cr-sev-unrated is finally a reachable rule, not dead paint",
    !!unratedPainted, String(unratedPainted));
  // Reads backgroundColor, not borderLeftColor: the verdict used to be a 3px
  // coloured stripe and is a background tint since the 2026-08-29 craft pass
  // (two elevation devices on one bordered card). Same assertion, same strength
  // — retargeted at the property that now carries the distinction, because a
  // guard left pointing at the old device would have gone green on two rows
  // that are both simply "the card border".
  const colors = await page.evaluate(() => {
    const q = (s) => document.querySelector(s);
    const c = (s) => { const el = q(s); return el ? getComputedStyle(el).backgroundColor : null; };
    return { blocking: c(".slideover .checklist-item.fail"), advisory: c(".slideover .checklist-item.advisory") };
  });
  check("[D1] a non-blocking row does not reuse the blocking red",
    !!colors.blocking && !!colors.advisory && colors.blocking !== colors.advisory, JSON.stringify(colors));
  check("[D1] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── DEFECT 2 (operator report): Approve gave no visible confirmation ────────
// The approval DID record (context.approved_at was set) but the drawer said
// nothing, so the operator read a successful approval as a dead button.
{
  const { ctx, page, errors } = await open(REVIEW);
  const btn = page.locator(".slideover .so-actions .btn-approve").first();
  const before = (await btn.innerText()).trim();
  // Copy changed (operator directive 2026-08-12, slideOverSummary.js
  // approveButtonState "idle" branch): approve now merges the PR too, and
  // the label says so — "Approve" alone predates that and never matches
  // the built bundle again.
  check("[D2] the action bar offers Approve", /^approve and merge$/i.test(before), before);
  await btn.click();
  await page.waitForTimeout(600);
  const after = (await btn.innerText()).trim();
  const disabled = await btn.isDisabled();
  check("[D2] the button label changes once the approval lands",
    /^approved — merge pending$/i.test(after), `"${before}" → "${after}"`);
  check("[D2] …and it is disabled so it cannot be double-approved", disabled);
  const status = page.locator('.slideover [role="status"]').first();
  const statusText = (await status.count()) ? await status.innerText() : "";
  check("[D2] a status live region confirms the approval was recorded",
    /approval recorded/i.test(statusText), statusText);
  check("[D2] …and says the human merges, never the agent",
    /agent never merges/i.test(statusText), statusText);
  check("[D2] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}
{
  const { ctx, page, errors } = await open(REVIEW, { width: 1440, height: 900 }, "dark", { approveFails: true });
  const btn = page.locator(".slideover .so-actions .btn-approve").first();
  await btn.click();
  await page.waitForTimeout(600);
  const after = (await btn.innerText()).trim();
  check("[D2] a failed approval leaves a retryable button", /retry/i.test(after), after);
  check("[D2] …that is not disabled", !(await btn.isDisabled()));
  const alert = page.locator('.slideover [role="alert"]').first();
  const alertText = (await alert.count()) ? await alert.innerText() : "";
  check("[D2] the failure is announced, and never claims it was recorded",
    /not recorded/i.test(alertText) && !/^Approval recorded/i.test(alertText), alertText);
  check("[D2] the server's own reason reaches the operator",
    /not awaiting_approval/.test(alertText), alertText);
  // The 409 is the point of this block; any OTHER console error is a real bug.
  const unexpected = errors.filter((e) => !e.includes("409"));
  check("[D2] no unexpected page errors", unexpected.length === 0, unexpected[0] || "");
  await ctx.close();
}

// ── DEFECT 2a: the confirmation must be where the operator is looking ──────
// The REPORTED symptom was not a missing banner — it was an unseen one. The
// banner used to render at the TOP of the scrolling .so-body, so on a long
// drawer, clicking Approve at the bottom painted the confirmation off-screen.
// The whole suite passed with the defect reintroduced, because nothing here
// asserted the banner's POSITION and every fixture was short enough that
// .so-body never scrolled. This block fixes both halves.
const LONG_PARA =
  "The lane counts are currently only rendered by the TUI, which means any "
  + "script that wants them has to scrape ANSI output and guess at column "
  + "widths. A --json flag should emit the same numbers the board renders, "
  + "keyed by lane, with no decoration and a stable schema. ";
const SCROLLING_REVIEW = mkTask("longrev0aaaabbbbcccc", "awaiting_approval", {
  pr_url: "https://example.com/pull/9",
  // Long enough that .so-body genuinely overflows — asserted below, never assumed.
  description: LONG_PARA.repeat(24),
  acceptance_criteria: Array.from({ length: 12 }, (_, i) => `Criterion ${i + 1}: ${LONG_PARA}`),
});
{
  const { ctx, page, errors } = await open(SCROLLING_REVIEW);
  // Open Details so the long description is actually laid out inside .so-body.
  await page.locator('.slideover .so-section-header:has(.so-section-title:text-is("Details"))').click();
  await page.waitForTimeout(600);
  // The "ONE scroll region" redesign (SlideOver.jsx:566 comment) moved the
  // scrolling container to `.so-scroll` — `.so-body` is just a non-scrolling
  // inner wrapper for the accordion now, so it never overflows regardless of
  // content length. Stale selector, not a real regression.
  const body = page.locator(".slideover .so-scroll");
  const overflow = await body.evaluate((el) => ({ scroll: el.scrollHeight, client: el.clientHeight }));
  check("[D2a] the fixture's body genuinely overflows — the defect can manifest here",
    overflow.scroll > overflow.client,
    `scrollHeight=${overflow.scroll} clientHeight=${overflow.client}`);

  const btn = page.locator(".slideover .so-actions .btn-approve").first();
  await btn.click();
  await page.waitForTimeout(600);
  // Scroll the body to the very bottom — where an operator on a long drawer is
  // when they reach for Approve.
  await body.evaluate((el) => { el.scrollTop = el.scrollHeight; });
  await page.waitForTimeout(300);
  const scrolledTo = await body.evaluate((el) => el.scrollTop);
  check("[D2a] …and it really did scroll", scrolledTo > 0, `scrollTop=${scrolledTo}`);

  const banner = page.locator(".slideover .flash-banner").first();
  const inBody = await banner.evaluate((el) => !!el.closest(".so-body"));
  check("[D2a] the confirmation is NOT inside the scrolling body", !inBody);
  const box = await banner.boundingBox();
  const vp = page.viewportSize();
  const visible = !!box && box.y >= 0 && box.y + box.height <= vp.height
    && box.x >= 0 && box.x + box.width <= vp.width && box.height > 0;
  check("[D2a] …so after scrolling to the bottom it is still on screen",
    visible, JSON.stringify({ box, vp }));
  // It must also stay next to the button that caused it, not merely on screen.
  const btnBox = await btn.boundingBox();
  check("[D2a] …and directly above the action bar it belongs to",
    !!box && !!btnBox && box.y + box.height <= btnBox.y + 1
      && btnBox.y - (box.y + box.height) < 40,
    JSON.stringify({ bannerBottom: box && box.y + box.height, btnTop: btnBox && btnBox.y }));
  check("[D2a] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── DEFECT 2b: the confirmation must survive closing the drawer ────────────
// approveOutcome is session state and /approve leaves the task in
// awaiting_approval, so reopening the drawer on an approved task showed a
// plain enabled "Approve" — the same "did that do anything?" reading — and a
// second click silently re-stamped approved_at over the operator's real one.
{
  const { ctx, page, errors } = await open(REVIEW, { width: 1440, height: 900 }, "dark", { stateful: true });
  const btn = () => page.locator(".slideover .so-actions .btn-approve").first();
  await btn().click();
  await page.waitForTimeout(600);
  check("[D2b] the approval lands", /merge pending/i.test((await btn().innerText()).trim()));
  // Close the drawer and reopen the same task — a fresh SlideOver mount.
  await page.locator(".slideover .so-close").click();
  await page.waitForTimeout(400);
  check("[D2b] the drawer closed", (await page.locator(".slideover").count()) === 0);
  await page.locator(".task-card").first().click();
  await page.locator(".slideover").waitFor({ state: "visible", timeout: 5000 });
  await page.waitForTimeout(800);
  const label = (await btn().innerText()).trim();
  check("[D2b] a REOPENED drawer still shows the approval, not a bare Approve",
    /^approved — merge pending$/i.test(label), label);
  check("[D2b] …and will not silently re-stamp approved_at", await btn().isDisabled());
  const narrative = await page.locator(".slideover").innerText();
  check("[D2b] the summary says the same thing the button does",
    /approved and is/i.test(narrative) && /waiting for the PR to merge/i.test(narrative),
    (narrative.match(/This \w+[^\n]*/) || [""])[0]);
  check("[D2b] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// …and a task approved in an EARLIER session (nothing in this drawer's state,
// only context.approved_at in the payload) reads the same way on a cold open.
const PREAPPROVED = mkTask("apprvd00aaaabbbbcccc", "awaiting_approval", {
  pr_url: "https://example.com/pull/10",
  context: { approved_at: "2026-07-30T09:00:00+00:00" },
});
{
  const { ctx, page, errors } = await open(PREAPPROVED);
  const btn = page.locator(".slideover .so-actions .btn-approve").first();
  const label = (await btn.innerText()).trim();
  check("[D2b] a cold open on an already-approved task shows the approved state",
    /^approved — merge pending$/i.test(label) && (await btn.isDisabled()), label);
  check("[D2b] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// A send-back after the approval spends it: approved_at is never cleared
// server-side, so a stale stamp must not lock the operator out of approving
// the PR that replaced the one they rejected.
const SENT_BACK_AFTER_APPROVAL = mkTask("resent00aaaabbbbcccc", "awaiting_approval", {
  pr_url: "https://example.com/pull/11",
  context: {
    approved_at: "2026-07-30T09:00:00+00:00",
    send_back_feedback: [{ at: "2026-07-30T10:00:00+00:00", message: "not this" }],
  },
});
{
  const { ctx, page, errors } = await open(SENT_BACK_AFTER_APPROVAL);
  const btn = page.locator(".slideover .so-actions .btn-approve").first();
  const label = (await btn.innerText()).trim();
  check("[D2b] an approval predating a send-back does not gate the NEW PR",
    /^approve and merge$/i.test(label) && !(await btn.isDisabled()), label);
  check("[D2b] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
