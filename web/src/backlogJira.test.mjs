import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { prefillFromIssue, promptFromIssue, jiraStatusCategory, jiraStatusChipStyle, externalIdFromIssue, importedStatusCategory, importedChip, jiraResultHeader, jiraEmptyMessage, formatIssueUpdated } from "./jiraImport.js";

// Task 1.6 — starting a task from a Jira ticket. Like integrations.test.mjs /
// settingsOverlay.test.mjs, there's no jsdom/React renderer wired into this
// project's `node --test` harness, so the .jsx assertions below read source
// rather than mounting components. jiraImport.js's pure functions get real
// behavioral tests.
//
// The ticket LIST moved out of the composer's nested "Import from Jira"
// disclosure and onto the Backlog page (Backlog.jsx), so the browse/render
// assertions that used to read TaskComposer.jsx read Backlog.jsx now — the
// behaviour they pin is unchanged, only its address is. What stayed in the
// composer is the source/external_id pass-through, still asserted there.

const here = fileURLToPath(new URL(".", import.meta.url));
const composerJsx = readFileSync(here + "TaskComposer.jsx", "utf8");
const backlogJsx = readFileSync(here + "Backlog.jsx", "utf8");
const appJsx = readFileSync(here + "App.jsx", "utf8");
const apiJs = readFileSync(here + "api.js", "utf8");
const jiraImportJs = readFileSync(here + "jiraImport.js", "utf8");

// ── jiraImport.js — real unit tests ─────────────────────────────────────────

test("prefillFromIssue builds 'KEY: summary' as the title", () => {
  const { title } = prefillFromIssue({
    key: "PROJ-42", summary: "Fix the retry loop", description: "", url: "https://x/browse/PROJ-42",
  });
  assert.equal(title, "PROJ-42: Fix the retry loop");
});

test("prefillFromIssue appends the Jira traceability line to the description", () => {
  const { description } = prefillFromIssue({
    key: "PROJ-1", summary: "S", description: "Body text.", url: "https://acme.atlassian.net/browse/PROJ-1",
  });
  assert.equal(description, "Body text.\n\nImported from Jira: https://acme.atlassian.net/browse/PROJ-1");
});

test("prefillFromIssue still appends the traceability line when the issue has no description", () => {
  const { description } = prefillFromIssue({
    key: "PROJ-2", summary: "S", description: "", url: "https://acme.atlassian.net/browse/PROJ-2",
  });
  assert.equal(description, "\n\nImported from Jira: https://acme.atlassian.net/browse/PROJ-2");
});

test("promptFromIssue joins title+description exactly as splitPrompt.js expects to split them back apart", () => {
  const issue = { key: "PROJ-9", summary: "Do X", description: "Details", url: "https://x/browse/PROJ-9" };
  const prompt = promptFromIssue(issue);
  assert.equal(prompt, "PROJ-9: Do X\n\nDetails\n\nImported from Jira: https://x/browse/PROJ-9");
  // First line only is the title; everything else is description (promptSplit.js's contract).
  const firstLine = prompt.split("\n")[0];
  assert.equal(firstLine, "PROJ-9: Do X");
});

// ── M1: status CATEGORY normalizer + chip style (real behavioral tests) ────

test("jiraStatusCategory maps done/closed/resolved to 'done'", () => {
  assert.equal(jiraStatusCategory("Done"), "done");
  assert.equal(jiraStatusCategory("Closed"), "done");
  assert.equal(jiraStatusCategory("resolved"), "done");
});

test("jiraStatusCategory maps in progress/review to 'active'", () => {
  assert.equal(jiraStatusCategory("In Progress"), "active");
  assert.equal(jiraStatusCategory("In Review"), "active");
  assert.equal(jiraStatusCategory("Review"), "active");
});

test("jiraStatusCategory maps to do/backlog/open/new to 'todo'", () => {
  assert.equal(jiraStatusCategory("To Do"), "todo");
  assert.equal(jiraStatusCategory("Backlog"), "todo");
  assert.equal(jiraStatusCategory("Open"), "todo");
  assert.equal(jiraStatusCategory("New"), "todo");
});

test("jiraStatusCategory falls back to 'unknown' for a custom workflow status", () => {
  assert.equal(jiraStatusCategory("Blocked on Vendor"), "unknown");
  assert.equal(jiraStatusCategory(""), "unknown");
  assert.equal(jiraStatusCategory(undefined), "unknown");
});

test("a Done ticket and a To Do ticket get different chip classes/colours", () => {
  const done = jiraStatusChipStyle("Done");
  const todo = jiraStatusChipStyle("To Do");
  assert.notEqual(done, null);
  assert.notEqual(todo, null);
  assert.notEqual(done.color, todo.color);
  assert.notEqual(done.background, todo.background);
});

test("jiraStatusChipStyle returns null for unknown status (caller keeps its neutral className)", () => {
  assert.equal(jiraStatusChipStyle("Some Custom Status"), null);
});

test("chip tokens are the EXISTING bridged semantic ramp, never a raw hex", () => {
  for (const status of ["Done", "In Progress", "To Do"]) {
    const style = jiraStatusChipStyle(status);
    for (const value of Object.values(style)) {
      assert.match(value, /^var\(--[a-z-]+\)$/, `${status} chip style must reference a CSS var, got ${value}`);
    }
  }
});

test("Backlog.jsx derives the chip style from jiraStatusChipStyle, not a static class", () => {
  assert.match(backlogJsx, /jiraStatusChipStyle[\s\S]{0,200}from ["']\.\/jiraImport\.js["']/);
  assert.match(backlogJsx, /const chipStyle = jiraStatusChipStyle\(issue\.status\);/);
  // The neutral fallback (unknown status) still reads via className, not colour alone.
  assert.match(backlogJsx, /border-line bg-panel text-text-muted/);
});

// ── Starting a ticket goes through the composer, not around it ──────────────

test("the composer no longer contains a ticket picker of its own — one place to browse", () => {
  // Two creation paths drift. The Backlog page browses; the composer composes.
  for (const gone of [/searchJiraIssues/, /handleJiraPick/, /jiraResults/, /jiraQuery/]) {
    assert.doesNotMatch(composerJsx, gone,
      `the composer must not re-grow its own ticket picker: ${gone}`);
  }
});

test("App.jsx seeds the SAME NewTaskModal from a backlog ticket — no second create path", () => {
  // The one createTask call in App.jsx is inside NewTaskModal.handleSubmit,
  // reached only after the grill. A backlog start seeds that modal's `initial`
  // and takes the identical route.
  assert.equal((appJsx.match(/await createTask\(/g) || []).length, 1,
    "there must be exactly ONE createTask call site in App.jsx");
  assert.match(appJsx, /initial=\{backlogSeed\}/,
    "a backlog ticket must be started by seeding NewTaskModal, not by its own create call");
  assert.match(appJsx, /source: tracker,/);
  assert.match(appJsx, /externalId: externalIdFromIssue\(issue\)/);
  assert.doesNotMatch(backlogJsx, /createTask/,
    "the Backlog page must never create a task itself");
});

// ── api.js — the new read call + source passthrough ────────────────────────

test("the issue-browse call hits /api/integrations/{tracker}/issues with q + limit", () => {
  assert.match(apiJs, /export async function searchJiraIssues\(q, limit = 50\)/);
  assert.match(apiJs, /export async function searchLinearIssues\(q, limit = 50\)/);
  // ONE implementation for both: the two endpoints have the same contract and
  // return the same row shape, so a per-tracker copy could only drift.
  assert.match(apiJs, /\/api\/integrations\/\$\{tracker\}\/issues\?\$\{params\}/);
  assert.equal((apiJs.match(/issues\?\$\{params\}/g) || []).length, 1,
    "there must be exactly one browse implementation, not one per tracker");
});

// SCRUM-3: the client used to request only 20 while the backend clamp allows
// 50 — 25 open tickets in a 45-ticket project silently never appeared.
test("SCRUM-3: client request limit matches the backend clamp (50, not 20)", () => {
  assert.match(apiJs, /export async function searchJiraIssues\(q, limit = 50\)/);
  assert.doesNotMatch(apiJs, /limit = 20/);
});

// The wire shape grew one key (`eval_result`) for the grill's intake-eval
// verdict — the regex is updated to match, per the same "updated not
// deleted" precedent as the backend-picker test just below. `source` itself
// is unaffected: an undefined value for every typed task, exactly as before.
test("createTask forwards source (undefined for every typed task)", () => {
  assert.match(apiJs, /export async function createTask\(\{[^}]*\bsource\b[^}]*\}\)/s);
  assert.match(apiJs, /JSON\.stringify\(\{ title, description, repo_path, project_id, kind, priority, acceptance_criteria, source, external_id, backend, follows_id, eval_result \}\)/);
});

// The board can now pick a coder backend (claude|codex|local) — this test
// used to pin the OPPOSITE ("single in-process Claude backend; no picker").
// That premise is gone: SUPPORTED_BACKENDS in agent/backend.py always had
// codex/local, and only the board's composer lacked a way to reach them.
// Updated per that feature, not deleted, per the "no net reduction in tests
// or assertions" rule.
test("createTask forwards backend to the wire body (board coder-backend picker)", () => {
  assert.match(apiJs, /export async function createTask\(\{[^}]*\bbackend\b[^}]*\}\)/s);
  assert.match(apiJs, /JSON\.stringify\(\{ title, description, repo_path, project_id, kind, priority, acceptance_criteria, source, external_id, backend, follows_id, eval_result \}\)/);
});

// ── TaskComposer.jsx — coder-backend picker (public issue #5) ──────────────
// The board's task composer must gain a coder-backend picker whose options
// come from the server (agent.backend.SUPPORTED_BACKENDS via GET
// /api/config's `coder_backends`), not a hardcoded JS array, and the picker
// must never let a user believe it changes who REVIEWS the work. There's no
// jsdom renderer here (see the file banner), so these read source, like every
// other .jsx assertion in this file.

test("the composer's backend picker options are server-derived, never a hardcoded array", () => {
  assert.match(composerJsx, /backendOptions\s*=\s*config\?\.coder_backends\s*\?\?\s*\[\]/,
    "options must come from GET /api/config's coder_backends field");
  assert.match(composerJsx, /backendOptions\.map\(/,
    "the <option> list must be rendered FROM that server value");
  // A backend added to SUPPORTED_BACKENDS must appear with no web change — so
  // there must be no second, hand-written copy of the backend list anywhere
  // in this file (e.g. ["claude", "codex", "local"]).
  assert.doesNotMatch(composerJsx, /\[\s*["']claude["']\s*,\s*["']codex["']\s*,\s*["']local["']\s*\]/,
    "the picker must not hardcode its own copy of SUPPORTED_BACKENDS");
});

test("the picker states, in the UI, that the choice affects the coder only — once the choice is non-default", () => {
  // Pinned roles must likewise be server-derived (agent.backend.CLAUDE_PINNED_ROLES
  // via /api/config's claude_pinned_roles), never a second literal that could
  // drift from the one make_backend actually enforces.
  assert.match(composerJsx, /claudePinnedRoles\s*=\s*config\?\.claude_pinned_roles\s*\?\?\s*\[\]/);
  // 2026-09-01 operator feedback: the statement used to render unconditionally
  // ("Coder backend only…"), which put backend-internals jargon in front of
  // every user even on the config default. It now lives in coderBackendCaption
  // (own test file), called with the EFFECTIVE backend (independent review
  // 2026-09-02: the picker's raw value alone missed a `worker.backend`-configured
  // install whose picker was left untouched), and the composer must not keep a
  // second hardcoded copy of the copy or the role list.
  assert.match(
    composerJsx,
    /import\s*\{\s*coderBackendCaption\s*,\s*effectiveCoderBackend\s*\}\s*from\s*["']\.\/coderBackendCaption\.js["']/,
  );
  assert.match(composerJsx, /effectiveCoderBackend\(\s*backend\s*,\s*config\s*\)/);
  assert.match(composerJsx, /coderBackendCaption\(\s*effectiveBackend\s*,\s*claudePinnedRoles\s*\)/);
  assert.doesNotMatch(composerJsx, /Coder backend only/);

  // Reviewer finding 2026-09-02: `doesNotMatch(/reviewer.*planner.*supervisor/)`
  // passed only because `.` never crosses newlines, while a hardcoded
  // multi-line "reviewer/\nplanner/supervisor/..." role list sat right there
  // in a comment a few lines up — the assertion was vacuous. Strip comments
  // first, then check across newlines with `[\s\S]`, and prove the check can
  // actually fail (positive controls: the stripper didn't gut the file, and
  // the regex itself is live against a synthetic hit).
  const composerNoComments = composerJsx
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  assert.doesNotMatch(composerNoComments, /reviewer[\s\S]*planner[\s\S]*supervisor/);
  // Positive control 1: the comment stripper must not have gutted the file —
  // real, still-present source must survive it.
  assert.match(composerNoComments, /claudePinnedRoles/);
  assert.match(composerNoComments, /coderBackendCaption/);
  // Positive control 2: the (comment-stripped) regex itself must be able to
  // fail — a fixture that cannot fail proves nothing.
  assert.match("reviewer\nplanner\nsupervisor", /reviewer[\s\S]*planner[\s\S]*supervisor/);
  // Positive control 3: the SAME regex, run against a synthetic string built
  // the way the stripper's OUTPUT is shaped (comment markers removed, code
  // left intact), still catches a role list that survived stripping — proof
  // this is a live check on `composerNoComments`, not just on raw literals.
  const syntheticLeak = composerNoComments + "\nconst x = 'reviewer, planner, supervisor';\n";
  assert.match(syntheticLeak, /reviewer[\s\S]*planner[\s\S]*supervisor/);

  // The render must be gated on the helper's return, not unconditional —
  // that gate is the whole fix, so pin it here too, not only in the helper's
  // own test file. Whitespace-tolerant so reformatting (prettier, etc.)
  // cannot silently defeat a single-space-sensitive regex.
  assert.match(composerJsx, /backendOptions\.length\s*>\s*0\s*&&\s*backendCaption\s*&&\s*\(/);

  // Stale-comment fix: the picker's tooltip always discloses the coder-only
  // scope now (title prop, every render); the caption <p> below is the
  // conditional one. The old comment pointed at "the title/help text below"
  // as if it too were conditional — that phrasing must be gone, replaced by
  // a comment that actually names CLAUDE_PINNED_ROLES.
  assert.doesNotMatch(composerJsx, /see the title\/help text below/);
  assert.match(composerJsx, /CLAUDE_PINNED_ROLES/);
});

test("a backend the server reports UNAVAILABLE is refused at COMPOSE TIME, showing the server's reason", () => {
  // Fail closed at compose time (never a silent fallback to claude), driven by
  // the SERVER's per-backend availability — `backendAvailability` built from
  // GET /api/config — not a client-side re-implementation of make_backend's
  // config check. That is why the local-without-base_url case (and every other
  // unrunnable backend) is refused here: the server marks it available:false
  // with a reason. Fail-closed only once the server has answered, never on an
  // absent/in-flight field (that would block on missing evidence).
  assert.match(composerJsx,
    /selectedBackendUnavailable\s*=\s*selectedBackendInfo\s*\n?\s*\?\s*!selectedBackendInfo\.available/);
  // It must block submission, not just warn.
  assert.match(composerJsx, /!selectedBackendUnavailable && !busy/);
  // The refusal surfaces the server's own reason (which names the missing key),
  // rather than the board re-deriving and hardcoding it.
  assert.match(composerJsx, /selectedBackendInfo\.reason/);
});

test("an untouched backend control behaves exactly as before the picker existed", () => {
  // "" (untouched) is the initial state and is forwarded as-is — the server
  // treats a falsy `backend` as "use worker.backend", so this never overrides
  // the config default unless the operator actually picked something.
  assert.match(composerJsx, /const \[backend, setBackend\] = useState\(initial\?\.backend \?\? ""\)/);
});

test("App.jsx forwards the composer's chosen backend into createTask, unrecomputed", () => {
  // Criterion 5, enforced by a CI-run gate (not the un-run Playwright suite):
  // the value that reaches CreateTaskRequest.backend must be the exact field
  // the composer produced, not something App.jsx derives on its own.
  assert.match(appJsx, /backend: fields\.backend,/);
});

test("TaskComposer's onStart payload includes the chosen backend", () => {
  assert.match(composerJsx, /onStart\(\{[\s\S]*?\bbackend,[\s\S]*?\}\)/);
});

// ── Backlog.jsx — the list ──────────────────────────────────────────────────

test("the composer's ticket affordance is now a pointer to the Backlog page, gated on jiraConfigured", () => {
  assert.match(composerJsx, /\{jiraConfigured && source !== "jira" && onOpenBacklog && \(/,
    "the pointer must be gated on a configured tracker");
  assert.match(composerJsx, /Open the Backlog/);
  // …and it must actually navigate, not just describe a page.
  assert.match(composerJsx, /onClick=\{onOpenBacklog\}/);
  assert.match(appJsx, /onOpenBacklog=\{\(\) => \{ setShowNewTask\(false\); setPage\("backlog"\); \}\}/);
});

test("which trackers are configured comes from fetchIntegrations, never a raw config poke", () => {
  assert.match(composerJsx, /fetchIntegrations\(\)/);
  assert.match(composerJsx, /\.find\(\(i\)\s*=>\s*i\.name\s*===\s*["']jira["']\)/);
  assert.match(composerJsx, /fetchIntegrations\(\)[\s\S]{0,500}\}, \[\]\);/);
  assert.match(composerJsx, /setJiraConfigured\(Boolean\(jira\?\.configured\)\)/);
  // The Backlog page reads the whole registry, not one named integration —
  // it lists every configured tracker.
  assert.match(backlogJsx, /fetchIntegrations\(\)/);
  assert.match(backlogJsx, /setTrackers\(configuredTrackers\(r\)\)/);
});

test("typing is debounced 300ms; the empty browse loads immediately", () => {
  assert.match(backlogJsx, /setTimeout\(\(\) => \{[\s\S]*?searchTrackerIssues\(t, q, JIRA_LIMIT\)/);
  // Typing waits 300ms; an empty query (browse-all on open) loads with no delay.
  assert.match(backlogJsx, /\}, q \? 300 : 0\);/);
});

test("a stale response is discarded via an ignore flag + cleanup timer (race guard)", () => {
  assert.match(backlogJsx, /let ignore = false;/);
  assert.match(backlogJsx, /return \(\) => \{ ignore = true; clearTimeout\(h\); \};/);
});

test("starting a ticket prefills the prompt via promptFromIssue and sets the hidden source marker", () => {
  const fn = appJsx.match(/const seedFrom = \(issue\) => \(\{[\s\S]*?\}\);/)?.[0];
  assert.ok(fn, "the backlog seed builder must be found");
  assert.match(fn, /prompt: promptFromIssue\(issue\)/);
  // The source is the ROW'S TRACKER, not a hardcoded "jira" — dedupe keys on
  // (source, external_id), so stamping a Linear ticket "jira" would let it
  // collide with a Jira ticket that happens to share its key.
  assert.match(fn, /source: tracker,/);
  assert.match(appJsx, /const tracker = head\.tracker === "linear" \? "linear" : "jira";/);
  assert.match(fn, /externalId: externalIdFromIssue\(issue\)/);
});

// ── SCRUM-9: the FULL issue is fetched before the composer is prefilled ────
// (the browse list's brief truncates description to 2000 chars — the created
// task must carry the whole spec, not that cut-off list-view text).

test("the backlog start path re-fetches the picked issue in full, from ITS tracker", () => {
  // The import list may carry more names; the pin is that the detail fetch is
  // imported from api.js, not the exact roster.
  assert.match(appJsx, /import \{[^}]*\bfetchTrackerIssue\b[^}]*\} from ["']\.\/api\.js["']/);
  const effect = appJsx.match(/if \(!backlogHeadKey\)[\s\S]*?\}, \[backlogHeadKey\]\);/)?.[0];
  assert.ok(effect, "the backlog seed effect must be found");
  assert.match(effect, /fetchTrackerIssue\(tracker, head\.key\)/);
  assert.match(effect, /\.then\(\(full\) => \{ if \(!ignore\) setBacklogSeed\(seedFrom\(full\)\); \}\)/);
  // A failed detail fetch falls back to the list brief rather than dead-ending.
  assert.match(effect, /\.catch\(\(\) => \{ if \(!ignore\) setBacklogSeed\(seedFrom\(head\)\); \}\)/);
  // …and the composer must not mount on a null seed (it would open empty and
  // the ticket's text could never be applied — `initial` is read once, at mount).
  assert.match(appJsx, /\{backlogHeadKey && backlogSeed && \(/);
});

test("api.js exposes a single-issue detail fetch for BOTH trackers", () => {
  assert.match(apiJs, /export async function fetchJiraIssue\(key\)/);
  assert.match(apiJs, /export async function fetchLinearIssue\(key\)/);
  assert.match(apiJs, /\/api\/integrations\/\$\{tracker\}\/issues\/\$\{encodeURIComponent\(key\)\}/);
});

test("source defaults to 'board' and survives a re-seeded composer (grill-fail echo)", () => {
  assert.match(composerJsx, /const source = initial\?\.source \?\? "board";/);
});

test("source rides along in the onStart payload alongside the other echoed fields", () => {
  assert.match(composerJsx, /prompt,\s*\n\s*prUrl,\s*\n\s*customRepo,\s*\n\s*source,\s*\n\s*externalId,\s*\n[\s\S]*?followsId: initial\?\.followsId \?\? null,\s*\n\s*\}\);/);
});

// SCRUM-3: "No matching tickets." now comes from jiraEmptyMessage (empty
// browse gets its own, honest copy) rather than being hardcoded inline in the
// component — assert the wiring here, and the literal copy in jiraImport.js.
test("empty, loading and error states use the design-direction copy/patterns", () => {
  assert.match(backlogJsx, /\{!refreshing && !allFailed && issues && issues\.length === 0 &&/);
  assert.match(backlogJsx, /jiraEmptyMessage\(shownQuery\)/);
  assert.match(jiraImportJs, /No matching tickets\./);
  assert.match(backlogJsx, /className="skeleton /, "must use the shared shimmer skeleton, not a spinner");
  assert.match(backlogJsx, /role="alert"/);
  assert.match(backlogJsx, /Try again/);
  assert.match(backlogJsx, /aria-live="polite"/);
});

// An upstream failure and an empty backlog are different facts, and a missing
// integration is a third. Reporting any of them as another is the exact class
// of lie this page must not tell.
test("a 503-unconfigured tracker gets its own state, with the configuration steps", () => {
  const branch = backlogJsx.match(/if \(trackers && trackers\.length === 0\) \{[\s\S]*?\n  \}/)?.[0];
  assert.ok(branch, "the not-configured branch must exist");
  assert.match(branch, /noTrackerMessage\(\)/);
  assert.match(branch, /Settings ▸ Integrations/, "it must say WHERE to configure it");
  // BOTH credentials are named — the page offers both trackers now, so telling
  // the operator about only one of them is a half-truth.
  assert.match(branch, /JIRA_API_TOKEN/);
  assert.match(branch, /LINEAR_API_KEY/);
  assert.doesNotMatch(branch, /No open tickets/, "an unconfigured tracker is not an empty backlog");
});

// P1-5: a dead server is not an unconfigured integration. fetchIntegrations
// used to fold every non-ok response into `{integrations: []}`, so a 500
// rendered "Jira is not configured" — the exact inverse of the rule this page
// states — and sent the operator to fix a token that was never the problem.
test("an unreachable server gets its OWN state, never the not-configured one", () => {
  // fetchIntegrations' own contract is exercised for real in api.test.mjs
  // (stubbed fetch, thrown value read) — a regex here would pass against the
  // old swallowing code as long as the word `throw` appeared in the function.
  const branch = backlogJsx.match(/if \(registryError\) \{[\s\S]*?\n  \}/)?.[0];
  assert.ok(branch, "the unreachable-server branch must exist");
  assert.match(branch, /Couldn&apos;t reach the no_human server/);
  assert.match(branch, /\{registryError\}/, "the reason must be shown verbatim");
  assert.doesNotMatch(branch, /not configured/,
    "an unreachable server must never be reported as a missing integration");
  assert.match(branch, /Try again/);
});

test("a 502 upstream failure says the tracker could not be READ, never 'no tickets'", () => {
  const alert = backlogJsx.match(/role="alert"[\s\S]*?<\/div>/)?.[0];
  assert.ok(alert, "the error alert must exist");
  // Named per tracker: with two lists merged, "couldn't reach Jira" said over a
  // page full of Linear tickets would be unreadable.
  assert.match(alert, /Couldn&apos;t reach \{e\.label\}/);
  assert.match(alert, /\{e\.message\}/, "the server's own detail must be shown verbatim");
  assert.match(alert, /not empty/, "it must deny the empty-backlog reading explicitly");
});

// P1-4. The page used to explain Linear's absence with "the Linear side has no
// issue listing yet". That was false: LinearAdapter.search() has been a working
// paginating GraphQL listing all along and only the HTTP route was missing. A
// UI explanation that states a fact about the code is a claim, and this one was
// wrong — so the fix is the route, and the rule is that the line is DERIVED.
test("Linear tickets are listed, and no hand-written claim about Linear survives", () => {
  assert.match(backlogJsx, /searchTrackerIssues/,
    "the page must fetch every configured tracker, not Jira alone");
  assert.match(backlogJsx, /sourcesLine\(trackers\)/,
    "the which-trackers line must be derived from what the server reported");
  for (const lie of [
    /the Linear side has no issue listing/,
    /Linear is not connected/,
    /Jira only for now/,
    /no Linear issue-listing endpoint/,
  ]) {
    assert.doesNotMatch(backlogJsx, lie,
      `a hand-written claim about Linear must not survive: ${lie}`);
  }
});

test("api.js and the server agree that the Linear listing exists", () => {
  assert.match(apiJs, /export async function searchLinearIssues/);
  assert.match(apiJs, /export async function fetchLinearIssue/);
});

// ── Grill flow stays untouched ───────────────────────────────────────────

test("TaskComposer never defines or calls grill machinery directly — it only hands off via onStart", () => {
  for (const forbidden of [/function startGrill/, /_grillParams/, /_startGrillSSE/, /grillStepSSE/, /grillStep\(/]) {
    assert.doesNotMatch(composerJsx, forbidden, `grill logic must stay in App.jsx, not TaskComposer.jsx: ${forbidden}`);
  }
});

test("App.jsx's grill request body is untouched — source never leaks into /api/grill", () => {
  assert.match(appJsx, /function _grillParams\(qaOverride, spec = fields\)\s*\{/);
  assert.match(
    appJsx,
    /return \{\s*\n\s*title: spec\.title, description: spec\.description,[\s\S]*?qa_history: qaOverride \?\? \[\],\s*\n\s*\};/,
    "the grill request shape must stay exactly title/description/repo_path/project_id/qa_history",
  );
});

test("App.jsx's createTask call carries fields.source through, on top of the untouched fields", () => {
  assert.match(appJsx, /source: fields\.source,/);
});

// ── SCRUM-33: external_id dedup — picker sends the Jira key; typed tasks don't ──

test("externalIdFromIssue returns the issue key (the dedup id sent to the backend)", () => {
  assert.equal(externalIdFromIssue({ key: "PROJ-42", summary: "Fix the retry loop" }), "PROJ-42");
});

test("externalIdFromIssue returns null with no key (mutation-proof, not a tautology)", () => {
  assert.equal(externalIdFromIssue({}), null);
  assert.equal(externalIdFromIssue(undefined), null);
});

test("the backlog seed sets externalId from the picked issue via externalIdFromIssue", () => {
  const fn = appJsx.match(/const seedFrom = \(issue\) => \(\{[\s\S]*?\}\);/)?.[0];
  assert.ok(fn, "the backlog seed builder must be found");
  assert.match(fn, /externalId: externalIdFromIssue\(issue\),/);
});

test("App.jsx's createTask call and api.js's wire body both carry external_id", () => {
  assert.match(appJsx, /external_id: fields\.externalId,/);
  assert.match(apiJs, /export async function createTask\(\{[^}]*\bexternal_id\b[^}]*\}\)/s);
  assert.match(apiJs, /JSON\.stringify\(\{[^}]*\bexternal_id\b[^}]*\}\)/s);
});

test("externalId defaults to null and NOTHING in the composer ever mutates it", () => {
  assert.match(composerJsx, /const externalId = initial\?\.externalId \?\? null;/);
  // It is now pure pass-through: the only writer is the Backlog seed in
  // App.jsx. A typed/board task therefore cannot acquire a dedup key by any
  // path, and a re-seed cannot lose one.
  assert.equal((composerJsx.match(/setExternalId\(/g) || []).length, 0,
    "no setter may exist — the value comes from `initial` and leaves via onStart");
  assert.equal((composerJsx.match(/setSource\(/g) || []).length, 0);
});

// ── SCRUM-18: accidental re-import trap — the "imported" chip ──────────────

test("importedStatusCategory maps 'done' to 'done'", () => {
  assert.equal(importedStatusCategory("done"), "done");
});

test("importedStatusCategory maps in-flight board statuses to 'active'", () => {
  assert.equal(importedStatusCategory("implementing"), "active");
  assert.equal(importedStatusCategory("pending"), "active");
  assert.equal(importedStatusCategory("awaiting_approval"), "active");
});

test("importedStatusCategory maps failed/blocked/escalated to 'warn'", () => {
  assert.equal(importedStatusCategory("failed"), "warn");
  assert.equal(importedStatusCategory("blocked"), "warn");
  assert.equal(importedStatusCategory("escalated"), "warn");
});

test("importedStatusCategory falls back to 'unknown' for an unrecognised value", () => {
  assert.equal(importedStatusCategory("something-new"), "unknown");
  assert.equal(importedStatusCategory(""), "unknown");
});

test("importedChip returns null when the ticket has no board task yet", () => {
  assert.equal(importedChip(null), null);
  assert.equal(importedChip(undefined), null);
});

test("importedChip labels a matched ticket with its board status and a non-null style", () => {
  const chip = importedChip({ task_id: "abc123", status: "done", count: 1 });
  assert.ok(chip);
  assert.match(chip.label, /imported/);
  assert.match(chip.label, /done/);
  assert.notEqual(chip.style, null);
});

test("importedChip still returns a chip for an in-progress board task (import stays possible, state visible)", () => {
  const chip = importedChip({ task_id: "abc123", status: "implementing", count: 1 });
  assert.ok(chip);
  assert.match(chip.label, /implementing/);
});

test("importedChip escalates to a review warning when count > 1 (duplicate external_ids)", () => {
  const chip = importedChip({ task_id: "abc123", status: "done", count: 2 });
  assert.ok(chip);
  assert.match(chip.label, /review|warn/i);
  assert.match(chip.label, /2/);
});

test("importedChip style references only bridged CSS vars, never a raw hex", () => {
  for (const imported of [
    { task_id: "a", status: "done", count: 1 },
    { task_id: "b", status: "failed", count: 1 },
    { task_id: "c", status: "done", count: 3 },
  ]) {
    const style = importedChip(imported).style;
    if (style) {
      for (const value of Object.values(style)) {
        assert.match(value, /^var\(--[a-z-]+\)$/, `style must reference a CSS var, got ${value}`);
      }
    }
  }
});

test("Backlog.jsx renders the imported chip from issue.imported next to the Jira status chip", () => {
  assert.match(backlogJsx, /importedChip[\s\S]{0,200}from ["']\.\/jiraImport\.js["']/);
  assert.match(backlogJsx, /const imp = importedChip\(issue\.imported\);/);
  assert.match(backlogJsx, /\{imp && \(/);
});

test("an imported ticket's checkbox is DISABLED — it can never join a bulk start", () => {
  assert.match(backlogJsx, /const disabled = Boolean\(issue\.imported\);/);
  const box = backlogJsx.match(/<input\s*\n\s*type="checkbox"[\s\S]*?\/>/)?.[0];
  assert.ok(box, "the row checkbox must be found");
  assert.match(box, /disabled=\{disabled\}/);
  // Re-running it is still possible — but only through a separate, labelled
  // action that names the consequence, never by a stray click or Select all.
  assert.match(backlogJsx, /Start again/);
  assert.match(backlogJsx, /creates a SECOND task for the same ticket/);
});

test("external_id_survives_grill_reseed_roundtrip: initial.externalId seeds state and onStart echoes the identical camelCase token", () => {
  // Seed side: a re-mount from `initial` (the grill-fail re-seed path) reads externalId.
  assert.match(composerJsx, /const externalId = initial\?\.externalId \?\? null;/);
  // Echo side: onStart emits the SAME camelCase token, so the next re-seed's `initial`
  // (== a prior onStart payload) round-trips it. A name mismatch on either end is
  // exactly the bug the parked attempt's reviewer flagged.
  assert.match(composerJsx, /prompt,\s*\n\s*prUrl,\s*\n\s*customRepo,\s*\n\s*source,\s*\n\s*externalId,\s*\n[\s\S]*?followsId: initial\?\.followsId \?\? null,\s*\n\s*\}\);/);
});

// ── SCRUM-3: result count header + browsing-all header ──────────────────────

test("jiraResultHeader: empty query reads as a browse-all count with a filter hint", () => {
  assert.match(jiraResultHeader("", 5, 50), /Showing 5 open tickets — type to filter/);
});

test("jiraResultHeader: truncation is honest when count hits the request limit", () => {
  assert.match(jiraResultHeader("", 50, 50), /Showing first 50 open tickets — type to narrow/);
});

test("jiraResultHeader: non-empty query reads as a match count, pluralised (mutation-proof)", () => {
  assert.equal(jiraResultHeader("bug", 3, 50), "3 matching tickets");
  assert.equal(jiraResultHeader("bug", 1, 50), "1 matching ticket");
  // Review finding: at the limit the MATCH set is truncated too — the search
  // path must say so, not report a confident total.
  assert.equal(jiraResultHeader("bug", 50, 50), "First 50 matching tickets — type to narrow");
  assert.equal(jiraResultHeader("bug", 49, 50), "49 matching tickets");
});

test("jiraResultHeader: empty-query singular reads '1 open ticket', not 'tickets'", () => {
  assert.equal(jiraResultHeader("", 1, 50), "Showing 1 open ticket — type to filter");
});

test("jiraResultHeader returns null for zero results (empty-state message speaks instead)", () => {
  assert.equal(jiraResultHeader("", 0, 50), null);
  assert.equal(jiraResultHeader("bug", 0, 50), null);
});

test("Backlog.jsx imports and calls jiraResultHeader with the JIRA_LIMIT display constant", () => {
  assert.match(backlogJsx, /jiraResultHeader[\s\S]{0,200}from ["']\.\/jiraImport\.js["']/);
  assert.match(backlogJsx, /const JIRA_LIMIT = 50;/);
  assert.match(backlogJsx, /jiraResultHeader\(shownQuery, issues\.length, JIRA_LIMIT\)/);
  // The shown query is stamped only when a response lands — never mid-flight.
  assert.match(backlogJsx, /setShownQuery\(q\);/);
});

// ── SCRUM-3: results stay visible during a refresh; skeletons only when empty ─

test("a `refreshing` flag exists and does NOT drive the list's visibility", () => {
  assert.match(backlogJsx, /const \[refreshing, setRefreshing\] = useState\(false\);/);
  assert.doesNotMatch(backlogJsx, /jiraLoading/);
});

test("the search effect never clears the rows on the loading path — only the error path may", () => {
  const effect = backlogJsx.match(
    /useEffect\(\(\) => \{\n\s*if \(!trackers \|\| !trackers\.length\) return undefined;[\s\S]*?\}, \[query, trackers, nonce, refreshNonce\]\);/,
  )?.[0];
  assert.ok(effect, "the ticket search effect must be found");
  assert.match(effect, /setRefreshing\(true\);/);
  // Rows are cleared ONLY when every tracker failed — there is nothing
  // truthful left to show. One of two failing still lists the other's tickets
  // beside a banner naming the one that did not answer.
  assert.match(effect, /setIssues\(errors\.length === results\.length \? undefined : merged\)/);
});

test("skeletons render only when there is nothing to show yet, not on every refresh", () => {
  assert.match(backlogJsx, /\(!issues \|\| issues\.length === 0\) && refreshing[\s\S]*?className="skeleton /);
});

test("the row list is not gated on the refreshing flag — it stays mounted in flight", () => {
  assert.match(backlogJsx, /\{issues && issues\.map/);
});

test("a subtle in-flight hint ('Updating…') is shown near the header while a refresh is in flight", () => {
  assert.match(backlogJsx, /Updating…/);
  assert.match(backlogJsx, /\{header && refreshing \? " · Updating…" : ""\}/);
});

// ── SCRUM-3: empty browse vs empty search get different copy ────────────────

test("jiraEmptyMessage: an empty (browse-all) query with zero results names the project, not 'matching'", () => {
  assert.equal(jiraEmptyMessage(""), "No open tickets in this project.");
  assert.equal(jiraEmptyMessage("   "), "No open tickets in this project.");
});

test("jiraEmptyMessage: a non-empty query with zero results keeps the existing matched-empty copy", () => {
  assert.equal(jiraEmptyMessage("foo"), "No matching tickets.");
});

test("Backlog.jsx wires the empty state through jiraEmptyMessage, not a hardcoded string", () => {
  assert.match(backlogJsx, /\{!refreshing && !allFailed && issues && issues\.length === 0 &&/);
  assert.match(backlogJsx, /jiraEmptyMessage\(shownQuery\)/);
});

// ── SCRUM-3: a malformed issue.updated must never render "Invalid Date" ─────

test("formatIssueUpdated: a valid ISO timestamp formats as 'Updated <date>'", () => {
  assert.match(formatIssueUpdated("2024-01-15T10:00:00Z"), /^Updated /);
});

test("formatIssueUpdated: a malformed string returns null (never 'Invalid Date')", () => {
  assert.equal(formatIssueUpdated("not-a-date"), null);
});

test("formatIssueUpdated: missing/empty input returns null", () => {
  assert.equal(formatIssueUpdated(""), null);
  assert.equal(formatIssueUpdated(undefined), null);
});

test("Backlog.jsx never inlines new Date(issue.updated).toLocaleDateString() and never renders 'Invalid Date'", () => {
  assert.doesNotMatch(backlogJsx, /Invalid Date/);
  assert.doesNotMatch(backlogJsx, /new Date\(issue\.updated\)\.toLocaleDateString\(\)/);
  assert.match(backlogJsx, /const updatedText = formatIssueUpdated\(issue\.updated\);/);
});

test("importedChip: missing status never renders the literal 'undefined'", () => {
  // Review 2026-07-25 residue: a payload with task_id+count but no status
  // (older server, partial row) must degrade to a plain "imported" chip.
  const chip = importedChip({ task_id: "t1", count: 1 });
  assert.ok(chip, "chip must still render");
  assert.ok(!chip.label.includes("undefined"), `label leaked: ${chip.label}`);
  assert.equal(chip.label, "imported");
});
