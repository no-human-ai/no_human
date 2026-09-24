// Approve-and-merge live progress (operator finding: a 2-4 minute synchronous
// land with zero feedback read as "doesn't work"). Drives the BUILT bundle
// (web/dist), with a fake, test-controllable `window.WebSocket` — the merge
// progress frames (`task_event`: merge_started/merge_step_*/human_merged)
// ride the existing broadcast socket, so this stub is what lets a test push
// them without a real backend.
//
// QUARANTINED (lane: manual — see e2e/manifest.mjs): pre-existing failure,
// not caused by this change. The very first check crashes uncaught —
// `page.evaluate: Execution context was destroyed, most likely because of a
// navigation` at the `el.click()` .btn-approve latency probe (no PASS lines
// run before it). No `<form>` wraps the button and no app-code navigation
// call was found near handleApprove in the time available to triage this
// walk; whether the destroyed-context is a walk race (evaluating across a
// re-render/remount boundary) or a genuine unwanted navigation is undecided.
// Left red rather than guessed at — see the Budget rule in .no_human/PLAN.md
// (do not change application code to make a walk pass, and do not paper over
// an undiagnosed failure by weakening the assertion).
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
await new Promise((r) => srv.listen(4691, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const now = new Date().toISOString();
const TASK = {
  id: "mergeprogress00aaaabbbb",
  title: "Approve and merge shows live progress",
  status: "awaiting_approval",
  kind: "feature",
  description: "Some description",
  created_at: now, updated_at: now,
  total_tokens: 1_000, attempts: 1,
  pr_url: "https://example.com/pull/7",
};

// The fake WebSocket: exposes every constructed instance on
// `window.__nhWS` so the Node side can push frames through
// `ws.__emit(frameObject)` — a controllable double of the real broadcast
// socket, not a real server.
async function stubWebSocket(page) {
  await page.addInitScript(() => {
    class FakeWebSocket {
      constructor(url) {
        this.url = url;
        this.readyState = 0;
        this.onopen = null;
        this.onmessage = null;
        this.onclose = null;
        this.onerror = null;
        window.__nhWS = window.__nhWS || [];
        window.__nhWS.push(this);
        setTimeout(() => {
          this.readyState = 1;
          if (this.onopen) this.onopen();
        }, 0);
      }
      send() {}
      close() {
        this.readyState = 3;
        if (this.onclose) this.onclose();
      }
      __emit(obj) {
        if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) });
      }
    }
    window.WebSocket = FakeWebSocket;
  });
}

async function pushWSFrame(page, frame) {
  await page.evaluate((f) => {
    const ws = (window.__nhWS || [])[(window.__nhWS || []).length - 1];
    if (ws) ws.__emit(f);
  }, frame);
}

function mockApi(page, {
  approveDelayMs = 0, approveStatus = 200, approveBody = null, eventsBody = null,
} = {}) {
  let approveCount = 0;
  const getApproveCount = () => approveCount;
  const handler = async (route) => {
    const req = route.request();
    const u = req.url();
    const j = (b, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(b) });
    if (req.method() === "POST" && u.includes("/approve") && !u.includes("/approve-landed")) {
      approveCount += 1;
      if (approveDelayMs) await new Promise((r) => setTimeout(r, approveDelayMs));
      const body = approveBody ?? {
        ok: true, landed_sha: "abc123def456",
        message: "Approved and merged — landed abc123def456 onto the default branch.",
      };
      return j(body, approveStatus);
    }
    if (req.method() !== "GET") return j({});
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/projects")) return j([]);
    if (u.match(/\/api\/tasks\/[^/]+\/diff/)) return j({ diff: "" });
    // `eventsBody`: lets AC2's drawer-row test seed the task's event history
    // (an `approve_refused` row) without needing a real backend — the
    // default `[]` is unchanged for every scenario that doesn't pass it.
    if (u.match(/\/api\/tasks\/[^/]+\/events/)) return j(eventsBody ?? []);
    if (u.match(/\/api\/tasks\/[^/]+$/)) return j(TASK);
    if (u.includes("/api/tasks")) return j([TASK]);
    return j({});
  };
  return { handler, getApproveCount };
}

const browser = await chromium.launch();

async function openDrawer(opts = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error" && !m.text().includes("WebSocket")) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await stubWebSocket(page);
  const mock = mockApi(page, opts);
  await page.route("**/api/**", mock.handler);
  await page.goto("http://127.0.0.1:4691/", { waitUntil: "networkidle" });
  await page.waitForTimeout(400);
  await page.locator(".task-card").first().click();
  await page.locator(".slideover").waitFor({ state: "visible", timeout: 5000 });
  await page.waitForTimeout(300);
  return { ctx, page, errors, mock };
}

// 9 — click → feedback latency: the button must flip to "Merging…" and
// disable itself within 100ms of the click, well before the (here, 3s)
// server round-trip resolves.
{
  const { ctx, page, errors } = await openDrawer({ approveDelayMs: 3000 });
  const btn = page.locator(".btn-approve");
  // Measured entirely IN-BROWSER (performance.now() either side of the
  // click + a MutationObserver on the button), not round-tripped through
  // Node/CDP — a Node-side Date.now() around Playwright's own click()/
  // waitFor() calls bundles in automation-protocol latency that has nothing
  // to do with the React state-change latency this criterion is about.
  const elapsed = await page.evaluate(() => new Promise((resolve) => {
    const el = document.querySelector(".btn-approve");
    const t0 = performance.now();
    const done = () => resolve(performance.now() - t0);
    const obs = new MutationObserver(() => {
      if (el.textContent.includes("Merging")) { obs.disconnect(); done(); }
    });
    obs.observe(el, { childList: true, characterData: true, subtree: true });
    el.click();
    if (el.textContent.includes("Merging")) { obs.disconnect(); done(); }
  }));
  check("[latency] button shows Merging… within 100ms of click", elapsed < 100, `${elapsed.toFixed(2)}ms`);
  check("[latency] button is disabled while merging", await btn.isDisabled());
  check("[latency] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// 10 — rapid double-click: exactly one POST /approve, even though the
// button is clicked twice ~20ms apart (a raced double-click beating the
// disable). The frontend guard is the button's own disabled state.
{
  const { ctx, page, mock } = await openDrawer({ approveDelayMs: 1000 });
  await page.evaluate(() => {
    const btn = document.querySelector(".btn-approve");
    btn.click();
    setTimeout(() => btn.click(), 20);
  });
  await page.waitForTimeout(300);
  check("[double-click] exactly one POST /approve reached the server",
    mock.getApproveCount() === 1, `saw ${mock.getApproveCount()}`);
  await ctx.close();
}

// 11 — streamed events: task_event frames pushed through the fake socket
// render the current step, and the resolved POST flips the label to
// "Approved — merge pending" — with no window where the label reads the
// plain idle "Approve and merge" after the click.
{
  const { ctx, page, errors } = await openDrawer({ approveDelayMs: 400 });
  const btn = page.locator(".btn-approve");
  const labelsSeen = [];
  const poll = setInterval(() => {
    btn.innerText().then((t) => labelsSeen.push(t)).catch(() => {});
  }, 15);

  await btn.click();
  // The `connectTaskProgress` socket opens from a useEffect keyed on
  // `merging` — give React a tick to commit and run it before pushing
  // frames, or `window.__nhWS`'s last entry could still be the board's own
  // WS from mount.
  await page.waitForTimeout(50);
  await pushWSFrame(page, {
    type: "task_event", task_id: TASK.id,
    event: { source: "human", kind: "merge_started", text: "merge started", ts: 1 },
  });
  await page.waitForTimeout(80);
  await pushWSFrame(page, {
    type: "task_event", task_id: TASK.id,
    event: { source: "human", kind: "merge_step_squash", text: "merge: squash", ts: 2 },
  });
  await page.waitForTimeout(80);
  const stepEl = page.locator(".approve-merge-step");
  const midText = await stepEl.innerText().catch(() => "");
  check("[stream] a merge_step_* frame renders its step name", /squash/i.test(midText), midText);

  await pushWSFrame(page, {
    type: "task_event", task_id: TASK.id,
    event: { source: "human", kind: "merge_step_push", text: "merge: push", ts: 3 },
  });
  await page.waitForTimeout(80);
  const pushText = await stepEl.innerText().catch(() => "");
  check("[stream] a later merge_step_* frame updates the step name", /push/i.test(pushText), pushText);

  await pushWSFrame(page, {
    type: "task_event", task_id: TASK.id,
    event: { source: "human", kind: "human_merged", text: "landed", ts: 4 },
  });
  await page.waitForTimeout(700); // let the mocked POST resolve
  clearInterval(poll);
  const finalText = await btn.innerText();
  check("[stream] the button transitions to Approved — merge pending on completion",
    /Approved.*merge pending/i.test(finalText), finalText);
  check("[stream] no dead-button window: label never reads the plain idle text after the click",
    !labelsSeen.some((t) => t.trim() === "Approve and merge"), JSON.stringify(labelsSeen.slice(0, 10)));
  check("[stream] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// 12 — failure: a land failure surfaces a persistent inline banner naming
// the step and the stderr, which stays up until the operator dismisses it.
{
  const { ctx, page } = await openDrawer({
    approveStatus: 500,
    approveBody: { detail: { step: "push", stderr: "remote: rejected — non-fast-forward, retry after fetch" } },
  });
  await page.locator(".btn-approve").click();
  const banner = page.locator(".flash-banner");
  await banner.waitFor({ state: "visible", timeout: 3000 });
  const text = await banner.innerText();
  check("[failure] banner names the failed step", /Failed: push/.test(text), text);
  check("[failure] banner includes the stderr text", text.includes("non-fast-forward"), text);

  await page.waitForTimeout(2000);
  check("[failure] banner persists (not auto-dismissed)", await banner.isVisible().catch(() => false));

  await page.locator(".flash-banner-dismiss").click();
  const stillVisible = await banner.isVisible().catch(() => false);
  check("[failure] banner disappears only after the dismiss click", !stillVisible);
  await ctx.close();
}

// 13 — refusal (task e24cee25/PR #643): a 409 refusal must render the exact
// server text, both as the inline drawer banner AND as a toast — the bug was
// that neither appeared and the click looked dead.
{
  const REFUSAL = "0936e40a3 is not an ancestor of fix/global-flags-defeat-the-merge-rules — refusing.";
  const { ctx, page } = await openDrawer({ approveStatus: 409, approveBody: { detail: REFUSAL } });
  await page.locator(".btn-approve").click();
  const banner = page.locator(".flash-banner");
  await banner.waitFor({ state: "visible", timeout: 3000 });
  const bannerText = await banner.innerText();
  check("[refusal] drawer banner contains the exact refusal text", bannerText.includes(REFUSAL), bannerText);

  const toast = page.locator(".nh-toast");
  await toast.waitFor({ state: "visible", timeout: 3000 });
  const toastText = await toast.innerText();
  check("[refusal] toast contains the exact refusal text", toastText.includes(REFUSAL), toastText);
  await ctx.close();
}

// 14 — the refusal survives closing the drawer: the card carries a
// persistent banner with the same text, dismissible via its own X.
{
  const REFUSAL = "Merge already in progress";
  const { ctx, page } = await openDrawer({ approveStatus: 409, approveBody: { detail: REFUSAL } });
  await page.locator(".btn-approve").click();
  await page.locator(".flash-banner").waitFor({ state: "visible", timeout: 3000 });

  await page.locator(".so-close").click();
  await page.locator(".slideover").waitFor({ state: "hidden", timeout: 3000 });

  const cardBanner = page.locator(".card-approve-error");
  await cardBanner.waitFor({ state: "visible", timeout: 3000 });
  const cardText = await cardBanner.innerText();
  check("[refusal] card banner survives closing the drawer with the same text",
    cardText.includes(REFUSAL), cardText);

  await page.locator(".card-approve-error-dismiss").click();
  const stillVisible = await cardBanner.isVisible().catch(() => false);
  check("[refusal] card banner disappears only after its own dismiss click", !stillVisible);
  await ctx.close();
}

// 15 — success path unchanged: a plain 200 approve must render neither a
// toast nor a card banner (the refusal-only UI must not leak into the happy
// path this bug never affected).
{
  const { ctx, page } = await openDrawer({ approveDelayMs: 50 });
  await page.locator(".btn-approve").click();
  await page.waitForTimeout(600);
  const toastVisible = await page.locator(".nh-toast").isVisible().catch(() => false);
  check("[success] no toast on a successful approve", !toastVisible);
  await page.locator(".so-close").click();
  await page.locator(".slideover").waitFor({ state: "hidden", timeout: 3000 });
  const cardBannerVisible = await page.locator(".card-approve-error").isVisible().catch(() => false);
  check("[success] no card banner on a successful approve", !cardBannerVisible);
  await ctx.close();
}

// 16 — drawer history: an `approve_refused` task event (as written by the
// backend on every refusal) renders as a labelled "Approve refused" row in
// the drawer's activity timeline, carrying the refusal text.
{
  const REFUSAL = "0936e40a3 is not an ancestor of fix/global-flags-defeat-the-merge-rules — refusing.";
  const { ctx, page } = await openDrawer({
    eventsBody: [{ source: "worker", kind: "approve_refused", text: REFUSAL, ts: 1 }],
  });
  // awaiting_approval tasks default-open on the Review section (defaultOpenSection),
  // and each accordion section only mounts (and fetches) its body while open — so
  // the Activity tab's poll-fetch of /events doesn't even fire until it's opened.
  await page.getByRole("button", { name: /^Activity/ }).click();
  const row = page.locator(".rich-kind.ak-approve_refused");
  await row.waitFor({ state: "visible", timeout: 3000 });
  const rowText = await row.innerText();
  check("[history] drawer event row is labelled Approve refused", /Approve refused/i.test(rowText), rowText);
  const body = page.locator(".rich-body", { hasText: "not an ancestor" });
  check("[history] drawer event row carries the refusal text", await body.isVisible().catch(() => false));
  await ctx.close();
}

// 17 — in-flight state (AC3): while a slow refusal is pending, the button is
// disabled and shows the busy label; once the refusal resolves, it flips to
// the retry label rather than sitting dead.
{
  const { ctx, page } = await openDrawer({
    approveDelayMs: 600, approveStatus: 409, approveBody: { detail: "Merge already in progress" },
  });
  const btn = page.locator(".btn-approve");
  await btn.click();
  await page.waitForTimeout(150);
  const midText = await btn.innerText();
  check("[in-flight] button is disabled while the refusal is pending", await btn.isDisabled(), midText);
  check("[in-flight] button shows a busy label while pending",
    /Approving…|Merging/i.test(midText), midText);

  await page.locator(".flash-banner").waitFor({ state: "visible", timeout: 3000 });
  const afterText = await btn.innerText();
  check("[in-flight] button reads Retry approve and merge once refused",
    /Retry approve and merge/i.test(afterText), afterText);
  check("[in-flight] button is re-enabled once refused", await btn.isEnabled());
  await ctx.close();
}

await browser.close();
srv.close();

console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nmerge-progress: all checks passed");
process.exit(failures.length ? 1 : 0);
