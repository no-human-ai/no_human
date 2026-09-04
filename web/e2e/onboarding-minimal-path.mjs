// Minimal onboarding path (spec §3 B1): tick one repo → "Skip setup — open the board"
// → the sidebar shows a compact "Finish setup" entry (above Settings) carrying
// the four deferred steps; clicking it expands the list, each "Done" removes its
// step, each row deep-links to a real Settings pane. Mocked API, no :8420.
//
// Run twice: once at desktop (1440×900, the original checks, unchanged) and
// once at phone width (390×844) to pin the AI-config popup off-screen fix —
// the ≤640px block used to re-lift .nh-aiconfig-nudge to `position: absolute`
// anchored to .nh-sidebar-foot, which sits outside the mobile nav's viewport,
// so a new mobile user never saw it.
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
await new Promise((r) => srv.listen(4641, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const overlaps = (p, q) => !!p && !!q &&
  p.x < q.x + q.width && q.x < p.x + p.width &&
  p.y < q.y + q.height && q.y < p.y + p.height;

const browser = await chromium.launch();

async function runPath(viewport, label) {
  const ctx = await browser.newContext({ viewport });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));

  const hits = new Set();
  let deferred = ["docs", "integrations", "history", "rules"];
  await page.route("**/api/**", (route) => {
    const req = route.request();
    const u = req.url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/onboarding/status")) return j({ completed: false });
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
    if (u.includes("/api/onboarding/complete")) { hits.add("complete"); return j({ ok: true, onboarding: { completed: true, deferred } }); }
    if (u.includes("/api/onboarding/deferred/") && u.endsWith("/done")) {
      const step = u.split("/api/onboarding/deferred/")[1].replace("/done", "");
      hits.add(`done:${step}`);
      deferred = deferred.filter((s) => s !== step);
      return j({ deferred });
    }
    if (u.includes("/api/onboarding/deferred")) return j({ deferred });
    if (u.includes("/api/tasks")) return j([]);
    return j({});
  });
  await page.goto("http://127.0.0.1:4641/", { waitUntil: "networkidle" });
  await page.waitForTimeout(400);

  const cont = () => page.getByRole("button", { name: /^Continue$/ }).click();

  // Walk to the repos step.
  for (let hop = 0; hop < 6; hop++) {
    if (await page.getByRole("heading", { name: /Which repositories do you work on/i })
        .isVisible().catch(() => false)) break;
    await cont(); await page.waitForTimeout(200);
  }
  await page.getByRole("button", { name: "Add alpha-svc" }).click();
  await page.waitForTimeout(200);

  const startBtn = page.getByRole("button", { name: /^Skip setup — open the board$/ });
  check("'Skip setup — open the board' appears once a repo is ticked",
    await startBtn.isVisible().catch(() => false));
  await startBtn.click();
  await page.waitForTimeout(500);

  check("POST /api/onboarding/complete was called", hits.has("complete"));

  // The sidebar now carries a compact "Finish setup" entry (collapsed) with a
  // count badge — NOT a big board card. It lives above the Settings nav row.
  const finishRow = page.locator(".nh-finish-setup-row");
  check("Finish-setup entry rendered in the sidebar",
    await finishRow.isVisible().catch(() => false));
  check("the entry shows the deferred count (4)",
    (await finishRow.locator(".nh-navrow-badge").textContent().catch(() => "")) === "4");
  // Collapsed by default: the item list is not shown until the entry is clicked.
  check("items hidden until the entry is expanded",
    (await page.locator(".finish-setup-open").count()) === 0);

  // The one-time AI-config popup renders on this same path (onboarded, not yet
  // dismissed) and used to be absolutely positioned above the Settings row,
  // overlapping the Finish-setup row and swallowing its clicks. A positive
  // control first — an absence claim needs one, or the overlap checks below
  // would pass vacuously if the popup were not visible at all.
  const nudge = page.locator(".nh-aiconfig-nudge");
  const cta = page.locator(".nh-aiconfig-nudge-cta");
  check("the one-time AI-config popup is rendered on this path",
    await cta.isVisible().catch(() => false));

  const finishBox = await finishRow.boundingBox();
  const nudgeBox = await nudge.boundingBox();
  const ctaBox = await cta.boundingBox();
  check("the AI-config popup does not overlap the Finish-setup row",
    !overlaps(finishBox, nudgeBox), JSON.stringify({ finishBox, nudgeBox }));
  check("the popup CTA does not overlap the Finish-setup row",
    !overlaps(finishBox, ctaBox), JSON.stringify({ finishBox, ctaBox }));

  // ── Phone-only geometry checks (390×844): pins the off-screen-popup fix ── //
  // The ≤640px block used to re-lift .nh-aiconfig-nudge to `position:
  // absolute; bottom: calc(100% + 8px)` anchored to .nh-sidebar-foot, which
  // sits outside the mobile nav's viewport — a new mobile user never saw the
  // nudge. Now it renders in normal flow inside .nh-main on phones instead.
  if (label === "phone") {
    let mobileGeometryChecks = 0;
    const vp = page.viewportSize();

    mobileGeometryChecks++;
    check("[phone] the popup CTA's bounding box is fully inside the viewport (not off-screen)",
      !!ctaBox && ctaBox.x >= 0 && ctaBox.y >= 0 &&
      ctaBox.x + ctaBox.width <= vp.width && ctaBox.y + ctaBox.height <= vp.height,
      JSON.stringify({ ctaBox, viewport: vp }));

    mobileGeometryChecks++;
    check("[phone] the popup CTA's top is at or below the top of the visible viewport (explicit y >= 0)",
      !!ctaBox && ctaBox.y >= 0, JSON.stringify({ ctaBoxY: ctaBox && ctaBox.y }));

    mobileGeometryChecks++;
    const scrollY = await page.evaluate(() => window.scrollY);
    check("[phone] the popup is visible without any page scroll (it renders near the top of the document)",
      scrollY === 0, JSON.stringify({ scrollY }));

    mobileGeometryChecks++;
    check("[phone] the popup does not overlap the Finish-setup entry",
      !overlaps(finishBox, ctaBox), JSON.stringify({ finishBox, ctaBox }));

    mobileGeometryChecks++;
    const hitTestable = ctaBox
      ? await page.evaluate(({ x, y }) => {
          const el = document.elementFromPoint(x, y);
          return !!el && !!el.closest(".nh-aiconfig-nudge-cta");
        }, { x: ctaBox.x + ctaBox.width / 2, y: ctaBox.y + ctaBox.height / 2 })
      : false;
    check("[phone] the popup CTA is hit-testable at its own center (nothing else covers it)",
      hitTestable === true);

    check("[phone] at least one mobile geometry check actually ran",
      mobileGeometryChecks >= 1, `ran ${mobileGeometryChecks}`);
  }

  // Expand it.
  await finishRow.click();
  await page.waitForTimeout(150);
  const openButtons = () => page.locator(".finish-setup-open");
  const before = await openButtons().count();
  check("expanded list shows the four deferred items", before === 4, `saw ${before}`);
  // The deep-link deep-links honestly — each row's title carries the real
  // Settings pane it lands on. The docs row maps to Projects (docs live with
  // repos), never the old generic fallback. (Which pane each key resolves to is
  // pinned by onboardingMinimal.test.mjs; opening the pane itself needs the whole
  // Settings data layer mocked, out of scope for this onboarding flow.)
  check("the docs row is present to deep-link", before >= 1);

  // "Done" on the first item (docs) removes it and calls the API.
  await page.locator(".finish-setup-done").first().click();
  await page.waitForTimeout(300);
  check("clicking Done posted /deferred/docs/done", hits.has("done:docs"));
  const after = await openButtons().count();
  check("the dismissed item disappeared", after === 3, `saw ${after}`);

  // Clear the remaining three: the whole entry must disappear once nothing is
  // deferred (the "disappears when the setup is finished" the user asked for).
  for (let i = 0; i < 3; i++) {
    await page.locator(".finish-setup-done").first().click();
    await page.waitForTimeout(200);
  }
  check("the Finish-setup entry is gone once every item is done",
    (await page.locator(".nh-finish-setup-row").count()) === 0);

  check("no page errors during the minimal path", errors.length === 0, errors[0] || "");

  await ctx.close();
}

await runPath({ width: 1440, height: 900 }, "desktop");
await runPath({ width: 390, height: 844 }, "phone");

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
