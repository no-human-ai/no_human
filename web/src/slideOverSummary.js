// Task-detail drawer, progressive disclosure (task 1.4).
//
// Pure derivations for the summary-first drawer: a plain-language narrative,
// chips, a milestone timeline, and per-section micro-summaries — all computed
// from data the drawer already fetches (`task`, `diff`), never from a raw
// backend enum. Kept out of SlideOver.jsx so the "what does the operator read
// at a glance" logic is node-testable without a DOM.

import { approvedAtOf, supersededAtOf } from "./approvalState.js";
import { routeTask } from "./boardLanes.js";
import { fmtCost, fmtTokens, taskBurn, taskCost, pricingIsReal } from "./cost.js";
import { formatDuration } from "./formatDuration.js";
import { httpPrUrl } from "./prUrl.js";
import { pluralize } from "./pluralize.js";
import { timestampMs } from "./parseTimestamp.js";

// The statuses whose gate the operator clears IN the drawer (Reply / Resume / the
// blocker's options). Single definition — SlideOver's `isParked` and the
// gate-aware default-open-section both read it. Deliberately NOT paused_quota:
// #205 gave it a blocker record and a Resume affordance (rendered via
// decisionFor / the DecisionPanel), but it is a SELF-RESOLVING park — it
// auto-resumes once its subscription quota refreshes — so it is not a
// "waiting on you" gate here.
export const PARKED_STATUSES = new Set(["awaiting_input", "blocked", "escalated"]);

// A parked task whose human explicitly stopped it (blocker.human_stopped,
// flattened on the board payload as blocker_human_stopped). SINGLE definition
// so the narrative header and the System pipeline chip read the SAME signal
// and can never contradict each other. Only meaningful for PARKED_STATUSES —
// human_stopped is stamped on awaiting_input/blocked/escalated (see
// _PARKED_STATUSES in app.py); SCRUM-98 settled the narrative, this brings
// the System chip in line.
export function isHumanStopped(task) {
  return !!(task && PARKED_STATUSES.has(task.status)
    && (task.blocker?.human_stopped || task.blocker_human_stopped));
}

// Does this task's open blocker (if any) still need a human answer? `null`
// when there's no question at all. reply_task (api/app.py) never clears
// `task.blocker` on reply — it only appends to `context.human_replies` and
// flips status to implementing — so a naive `task.blocker?.question` check
// keeps reading "Has a question for you" long after the human answered it.
// Order matters: terminal first (its own "asked before it ended" wording,
// unaffected by replies), then a reply that's actually newer than the
// question, then "still parked" reads as open, and everything else (a
// running task carrying a stale blocker — a machine wake resume, or a reply
// whose timestamp didn't parse) is treated as answered: a task the pipeline
// is actively running is by construction not waiting on the human, so "open"
// is the one verdict that cannot be true there.
export function questionState(task) {
  if (!task?.blocker?.question) return null;
  if (isTerminalStatus(task.status)) return "terminal";
  const raisedAt = timestampMs(task.blocker.raised_at);
  const replies = task.context?.human_replies || [];
  const answered = replies.some((r) => {
    if (!String(r?.answer || "").trim()) return false;
    const at = timestampMs(r?.at);
    return !Number.isNaN(at) && !Number.isNaN(raisedAt) && at > raisedAt;
  });
  if (answered) return "answered";
  if (PARKED_STATUSES.has(task.status) || task.status === "paused_quota") return "open";
  return "answered";
}

// The most recent PRIOR attempt (never the current/last one) that actually
// carries a review, or null. `orchestrator._persist_plan` overwrites
// `context.spec` on every attempt (no per-attempt spec is kept), but reviews
// live per-attempt on `attempts[].review_checklist` — so only the review side
// has a real prior artifact to point the drawer at.
export function lastReviewedAttempt(task) {
  const attempts = task?.attempts || [];
  for (let i = attempts.length - 2; i >= 0; i--) {
    const checklist = attempts[i]?.review_checklist;
    if (checklist?.items?.length) {
      return {
        attempt_number: attempts[i].attempt_number,
        passed: attempts[i].review_passed ?? checklist.passed,
        failed: checklist.items.filter((it) => !it.passed).length,
        total: checklist.items.length,
      };
    }
  }
  return null;
}

// The "N agents · M events" line under a functionality-group header (FxLane).
// Used to read "M ev" — an abbreviation nobody outside this codebase could
// parse (2026-08-12 operator feedback). Spelled out and pluralized the same
// way agentCount already is on the same line.
export function fxCountsLabel(agentCount, eventCount) {
  return `${agentCount} agent${agentCount === 1 ? "" : "s"} · ${eventCount} event${eventCount === 1 ? "" : "s"}`;
}

const ACTIVE_STATUSES = new Set([
  "pending", "context", "planning", "implementing", "reviewing", "testing",
]);

// Plain-language stage name for an in-flight status — used both in the
// narrative ("Coder is implementing") and the System section's micro-summary.
export const STATUS_STAGE_LABEL = {
  pending: "starting up",
  context: "gathering context",
  planning: "planning",
  implementing: "implementing",
  reviewing: "reviewing",
  testing: "running tests",
};

// Semantic colour token per status — the SAME palette the board's lanes use
// (boardLanes.js), so the drawer never invents a new meaning for a colour.
export const STATUS_COLOR_VAR = {
  pending: "var(--c-building)",
  context: "var(--c-building)",
  planning: "var(--c-building)",
  implementing: "var(--c-building)",
  reviewing: "var(--c-building)",
  testing: "var(--c-building)",
  awaiting_approval: "var(--c-review)",
  compound_parent: "var(--c-building)",
  awaiting_input: "var(--c-answer)",
  blocked: "var(--c-answer)",
  escalated: "var(--c-escalated)",
  paused_quota: "var(--c-answer)",
  done: "var(--c-done)",
  failed: "var(--c-escalated)",
};

export function colorForStatus(status) {
  return STATUS_COLOR_VAR[status] || "var(--text-muted)";
}

// Coarse one-or-two-word status for the always-visible header chip — the
// scannable "state at a glance" that the sentence narrative expands on. Plain
// words only, never a raw enum; color reuses the board-lane semantic tokens so
// one status → one color everywhere. (Redesign D-series: legible run.)
export function coarseStatus(task) {
  const s = task?.status;
  const colorVar = colorForStatus(s);
  let label = "Working";
  if (isHumanStopped(task)) label = "Parked";
  else if (s === "done") label = "Done";
  else if (s === "failed") label = "Failed";
  else if (s === "awaiting_approval") label = taskApprovedAt(task) ? "Merging" : "Ready for you";
  else if (s === "awaiting_input" || s === "blocked" || s === "escalated") label = "Needs you";
  else if (s === "paused_quota") label = "Paused";
  else if (s === "compound_parent") label = "Coordinating";
  else if (ACTIVE_STATUSES.has(s)) label = "Working";
  return { label, colorVar };
}

// A task in a terminal state (done/failed — cancelled is failed + a flag,
// see boardLanes.js) has nothing left to decide: any blocker it carries is
// history, not a live ask. Single definition so the milestone timeline, the
// details micro, and SlideOver's own inline check never drift from each other.
export function isTerminalStatus(status) {
  return status === "done" || status === "failed";
}

// User-facing label for a task kind — never the raw backend value verbatim
// for kinds that read as code (e.g. "ci_fix" → "CI fix").
const KIND_LABEL = {
  feature: "task",
  bugfix: "bug fix",
  code_review: "code review",
  investigation: "investigation",
  design_doc: "design doc",
  ci_fix: "CI fix",
  compound_parent: "multi-part task",
};

function kindLabel(task) {
  return KIND_LABEL[task?.kind] || "task";
}

// ── Narrative header ───────────────────────────────────────────────────────
// Returns { before, phrase, after, colorVar }: `phrase` is the part that gets
// coloured by the status's semantic token; `before`/`after` are plain text.
// Concatenating before+phrase+after must always read as a complete sentence
// and must NEVER contain a raw backend enum (no underscores, no upper-case
// status tokens) — enforced by slideOverSummary.test.mjs.
export function narrativeFor(task) {
  if (!task) {
    return { before: "Loading task details…", phrase: "", after: "", colorVar: "var(--text-dim)" };
  }
  const kind = kindLabel(task);
  const status = task.status;

  // A human who explicitly stopped a parked task (blocker.human_stopped,
  // flattened on the board payload as blocker_human_stopped) already gave
  // their answer: "stop, keep parked". This is stamped on ANY parked status
  // (/reply terminal:true hits awaiting_input, blocked, AND escalated — see
  // _PARKED_STATUSES in app.py), so it must be checked before any
  // status-specific branch below, not nested inside one — otherwise a
  // human-stopped escalated task would still narrate "waiting for your
  // decision" while the card and count already read it as stopped.
  if (isHumanStopped(task)) {
    return { before: `This ${kind} is`, phrase: "parked — you stopped it", after: "", colorVar: colorForStatus(status) };
  }

  if (status === "done") {
    return { before: `This ${kind} is`, phrase: "done", after: " — its pull request was merged.", colorVar: colorForStatus(status) };
  }
  if (status === "failed") {
    return { before: `This ${kind}`, phrase: "failed", after: " — retry it, or take a closer look at what went wrong.", colorVar: colorForStatus(status) };
  }
  if (status === "awaiting_approval") {
    // `task.approved_at` alone was dead in the drawer: TaskSummaryOut hoists the
    // field, TaskOut (what the drawer fetches) only carries context.approved_at,
    // so this branch never fired where it mattered most.
    if (taskApprovedAt(task)) {
      return { before: `This ${kind} was approved and is`, phrase: "waiting for the PR to merge", after: "", colorVar: colorForStatus(status) };
    }
    return { before: `This ${kind} opened a pull request and is`, phrase: "waiting for your review", after: "", colorVar: colorForStatus(status) };
  }
  if (status === "compound_parent") {
    return { before: `This ${kind} is`, phrase: "coordinating its sub-tasks", after: "", colorVar: colorForStatus(status) };
  }
  if (status === "awaiting_input" || status === "blocked") {
    return { before: `This ${kind} hit a question it can't answer alone —`, phrase: "waiting for your answer", after: "", colorVar: colorForStatus(status) };
  }
  if (status === "escalated") {
    return { before: `This ${kind} couldn't make progress on its own —`, phrase: "waiting for your decision", after: "", colorVar: colorForStatus(status) };
  }
  if (status === "paused_quota") {
    // `blocker.infra`: the park came from a dead agent session, not a wall.
    if (task.blocker && task.blocker.infra === true) {
      return { before: `This ${kind} is paused —`, phrase: "its agent session died; it retries on its own", after: "", colorVar: colorForStatus(status) };
    }
    return { before: `This ${kind} is paused —`, phrase: "waiting for its subscription quota to refresh", after: "", colorVar: colorForStatus(status) };
  }
  if (ACTIVE_STATUSES.has(status)) {
    const attempt = task.attempt_count > 0 ? ` — attempt ${task.attempt_count}` : "";
    // SCRUM-16: an active STATUS is not a live SESSION. `claimed` (the
    // scheduler's in-flight set, on every board payload since SCRUM-15) is
    // the only thing that means an agent is actually running right now — an
    // unclaimed active-status task is queued for a worker slot, and crediting
    // a live actor ("Coder is implementing") for it is the untruth this
    // narrative exists to avoid. `pending` keeps its own line below: it has
    // never claimed an actor.
    if (status !== "pending" && task.claimed !== true) {
      return {
        before: `This ${kind} is`,
        phrase: `queued — waiting for a worker slot${attempt}`,
        after: "",
        colorVar: "var(--c-context)",
      };
    }
    // Attribution must match the System view's lanes: the Coder is only at the
    // keyboard during `implementing`. Every OTHER stage is a different worker —
    // saying "Coder is planning" while the Coding lane reads "not started yet"
    // is the contradiction this guards against. Name the actual actor per stage.
    if (status === "reviewing") {
      return { before: "The reviewer is", phrase: `checking the work${attempt}`, after: "", colorVar: colorForStatus(status) };
    }
    if (status === "testing") {
      return { before: "Tests are", phrase: `running${attempt}`, after: "", colorVar: colorForStatus(status) };
    }
    if (status === "planning") {
      return { before: "The planner is", phrase: `planning the approach${attempt}`, after: "", colorVar: colorForStatus(status) };
    }
    if (status === "context") {
      return { before: "The orchestrator is", phrase: `gathering context${attempt}`, after: "", colorVar: colorForStatus(status) };
    }
    if (status === "pending") {
      return { before: `This ${kindLabel(task)} is`, phrase: "starting up", after: "", colorVar: colorForStatus(status) };
    }
    const stage = STATUS_STAGE_LABEL[status] || "working";
    return { before: "Coder is", phrase: `${stage}${attempt}`, after: "", colorVar: colorForStatus(status) };
  }
  // Unknown/other status: say so plainly rather than guessing.
  return { before: `This ${kind} is`, phrase: "in an unrecognized state", after: "", colorVar: "var(--text-muted)" };
}

// ── Chips row ──────────────────────────────────────────────────────────────
export function prUrlFor(task) {
  if (!task) return null;
  if (task.context?.pr_url) return task.context.pr_url;
  const attempts = task.attempts || [];
  for (let i = attempts.length - 1; i >= 0; i--) {
    if (attempts[i].pr_url) return attempts[i].pr_url;
  }
  return null;
}

function branchFor(task) {
  const attempts = task?.attempts || [];
  for (let i = attempts.length - 1; i >= 0; i--) {
    if (attempts[i].branch_name) return attempts[i].branch_name;
  }
  return null;
}

// { key, label, sub, href? } — `label` values are the tabular-nums figures;
// `sub` is the quiet caption under them. `authMode` decides the cost chip's
// basis: only api_key (BYO) mode pays Anthropic per token for real
// (cost.js's pricingIsReal) — subscription/OAuth (the default, and any
// absent value) pays a flat fee, so tokens lead there and the dollar figure
// becomes a marked-est. secondary, same convention as ledgerSpend.js's
// deriveSpendDisplay.
export function chipsFor(task, authMode) {
  if (!task) return [];
  const chips = [];
  const burn = taskBurn(task);
  const cost = taskCost(task);
  if (burn > 0) {
    chips.push(pricingIsReal(authMode)
      ? { key: "cost", label: fmtCost(cost), sub: `${fmtTokens(burn)} tok` }
      : { key: "cost", label: `${fmtTokens(burn)} tok`, sub: cost > 0 ? `~${fmtCost(cost)} est.` : "tokens" });
  }
  // "ran" (Σ phase durations, D1.3) sits before "wall". Only a POSITIVE number
  // gets a chip: the phase table is empty until D1.2 lands, so every real task
  // reads active_seconds null/0 and shows no ran chip — never a false "0s ran".
  if (typeof task.active_seconds === "number" && task.active_seconds > 0) {
    chips.push({ key: "ran", label: formatDuration(Math.round(task.active_seconds)), sub: "ran" });
  }
  if (task.wall_seconds != null) chips.push({ key: "time", label: formatDuration(Math.round(task.wall_seconds)), sub: "wall time" });
  if (task.attempt_count > 0) {
    chips.push({ key: "attempts", label: String(task.attempt_count), sub: task.attempt_count === 1 ? "attempt" : "attempts" });
  }
  const pr = prUrlFor(task);
  if (pr) {
    // Only http(s) URLs get an href - a demo-DB local-pr:// anchor would be a
    // dead link, so it degrades to a text chip (same rule as the board card,
    // via the ONE shared guard in prUrl.js).
    if (httpPrUrl(pr)) {
      chips.push({ key: "pr", label: branchFor(task) || "PR", sub: "pull request", href: pr });
    } else {
      chips.push({ key: "pr", label: branchFor(task) || "PR", sub: "pull request" });
    }
  }
  return chips;
}

// ── Artifacts — the concrete outputs of a run ──────────────────────────────
// What the task actually PRODUCED, digested for the primary pane: the pull
// request, the files it changed, and the review verdict. Pure over
// (task, diff) — the same data the drawer already holds — so the redesigned
// running-task view can show "what came out of this" at a glance without
// waiting on the raw event stream. Returns [{ key, label, value, href?, tone? }].
export function artifactsFor(task, diff) {
  if (!task) return [];
  const out = [];

  const pr = prUrlFor(task);
  if (pr) {
    out.push({
      key: "pr",
      label: "Pull request",
      value: branchFor(task) || "opened",
      // A demo-DB local-pr:// anchor is a dead link — same guard as chipsFor.
      href: httpPrUrl(pr) ? pr : null,
    });
  }

  const stats = diffStats(diff);
  if (stats.files > 0) {
    out.push({
      key: "files",
      label: "Files changed",
      value: `${stats.files} ${pluralize(stats.files, "file")} · +${stats.added} −${stats.removed}`,
    });
  }

  // The latest attempt that carries a review — the verdict the reviewer landed.
  const attempts = task.attempts || [];
  for (let i = attempts.length - 1; i >= 0; i--) {
    const checklist = attempts[i]?.review_checklist;
    if (checklist?.items?.length) {
      const v = reviewVerdict(checklist);
      // A not-yet-passed checklist with zero failed items is a review still in
      // progress, not a verdict — "0 findings to address" (red) would be a lie.
      // Only surface the review line once there's an actual verdict to show.
      if (v.verdict !== "PASSED" && v.findings === 0) break;
      out.push({
        key: "review",
        label: "Review",
        value: v.verdict === "PASSED"
          ? (v.detail ? `passed · ${v.detail}` : "passed")
          : `${v.findings} ${pluralize(v.findings, "finding")} to address`,
        tone: v.tone,
      });
      break;
    }
  }

  return out;
}

// ── Milestone timeline ─────────────────────────────────────────────────────
// created → planned → attempt N → review → PR → done. Derived from `task`
// (created_at/status/attempts/context) — no separate event fetch needed, so
// the summary never blocks on the per-tab lazy event stream.
export function milestonesFor(task) {
  if (!task) return [];
  const attempts = task.attempts || [];
  const last = attempts[attempts.length - 1];
  const hasSpec = !!(task.context?.spec && (task.context.spec.approach || task.context.spec.files_to_change?.length));
  const planned = hasSpec || attempts.length > 0 || !["pending", "context", "planning"].includes(task.status);
  const reviewed = !!last?.review_checklist?.items?.length;
  const pr = !!prUrlFor(task);
  const isDone = task.status === "done";

  const items = [
    { key: "created", label: "Created", done: !!task.created_at },
    { key: "planned", label: "Planned", done: planned },
    { key: "attempt", label: attempts.length ? `Attempt ${attempts.length}` : "Attempt", done: attempts.length > 0 },
    { key: "review", label: "Review", done: reviewed },
    { key: "pr", label: "Pull request", done: pr },
    { key: "done", label: "Done", done: isDone },
  ];

  // The latest milestone reached "pulses" while the task is still active —
  // terminal tasks (done/failed) show a settled trail, nothing pulsing.
  const isTerminal = isTerminalStatus(task.status);
  let lastDoneIdx = -1;
  items.forEach((it, i) => { if (it.done) lastDoneIdx = i; });
  return items.map((it, i) => ({ ...it, current: !isTerminal && i === lastDoneIdx }));
}

// ── Section micro-summaries ────────────────────────────────────────────────
// Single-slot memo keyed on the diff string itself — the drawer shows one
// task's diff at a time, and re-renders (SSE ticks, section toggles) pass the
// SAME string repeatedly, so there's no need to rescan line-by-line each time.
let _diffStatsCache = { diff: undefined, stats: undefined };

function diffStats(diff) {
  if (!diff) return { added: 0, removed: 0, files: 0 };
  if (_diffStatsCache.diff === diff) return _diffStatsCache.stats;
  const files = new Set();
  let added = 0;
  let removed = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git")) {
      const m = line.match(/diff --git a\/(.+?) b\//);
      if (m) files.add(m[1]);
      continue;
    }
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added++;
    else if (line.startsWith("-")) removed++;
  }
  const stats = { added, removed, files: files.size };
  _diffStatsCache = { diff, stats };
  return stats;
}
export { diffStats };

function relativeTimeFrom(iso, nowMs = Date.now()) {
  if (!iso) return null;
  const then = timestampMs(iso);
  if (Number.isNaN(then)) return null;
  const s = Math.max(0, Math.round((nowMs - then) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// ── review verdict, severity-class aware ───────────────────────────────────
// The gate fails on BLOCKING findings only: a reviewer grades each finding and
// anything graded low/nit is advisory. Mirrors ADVISORY_SEVERITIES in
// src/no_human/review/reviewer.py, including its degrade-safe rule that an
// unclassified finding blocks. slideOverSummary.test.mjs reads that frozenset
// out of the Python source and asserts the two sets are identical, so this
// cannot silently drift.
export const ADVISORY_SEVERITIES = new Set(["low", "nit"]);

function normSeverity(item) {
  return String(item?.severity ?? "").trim().toLowerCase();
}

// A failing finding blocks unless the reviewer graded it low or nit.
export function isBlockingFinding(item) {
  if (!item || item.passed) return false;
  return !ADVISORY_SEVERITIES.has(normSeverity(item));
}

// The chip text for one checklist row. A PASSING criterion carries no grade and
// needs no chip. A FAILING finding the reviewer never graded is "unrated" — the
// same token severityBreakdown() counts in the section header below, so the row
// and the header can no longer disagree (the header said "1 unrated" beside a
// row that rendered nothing at all, and `.cr-sev-unrated` was reachable from no
// code path). An ungraded finding also BLOCKS, per reviewer.py's degrade-safe
// rule — silently showing no chip understated it.
export function severityChip(item) {
  if (!item) return null;
  return normSeverity(item) || (item.passed ? null : UNRATED);
}

// Row modifier: a passing row, an advisory (non-blocking) finding, or a
// blocking failure. Advisory gets its own class so it never wears the
// blocking red.
export function checklistRowClass(item) {
  if (!item || item.passed) return "pass";
  return isBlockingFinding(item) ? "fail" : "advisory";
}

// The bucket for a failing finding the reviewer left ungraded. Not a severity —
// the absence of one — so it sorts after the real grades.
const UNRATED = "unrated";

// reviewer.py's schema is critical|high|medium|low|nit and nothing else;
// slideOverSummary.test.mjs reads that vocabulary out of the Python source and
// asserts this list and the .cr-sev-* rules in styles.css both match it. ("med"
// and "major" used to sit here and in the stylesheet: "med" is
// history/analyzer.py's unrelated importance label and "major" was never
// emitted by anything.)
const SEVERITY_ORDER = ["critical", "high", "medium", "low", "nit", UNRATED];

function severityBreakdown(failed) {
  const counts = new Map();
  for (const it of failed) {
    const key = normSeverity(it) || UNRATED;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const rank = (k) => {
    const i = SEVERITY_ORDER.indexOf(k);
    return i === -1 ? SEVERITY_ORDER.length : i;
  };
  return [...counts.entries()]
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]))
    .map(([sev, n]) => `${n} ${sev}`);
}

// The review section's header. On a PASS the verdict word leads and the
// findings are counted as findings — never as a pass ratio, which read as an
// 80% failure on a review that passed with 4 advisory findings and 1 criterion.
// A FAIL is unchanged: verdict word, no detail.
export function reviewVerdict(checklist) {
  const items = checklist?.items || [];
  const failed = items.filter((it) => !it.passed);
  const blocking = failed.filter(isBlockingFinding).length;
  const advisory = failed.length - blocking;
  if (checklist?.passed !== true) {
    return { verdict: "FAILED", tone: "fail", detail: null, blocking, advisory, findings: failed.length };
  }
  let detail = null;
  if (failed.length) {
    const noun = failed.length === 1 ? "finding" : "findings";
    const kind = blocking === 0 ? "non-blocking " : "";
    detail = `${failed.length} ${kind}${noun} (${severityBreakdown(failed).join(", ")})`;
  }
  return { verdict: "PASSED", tone: "pass", detail, blocking, advisory, findings: failed.length };
}

// ── test result verdict ─────────────────────────────────────────────────────
// The test-results card used to show the bare word "clean" whenever
// `tamper_flag` was unset — with no regard for whether the attempt actually
// carried failures. `orchestrator.py`'s change-aware gate has TWO distinct
// excuse paths that let an attempt with a nonzero failed count still open a
// PR (constraint: gate logic itself is untouched, this only renders it
// honestly):
//   1. `_newly_failing_vs_base` (plain red run): every failing id already
//      fails on the base tree → `pre_existing_failures` is written with the
//      ids (orchestrator.py `_run_attempt`, the `newly_failing == []` branch).
//   2. `_invocation_error_reproduces_on_base` (the runner itself errored):
//      the SAME invocation error reproduces on the base tree → the attempt
//      proceeds with `invocation_error: true, reproduces_on_base: true` and
//      NO `pre_existing_failures` key — a different record shape for the
//      same "gate excused this, don't blame the change" outcome.
// Neither bucket is "clean" (there IS a nonzero failed/invocation-error
// count) and neither is a genuine "failed" (the gate did not fail the
// attempt on it) — they get their own "excused" tone naming the excuse.
// Anything left over — `ok === false` with no excuse recorded — is a
// genuinely red result and must never render as clean.
export function testResultVerdict(testResults) {
  if (!testResults) return null;
  if (testResults.tamper_flag) return null;
  const failed = Number(testResults.failed) || 0;
  const errors = Number(testResults.errors) || 0;
  const preExisting = Array.isArray(testResults.pre_existing_failures)
    ? testResults.pre_existing_failures.filter(Boolean)
    : [];
  if (preExisting.length > 0) {
    const n = preExisting.length;
    return {
      tone: "excused",
      label: `passed — ${n} pre-existing failure${n === 1 ? "" : "s"} also fail `
        + `on the base tree, excused: ${preExisting.join(", ")}`,
    };
  }
  if (testResults.invocation_error && testResults.reproduces_on_base) {
    return {
      tone: "excused",
      label: "passed — test invocation also fails on the base tree, "
        + "excused (no test evidence)",
    };
  }
  if (testResults.ok === false || failed > 0 || errors > 0) {
    return { tone: "failed", label: "failed" };
  }
  return { tone: "clean", label: "clean" };
}

// ── approval feedback ──────────────────────────────────────────────────────
// Approve recorded server-side while the drawer said nothing, so the operator
// read a successful approval as a dead button. These two pure helpers give the
// click an immediate state change and a live-region message.

// When this task was approved, or null. Derived from the PAYLOAD, not from the
// session: `approveOutcome` is reset on every taskId change, and the approve
// endpoint leaves a normal PR task in awaiting_approval (it stamps
// context.approved_at and stops — the human still merges). Reading session
// state alone meant reopening the drawer on an approved task showed a plain,
// enabled "Approve", and a second click silently re-stamped approved_at.
//
// TaskOut (the drawer's detail payload) carries it under `context`;
// TaskSummaryOut (the board's) hoists it to the top level. Same fact.
export function taskApprovedAt(task) {
  const at = approvedAtOf(task);
  if (!at) return null;
  // A later escalation/send-back/fresh-attempt superseded this approval
  // server-side (core/db.py::_write_status stamps context.approval_
  // superseded_at, write-once, the moment the row leaves awaiting_approval
  // for anything other than done) — that stamp is authoritative and checked
  // FIRST; the send_back_feedback heuristic below stays as a fallback for
  // older servers/payloads that predate the marker.
  if (supersededAtOf(task)) return null;
  // "Send back" returns the task to the queue for a NEW attempt with a NEW PR,
  // and it never clears approved_at server-side. An approval recorded before
  // the latest send-back is SPENT — treating it as live would lock the operator
  // out of approving the PR that replaced the one they rejected.
  const t = timestampMs(at);
  const backs = task?.context?.send_back_feedback;
  if (Number.isFinite(t) && Array.isArray(backs)) {
    for (const b of backs) {
      const bt = timestampMs(b?.at);
      if (Number.isFinite(bt) && bt > t) return null;
    }
  }
  return at;
}

// `outcome` is null before the click resolves, "ok" once the server recorded
// the approval, "error" if it did not. `approvedAt` is taskApprovedAt(task) —
// the same state, but one that survives closing and reopening the drawer.
//
// Re-approval is BLOCKED, not merely labelled: a second POST cannot produce a
// merge (only the human's git host can) and its only effect is to overwrite the
// timestamp of when the human actually approved.
function mmss(ms) {
  const total = Math.max(0, Math.floor((ms || 0) / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function approveButtonState({
  busy = false, outcome = null, approvedAt = null, merging = false, elapsedMs = 0, step = null,
} = {}) {
  // Wording matched to Board.jsx's action hint for the same state, so the card
  // and the drawer never describe one task two ways.
  //
  // `approvedAt` outranks a client-side "error", and the exclusion that used to
  // be here (`outcome !== "error"`) is why: it produced an ENABLED "Retry
  // approve" whose handler is a no-op, because SlideOver's handleApprove
  // returns early on `approvedAt`. Reachable without any server fault - the
  // POST lands, `outcome` is set to "ok", and a transient failure of the
  // refetch in the same try flips it to "error"; the next refresh then delivers
  // `approved_at`. A concurrent `nh approve` reaches the same state.
  //
  // `approvedAt` is the SERVER's record and `outcome` is this session's guess
  // at it, so when they disagree the server wins. An error with no server
  // record is still retryable, which is the case that exclusion was aimed at
  // and is unaffected. `taskApprovedAt` has already discarded an approval spent
  // by a later send-back, so this cannot latch on a stale stamp.
  if (outcome === "ok" || approvedAt) {
    return { label: "Approved — merge pending", disabled: true, tone: "ok" };
  }
  // The land is a synchronous 2-4 minute server call — this branch is what
  // keeps the button reflecting it instead of looking dead. Checked before
  // `busy` (the click-to-request-resolves state) since a merge in progress
  // subsumes it: the elapsed timer is the point, not just "disabled".
  if (merging) {
    const label = `Merging… ${mmss(elapsedMs)}`;
    return { label, disabled: true, tone: "busy", step: step || null };
  }
  if (busy) return { label: "Approving…", disabled: true, tone: "busy" };
  // Operator directive 2026-08-12: approve now MERGES the PR (a local squash
  // under the operator identity, then push) rather than only recording
  // approval for a human to merge by hand elsewhere — the label says so.
  if (outcome === "error") return { label: "Retry approve and merge", disabled: false, tone: "error" };
  return { label: "Approve and merge", disabled: false, tone: "idle" };
}

// The banner text plus the ARIA role it lands in. The server's own message
// leads when it sends one — an already-satisfied approval has no PR to merge,
// so a hardcoded merge instruction would lie on that path.
export function approvalFeedback({ ok = true, message = "", remaining = 0, error = "" } = {}) {
  if (!ok) {
    const why = String(error || "").trim();
    return {
      role: "alert",
      tone: "error",
      text: `Approval NOT recorded${why ? ` - ${why}` : ""}. Nothing changed; click Retry approve and merge.`,
    };
  }
  const base = String(message || "").trim()
    // The server's own message (a landed sha, a skip reason) leads whenever it
    // sends one — this fallback only covers a response with no `message`.
    || "Approval recorded. The PR will be merged automatically - the agent never merges on its own.";
  const more = remaining > 0 ? ` ${remaining} more waiting - use Next review.` : "";
  return { role: "status", tone: "ok", text: base + more };
}

// `merge_step_<step>` -> the bare step name the button shows next to the
// elapsed timer. Anything else (merge_started, human_merged, merge_failed,
// an unrelated frame kind) is not a step label.
export function mergeStepLabel(kind) {
  const k = String(kind || "");
  if (!k.startsWith("merge_step_")) return null;
  const step = k.slice("merge_step_".length);
  return step || null;
}

// The persistent inline alert for a land failure — intake-resolved shape:
// a heading naming the failed step, the first 200 chars of stderr, and a
// dismissible close button (the dismiss affordance itself lives in the
// component that renders this; this just marks it dismissible via `role`).
export function landFailureFeedback({ step, stderr } = {}) {
  const stepName = String(step || "unknown").trim();
  const capped = String(stderr || "").slice(0, 200);
  return {
    role: "alert",
    tone: "error",
    dismissible: true,
    text: `Failed: ${stepName}${capped ? ` — ${capped}` : ""}`,
  };
}

function reviewMicro(task) {
  const attempts = task.attempts || [];
  const last = attempts[attempts.length - 1];
  const checklist = last?.review_checklist;
  if (!checklist?.items?.length) {
    // Fewer than 2 attempts: there's nothing to be "unreviewed relative to"
    // yet, so the plain empty state stands. From 2 attempts on, name which
    // attempt this is and, if an earlier one carries a review, point at it —
    // the stage strip can simultaneously say "Reviewing DONE" (attempt
    // history) while THIS attempt hasn't been reviewed yet.
    if (attempts.length <= 1) {
      return { text: "Not reviewed yet", colorVar: "var(--text-muted)" };
    }
    const n = attempts.length;
    const prior = lastReviewedAttempt(task);
    if (prior) {
      return {
        text: `Attempt ${n} not reviewed yet · attempt ${prior.attempt_number}: ${prior.passed ? "PASS" : "FAIL"}`,
        colorVar: "var(--text-muted)",
      };
    }
    return { text: `Attempt ${n} not reviewed yet`, colorVar: "var(--text-muted)" };
  }
  const total = checklist.items.length;
  const failed = checklist.items.filter((it) => !it.passed).length;
  // `checklist.passed` is the backend's authoritative verdict — the SAME value
  // ReviewTab's "Reviewer verdict — PASSED/FAILED" header reads (a stage can
  // fail for reasons an item count alone doesn't show). This micro-summary
  // must never say "All checks passed" while the section it teases says
  // FAILED, so when the two disagree the authoritative flag wins.
  if (failed === 0 && checklist.passed !== false) {
    return { text: "All checks passed", colorVar: "var(--green)" };
  }
  if (failed === 0 && checklist.passed === false) {
    return { text: "Reviewer failed", colorVar: "var(--red)" };
  }
  // A review that PASSED can still carry findings — the gate fails on blocking
  // ones only. Counting those as "1/5 passed" read as an 80% failure on a
  // legitimate PASS, so the verdict leads and the ratio is gone.
  if (checklist.passed === true) {
    const v = reviewVerdict(checklist);
    return { text: `Passed - ${v.detail}`, colorVar: "var(--green)" };
  }
  const prev = attempts[attempts.length - 2]?.review_checklist;
  if (prev?.items?.length === total) {
    const prevPassed = prev.items.filter((it) => it.passed).length;
    const passed = total - failed;
    const fixed = passed - prevPassed;
    if (fixed > 0) {
      return { text: `${failed} finding${failed === 1 ? "" : "s"} · ${fixed} fixed`, colorVar: "var(--red)" };
    }
  }
  return { text: `${failed} finding${failed === 1 ? "" : "s"} · ${total - failed}/${total} passed`, colorVar: "var(--red)" };
}

// Returns { text, colorVar } or null (nothing worth saying yet).
export function sectionSummary(key, { task, diff } = {}) {
  if (!task) return null;
  const attempts = task.attempts || [];
  const last = attempts[attempts.length - 1];
  switch (key) {
    case "system": {
      if (task.status === "done") return { text: "Pipeline complete", colorVar: colorForStatus("done") };
      if (task.status === "failed") return { text: "Pipeline stopped — failed", colorVar: colorForStatus("failed") };
      if (isHumanStopped(task)) return { text: "Parked — you stopped it", colorVar: colorForStatus(task.status) };
      if (PARKED_STATUSES.has(task.status)) return { text: "Paused, waiting on you", colorVar: colorForStatus(task.status) };
      if (task.status === "paused_quota") {
        const dead = task.blocker && task.blocker.infra === true;
        return { text: dead ? "Paused — session died" : "Paused for quota", colorVar: colorForStatus(task.status) };
      }
      if (task.status === "compound_parent") return { text: "Coordinating sub-tasks", colorVar: colorForStatus(task.status) };
      const stage = STATUS_STAGE_LABEL[task.status];
      return { text: stage ? `Running — ${stage}` : "Running", colorVar: colorForStatus(task.status) };
    }
    case "activity": {
      const ts = task.last_activity || task.updated_at;
      const rel = relativeTimeFrom(ts);
      return { text: rel ? `Last activity ${rel}` : "No activity yet", colorVar: "var(--text-muted)" };
    }
    case "subtasks":
      return { text: "Split into sub-tasks", colorVar: "var(--text-muted)" };
    case "details": {
      // An open question is the more actionable fact — it wins over the
      // criteria count when both are present (the criteria are still one
      // click away inside the section either way). An "answered" blocker
      // falls through to the criteria summary below: reply_task never
      // clears task.blocker, so a raw `task.blocker?.question` check would
      // keep showing the badge after the human already replied.
      const qState = questionState(task);
      if (qState === "terminal") {
        return { text: "Asked before it ended", colorVar: "var(--text-muted)" };
      }
      if (qState === "open") {
        return { text: "Has a question for you", colorVar: colorForStatus(task.status) };
      }
      const total = task.acceptance_criteria?.length || 0;
      if (total > 0) {
        const progress = task.context?.progress?.acceptance_criteria || [];
        const tracked = progress.length > 0;
        // No backend writer populates context.progress.acceptance_criteria
        // today (grep confirms there is none) — every task is "untracked" in
        // production. "Not tracked" read as an unexplained permanent defect;
        // the count IS known and meaningful, so state that instead. This
        // branch stays correct for if/when a per-criterion writer lands.
        if (!tracked) {
          return {
            text: `${total} acceptance ${pluralize(total, "criterion", "criteria")}`,
            colorVar: "var(--text-muted)",
          };
        }
        const done = progress.filter((p) => p?.status === "done").length;
        return { text: `${done}/${total} criteria done`, colorVar: "var(--text-muted)" };
      }
      return { text: "Description & criteria", colorVar: "var(--text-muted)" };
    }
    case "spec": {
      // The spec isn't per-attempt (`orchestrator._persist_plan` overwrites
      // `context.spec` on every attempt) — there's no prior-attempt spec
      // artifact to link to, so this states scope but invents no link.
      const spec = task.context?.spec;
      const n = spec?.files_to_change?.length;
      return { text: n ? `${n} file${n === 1 ? "" : "s"} planned` : "No spec for this attempt yet", colorVar: "var(--text-muted)" };
    }
    case "review":
      return reviewMicro(task);
    case "diff": {
      const { added, removed, files } = diffStats(diff);
      if (!added && !removed) return { text: "No changes yet", colorVar: "var(--text-muted)" };
      return { text: `+${added} −${removed} across ${files} file${files === 1 ? "" : "s"}`, colorVar: "var(--text-muted)" };
    }
    case "attempts": {
      if (!attempts.length) return { text: "No attempts yet", colorVar: "var(--text-muted)" };
      let verdict = null;
      let colorVar = "var(--text-muted)";
      if (last?.review_passed != null) {
        verdict = last.review_passed ? "latest passed" : "issues found";
        colorVar = last.review_passed ? "var(--green)" : "var(--red)";
      }
      return { text: `${attempts.length} attempt${attempts.length === 1 ? "" : "s"}${verdict ? ` · ${verdict}` : ""}`, colorVar };
    }
    default:
      return null;
  }
}

// ── Gate-aware default-open section ────────────────────────────────────────
// Mirrors the pre-1.4 gate-aware first-tab logic: the section that clears
// this task's gate starts open (review's diff+approve, or details' question
// +canned answers); an in-flight task opens on System.
export function defaultOpenSection(task) {
  if (!task) return null;
  if (task.status === "awaiting_approval") return "review";
  if (PARKED_STATUSES.has(task.status)) {
    // A parked task with a blocker now shows the DecisionPanel above the
    // accordion — it carries the question and the actions, so we leave the
    // sections collapsed instead of auto-opening Details onto a wall of
    // description text. Only when there's no blocker to build a panel from do
    // we fall back to opening Details.
    return task.blocker ? null : "details";
  }
  // A compound parent has no pipeline of its own — System renders empty. Open
  // on Sub-tasks when the decomposition actually produced any; otherwise fall
  // back to the pre-existing System default (e.g. mid-decomposition, before
  // subtasks are known).
  if (task.status === "compound_parent") {
    const subtasks = task.context?.decomposition?.subtasks;
    if (Array.isArray(subtasks) && subtasks.length > 0) return "subtasks";
  }
  return "system";
}

// ── Verifiers (review/verifiers.py, VerifierResult.as_dict()) ─────────────
// Pure formatter over `attempt.verifier_results` — the same shape the PR
// body's Evidence table row and <details> list render from
// (core/pr_evidence.py's `verifiers_pin()`), duplicated here rather than
// imported: this is JS reading a JSON column, not a shared module boundary.
// Returns null for anything that is not a non-empty array — `undefined`
// (column never populated for this attempt), `[]` (verifiers ran, matched
// nothing), and any non-array all render as "nothing to show" rather than
// an empty section.
export function verifierRows(results) {
  if (!Array.isArray(results) || results.length === 0) return null;
  const total = results.length;
  const failedIds = results
    .filter((r) => !r?.passed)
    .map((r) => String(r?.verifier_id ?? ""))
    .sort();
  const summary = failedIds.length
    ? `${failedIds.length} of ${total} failed — ${failedIds.join(", ")}`
    : `${total} of ${total} satisfied`;
  const rows = results.map((r) => {
    const ok = !!r?.passed;
    const file = r?.file ?? "";
    const line = r?.line ?? null;
    return {
      id: String(r?.verifier_id ?? ""),
      ok,
      filesChecked: Array.isArray(r?.files_checked) ? r.files_checked.length : 0,
      // Only a failure names a location/comment — a pass has nothing to
      // point at, and rendering an empty location on every row would read
      // as "no location was ever recorded" for the passes.
      location: !ok && file ? (line ? `${file}:${line}` : file) : "",
      comment: !ok ? String(r?.evidence ?? r?.comment ?? "") : "",
    };
  });
  return { summary, rows };
}

// mergePolicyRows() is a pure formatter over `task.context.merge_policy[sha]`
// (core/merge_policy.py's `PolicyVerdict.as_dict()`) — the same verdict
// `nh status`'s `merge-ready: N` line and the board's MERGE-READY chip
// (mergeReadyChip, below) read via `api/models.py`'s `merge_ready_for`. It
// must not compute its own "ready" logic — it only reshapes the verdict
// already decided server-side, so the Review tab can never disagree with the
// chip or the CLI about what "ready" means.
//
// Null discipline mirrors verifierRows: no verdict was ever persisted for
// this exact head sha (no attempt yet, or a verdict stamped for an OLDER
// commit that the sha-keyed lookup already excluded) reads as "nothing to
// show", not as an empty/failing section.
export function mergePolicyRows(verdict) {
  if (!verdict || typeof verdict !== "object" || Array.isArray(verdict)) return null;
  if (!Array.isArray(verdict.rules) || verdict.rules.length === 0) return null;
  const source = verdict.source === "file" ? "file" : "default";
  const sourceLine =
    source === "file" ? "policy: .no_human/merge_policy.yaml" : "policy: built-in default";
  const rows = verdict.rules.map((r) => ({
    name: String(r?.name ?? ""),
    ok: !!r?.passed,
    detail: String(r?.detail ?? ""),
  }));
  return {
    summary: String(verdict.summary ?? ""),
    ready: !!verdict.ready,
    source,
    sourceLine,
    policyChanged: !!verdict.policy_changed_in_diff,
    problems: Array.isArray(verdict.problems) ? verdict.problems.map(String) : [],
    rows,
  };
}

// mergeReadyChip() — the board card's MERGE-READY chip. Advisory only: it
// reflects the repo's merge-ready policy for the task's current head, not a
// merge precondition (`nh approve` decides from its own independent-reviewer
// PASS check — see api/models.py's merge_ready_for docstring). Shown only in
// the Review PR lane: a task that has since moved on (approved+landed →
// done, or re-opened → back to working) no longer has a PR to review, so the
// chip would be stale noise anywhere else.
export function mergeReadyChip(task) {
  return task?.merge_ready === true && routeTask(task) === "review" ? "MERGE-READY" : null;
}
