// Behavioural guard for the onboarding telemetry-consent wiring
// (App.jsx:984 `askTelemetry={shouldAskTelemetry(onboardStatus)}`).
//
// That one prop is the ONLY thing deciding whether a real user ever sees the
// "Usage insights" consent step. `shouldAskTelemetry` (onboardingConsent.js)
// is fail-OPEN: a missing/null status asks. That makes the two ways this
// wiring can break ASYMMETRIC — one mocked status payload cannot catch both:
//
//   * `askTelemetry={false}` (a literal instead of the computed gate) HIDES a
//     step that should show, for a never-asked install.
//   * dropping `setOnboardStatus(s)` from the status-fetch `.then` leaves
//     `onboardStatus === null` forever. Because the gate is fail-open, that
//     does NOT hide anything — it makes the step appear for an
//     ALREADY-asked install that should never see it again.
//
// So this suite drives three status payloads (never-asked, asked-absent
// "old server", already-asked) against the real built bundle in a real
// browser, and reads the rendered step rail + insights panel — the same
// thing a human eye would see. Mocked API, no :8420, no writes.
//
// This suite drives the BUILT bundle (web/dist): `npm run build` must
// precede it, and must be re-run before EVERY ablation cycle — reading
// `node e2e/onboarding-consent-step.mjs` against a stale bundle is stale-
// bundle noise, not evidence (defect 1a6a52ff).
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { TELEMETRY_CONSENT_QUESTION } from "../src/onboardingConsent.js";

const DIST = new URL("../dist", import.meta.url).pathname;

// Fail CLOSED: an absent bundle must be a hard failure, never a silent skip.
if (!fs.existsSync(path.join(DIST, "index.html"))) {
  console.error("web/dist is missing — run npm run build");
  process.exit(1);
}

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
const srv = http.createServer((q, r) => {
  const u = q.url.split("?")[0];
  let f = path.join(DIST, u === "/" ? "index.html" : u);
  if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(DIST, "index.html");
  r.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream" });
  r.end(fs.readFileSync(f));
});
await new Promise((r) => srv.listen(4644, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const BASE_STEPS_COUNT = 8; // welcome/repos/projects/docs/integrations/history/rules/summary — Onboarding.jsx BASE_STEPS.
const allErrors = [];

// Every route a full welcome->Launch walk touches (see web/src/api.js), so a
// walk never dies on an unmocked call and reports a false "step absent".
function mockRoutes(page, status) {
  page.route("**/api/**", (route) => {
    const u = route.request().url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/onboarding/status")) return j(status);
    if (u.includes("/api/onboarding/repos/detect")) return j({ repos: [] });
    if (u.includes("/api/repos/discover"))
      return j({ repos: [], roots_scanned: [], roots_refused: [], walk_truncated: false, capped: false, note: "" });
    if (u.includes("/api/onboarding/readiness"))
      return j({ total: 0, usable: 0, first_usable: null, needs_proving: [] });
    if (u.includes("/api/onboarding/history/extract")) return j({ available: false });
    if (u.includes("/api/integrations/setup")) return j({ integrations: [] });
    if (u.includes("/api/tasks")) return j([]);
    return j({});
  });
}

async function newPage(browser, status) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => allErrors.push(e.message));
  mockRoutes(page, status);
  await page.goto("http://127.0.0.1:4644/", { waitUntil: "networkidle" });
  await page.waitForTimeout(400);
  return { ctx, page };
}

// `.ob-step-label` is visually upper-cased via CSS `text-transform`, which
// Playwright's innerText reflects (it returns the RENDERED text) — so labels
// are lower-cased before comparison to stay independent of that styling.
const railLabels = async (page) =>
  (await page.locator(".ob-stepper .ob-step-label").allInnerTexts()).map((t) => t.trim().toLowerCase());
// The Email step (required, not skippable) sits between Welcome and
// Repositories, and its Continue is `disabled` until the field holds a
// well-formed address — a bare click would sit on Playwright's enabled
// actionability check for 30s, and the walk below would die before reaching
// Launch (making its "no insights step was ever shown" result vacuous). Go
// THROUGH the step the way a user does: whenever the address field is on
// screen, type into it, then Continue.
const EMAIL = "walker@example.com";
const cont = async (page) => {
  const field = page.getByPlaceholder("you@example.com");
  if (await field.isVisible().catch(() => false)) {
    await field.fill(EMAIL);
    await page.waitForTimeout(100);
  }
  await page.getByRole("button", { name: /^Continue$/ }).click();
  await page.waitForTimeout(200);
};

const browser = await chromium.launch();

// The usage-insights CONSENT step was REMOVED (operator, 2026-08-26): telemetry
// is on by default and never asked about. So NO onboarding-status payload may
// ever produce a "usage insights" rail entry, and the rail is always the fixed
// 8 base steps — this suite proves the step is gone in the real built bundle.
const STATUSES = [
  ["never asked",              { completed: false, telemetry_asked: false }],
  ["telemetry_asked absent",   { completed: false }],
  ["already asked",            { completed: false, telemetry_asked: true }],
];

for (const [name, status] of STATUSES) {
  const { ctx, page } = await newPage(browser, status);
  const labels = await railLabels(page);
  check(`[${name}] no "usage insights" step in the rail`, !labels.includes("usage insights"),
    `rail labels: ${JSON.stringify(labels)}`);
  check(`[${name}] rail entry count = BASE_STEPS (8)`, labels.length === BASE_STEPS_COUNT,
    `got ${labels.length}`);
  await ctx.close();
}

// The consent question/heading must appear nowhere in a full walk, and the walk
// must still reach Launch (a vacuous "never seen" from a walk that died early
// would be a false pass — so we assert BOTH, in the same walk).
{
  const { ctx, page } = await newPage(browser, { completed: false, telemetry_asked: false });
  let sawInsights = false;
  let reachedLaunch = false;
  for (let hop = 0; hop < 12; hop++) {
    if (await page.getByRole("heading", { name: /^Usage insights$/i }).isVisible().catch(() => false)) {
      sawInsights = true;
    }
    if (await page.getByText("Repos with a proven test command").isVisible().catch(() => false)) {
      reachedLaunch = true;
      break;
    }
    await cont(page);
  }
  check("the walk reaches Launch (so the 'no insights' result is not vacuous)", reachedLaunch,
    reachedLaunch ? "" : "walk died before reaching the Launch/summary step");
  check("the Usage insights consent step is never shown during a full walk", !sawInsights,
    sawInsights ? "the removed consent step reappeared" : "");
  // The consent copy string must not appear on any step either.
  check("the consent question text is absent from the wizard",
    !(await page.getByText(TELEMETRY_CONSENT_QUESTION).isVisible().catch(() => false)));
  await ctx.close();
}

check("no page errors while rendering the wizard", allErrors.length === 0, allErrors[0] || "");

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
