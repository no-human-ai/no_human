// Drives the BUILT bundle (web/dist) — never the dev server — per the standing gotcha.
// Serves dist over http, stubs /api/*, and exercises the 5A composer end to end.
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const DIST = new URL("../dist", import.meta.url).pathname;
const OUT = new URL("./shots", import.meta.url).pathname;
fs.mkdirSync(OUT, { recursive: true });

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2" };
const server = http.createServer((req, res) => {
  const url = req.url.split("?")[0];
  let file = path.join(DIST, url === "/" ? "index.html" : url);
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST, "index.html");
  res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "application/octet-stream" });
  res.end(fs.readFileSync(file));
});
await new Promise((r) => server.listen(4599, r));

const PROJECTS = [
  { id: "p1", name: "no_human", repo_paths: ["/a/no_human"], primary_repo: "/a/no_human" },
  { id: "p2", name: "metrics-core", repo_paths: ["/a/metrics-core-a", "/a/metrics-core-b"], primary_repo: "/a/metrics-core-a" },
];
const CONFIG = { notifications: { email_to: "dana.lee@example.com" }, llm: { auth_profile: "personal" } };
// GET /api/repos/discover — the auto-discovered clone roots. One clean repo and
// one mid-edit repo, because the dirty flag is the reason the scan probes git
// at all: a user must see it before pointing a task at that checkout.
const DISCOVERED = {
  repos: [
    { path: "/Users/dev/git/alpha", name: "alpha", is_git: true, branch: "main",
      detached: false, dirty: false, dirty_scan: "complete", ecosystem: "python" },
    { path: "/Users/dev/Projects/gamma", name: "gamma", is_git: true, branch: "feat/x",
      detached: false, dirty: true, dirty_scan: "complete", ecosystem: "node" },
  ],
  roots_scanned: ["/Users/dev/git", "/Users/dev/Projects"],
  roots_missing: [], roots_refused: [], total_found: 2,
  limit: 200, capped: false, walk_truncated: false, note: "", elapsed_ms: 42,
};

const failures = [];
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
  if (!ok) failures.push(name + (detail ? ": " + detail : ""));
};

const browser = await chromium.launch();

for (const theme of ["dark", "light"]) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error" && !m.text().includes("WebSocket")) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

  let grillPayload = null;
  let scaffoldPayload = null;
  await page.route("**/api/**", async (route) => {
    const u = route.request().url();
    const json = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/repos/scaffold")) {
      scaffoldPayload = route.request().postDataJSON();
      return route.fulfill({
        status: 201, contentType: "application/json",
        body: JSON.stringify({ repo_path: `${scaffoldPayload.parent}/${scaffoldPayload.name}`, project_id: "p-new" }),
      });
    }
    if (u.includes("/api/tasks") && route.request().method() === "GET") return json([]);
    if (u.includes("/api/projects")) return json(PROJECTS);
    if (u.includes("/api/config")) return json(CONFIG);
    if (u.includes("/api/onboarding")) return json({ completed: true });
    if (u.includes("/api/worker")) return json({ running: true, workers: 1 });
    if (u.includes("/api/fs/suggest")) return json({
      base: "/Users/dev/git",
      suggestions: [
        { path: "/Users/dev/git/alpha", name: "alpha", is_repo: true },
        { path: "/Users/dev/git/beta", name: "beta", is_repo: false },
      ],
    });
    if (u.includes("/api/repos/discover")) return json(DISCOVERED);
    if (u.includes("/api/grill/stream")) {
      // Faithful POST-SSE stub — the production happy path (api.js grillStepSSE).
      grillPayload = route.request().postDataJSON();
      const body = [
        'data: {"kind":"tool_use","text":"grep -rn uploader"}',
        'data: {"kind":"grill_question","type":"question","round":1,"question":"Which uploader?","suggestions":["A: the S3 one"]}',
        'data: {"kind":"done"}',
        "",
      ].join("\n");
      return route.fulfill({ status: 200, contentType: "text/event-stream", body });
    }
    if (u.includes("/api/grill")) {
      grillPayload = route.request().postDataJSON();
      return json({ type: "question", round: 1, question: "Which uploader?", suggestions: ["A: the S3 one"] });
    }
    return json({});
  });

  await page.addInitScript((t) => localStorage.setItem("nh-theme", t), theme);
  await page.goto("http://127.0.0.1:4599/", { waitUntil: "networkidle" });

  // Open the composer via the keyboard shortcut ("n") — the shipped trigger.
  await page.keyboard.press("n");
  const dialog = page.locator('[role="dialog"][aria-label="New task"]');
  await dialog.waitFor({ state: "visible", timeout: 5000 });
  check(`[${theme}] composer opens`, await dialog.isVisible());

  const promptAfterOpen = await dialog.locator("textarea").inputValue();
  check(`[${theme}] the 'n' shortcut does not leak into the prompt`, promptAfterOpen === "", JSON.stringify(promptAfterOpen));

  // The name arrives via an async config fetch — poll briefly instead of
  // racing it (this check flaked on both base and head; PR #116 review).
  let greeting = "";
  for (let i = 0; i < 20; i++) {
    greeting = await dialog.locator("h2").innerText();
    if (greeting === "Hey there, Dana") break;
    await page.waitForTimeout(100);
  }
  check(`[${theme}] greets by derived name`, greeting === "Hey there, Dana", greeting);

  // Chips: all 7, feature selected by default.
  const chipLabels = await dialog.locator('[role="radiogroup"][aria-labelledby="kind-eyebrow"] [role="radio"]').allInnerTexts();
  check(`[${theme}] 7 kind chips render as a labelled radio group`, chipLabels.length === 7, chipLabels.join("|"));

  // Submit is disabled with an empty prompt.
  const submit = dialog.locator('button[type="submit"]');
  check(`[${theme}] submit disabled on empty prompt`, await submit.isDisabled());

  // Discoverability: a first-time user must see KIND + REPOSITORY before the
  // submit button, not after it (N5 — the button used to render first).
  const order = await page.evaluate(() => {
    const d = document.querySelector('[role="dialog"][aria-label="New task"]');
    const repo = d.querySelector("#repo-eyebrow");
    const submitEl = d.querySelector('button[type="submit"]');
    if (!repo || !submitEl) return { found: false };
    const domBefore = !!(repo.compareDocumentPosition(submitEl) & Node.DOCUMENT_POSITION_FOLLOWING);
    return { found: true, domBefore, repoY: repo.getBoundingClientRect().top, submitY: submitEl.getBoundingClientRect().top };
  });
  check(`[${theme}] REPOSITORY control precedes the submit button in document order`,
    order.found && order.domBefore && order.repoY < order.submitY, JSON.stringify(order));

  check(`[${theme}] the repo-mode toggle states its purpose`,
    await dialog.getByRole("button", { name: "use another repo" }).isVisible());

  // Free-text repo mode: the path input must offer live directory autocomplete
  // (the same /api/fs/suggest datalist Settings and Onboarding have).
  await dialog.getByRole("button", { name: "use another repo" }).click();
  const pathField = dialog.locator('input[aria-label="Repository path"]');
  await pathField.fill("/Users/dev/git/");
  await page.waitForTimeout(400); // debounce (150ms) + fetch stub
  const dl = await page.evaluate(() => {
    const inp = document.querySelector('[role="dialog"][aria-label="New task"] input[aria-label="Repository path"]');
    if (!inp) return { found: false };
    const listId = inp.getAttribute("list");
    const list = listId && document.getElementById(listId);
    return {
      found: true, listId,
      paired: !!list && list.tagName === "DATALIST",
      options: list ? [...list.querySelectorAll("option")].map((o) => o.value) : [],
    };
  });
  check(`[${theme}] free-text repo input is paired with a datalist`, dl.found && dl.paired, JSON.stringify(dl));
  check(`[${theme}] typing a path yields autocomplete options`,
    dl.options.includes("/Users/dev/git/alpha") && dl.options.includes("/Users/dev/git/beta"), JSON.stringify(dl.options));

  // Auto-discovery: the user should not have to remember a path. The list is
  // COLLAPSED here on purpose - two saved projects exist, so opening it
  // unprompted would push the rest of the form down for a user who did not ask.
  const discoverToggle = dialog.getByRole("button", { name: /repositories found on this machine/ });
  check(`[${theme}] free-text mode offers the discovered repositories`,
    await discoverToggle.isVisible());
  check(`[${theme}] the discovered list starts collapsed when a project is already pickable`,
    (await discoverToggle.getAttribute("aria-expanded")) === "false",
    await discoverToggle.getAttribute("aria-expanded"));
  check(`[${theme}] no discovered row is rendered while the list is collapsed`,
    (await dialog.locator("[data-discovered-repo]").count()) === 0);

  await discoverToggle.click();
  const rows = dialog.locator("[data-discovered-repo]");
  check(`[${theme}] opening the list shows every discovered repository`,
    (await rows.count()) === 2, String(await rows.count()));
  const gamma = dialog.locator(String.raw`[data-discovered-repo="/Users/dev/Projects/gamma"]`);
  const rowText = async (loc) => (await loc.innerText()).replace(/\n/g, " | ");
  const alpha = dialog.locator('[data-discovered-repo="/Users/dev/git/alpha"]');
  check(`[${theme}] a mid-edit repo is flagged on its row`,
    (await rowText(gamma)).includes("uncommitted changes"), await rowText(gamma));
  check(`[${theme}] a clean repo is NOT flagged`,
    !(await rowText(alpha)).includes("uncommitted changes"), await rowText(alpha));
  check(`[${theme}] the branch rides along on the row`,
    (await rowText(gamma)).includes("feat/x"), await rowText(gamma));

  // The gate that must survive the new block: REPOSITORY still precedes the
  // submit button, with the discovered list OPEN.
  const orderOpen = await page.evaluate(() => {
    const d = document.querySelector('[role="dialog"][aria-label="New task"]');
    const repo = d.querySelector("#repo-eyebrow");
    const submitEl = d.querySelector('button[type="submit"]');
    return {
      domBefore: !!(repo.compareDocumentPosition(submitEl) & Node.DOCUMENT_POSITION_FOLLOWING),
      repoY: repo.getBoundingClientRect().top, submitY: submitEl.getBoundingClientRect().top,
    };
  });
  check(`[${theme}] the open discovered list does not reorder REPOSITORY and submit`,
    orderOpen.domBefore && orderOpen.repoY < orderOpen.submitY, JSON.stringify(orderOpen));

  await alpha.click();
  check(`[${theme}] one click puts the discovered path in the repository field`,
    (await pathField.inputValue()) === "/Users/dev/git/alpha", await pathField.inputValue());
  check(`[${theme}] picking a repo closes the list`,
    (await dialog.locator("[data-discovered-repo]").count()) === 0);

  // Create-a-new-repo (plan Task 5): the affordance exists in free-text mode,
  // and a stubbed POST /api/repos/scaffold flows its result into the repo path.
  const createBtn = dialog.getByRole("button", { name: "create a new repo" });
  check(`[${theme}] free-text mode offers "create a new repo"`, await createBtn.isVisible());
  await createBtn.click();
  const parentField = dialog.locator('input[aria-label="Parent directory"]');
  const nameField = dialog.locator('input[aria-label="New repository name"]');
  check(`[${theme}] create-repo reveals parent + name inputs`,
    (await parentField.isVisible()) && (await nameField.isVisible()));
  // Its parent PathInput must have its OWN datalist - sharing the repo-path
  // field's id would break the browser's pairing for both.
  const newRepoListId = await parentField.getAttribute("list");
  check(`[${theme}] create-repo parent input has a distinct datalist`,
    newRepoListId === "composer-newrepo-parent" && newRepoListId !== dl.listId, String(newRepoListId));
  await parentField.fill("/Users/dev/git");
  await nameField.fill("shiny-new");
  await dialog.getByRole("button", { name: "Create repo" }).click();
  await page.waitForTimeout(400);
  check(`[${theme}] Create repo POSTs parent+name to /api/repos/scaffold`,
    scaffoldPayload?.parent === "/Users/dev/git" && scaffoldPayload?.name === "shiny-new",
    JSON.stringify(scaffoldPayload));
  check(`[${theme}] the created repo's path lands in the repository field`,
    (await pathField.inputValue()) === "/Users/dev/git/shiny-new", await pathField.inputValue());

  // Focus/announce: once the panel unmounts, a keyboard/screen-reader user
  // must not be dropped on document.body with no signal.
  const focusInfo = await page.evaluate(() => ({
    label: document.activeElement?.getAttribute("aria-label"),
    value: document.activeElement?.value,
  }));
  check(`[${theme}] focus returns to the repository path input after a successful create`,
    focusInfo.label === "Repository path" && focusInfo.value === "/Users/dev/git/shiny-new",
    JSON.stringify(focusInfo));
  check(`[${theme}] success is announced via the existing role=alert element`,
    await dialog.locator('[role="alert"]', { hasText: "Repository created" }).isVisible());

  check(`[${theme}] success closes the create-repo panel`,
    !(await parentField.isVisible().catch(() => false)));

  // An unrelated re-render (typing in the prompt textarea) must not steal
  // focus back from wherever the operator has since moved it.
  await dialog.locator("textarea").fill("unrelated re-render probe");
  await page.waitForTimeout(300);
  const tagAfterRerender = await page.evaluate(() => document.activeElement?.tagName);
  check(`[${theme}] an unrelated re-render does not steal focus back to the repository input`,
    tagAfterRerender === "TEXTAREA", tagAfterRerender);
  await dialog.locator("textarea").fill("");

  // F1: Enter in the PARENT field must be intercepted like the name field —
  // never an implicit form submission. Fill the prompt first so a leak WOULD
  // submit the composer (repoPath is already set from the create above).
  await dialog.locator("textarea").fill("guard probe: Enter must not submit");
  await createBtn.click(); // reopen the panel (success cleared both fields)
  await parentField.fill("/Users/dev/git");
  scaffoldPayload = null;
  await parentField.press("Enter"); // name empty -> must be a no-op
  await page.waitForTimeout(300);
  check(`[${theme}] Enter in the parent field with name empty neither submits nor closes`,
    (await dialog.isVisible()) && (await parentField.isVisible())
      && grillPayload === null && scaffoldPayload === null,
    `grill=${JSON.stringify(grillPayload)} scaffold=${JSON.stringify(scaffoldPayload)}`);
  await nameField.fill("enter-made");
  await parentField.press("Enter"); // both filled -> creates
  await page.waitForTimeout(400);
  check(`[${theme}] Enter in the parent field with both filled fires the scaffold POST`,
    scaffoldPayload?.parent === "/Users/dev/git" && scaffoldPayload?.name === "enter-made",
    JSON.stringify(scaffoldPayload));
  check(`[${theme}] no task submission happened during the Enter probes`,
    grillPayload === null, JSON.stringify(grillPayload));
  await dialog.locator("textarea").fill(""); // drop the probe prompt
  // Restore saved-project mode so the checks below start from the default state.
  await dialog.getByRole("button", { name: "use a saved project" }).click();

  // Type a prompt → submit enables (a project is selected by default).
  await dialog.locator("textarea").fill("Add a retry to the uploader\nBack off, cap at 3.");
  check(`[${theme}] submit enabled once prompt + repo present`, await submit.isEnabled());

  // code_review chip → the PR-URL field appears and BLOCKS submit until a ref exists.
  await dialog.getByRole("radio", { name: "Code review" }).click();
  const prField = dialog.locator('input[aria-label="PR or MR URL"]');
  check(`[${theme}] code_review reveals the PR-URL field`, await prField.isVisible());
  check(`[${theme}] code_review blocks submit with no PR ref`, await submit.isDisabled());
  await prField.fill("https://github.com/acme/app/pull/42");
  check(`[${theme}] a valid PR URL unblocks submit`, await submit.isEnabled());
  // Shorthand must be accepted too (mirrors vcs/pr_refs.py).
  await prField.fill("code.example.com/dev/acme-test PR #7001");
  check(`[${theme}] shorthand PR ref unblocks submit`, await submit.isEnabled());

  if (theme === "dark") await page.screenshot({ path: `${OUT}/composer-code-review-dark.png` });

  // Preflight is OFF: `border` sets width only, so without `border-solid` the
  // computed style collapses to none/0px (divs) or a UA bevel (buttons).
  //
  // A bare `[role="dialog"]` is not unique here: with a fresh context (no
  // localStorage) and onboarding mocked complete, App.jsx's one-time AI-config
  // nudge (`aria-label="Complete AI configuration"`) is ALSO a `role="dialog"`
  // and renders at the same time as this composer — `document.querySelector`
  // grabs whichever is first in DOM order, which is the nudge, not the
  // composer, so `d.querySelector("form textarea")` comes back null. Scope to
  // this dialog's own aria-label (set at TaskComposer.jsx's `aria-label="New
  // task"`) so it can't match the wrong dialog.
  const borders = await page.evaluate(() => {
    const d = document.querySelector('[role="dialog"][aria-label="New task"]');
    const probe = (el) => { const c = getComputedStyle(el); return { w: c.borderTopWidth, s: c.borderTopStyle }; };
    return {
      card: probe(d),
      surface: probe(d.querySelector("form textarea").parentElement),
      pill: probe(d.querySelector('select[aria-label="Priority"]').parentElement),
      chip: probe(d.querySelector('[aria-labelledby="kind-eyebrow"] [role="radio"]')),
      input: probe(d.querySelector('input[aria-label="PR or MR URL"]')),
    };
  });
  const bad = Object.entries(borders).filter(([, v]) => v.s !== "solid" || v.w !== "1px");
  check(`[${theme}] every bordered control paints a real 1px solid border`, bad.length === 0, JSON.stringify(bad));

  // Back to feature, and submit → the grill must start with the split prompt.
  await dialog.getByRole("radio", { name: "Feature" }).click();
  check(`[${theme}] switching kind hides the PR field`, !(await prField.isVisible()));
  await page.screenshot({ path: `${OUT}/composer-${theme}.png` });

  await submit.click();
  await page.waitForTimeout(800);
  check(`[${theme}] submit starts the intake grill`, grillPayload !== null);
  check(`[${theme}] title = first line`, grillPayload?.title === "Add a retry to the uploader", JSON.stringify(grillPayload?.title));
  check(`[${theme}] description = remainder`, grillPayload?.description === "Back off, cap at 3.", JSON.stringify(grillPayload?.description));
  check(`[${theme}] project_id sent`, grillPayload?.project_id === "p1", JSON.stringify(grillPayload?.project_id));

  // The grill screen replaced the composer (flow preserved).
  const grillVisible = await page.locator(".grill-round-badge, .grill-loading").first().isVisible().catch(() => false);
  check(`[${theme}] grill screen renders after submit`, grillVisible);
  if (theme === "dark") await page.screenshot({ path: `${OUT}/grill-after-submit-dark.png` });

  check(`[${theme}] zero console errors`, errors.length === 0, errors.join(" | "));
  await ctx.close();
}

// Escape closes the composer, and a narrow viewport stays usable.
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error" && !m.text().includes("WebSocket")) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const json = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/projects")) return json(PROJECTS);
    if (u.includes("/api/config")) return json(CONFIG);
    if (u.includes("/api/onboarding")) return json({ completed: true });
    if (u.includes("/api/tasks")) return json([]);
    return json({});
  });
  await page.goto("http://127.0.0.1:4599/", { waitUntil: "networkidle" });
  await page.keyboard.press("n");
  const dialog = page.locator('[role="dialog"][aria-label="New task"]');
  await dialog.waitFor({ state: "visible", timeout: 5000 });
  await page.screenshot({ path: `${OUT}/composer-mobile.png` });
  const box = await dialog.boundingBox();
  check("[mobile] composer fits the 390px viewport", box.width <= 390, `width=${box.width}`);
  // No horizontal page overflow.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  check("[mobile] no horizontal overflow", !overflow);

  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  check("[mobile] Escape closes the composer", !(await dialog.isVisible()));
  check("[mobile] zero console errors", errors.length === 0, errors.join(" | "));
  await ctx.close();
}

// A FAILED grill must not destroy what the operator typed (both the SSE stream and
// its sync fallback fail here — the common "Stream closed" case).
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const json = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/grill")) {
      return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "grill exploded" }) });
    }
    if (u.includes("/api/projects")) return json(PROJECTS);
    if (u.includes("/api/config")) return json(CONFIG);
    if (u.includes("/api/onboarding")) return json({ completed: true });
    if (u.includes("/api/tasks")) return json([]);
    return json({});
  });
  await page.goto("http://127.0.0.1:4599/", { waitUntil: "networkidle" });
  await page.keyboard.press("n");
  const dialog = page.locator('[role="dialog"][aria-label="New task"]');
  await dialog.waitFor({ state: "visible", timeout: 5000 });

  const TYPED = "Add a retry to the uploader\nBack off, cap at 3.";
  await dialog.locator("textarea").fill(TYPED);
  await dialog.getByRole("radio", { name: "Bug fix" }).click();
  await dialog.locator('button[type="submit"]').click();
  await page.waitForTimeout(1500);

  const backOnComposer = await dialog.isVisible();
  check("[grill-fail] composer comes back", backOnComposer);
  const survived = await dialog.locator("textarea").inputValue();
  check("[grill-fail] the typed prompt SURVIVES the failure", survived === TYPED, JSON.stringify(survived));
  const kindKept = await dialog.getByRole("radio", { name: "Bug fix" }).getAttribute("aria-checked");
  check("[grill-fail] the chosen kind survives", kindKept === "true", String(kindKept));
  const errShown = await dialog.getByText("grill exploded").isVisible().catch(() => false);
  check("[grill-fail] the error is shown", errShown);
  await ctx.close();
}

// BACKDROP — reported by a friend testing the app: "clicking anywhere outside it
// closes that window — you can be in the middle of a long intake grill and
// accidentally click somewhere and it got closed." A click on the backdrop must
// never discard typed work. Escape and Cancel stay as the deliberate exits.
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const json = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/onboarding")) return json({ completed: true });
    if (u.includes("/api/projects")) return json(PROJECTS);
    if (u.includes("/api/config")) return json(CONFIG);
    if (u.includes("/api/tasks")) return json([]);
    return json({});
  });
  await page.goto("http://127.0.0.1:4599/", { waitUntil: "networkidle" });
  await page.keyboard.press("n");
  const dialog = page.locator('[role="dialog"][aria-label="New task"]');
  await dialog.waitFor({ state: "visible", timeout: 5000 });

  const TYPED = "A long prompt the operator does not want to lose";
  await dialog.locator("textarea").fill(TYPED);

  // Click the backdrop, well outside the dialog box.
  await page.mouse.click(8, 8);
  await page.waitForTimeout(400);
  const stillOpen = await dialog.isVisible().catch(() => false);
  check("[backdrop] a click outside does NOT close the composer", stillOpen);
  if (!stillOpen) {
    // Everything below reads the dialog. Without this the suite spends a 30s
    // locator timeout and never runs the Escape and Cancel checks.
    console.log("SKIPPING the rest: the composer is gone");
  } else {
  check("[backdrop] the typed prompt survives the outside click",
    (await dialog.locator("textarea").inputValue()) === TYPED);

  // React state surviving is NOT the property that matters: the operator keeps
  // TYPING. If the backdrop click moved focus to <body>, the dialog is still
  // open and its state intact while every further keystroke goes nowhere — the
  // same symptom as the original report, reached by a click. Observe the caret.
  const focused = await page.evaluate(() => ({
    tag: document.activeElement?.tagName || "none",
    cls: String(document.activeElement?.className || ""),
  }));
  check("[backdrop] the click does not steal the caret out of the prompt",
    focused.tag === "TEXTAREA", `${focused.tag}.${focused.cls}`);
  await page.keyboard.type(" MORE");
  check("[backdrop] keystrokes after the stray click still reach the prompt",
    (await dialog.locator("textarea").inputValue()).endsWith(" MORE"));

  // The deliberate exits must still work, or this trades one trap for another.
  await page.keyboard.press("Escape");
  await page.waitForTimeout(400);
  check("[backdrop] Escape still closes the composer",
    !(await dialog.isVisible().catch(() => false)));

  await page.keyboard.press("n");
  await dialog.waitFor({ state: "visible", timeout: 5000 });
  await dialog.getByRole("button", { name: /^cancel$/i }).click();
  await page.waitForTimeout(400);
  check("[backdrop] Cancel still closes the composer",
    !(await dialog.isVisible().catch(() => false)));
  }
  check("[backdrop] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
server.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
