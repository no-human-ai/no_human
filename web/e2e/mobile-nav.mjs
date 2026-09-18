// Regression guard: every top-level nav control must be REACHABLE on a phone.
// Playwright's .click() scrolls a target into view programmatically, so a click
// test alone passes even when the button renders off-screen — this asserts on
// geometry (rect.right <= innerWidth), which is what a human's thumb sees.
//
// QUARANTINED (lane: manual — see e2e/manifest.mjs): 4 of ~100 checks fail —
// "no nav label is clipped" at the two narrowest viewports (small, mobile-short;
// both themes) — always the SAME label, always the SAME 2px: `In progress
// 64>62`. Real CSS regression, not a walk bug: src/styles.css:379 gives
// `.nh-navrow-label` `flex: 1; min-width: 0` so it shrinks and ellipsizes, but
// the narrow-viewport override at src/styles.css:627 sets `flex: 0 0 auto;
// max-width: 100%` — `flex-basis: auto` sizes the label to its own content
// instead of the available track, so at these widths it overflows its box
// before the `text-overflow: ellipsis` rule ever gets a chance to fire. Fixing
// it is an application CSS change and out of scope for this task (see OUT OF
// SCOPE in .no_human/PLAN.md — "fixing product bugs is a different task");
// this walk stays red on that one defect until the narrow-viewport override
// keeps `min-width: 0` (or drops `flex: 0 0 auto` back to `flex: 1`).
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
await new Promise((r) => srv.listen(4620, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const browser = await chromium.launch();

for (const [label, viewport] of [
  ["mobile", { width: 390, height: 844 }],
  ["small", { width: 360, height: 780 }],
  // Inside BOTH the max-width:640px mobile block and the short-viewport
  // max-height block — the exact overlap where a specificity leak between
  // them would show up (a phone is always also a "short" viewport).
  ["mobile-short", { width: 360, height: 700 }],
  ["desktop", { width: 1440, height: 900 }],
]) {
  for (const theme of ["dark", "light"]) {
    const ctx = await browser.newContext({ viewport });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.addInitScript((t) => localStorage.setItem("nh-theme", t), theme);
    await page.route("**/api/**", (route) => {
      const u = route.request().url();
      const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
      if (u.includes("/api/onboarding")) return j({ completed: true });
      if (u.includes("/api/tasks")) return j([]);
      if (u.includes("/api/projects")) return j([]);
      return j({});
    });
    await page.goto("http://127.0.0.1:4620/", { waitUntil: "networkidle" });
    await page.waitForTimeout(500);

    const geom = await page.evaluate(() => {
      const vw = window.innerWidth;
      const btns = [...document.querySelectorAll(".nh-navrow")].map((b) => {
        const r = b.getBoundingClientRect();
        return {
          label: b.textContent.trim().slice(0, 10),
          left: Math.round(r.left), right: Math.round(r.right),
          onScreen: r.left >= 0 && r.right <= vw,
          current: b.getAttribute("aria-current"),   // measured in the real DOM
          active: b.classList.contains("active"),
        };
      });
      // The nav strip itself: does its content fit the box it is drawn in?
      // `overflow-x: auto` + a hidden scrollbar means an overflowing strip
      // looks IDENTICAL to a fitting one — the rows just stop, with no edge,
      // no bar and no fade. Every rect below would still read "on screen" for
      // the rows that happen to be scrolled into view, so this is the check
      // that catches a hidden overflow rather than its visible symptom.
      const nav = document.querySelector(".nh-sidenav");
      const navOverflow = nav ? nav.scrollWidth - nav.clientWidth : 0;
      const toggle = document.querySelector(".nh-theme-toggle");
      const tr = toggle?.getBoundingClientRect();
      const tag = document.querySelector(".legion-tag");
      // Dialog-opening controls: read their disclosure attributes from the DOM.
      const settingsBtn = document.querySelector(".nh-settings-row");
      const newTaskBtn = document.querySelector(".btn-new-task");
      return {
        vw,
        btns,
        navOverflow,
        toggleOnScreen: tr ? tr.right <= vw && tr.left >= 0 : null,
        tagVisible: tag ? getComputedStyle(tag).display !== "none" : false,
        pageOverflowX: document.documentElement.scrollWidth > vw + 1,
        settingsPopup: settingsBtn?.getAttribute("aria-haspopup") || null,
        settingsExpanded: settingsBtn?.getAttribute("aria-expanded") || null,
        newTaskPopup: newTaskBtn?.getAttribute("aria-haspopup") || null,
        newTaskExpanded: newTaskBtn?.getAttribute("aria-expanded") || null,
        navColumnGap: nav ? getComputedStyle(nav).columnGap : null,
        navRowGap: nav ? getComputedStyle(nav).rowGap : null,
        clippedLabels: [...document.querySelectorAll(".nh-navrow-label")]
          .filter((l) => l.scrollWidth > l.clientWidth + 1)
          .map((l) => `${l.textContent.trim()} ${l.scrollWidth}>${l.clientWidth}`),
      };
    });

    // 1.5: nav rows moved from .nh-sidenav-btn to .nh-navrow (grouped nav +
    // the bottom-pinned Settings row) — a stale selector here would silently
    // find 0 items and "pass" on an empty array, so require at least Board/
    // Done/Failed/Stats/Settings (5) to be present.
    check(`[${label}/${theme}] the nav-row selector actually finds rows (not a stale selector)`, geom.btns.length >= 5, `${geom.btns.length} items`);
    const off = geom.btns.filter((b) => !b.onScreen);
    check(
      `[${label}/${theme}] every nav item is on screen`,
      off.length === 0,
      off.length ? off.map((b) => `${b.label}@${b.left}-${b.right} of ${geom.vw}`).join(", ") : `${geom.btns.length} items`,
    );
    check(
      `[${label}/${theme}] the nav strip does not overflow its own box (nothing hidden behind a scrollbar-less scroll)`,
      geom.navOverflow <= 1,
      `scrollWidth - clientWidth = ${geom.navOverflow}px`,
    );

    // Regression guard for the short-viewport-block specificity leak: the
    // mobile block sets `.nh-sidenav { gap: 2px }` (0,1,0). The short-viewport
    // block used to set `.nh-sidebar .nh-sidenav { gap: var(--sp-3) }`
    // (0,2,0) — higher specificity always wins over source order, and every
    // phone is also a short viewport, so the desktop gap silently leaked onto
    // every phone (12px instead of 2px), shrinking each row until "In
    // progress" started to ellipsise. Only phone widths are guarded here —
    // the desktop gap value is legitimately larger and checked elsewhere.
    if (geom.vw <= 640) {
      check(
        `[${label}/${theme}] mobile nav keeps its own 2px gap (no short-viewport-block specificity leak)`,
        geom.navColumnGap === "2px",
        `columnGap=${geom.navColumnGap} rowGap=${geom.navRowGap}`,
      );
      check(
        `[${label}/${theme}] no nav label is clipped`,
        geom.clippedLabels.length === 0,
        geom.clippedLabels.join(", "),
      );
    }
    check(`[${label}/${theme}] theme toggle is on screen`, geom.toggleOnScreen !== false);
    check(`[${label}/${theme}] no horizontal page overflow`, !geom.pageOverflowX);

    // a11y: the current page must be announced to a screen reader. On load the
    // board is showing, so exactly one nav row carries aria-current="page" and
    // it is the active row; no other row falsely claims to be current. (Settings
    // is an overlay trigger, never a page — it must never carry aria-current.)
    const currentRows = geom.btns.filter((b) => b.current === "page");
    check(
      `[${label}/${theme}] exactly one nav row marks aria-current=page`,
      currentRows.length === 1,
      `${currentRows.length} rows: ${currentRows.map((b) => b.label).join(", ") || "none"}`,
    );
    check(
      `[${label}/${theme}] the aria-current row is the active one`,
      currentRows.length === 1 && currentRows[0].active,
      currentRows.length === 1 ? `current="${currentRows[0].label}" active=${currentRows[0].active}` : "",
    );

    // a11y: a control that opens a modal dialog must announce it (aria-haspopup)
    // and its open state (aria-expanded). Both start collapsed on load.
    check(
      `[${label}/${theme}] Settings button announces its dialog (haspopup=dialog, collapsed)`,
      geom.settingsPopup === "dialog" && geom.settingsExpanded === "false",
      `haspopup=${geom.settingsPopup} expanded=${geom.settingsExpanded}`,
    );
    check(
      `[${label}/${theme}] New Task button announces its dialog (haspopup=dialog, collapsed)`,
      geom.newTaskPopup === "dialog" && geom.newTaskExpanded === "false",
      `haspopup=${geom.newTaskPopup} expanded=${geom.newTaskExpanded}`,
    );

    // The tagline is decorative and may be dropped on a phone — but it must SURVIVE
    // on desktop, where the sidebar is a vertical rail with room for it.
    if (label === "desktop") {
      check(`[${label}/${theme}] brand tagline still shown on desktop`, geom.tagVisible);

      // New Task open/close state — desktop only, where the button is reliably
      // clickable. The binding (aria-expanded={showNewTask}) is viewport-agnostic,
      // so one solid round-trip catches a regression the collapsed checks cannot:
      // opening must flip the trigger to "true", closing (Escape) back to "false".
      //
      // `.btn-new-task` is no longer unique to the sidebar trigger — Board.jsx's
      // empty-state CTA ("New task") reuses the same class, so a bare class
      // locator now hits Playwright's strict-mode ambiguity check. Target the
      // sidebar trigger by its accessible name instead ("+ New Task", exact —
      // Board's empty-state button is spelled "New task", no "+").
      const newTaskTrigger = page.getByRole("button", { name: "+ New Task", exact: true });
      await newTaskTrigger.click();
      await page.waitForTimeout(400);
      const ntOpen = await newTaskTrigger.getAttribute("aria-expanded").catch(() => null);
      check(`[${label}/${theme}] New Task reports expanded while its dialog is open`, ntOpen === "true", `expanded=${ntOpen}`);
      await page.keyboard.press("Escape");
      await page.waitForTimeout(300);
      const ntClosed = await newTaskTrigger.getAttribute("aria-expanded").catch(() => null);
      check(`[${label}/${theme}] New Task returns to collapsed after Escape`, ntClosed === "false", `expanded=${ntClosed}`);
    }

    // And Settings must actually open.
    await page.getByRole("button", { name: /^Settings$/ }).click();
    await page.waitForTimeout(600);
    const onSettings = await page.getByRole("button", { name: /NEW PROJECT/i }).isVisible().catch(() => false);
    check(`[${label}/${theme}] Settings page opens`, onSettings);
    // With the dialog open, the trigger must report aria-expanded="true".
    const settingsExpandedOpen = await page.locator(".nh-settings-row").getAttribute("aria-expanded").catch(() => null);
    check(
      `[${label}/${theme}] Settings button reports expanded while its dialog is open`,
      settingsExpandedOpen === "true",
      `expanded=${settingsExpandedOpen}`,
    );
    check(`[${label}/${theme}] no page errors`, errors.length === 0, errors[0] || "");

    await ctx.close();
  }
}

// Tall-viewport guard: `.nh-sidebar`'s `overflow-y: auto` is the backstop for
// short viewports, and it must stay a no-op backstop on a tall one — no
// scroll, no clipping, `overflow-y` still `auto` (not accidentally dropped).
{
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1200 } });
  const page = await ctx.newPage();
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/tasks")) return j([]);
    if (u.includes("/api/projects")) return j([]);
    return j({});
  });
  await page.goto("http://127.0.0.1:4620/", { waitUntil: "networkidle" });
  await page.waitForTimeout(500);
  const tall = await page.evaluate(() => {
    const sidebar = document.querySelector(".nh-sidebar");
    return {
      overflowY: sidebar ? getComputedStyle(sidebar).overflowY : null,
      scrollHeight: sidebar ? sidebar.scrollHeight : null,
      clientHeight: sidebar ? sidebar.clientHeight : null,
    };
  });
  check(
    `[tall/1920x1200] sidebar does not scroll (overflow-y:auto stays a no-op backstop)`,
    tall.scrollHeight !== null && tall.scrollHeight <= tall.clientHeight + 1,
    `scrollHeight=${tall.scrollHeight} clientHeight=${tall.clientHeight}`,
  );
  check(
    `[tall/1920x1200] sidebar overflow-y is still "auto"`,
    tall.overflowY === "auto",
    `overflowY=${tall.overflowY}`,
  );
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
