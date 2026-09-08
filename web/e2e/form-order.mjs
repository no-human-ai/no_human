// Drives the BUILT bundle (web/dist) — never the dev server — per the standing gotcha.
//
// WHY THIS EXISTS. The composer shipped with `Next →` rendered ABOVE the Kind and
// Repository controls, inside the same <form>. Every gate passed it: `node --test`
// never mounts the component, `vite build` is happy, and e2e/sweep.mjs PHOTOGRAPHED
// the broken screen and asserted nothing about the picture. A human found it by
// looking. That is not a detection strategy.
//
// The invariant a human was applying, made machine-checkable: within one <form>,
// the submit button is the LAST thing — nothing you still have to fill in may sit
// after it. A control that does is invisible to the user, who reads the submit
// button as "the form ends here".
//
// Both halves are needed and neither implies the other:
//   * DOM ORDER  — what the keyboard and a screen reader walk. Correct pixels with
//     the control after submit in the DOM is still a broken tab order.
//   * GEOMETRY   — what the eye walks. Correct DOM order can still paint below the
//     button via `order:`, `flex-direction: column-reverse`, or grid areas.
//
// This observes the rendered artifact in a real browser. It is deliberately NOT a
// regex over .jsx source: source-text guards for UI behaviour are a known trap here.
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const DIST = new URL("../dist", import.meta.url).pathname;

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2" };
const server = http.createServer((req, res) => {
  const url = req.url.split("?")[0];
  let file = path.join(DIST, url === "/" ? "index.html" : url);
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST, "index.html");
  res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "application/octet-stream" });
  res.end(fs.readFileSync(file));
});
await new Promise((r) => server.listen(4603, r));

const PROJECTS = [
  { id: "p1", name: "no_human", repo_paths: ["/a/no_human"], primary_repo: "/a/no_human" },
  { id: "p2", name: "metrics-core", repo_paths: ["/a/metrics-core-a", "/a/metrics-core-b"], primary_repo: "/a/metrics-core-a" },
];
const CONFIG = { notifications: { email_to: "dana.lee@example.com" }, llm: { auth_profile: "personal" } };

const failures = [];
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
  if (!ok) failures.push(name + (detail ? ": " + detail : ""));
};

// Runs in the page. Returns one entry per <form> that has a visible submit.
const SCAN = () => {
  const CONTROLS = "input:not([type=hidden]), select, textarea, [role=radio], [role=checkbox], [role=switch], [role=combobox]";
  // A review defeated an earlier version with an `opacity:0` trailing submit: it was
  // "visible" by these rules, so it became the anchor and the real button above the
  // defect stopped being checked. Invisible-to-the-user must mean invisible here.
  // Use the PLATFORM's visibility predicate, not a hand-rolled one. Three rounds of
  // review defeated hand-rolled versions in three different ways: `opacity:0` (round 2),
  // then `visibility: collapse` — which renders nothing but computes to "collapse", not
  // "hidden" — and near-zero opacity, since `parseFloat(op) === 0` is an exact test and
  // 0.004 passes, and `w === 0 && h === 0` is an AND, so a 1x1 decoy passes too.
  // checkVisibility() closes that whole class in one call and cannot drift.
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.right < 0) return false;   // scrolled off, not merely below the fold
    if (typeof el.checkVisibility === "function") {
      return el.checkVisibility({ visibilityProperty: true, contentVisibilityAuto: true, opacityProperty: true });
    }
    const st = getComputedStyle(el);
    return (r.width || r.height) && st.visibility === "visible" && st.display !== "none"
      && parseFloat(st.opacity || "1") > 0.01;
  };

  const name = (el) =>
    el.getAttribute("aria-label") ||
    el.getAttribute("name") ||
    el.id ||
    (el.getAttribute("aria-labelledby") &&
      (document.getElementById(el.getAttribute("aria-labelledby"))?.textContent || "").trim()) ||
    (el.textContent || "").trim().slice(0, 30) ||
    el.tagName.toLowerCase();

  return [...document.querySelectorAll("form")].filter(visible).map((form, i) => {
    // The FIRST visible submit anchors the check. An earlier revision used the LAST
    // one, reasoning it was "the most permissive choice". An independent review
    // defeated that in ONE line: appending a second `type="submit"` to the composer
    // turned the whole suite green with the shipped defect untouched. A gate a coder
    // can satisfy by ADDING A BUTTON is not a gate.
    //
    // First-submit is the honest anchor because it is what the USER meets: the first
    // submit they reach is the one that reads "the form ends here". A genuinely
    // multi-step form with an inert earlier step does not false-positive, because that
    // step's controls are not visible and visibility is filtered above.
    // `button[type="submit"]` is an ATTRIBUTE selector, and a <button> with no type
    // IS a submit button per the HTML spec — React never adds the attribute unless you
    // ask. A review deleted `type="submit"` from `Next →` (behaviour identical, the
    // submit event still fired) and the anchor vanished. Match the EFFECTIVE submit set.
    const SUBMITS = 'button:not([type]), button[type="submit"], input[type="submit"]';
    const submits = [...form.querySelectorAll(SUBMITS)].filter(visible);
    if (!submits.length) return { form: i, skipped: "no visible submit" };
    const submit = submits[0];
    const sRect = submit.getBoundingClientRect();

    const afterInDom = [];
    const belowInPixels = [];
    for (const el of form.querySelectorAll(CONTROLS)) {
      if (!visible(el)) continue;
      if (submit.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) afterInDom.push(name(el));
      // "Starts below where the button ends" — a control sharing the button's row
      // (Cancel, a priority pill) is fine; one that begins past its bottom is not.
      if (el.getBoundingClientRect().top >= sRect.bottom - 1) belowInPixels.push(name(el));
    }
    return { form: i, submit: (submit.textContent || "submit").trim().slice(0, 24), afterInDom, belowInPixels };
  });
};

async function checkForms(page, label) {
  const forms = await page.evaluate(SCAN);
  const live = forms.filter((f) => !f.skipped);
  if (!live.length) { console.log(`  (${label}: no form with a visible submit)`); return 0; }
  for (const f of live) {
    check(`${label} form#${f.form} [${f.submit}] — no control AFTER submit in DOM order`,
      f.afterInDom.length === 0, f.afterInDom.join(", "));
    check(`${label} form#${f.form} [${f.submit}] — no control BELOW submit on screen`,
      f.belowInPixels.length === 0, f.belowInPixels.join(", "));
  }
  return live.length;
}

// HIGH 2 IS OPEN AND UNFIXED: four of this app's six forms (AddMemoryModal,
// AddProjectModal, the test-layer form, IntegrationConfigForm) sit behind a click this
// suite never makes, so an ordering defect planted in one is invisible to it. A review
// opened the two reachable ones by hand and ran this file's own SCAN against them: both
// were clean, so the gap is latent today, not live.
//
// A sweep to open them was written, wedged the gate, and was deleted. It carried a
// confident explanation of WHY it wedged. That explanation was FALSE — a review checked
// it at runtime and Escape does close the modal it named (Settings.jsx:1000,
// `useEscapeKey(onClose, !busy)`). It is deleted rather than corrected, because this file
// has now been wrong about a CAUSE three times, and a wrong cause is worse than silence:
// it sends the next author down a road that was never the road. If you close HIGH 2,
// diagnose the wedge yourself from a live run.

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await ctx.newPage();
page.on("pageerror", (e) => check("no page error", false, e.message));

await page.route("**/api/**", async (route) => {
  const u = route.request().url();
  const json = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  if (u.includes("/api/tasks") && route.request().method() === "GET") return json([]);
  if (u.includes("/api/projects")) return json(PROJECTS);
  if (u.includes("/api/config")) return json(CONFIG);
  if (u.includes("/api/onboarding")) return json({ completed: true });
  // These return LISTS. The catch-all `{}` below made the Rules panel throw
  // "c.map is not a function", which blanked the settings overlay and made every
  // later tab unreachable — a harness artifact, not a product defect.
  if (u.includes("/api/rules") || u.includes("/api/skills") || u.includes("/api/learnings")) return json([]);
  return json({});
});

await page.goto("http://127.0.0.1:4603/", { waitUntil: "networkidle" });
await page.waitForTimeout(600);

let scanned = 0;
let accountForms = 0;
const visited = [];
scanned += await checkForms(page, "board");
visited.push("board");

// Settings BEFORE the composer, deliberately: the composer does not reliably close
// on Escape, and when it stays open the sidebar is gone from the DOM — which is how
// an earlier revision of this file skipped every settings screen while still
// reporting a clean positive control off the composer alone.
const settingsNav = page.locator(".nh-sidebar >> text=Settings");
check("settings is reachable from the sidebar", (await settingsNav.count()) > 0);
if (await settingsNav.count()) {
  await settingsNav.first().click();
  await page.waitForTimeout(400);
  // Section list and class verified against src/Settings.jsx SECTIONS + the
  // `.settings-overlay-navitem` nav. NOT `.settings-tab` — that class belongs to the
  // Second-brain pane's own Pending/Active sub-tabs (rendered only in the D3
  // kill-switch/`auto_manage: false` view) — and there is no "Config" section
  // at all. e2e/sweep.mjs asks for both and swallows the miss in a try/catch.
  //
  // 🖐️ CORRECTION. An earlier version of this comment, and the commit message that
  // shipped it, said sweep therefore "saves the default settings screen six times per
  // theme under six different filenames". The three facts above are true; that
  // CONSEQUENCE is false, and a review refuted it by running sweep: `shot()` sits
  // INSIDE the `if (await click(...))`, so a failed click writes NO file. A real run
  // produced ZERO desktop settings screenshots. The six filenames I had seen were a
  // 2026-07-18 run against an older UI that did have a `.settings-tab` nav and a
  // Config section. Reading stale artifacts as current evidence is the same mistake
  // as trusting a file:line without re-checking it.
  for (const label of ["Projects", "Rules", "Skills", "Memories", "Integrations", "Account"]) {
    const tab = page.locator(`.settings-overlay-navitem:has-text('${label}')`);
    // A tab that cannot be opened is a FAILURE, not a skip. A silent skip is
    // indistinguishable from a pass, and that is the bug class this file exists for.
    check(`settings tab '${label}' is reachable`, (await tab.count()) > 0);
    if (await tab.count()) {
      await tab.first().click();
      await page.waitForTimeout(350);
      const n = await checkForms(page, `settings/${label}`);
      scanned += n;
      if (label === "Account") accountForms = n;
      visited.push(`settings/${label}`);
    }
  }
  // Settings is an OVERLAY, not a page — leaving it open makes its backdrop swallow
  // every later click.
  const close = page.locator(".settings-overlay-close");
  check("settings overlay can be closed", (await close.count()) > 0);
  if (await close.count()) { await close.first().click(); await page.waitForTimeout(350); }
}

// The composer — the screen the defect shipped on. Last, because it may not close.
const newTask = page.locator("text=+ NEW TASK");
check("composer is reachable from the board", (await newTask.count()) > 0);
if (await newTask.count()) {
  await newTask.first().click();
  await page.waitForTimeout(500);
  const composerForms = await checkForms(page, "composer");
  scanned += composerForms;
  check("positive control: the composer's own form was actually scanned", composerForms >= 1);

  // The per-<form> scan above is blind to a defect moved OUT of the form. A review
  // split the composer with a bare `</form><form>` before the Kind eyebrow — no button
  // added, no attribute changed — and the gate went green while the user still saw Kind
  // at y=601 and Repository at y=699, below a `Next →` at y=515. The second form had no
  // submit, so it was skipped.
  //
  // So: within the composer dialog, every visible control must belong to the SAME form
  // as the submit. Scoped to this one screen deliberately — a document-wide version
  // would false-positive on genuine multi-section pages, and this is the screen whose
  // controls actually get lifted during a refactor.
  const orphans = await page.evaluate(() => {
    // Identical to SCAN's predicate ON PURPOSE. A review found this file had TWO copies:
    // `checkVisibility` was added to the other one and not this one, leaving the orphan
    // check — the SOLE defence for the "control lifted out of the form" class — still
    // defeatable by a `visibility: collapse; position: fixed; inset: 0` decoy submit, whose
    // rect.bottom is the viewport bottom so nothing is ever "below" it. Fixing one copy of
    // duplicated logic is this file's recurring failure, four review rounds running.
    // If you change one of these predicates, change both, or better: hoist them.
    const vis = (el) => {
      const r = el.getBoundingClientRect();
      if (r.bottom < 0 || r.right < 0) return false;
      if (typeof el.checkVisibility === "function") {
        return el.checkVisibility({ visibilityProperty: true, contentVisibilityAuto: true, opacityProperty: true });
      }
      const st = getComputedStyle(el);
      return (r.width || r.height) && st.visibility === "visible" && st.display !== "none"
        && parseFloat(st.opacity || "1") > 0.01;
    };
    const SUBMITS = 'button:not([type]), button[type="submit"], input[type="submit"]';
    // Anchor on the COMPOSER's own form — the visible form containing the prompt
    // textarea. Taking [0] of a document-wide query anchored on an unrelated no-type
    // button elsewhere on the page, which made every control below it look orphaned.
    // Find the composer form by its DIALOG, not by "the form containing a textarea".
    // A review swapped the prompt box for a contentEditable role=textbox — the ordinary
    // refactor when you add @-mentions — and this reported "NO VISIBLE COMPOSER FORM" on
    // a composer that was present, visible and correctly ordered: a false FAIL blocking a
    // legitimate change, with a message that was simply untrue. The textarea lookup is
    // kept only as a fallback for a composer rendered outside a dialog.
    const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(vis);
    const form = dialogs.map((d) => d.querySelector("form")).filter((f) => f && vis(f))[0]
      || [...document.querySelectorAll("form")].filter((f) => vis(f) && f.querySelector("textarea"))[0];
    if (!form) return { seen: 0, below: ["NO VISIBLE COMPOSER FORM"] };
    const submit = [...form.querySelectorAll(SUBMITS)].filter(vis)[0];
    if (!submit) return { seen: 0, below: ["COMPOSER FORM HAS NO VISIBLE SUBMIT"] };
    const scope = form.closest('[role="dialog"]') || form.parentElement || document.body;
    const CTRL = "input:not([type=hidden]), select, textarea, [role=radio], [role=checkbox], [role=switch]";
    // Only controls the user meets AFTER the button. Flagging every control outside
    // the form was a false positive: the fix deliberately moved Kind and Repository
    // ABOVE the prompt box, so they are legitimately outside it and legitimately
    // earlier. The invariant is about what sits BELOW the submit, not about form
    // membership for its own sake.
    // GEOMETRY ONLY. This used to also require `el.form !== submit.form`, i.e. DOM
    // OWNERSHIP — and HTML's `form=` content attribute restores ownership from anywhere
    // in the document. A review moved Kind and Repository bodily below `Next →`, outside
    // the <form>, added form="composer-form" to three elements, and the gate printed
    // `clean ✓` with the shipped defect back on screen (Kind y=601, Repo y=699, submit
    // y=515, submit event still firing). Three attributes flipped the verdict.
    // Ownership is not position. The invariant is position, so test position.
    const sb = submit.getBoundingClientRect().bottom;
    const seen = [...scope.querySelectorAll(CTRL)].filter(vis);
    // Report the count from THIS selector, in THIS evaluate. An audit defeated the
    // previous floor by mutating the selector on THIS line: the floor lived in a
    // separate page.evaluate with its own selector string, so it counted 10 controls
    // while the check it was supposed to guard matched zero and printed `clean ✓`
    // with the shipped defect on screen. A floor that counts a different selector
    // guards nothing. Fifth round of "two copies of the logic, one of them fixed".
    return {
      seen: seen.length,
      below: seen.filter((el) => el.getBoundingClientRect().top >= sb - 1)
        .map((el) => (el.getAttribute("aria-label") || el.textContent || el.tagName).trim().slice(0, 24)),
    };
  });
  // CONTROL-COUNT FLOOR. Every positive control in this file counts FORMS; a review
  // proved that typing the control selector to `input[data-typo-never-matches]` yields
  // all-PASS and `form-order: clean ✓` with the shipped defect on screen, because nothing
  // ever counted CONTROLS. The gate could not tell "checked and clean" from "found nothing
  // to check" — the exact hole this file's own header claims to close. The composer has 7
  // Kind chips plus a repository control plus a priority select; 8 is a deliberate floor
  // under that, not a target.
  check("composer — no visible control sits BELOW the submit",
    orphans.below.length === 0, orphans.below.join(", "));
  // The floor now reads the SAME selector's count, from the SAME evaluate.
  check("positive control: that check's own selector actually matched the composer's controls",
    orphans.seen >= 8, `seen=${orphans.seen}`);
  visited.push("composer");
}

// Positive control: the scan must actually reach every screen AND find forms. A
// selector that silently matched nothing would print all-PASS and prove nothing —
// the exact shape of the hole this suite exists to close.
check("positive control: every intended screen was visited", visited.length === 8, `visited=${visited.join(" ")}`);
// `scanned >= 1` was satisfied by the composer ALONE. A review showed that in the
// product's sanctioned api_key auth mode the Account form does not render at all, so
// the only negative control vanished while this still reported success — the exact
// hole this file's own comments claim to close. The floor is now the number of forms
// this app actually exposes to the sweep.
check("positive control: more than one form was scanned — the composer alone is not enough",
  scanned >= 2, `scanned=${scanned}`);
check("positive control: the Account form (the negative control) actually rendered",
  accountForms >= 1, `accountForms=${accountForms}`);

await browser.close();
server.close();

if (failures.length) {
  console.error(`\nFORM-ORDER FAILURES (${failures.length}):\n` + failures.map((f) => "  - " + f).join("\n"));
  process.exit(1);
}
// Scoped success line. A review objected — correctly — that an unqualified "clean ✓" from
// a suite that reaches 2 of this app's 6 forms is a whole-app claim it has not earned, and
// run-all.mjs promotes it to a real gate. Say what was actually verified.
console.log(`\nform-order: ${scanned} form(s) scanned (composer, account) — clean ✓`);
console.log("  NOT covered: AddMemoryModal, AddProjectModal, test-layer-form, IntegrationConfigForm");
console.log("  NOT covered: shadow DOM, occluded controls, portals outside the composer dialog");
