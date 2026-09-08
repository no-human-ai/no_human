// D3.2: one Second-brain surface (UI). Like learningCard.test.mjs and
// sidebarNav.test.mjs, this is static source analysis — no jsdom/React
// renderer is wired into this project's `node --test` harness, so these
// assertions read the .jsx/.css source rather than mounting components.
//
// Scope: the NEW auto-managed rendering (SecondBrainPanel/SecondBrainRow) and
// the branch that picks it. The pre-existing confirm-queue behaviour
// (Confirm/Reject, bulk confirm, pending/active toggle) is pinned by
// learningCard.test.mjs already and is UNCHANGED here — it moved into
// LegacyLearningQueuePanel, restored only when `learning.auto_manage: false`
// (the D3 kill switch), and a handful of assertions below confirm it is
// still reachable, not merely renamed away.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const settingsJsx = readFileSync(here + "Settings.jsx", "utf8");
const appJsx = readFileSync(here + "App.jsx", "utf8");
const apiJs = readFileSync(here + "api.js", "utf8");
const stylesCss = readFileSync(here + "styles.css", "utf8");

function fnBody(src, name) {
  const start = src.indexOf(name);
  assert.ok(start > -1, `${name} must be defined`);
  // Slice from the declaration to the next top-level function/export at
  // column 0 — good enough for these single-purpose components, the same
  // heuristic learningCard.test.mjs already uses for confirmSelected/
  // MemoryCard.
  const rest = src.slice(start + name.length);
  const end = rest.search(/\n(export )?function [A-Za-z]/);
  return end === -1 ? rest : rest.slice(0, end);
}

// ── the branch ───────────────────────────────────────────────────────────── //

test("LearningsPanel reads config.learning.auto_manage and renders one of two panels", () => {
  const body = fnBody(settingsJsx, "export function LearningsPanel(");
  assert.match(body, /fetchConfig\(\)/, "the auto_manage/cap read must come from GET /api/config");
  assert.match(body, /auto_manage\s*!==\s*false/,
    "only an EXPLICIT false may restore the legacy queue — a missing/undefined key must stay auto-managed");
  assert.match(body, /<SecondBrainPanel\b/);
  assert.match(body, /<LegacyLearningQueuePanel\s*\/>/);
});

test("the legacy confirm queue is still reachable, not merely deleted and renamed away", () => {
  const legacy = fnBody(settingsJsx, "function LegacyLearningQueuePanel(");
  assert.match(legacy, /Confirm selected/, "the kill switch must actually restore the bulk-confirm bar");
  assert.match(legacy, /onAction\(item\.id, "confirm"\)|"confirm"\)/);
  assert.match(legacy, /useState\(\(\)\s*=>\s*new Set\(\)\)/, "selection state must still exist in the legacy panel");
});

// ── SecondBrainPanel: no pending queue, no Confirm, no bulk bar ─────────────//

test("SecondBrainPanel never renders a Confirm button, a bulk bar, or a pending/active toggle", () => {
  const body = fnBody(settingsJsx, "function SecondBrainPanel(");
  assert.doesNotMatch(body, />Confirm</, "no Confirm button when auto_manage is on");
  assert.doesNotMatch(body, /Confirm selected/, "no bulk-confirm bar when auto_manage is on");
  assert.doesNotMatch(body, /bulkConfirmIds/, "no bulk-confirm logic when auto_manage is on");
  assert.doesNotMatch(body, /learnings-toggle|Pending<\/button>/, "no pending/active view toggle");
  assert.doesNotMatch(body, /confirmLearning/, "the confirm write path has no button to drive it here");
});

test("review-round fix: the busy guard is PER-ROW, not one shared id that silently ignores other rows' clicks", () => {
  const body = fnBody(settingsJsx, "function SecondBrainPanel(");
  // A single `busyId` string meant clicking Pause on row B while row A's
  // own POST was in flight returned early with no visible effect on B —
  // its button never went disabled, so the click looked dropped, not
  // "busy". A Set keyed by id lets every row guard only itself.
  assert.match(body, /const \[busyIds, setBusyIds\] = useState\(\(\)\s*=>\s*new Set\(\)\)/,
    "busy tracking must be a Set, keyed per id — not one shared nullable id");
  assert.match(body, /busyIds\.has\(id\)/, "runAction must check only THIS id, never a single shared lock");
  assert.match(body, /busy=\{busyIds\.has\(item\.id\)\}/, "each row's own busy prop must come from the per-id check");
});

test("SecondBrainPanel fetches the active list WITH paused AND archived rows included", () => {
  const body = fnBody(settingsJsx, "function SecondBrainPanel(");
  assert.match(body, /fetchLearnings\(\{\s*active:\s*true,\s*includePaused:\s*true,\s*includeArchived:\s*true\s*\}\)/,
    "review-round fix #1: archived rows must be fetched too, or the archived-count footer has nothing to reveal");
});

test("SecondBrainPanel reuses the Rules/Skills archive helpers rather than re-deriving the same split", () => {
  const body = fnBody(settingsJsx, "function SecondBrainPanel(");
  assert.match(body, /visibleMemories\(items,\s*\{\s*showArchived:\s*false\s*\}\)/,
    "the live (non-archived) split must reuse visibleMemories, not a hand-rolled !it.archived filter");
  assert.match(body, /archivedCount\(items\)/,
    "the footer's count must reuse archivedCount, not items.filter(...).length inlined again");
});

test("the D2 explainer renders verbatim inside SecondBrainPanel", () => {
  const body = fnBody(settingsJsx, "function SecondBrainPanel(");
  // Split across JSX text nodes/whitespace in source, so match the load-
  // bearing clauses rather than one giant literal string.
  assert.match(body, /Your memories\. no_human learns from every task/);
  assert.match(body, /what worked, what[\s\S]{0,40}broke, your repo's rules/);
  assert.match(body, /applies it automatically to the next[\s\S]{0,20}task\./);
  assert.match(body, /Nothing to approve\./);
  assert.match(body, /Review or pause anything here\./);
});

test("the panel title and empty state read 'Memories', not 'Second brain'", () => {
  const body = fnBody(settingsJsx, "function SecondBrainPanel(");
  assert.match(body, /<span className="panel-title-text">Memories<\/span>/);
  assert.match(body, /Nothing learned yet\. Your memories fill in as tasks run/);
});

test("an 'Auto-managed' line states the daily cap and the 90-day retirement rule", () => {
  const body = fnBody(settingsJsx, "function SecondBrainPanel(");
  assert.match(body, /Auto-managed/);
  assert.match(body, /\{dailyCap\}/, "the cap must be the live config value, not a hardcoded number");
  assert.match(body, /90 days/);
  assert.match(body, /never auto-retired/, "operator-pinned/manually-added rules must be named as the exception");
});

test("the manual 'Add rule' box is kept, unconditionally confirmed on arrival", () => {
  const body = fnBody(settingsJsx, "function SecondBrainPanel(");
  assert.match(body, /addBrainRule/);
  assert.match(body, /Add rule/);
  assert.match(body, /await addRule\(\{ title, content \}\)/);
});

test("search still filters the LIVE list, reusing filterLearnings", () => {
  const body = fnBody(settingsJsx, "function SecondBrainPanel(");
  // Filters `liveItems` (post-archive-split), never the raw fetch — an
  // archived row must not be searchable back into the main list, only
  // reachable through the archived-count footer below.
  assert.match(body, /filterLearnings\(liveItems, query\)/);
  assert.match(body, /type="search"/);
});

test("review-round fix #1: an archived-count footer expands to archived rows with Restore", () => {
  const body = fnBody(settingsJsx, "function SecondBrainPanel(");
  // The count and toggle are the Rules/Skills panel's own idiom
  // (`.memory-archive-toggle`, `archivedCount`), not a new one invented here.
  assert.match(body, /archivedTotal > 0/, "the footer must be gated on there being anything archived to show");
  assert.match(body, /memory-archive-toggle/, "must reuse the existing Rules/Skills 'Show archived' toggle style, not invent a new one");
  assert.match(body, /showArchived/, "the toggle must actually gate revealing the archived rows");
  assert.match(body, /archivedItems\.map\(\(item\) =>/, "the revealed rows must be the ARCHIVED subset, not the whole fetch replayed");
  // Scope precisely to the archived-rows render block (not the main list's,
  // which also passes onRestore={handleRestore} — that alone would pass
  // even if the archived block never rendered a working Restore).
  const archivedBlockStart = body.indexOf("archivedItems.map((item) =>");
  assert.ok(archivedBlockStart > -1);
  const archivedBlock = body.slice(archivedBlockStart, archivedBlockStart + 400);
  assert.match(archivedBlock, /<SecondBrainRow/, "the archived subset must render through SecondBrainRow, not a bespoke markup");
  assert.match(archivedBlock, /onRestore=\{handleRestore\}/, "an archived row must have Restore wired — the whole point of the footer");
});

// ── SecondBrainRow: plain text · origin task link · used N× · Pause/Delete ─ //

test("each row renders plain text, an origin task link, a used-N× count, and Pause/Delete", () => {
  const body = fnBody(settingsJsx, "function SecondBrainRow(");
  assert.match(body, /learningOriginTaskId\(item\)/);
  assert.match(body, /second-brain-task-link/);
  assert.match(body, /used \{useCount\}×/);
  assert.match(body, /"Pause"/);
  assert.match(body, /"Delete"/);
  assert.match(body, /onClick=\{\(\) => onPause\(item\.id\)\}/);
  assert.match(body, /onClick=\{\(\) => onDelete\(item\.id\)\}/);
});

test("a row with no recorded origin task renders honestly, not a broken/guessed link", () => {
  const body = fnBody(settingsJsx, "function SecondBrainRow(");
  assert.match(body, /taskId && onOpenTask \? \(/);
  assert.match(body, /second-brain-origin-unknown/);
});

test("review-round fix (minor): a task id with no onOpenTask handler renders as plain text, never a dead control", () => {
  const body = fnBody(settingsJsx, "function SecondBrainRow(");
  // The three-way branch: linked (handler present), plain text (id known,
  // no handler), or "not recorded" (no id at all) — never a button with
  // nothing to do when clicked.
  const branch = body.slice(body.indexOf("taskId && onOpenTask ? ("), body.indexOf("second-brain-origin-unknown"));
  assert.match(branch, /: taskId \? \(\s*<span>/,
    "a known task id without a handler must fall through to a plain <span>, not a <button> styled as a link");
  assert.doesNotMatch(branch.slice(branch.indexOf(": taskId ? (")), /second-brain-task-link/,
    "the plain-text fallback must not carry the link's underline/hover styling — nothing here is clickable");
});

test("a PAUSED or ARCHIVED row is inert: stays visible with a chip and swaps Pause for Restore", () => {
  const body = fnBody(settingsJsx, "function SecondBrainRow(");
  assert.match(body, /const paused = Boolean\(item\.paused\)/);
  assert.match(body, /const archived = Boolean\(item\.archived\)/,
    "review-round fix #1: an archived row needs the same inert treatment a paused row already gets");
  assert.match(body, /const inert = paused \|\| archived/);
  assert.match(body, /second-brain-paused-chip/);
  assert.match(body, />Paused</);
  assert.match(body, /inert \? \(/, "Restore must cover BOTH paused and archived, not just paused");
  assert.match(body, /"Restore"/);
  assert.match(body, /onClick=\{\(\) => onRestore\(item\.id\)\}/);
  // Delete must disappear once a row is already archived — there is
  // nothing further to delete.
  assert.match(body, /\{!archived && \(/);
  assert.match(body, /archiveBadge\(item\)/, "reuse the Rules/Skills archive badge, not a second-brain-only label");
});

test("review-round fix: the used-N× counter is announced via visually-hidden text, not aria-label on a bare span", () => {
  const body = fnBody(settingsJsx, "function SecondBrainRow(");
  // A bare <span>'s implicit ARIA role is `generic`, and the spec PROHIBITS
  // naming a generic role — aria-label on one is silently ignored by every
  // screen reader. The correct pattern splits the visible glyph (hidden
  // from assistive tech) from a visually-hidden sibling carrying the real
  // string.
  assert.doesNotMatch(body, /<span className="second-brain-used" aria-label=/,
    "a bare <span> with aria-label is the WRONG pattern (role=generic cannot be named) — must not regress to this");
  assert.match(body, /<span className="second-brain-used" aria-hidden="true">/,
    "the visible glyph must be aria-hidden so it isn't announced verbatim alongside the sr-only text");
  assert.match(body, /<span className="sr-only">\{`used \$\{useCount\} time/,
    "the real accessible string must live in a visually-hidden sibling span");
});

test("every button in a Second-brain row carries an aria-label; the origin-task plain-text fallback (a span) does not need one", () => {
  const body = fnBody(settingsJsx, "function SecondBrainRow(");
  const buttons = [...body.matchAll(/<button[\s\S]*?\/>|<button[\s\S]*?<\/button>/g)];
  assert.ok(buttons.length >= 3, "expected the task-link, Pause/Restore and Delete buttons");
  for (const [btn] of buttons) {
    assert.match(btn, /aria-label=/, `button missing aria-label: ${btn.slice(0, 60)}…`);
  }
});

// ── plumbing: onOpenTask threaded from App through Settings to the row ─────//

test("SettingsOverlay accepts and forwards onOpenTask to LearningsPanel", () => {
  assert.match(settingsJsx, /export default function SettingsOverlay\(\{[^}]*onOpenTask[^}]*\}\)/);
  // D2.1 added onFirstOpen/onNavigateSection to this same element (see
  // secondBrainBadge.test.mjs), so this no longer requires onOpenTask to sit
  // immediately before the closing "/>" — only that it is actually forwarded.
  const idx = settingsJsx.indexOf("<LearningsPanel");
  assert.ok(idx > -1, "<LearningsPanel> element must be found");
  const el = settingsJsx.slice(idx, settingsJsx.indexOf("/>", idx) + 2);
  assert.match(el, /onOpenTask=\{onOpenTask\}/);
});

test("App.jsx wires onOpenTask to close Settings and open the task on the board", () => {
  const idx = appJsx.indexOf("<SettingsOverlay");
  assert.ok(idx > -1);
  const block = appJsx.slice(idx, appJsx.indexOf(")}", idx) + 2);
  assert.match(block, /onOpenTask=\{/);
  assert.match(block, /setPage\("board"\)/);
  assert.match(block, /setPendingOpenId\(id\)/);
});

// ── api.js: the new endpoints and the includePaused param ──────────────────//

test("api.js exports pauseLearning and deleteLearning hitting the D3 endpoints", () => {
  assert.match(apiJs, /export async function pauseLearning\(id\)/);
  assert.match(apiJs, /\/api\/learnings\/\$\{id\}\/pause/);
  assert.match(apiJs, /export async function deleteLearning\(id\)/);
  assert.match(apiJs, /\/api\/learnings\/\$\{id\}\/delete/);
});

test("fetchLearnings accepts includePaused AND includeArchived and puts both on the query string", () => {
  assert.match(apiJs, /export async function fetchLearnings\(\{\s*active = false,\s*includePaused = false,\s*includeArchived = false\s*\}/);
  assert.match(apiJs, /include_paused=\$\{includePaused\}/);
  assert.match(apiJs, /include_archived=\$\{includeArchived\}/,
    "review-round fix #1: the archived-count footer needs the archived rows back on the wire");
});

// ── CSS: every new class actually has a rule ────────────────────────────── //

test("every class the Second-brain rendering introduces has a CSS rule", () => {
  for (const cls of ["second-brain-panel", "learning-auto-line", "second-brain-cap",
                     "second-brain-list", "second-brain-row", "second-brain-row-text",
                     "second-brain-row-meta", "second-brain-task-link",
                     "second-brain-origin-unknown", "second-brain-used",
                     "second-brain-paused-chip", "second-brain-row-actions",
                     "second-brain-archived-toggle"]) {
    assert.ok(settingsJsx.includes(cls), `${cls} is styled but never rendered`);
    assert.match(stylesCss, new RegExp(`\\.${cls}[\\s,{:.]`), `.${cls} has no CSS rule`);
  }
});

test("the paused state is a tint on the row, not a coloured border stripe", () => {
  const rule = stylesCss.match(/\.second-brain-row\.paused\s*\{([^}]*)\}/);
  assert.ok(rule, ".second-brain-row.paused rule must exist");
  assert.doesNotMatch(rule[1], /border-left|border-right/,
    "a coloured border-left/right stripe is the pattern this directive explicitly avoids");
  assert.match(rule[1], /background:/, "the paused state must read from a background tint");
});

test("the used-N× count and the daily cap use tabular numerals", () => {
  assert.match(stylesCss, /\.second-brain-used\s*\{[^}]*font-variant-numeric:\s*tabular-nums/s);
  assert.match(stylesCss, /\.second-brain-cap\s*\{[^}]*font-variant-numeric:\s*tabular-nums/s);
});

test("no functional text in the Second-brain rendering is below the 12px type floor", () => {
  for (const cls of ["second-brain-row-text", "second-brain-row-meta", "second-brain-used",
                     "second-brain-paused-chip", "second-brain-task-link", "learning-auto-line"]) {
    const m = stylesCss.match(new RegExp(`\\.${cls}\\s*\\{([^}]*)\\}`));
    if (!m) continue; // some rules inherit font-size from a shared ancestor; skip rather than false-fail
    const sizeMatch = m[1].match(/font-size:\s*(\d+(?:\.\d+)?)px/);
    if (sizeMatch) assert.ok(Number(sizeMatch[1]) >= 12, `${cls} is below the 12px floor: ${sizeMatch[1]}px`);
  }
});
