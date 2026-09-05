import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  narrativeFor, chipsFor, milestonesFor, artifactsFor, sectionSummary, defaultOpenSection,
  diffStats, colorForStatus, PARKED_STATUSES, STATUS_STAGE_LABEL, isTerminalStatus,
  isHumanStopped, questionState, lastReviewedAttempt,
  ADVISORY_SEVERITIES, isBlockingFinding, reviewVerdict, severityChip,
  checklistRowClass, approveButtonState, approvalFeedback, taskApprovedAt,
  testResultVerdict, fxCountsLabel, mergeStepLabel, landFailureFeedback,
  coarseStatus,
} from "./slideOverSummary.js";

const SRC = dirname(fileURLToPath(import.meta.url));

// Derive the status list from the backend enum itself (src/no_human/core/task.py)
// rather than hand-copying it here — a hand-copied list silently drifts when a
// status is added (it did: compound_parent shipped in the backend without ever
// reaching this test). Path is resolved relative to this test file so it works
// regardless of cwd.
const TASK_PY = readFileSync(join(SRC, "../../src/no_human/core/task.py"), "utf8");
const ENUM_BODY_MATCH = TASK_PY.match(/class TaskStatus\(str, Enum\):\n([\s\S]*?)\n\s*\n/);
if (!ENUM_BODY_MATCH) {
  throw new Error("could not locate `class TaskStatus(str, Enum):` body in task.py — did it move/rename?");
}
const ALL_STATUSES = [...ENUM_BODY_MATCH[1].matchAll(/^\s*[A-Z_][A-Z0-9_]*\s*=\s*"([a-z_]+)"/gm)]
  .map((m) => m[1]);

function narrativeText(n) {
  return `${n.before} ${n.phrase} ${n.after}`;
}

// ── narrative: user-side language, never a raw enum ────────────────────────

test("narrative never leaks a raw status/kind enum (no underscores)", () => {
  for (const status of ALL_STATUSES) {
    for (const kind of ["feature", "bugfix", "code_review", "investigation", "design_doc", "ci_fix"]) {
      const task = { status, kind, attempt_count: 2 };
      const text = narrativeText(narrativeFor(task));
      assert.ok(!text.includes("_"), `narrative for ${status}/${kind} leaked an enum: "${text}"`);
    }
  }
});

test("the derived status list matches the backend enum exactly (14 values, includes compound_parent)", () => {
  assert.equal(ALL_STATUSES.length, 14, `expected 14 statuses, derived: ${JSON.stringify(ALL_STATUSES)}`);
  assert.ok(ALL_STATUSES.includes("compound_parent"), "compound_parent must be derived from task.py, not hand-omitted");
});

test("narrativeFor is total over every backend status — never throws, always colors, always says something", () => {
  for (const status of ALL_STATUSES) {
    const task = { status, kind: "feature", attempt_count: 1 };
    const n = narrativeFor(task);
    assert.ok(n.colorVar.startsWith("var(--"), `${status}: narrative colorVar must be a token, got "${n.colorVar}"`);
    assert.notEqual(n.phrase, "", `${status}: narrative phrase must not be empty`);
    assert.doesNotThrow(() => sectionSummary("system", { task }), `${status}: System micro-summary must not throw`);
  }
});

test("compound_parent narrative reads as coordinating sub-tasks, not a raw enum or generic 'Running'", () => {
  const n = narrativeFor({ status: "compound_parent", kind: "compound_parent" });
  assert.match(narrativeText(n), /coordinating/);
  assert.match(n.colorVar, /^var\(--c-(building|answer)\)$/);
  const sys = sectionSummary("system", { task: { status: "compound_parent" } });
  assert.notEqual(sys.text, "Running");
});

test("paused_quota reads as a self-resolving quota park, not a task-budget wait", () => {
  // paused_quota comes from _park_quota (subscription QUOTA exhausted), and it
  // auto-resumes when quota refreshes — distinct from BUDGET_EXHAUSTED (the
  // per-task token budget). Neither the narrative nor the badge may call it "budget".
  const text = narrativeText(narrativeFor({ status: "paused_quota", kind: "task" }));
  assert.match(text, /quota/i, "narrative names the subscription quota");
  assert.doesNotMatch(text, /budget/i, "narrative must not conflate quota with the task budget");
  const badge = sectionSummary("system", { task: { status: "paused_quota" } });
  assert.match(badge.text, /quota/i, "badge names quota");
  assert.doesNotMatch(badge.text, /budget/i, "badge must not say budget");
});

test("reviewing/testing attribution copy — not the coder, not a bare stage word", () => {
  const reviewing = narrativeFor({ status: "reviewing", claimed: true });
  assert.equal(narrativeText(reviewing).includes("Coder is reviewing"), false);
  assert.match(narrativeText(reviewing), /reviewer is checking the work/);

  const testing = narrativeFor({ status: "testing", claimed: true });
  assert.match(narrativeText(testing), /Tests are running/);
});

test("pre-coding stages are attributed to the real actor, never the Coder", () => {
  // The Coder is only at the keyboard during `implementing`. Saying "Coder is
  // planning" while the System view's Coding lane reads "not started yet" is
  // the contradiction this guards. Each stage must name the actual worker.
  const planning = narrativeFor({ status: "planning", claimed: true });
  assert.equal(narrativeText(planning).includes("Coder is"), false, "planning must not credit the Coder");
  assert.match(narrativeText(planning), /planner is planning the approach/);

  const context = narrativeFor({ status: "context", claimed: true });
  assert.equal(narrativeText(context).includes("Coder is"), false, "context must not credit the Coder");
  assert.match(narrativeText(context), /orchestrator is gathering context/);

  const pending = narrativeFor({ status: "pending" });
  assert.equal(narrativeText(pending).includes("Coder is"), false, "pending must not credit the Coder");
  assert.match(narrativeText(pending), /starting up/);

  // The one stage the Coder IS active must still say so — no over-correction.
  const implementing = narrativeFor({ status: "implementing", claimed: true });
  assert.match(narrativeText(implementing), /Coder is implementing/);
});

test("narrative colors the status phrase by its semantic token", () => {
  const review = narrativeFor({ status: "awaiting_approval" });
  assert.equal(review.colorVar, "var(--c-review)");
  assert.match(review.phrase, /review/);

  const answer = narrativeFor({ status: "awaiting_input" });
  assert.equal(answer.colorVar, "var(--c-answer)");

  const working = narrativeFor({ status: "implementing", attempt_count: 2, claimed: true });
  assert.equal(working.colorVar, "var(--c-building)");
  assert.match(working.phrase, /implementing/);
  assert.match(working.phrase, /attempt 2/);
});

test("narrative handles the null (loading) task without throwing", () => {
  const n = narrativeFor(null);
  assert.ok(n.before.length > 0);
});

test("approved-but-unmerged reads differently from a fresh PR (B2 #19 parity)", () => {
  const fresh = narrativeFor({ status: "awaiting_approval" });
  const approved = narrativeFor({ status: "awaiting_approval", approved_at: "2026-01-01" });
  assert.notEqual(fresh.phrase, approved.phrase);
  assert.match(approved.phrase, /merge/);
});

test("a human-stopped blocked task reads as parked by the human, not waiting for an answer", () => {
  const stopped = narrativeFor({ status: "blocked", blocker: { human_stopped: true } });
  const waiting = narrativeFor({ status: "blocked" });
  assert.notEqual(stopped.phrase, waiting.phrase);
  assert.doesNotMatch(narrativeText(stopped), /waiting for your answer/);
  assert.match(narrativeText(stopped), /parked|stopped/);
});

test("the flattened board field (blocker_human_stopped) reads the same as the full blocker object", () => {
  const stopped = narrativeFor({ status: "blocked", blocker_human_stopped: true });
  assert.match(narrativeText(stopped), /parked|stopped/);
  assert.doesNotMatch(narrativeText(stopped), /waiting for your answer/);
});

test("a human-stopped ESCALATED task also reads as parked, not 'waiting for your decision' "
  + "(human_stopped is stamped on any parked status, not just blocked/awaiting_input)", () => {
  const stopped = narrativeFor({ status: "escalated", blocker: { human_stopped: true } });
  const waiting = narrativeFor({ status: "escalated" });
  assert.notEqual(stopped.phrase, waiting.phrase);
  assert.doesNotMatch(narrativeText(stopped), /waiting for your decision/);
  assert.match(narrativeText(stopped), /parked|stopped/);
});

test("System chip for a human-stopped task says parked, not 'waiting on you' and differs from the non-stopped park", () => {
  const stopped = sectionSummary("system", { task: { status: "blocked", blocker: { human_stopped: true } } });
  const parked = sectionSummary("system", { task: { status: "blocked" } });
  assert.doesNotMatch(stopped.text, /waiting on you/i);
  assert.match(stopped.text, /parked|stopped/i);
  assert.equal(stopped.text, "Parked — you stopped it");
  assert.notEqual(stopped.text, parked.text);
});

test("System chip reads the flattened blocker_human_stopped field the same as the full blocker", () => {
  const stopped = sectionSummary("system", { task: { status: "blocked", blocker_human_stopped: true } });
  assert.equal(stopped.text, "Parked — you stopped it");
  assert.equal(isHumanStopped({ status: "blocked", blocker: { human_stopped: true } }), true);
  assert.equal(isHumanStopped({ status: "blocked", blocker_human_stopped: true }), true);
});

test("a parked task WITHOUT human_stopped still reads 'Paused, waiting on you'", () => {
  for (const status of ["awaiting_input", "blocked", "escalated"]) {
    const s = sectionSummary("system", { task: { status } });
    assert.equal(s.text, "Paused, waiting on you", `${status} without human_stopped must still read the generic park`);
  }
});

test("human_stopped changes the System chip for every parked status", () => {
  for (const status of ["awaiting_input", "blocked", "escalated"]) {
    const s = sectionSummary("system", { task: { status, blocker: { human_stopped: true } } });
    assert.doesNotMatch(s.text, /waiting on you/i, `${status} human-stopped must not say waiting on you`);
    assert.match(s.text, /parked|stopped/i, `${status} human-stopped must read parked/stopped`);
  }
});

test("a human-stopped task does NOT auto-open a section (DecisionPanel carries the ask) — defaultOpenSection unchanged", () => {
  assert.equal(defaultOpenSection({ status: "blocked", blocker: { human_stopped: true } }), null);
  assert.equal(defaultOpenSection({ status: "escalated", blocker: { human_stopped: true } }), null);
  assert.equal(defaultOpenSection({ status: "blocked" }), "details");
});

// ── chips: cost, wall-time, attempts, PR — tabular data only ───────────────

test("chips include cost+tokens, wall-time, attempts, and a PR link when present", () => {
  const task = {
    // taskBurn (the chip's visibility gate, and its "N tok" sub-label) still sums the raw
    // token buckets — that display is unchanged by the pricing fix. The price itself
    // (chips[0].label) is now taskCost, a pure read of cost_usd: the API prices it
    // server-side (core/cost.py), so the fixture must carry that field directly.
    total_tokens: 500_000, total_cache_read: 2_000_000, total_cache_creation: 0,
    cost_usd: 4.50, cost_model: "claude-sonnet-5",
    wall_seconds: 305, attempt_count: 2,
    attempts: [{ branch_name: "nh/task-1", pr_url: null }, { branch_name: "nh/task-1-v2", pr_url: "https://example.com/pr/9" }],
  };
  const chips = chipsFor(task, "api_key");
  const keys = chips.map((c) => c.key);
  assert.deepEqual(keys, ["cost", "time", "attempts", "pr"]);
  assert.ok(chips[0].label.startsWith("$"));
  assert.equal(chips[2].label, "2");
  assert.equal(chips[3].href, "https://example.com/pr/9");
});

test("chips omit zero/absent fields rather than showing a false 0", () => {
  const chips = chipsFor({ attempt_count: 0, wall_seconds: null });
  assert.deepEqual(chips, []);
});

// D1.3: the "ran" chip (Σ phase durations) sits immediately before "wall time".
test("a positive active_seconds adds a 'ran' chip right before wall time", () => {
  const chips = chipsFor({ active_seconds: 2460, wall_seconds: 61200 });
  const ran = chips.find((c) => c.key === "ran");
  const wall = chips.find((c) => c.key === "time");
  assert.deepEqual({ label: ran.label, sub: ran.sub }, { label: "41m", sub: "ran" });
  assert.equal(wall.sub, "wall time");
  assert.ok(chips.indexOf(ran) < chips.indexOf(wall), "'ran' must precede 'wall time'");
});

// Devil's advocate — the shipped state until D1.2 fills task_phases: no rows →
// active_seconds null/0 → NO ran chip, and never a false "0s ran".
test("no 'ran' chip when active_seconds is null or 0 (empty phase table)", () => {
  for (const v of [null, undefined, 0]) {
    const chips = chipsFor({ active_seconds: v, wall_seconds: 305 });
    assert.equal(chips.find((c) => c.key === "ran"), undefined, `active_seconds=${v}`);
  }
});

// Operator finding (demo walk): the PR affordance must survive the ticket
// finishing - a DONE task with a pr_url still gets the open-pull-request chip.
test("the PR chip renders for DONE tasks too, not only awaiting ones", () => {
  const task = {
    status: "done",
    attempts: [{ branch_name: "nh/task-7", pr_url: "https://example.com/pr/7" }],
  };
  const chips = chipsFor(task, "api_key");
  const pr = chips.find((c) => c.key === "pr");
  assert.ok(pr, "done task with a pr_url must still carry the PR chip");
  assert.equal(pr.href, "https://example.com/pr/7");
  // Truthful copy for any status: it names the artifact/action, never claims
  // the PR is "waiting for your review".
  assert.match(pr.sub, /pull request/);
  assert.doesNotMatch(pr.sub, /waiting/i);
});

test("a non-http pr_url (demo local-pr://) yields a text chip with NO href", () => {
  const task = {
    status: "done",
    attempts: [{ branch_name: "nh/task-8", pr_url: "local-pr://tasks/8" }],
  };
  const chips = chipsFor(task, "api_key");
  const pr = chips.find((c) => c.key === "pr");
  assert.ok(pr, "the PR chip still appears (the branch/PR exists)");
  assert.equal(pr.href, undefined, "a local-pr:// href would be a dead link");
  assert.equal(pr.sub, "pull request");
});

// ── artifactsFor — the running-task digest's "what came out of this" block ──
// The redesigned primary pane shows PR + files changed + review verdict instead
// of a wall of logs. Pure over (task, diff).
test("artifactsFor is empty when the task has produced nothing yet", () => {
  assert.deepEqual(artifactsFor(null, ""), []);
  assert.deepEqual(artifactsFor({ status: "planning", attempts: [] }, ""), []);
});

test("artifactsFor surfaces the PR (linked), the diff stat, and a passing review", () => {
  const diff =
    "diff --git a/src/a.py b/src/a.py\n+added line\n-removed line\n" +
    "diff --git a/src/b.py b/src/b.py\n+another add\n";
  const task = {
    status: "awaiting_approval",
    attempts: [{
      branch_name: "nh/task-42",
      pr_url: "https://example.com/pr/42",
      review_checklist: { passed: true, items: [{ passed: true }, { passed: true }] },
    }],
  };
  const arts = artifactsFor(task, diff);
  const pr = arts.find((a) => a.key === "pr");
  assert.ok(pr, "a PR must appear");
  assert.equal(pr.href, "https://example.com/pr/42");
  assert.equal(pr.value, "nh/task-42");
  const files = arts.find((a) => a.key === "files");
  assert.ok(files, "a files-changed line must appear when the diff is non-empty");
  assert.match(files.value, /2 files/);
  assert.match(files.value, /\+2/);   // two additions across the two files
  assert.match(files.value, /−1/);    // one removal (U+2212 minus, matches source)
  const review = arts.find((a) => a.key === "review");
  assert.ok(review, "a review verdict must appear once the reviewer has run");
  assert.equal(review.tone, "pass");
  assert.match(review.value, /passed/);
});

test("artifactsFor reports findings-to-address for a failed review, tone fail", () => {
  const task = {
    status: "reviewing",
    attempts: [{
      review_checklist: { passed: false, items: [{ passed: false }, { passed: true }] },
    }],
  };
  const arts = artifactsFor(task, "");
  const review = arts.find((a) => a.key === "review");
  assert.ok(review);
  assert.equal(review.tone, "fail");
  assert.match(review.value, /1 finding to address/);
  // No PR and an empty diff → only the review line, nothing fabricated.
  assert.equal(arts.find((a) => a.key === "pr"), undefined);
  assert.equal(arts.find((a) => a.key === "files"), undefined);
});

test("artifactsFor omits the review line for a mid-review checklist with no verdict yet", () => {
  // items present, not passed, nothing failed → review still running. Showing
  // "0 findings to address" in red would be a lie, so no review artifact.
  const task = {
    status: "reviewing",
    attempts: [{ review_checklist: { passed: false, items: [{ passed: true }] } }],
  };
  const arts = artifactsFor(task, "");
  assert.equal(arts.find((a) => a.key === "review"), undefined,
    "an in-progress review (0 findings, not passed) must not render a verdict line");
});

test("artifactsFor degrades a local-pr:// url to an unlinked value", () => {
  const task = {
    status: "done",
    attempts: [{ branch_name: "nh/task-9", pr_url: "local-pr://tasks/9" }],
  };
  const pr = artifactsFor(task, "").find((a) => a.key === "pr");
  assert.ok(pr);
  assert.equal(pr.href, null, "a local-pr:// href would be a dead link");
  assert.equal(pr.value, "nh/task-9");
});

// Operator finding (E2E walk part A): "open pull request" reads as the PR's
// OWN state, which is wrong once the PR is closed by a squash-merge on a done
// task. Neither branch (http href or local-pr:// text) may use the verb.
test("the PR chip caption never uses the verb 'open'", () => {
  const httpTask = {
    status: "done",
    attempts: [{ branch_name: "nh/task-9", pr_url: "https://example.com/pr/9" }],
  };
  const httpPr = chipsFor(httpTask, "api_key").find((c) => c.key === "pr");
  assert.equal(httpPr.sub, "pull request");
  assert.doesNotMatch(httpPr.sub, /open/i);

  const localTask = {
    status: "done",
    attempts: [{ branch_name: "nh/task-10", pr_url: "local-pr://tasks/10" }],
  };
  const localPr = chipsFor(localTask, "api_key").find((c) => c.key === "pr");
  assert.equal(localPr.sub, "pull request");
  assert.doesNotMatch(localPr.sub, /open/i);
});

// ── milestones ──────────────────────────────────────────────────────────────

test("milestones mark created→planned→attempt→review→pr→done in order", () => {
  const task = {
    created_at: "2026-01-01T00:00:00Z", status: "awaiting_approval",
    context: { spec: { approach: "x" }, pr_url: "https://x/pr/1" },
    attempts: [{ review_checklist: { items: [{ passed: true }] } }],
  };
  const m = milestonesFor(task);
  assert.deepEqual(m.map((x) => x.key), ["created", "planned", "attempt", "review", "pr", "done"]);
  assert.equal(m.find((x) => x.key === "done").done, false);
  assert.equal(m.find((x) => x.key === "pr").done, true);
});

test("a done task has no pulsing (current) milestone", () => {
  const task = { created_at: "x", status: "done", attempts: [] };
  const m = milestonesFor(task);
  assert.ok(m.every((x) => x.current === false));
});

test("an active task's LATEST reached milestone pulses", () => {
  const task = { created_at: "x", status: "implementing", attempts: [{ started_at: "x" }] };
  const m = milestonesFor(task);
  const current = m.filter((x) => x.current);
  assert.equal(current.length, 1);
  assert.equal(current[0].key, "attempt");
});

// ── section micro-summaries ───────────────────────────────────────────────

test("review micro-summary never contradicts ReviewTab's authoritative verdict "
  + "(checklist.passed, not a re-derived items count)", () => {
  // Every item.passed is true, but the backend's overall verdict is FAILED
  // (e.g. a stage-level failure an item count doesn't capture) — the tease
  // must say so, not "All checks passed" while the section says FAILED.
  const task = {
    attempts: [{ review_checklist: { passed: false, items: [{ passed: true }, { passed: true }] } }],
  };
  const s = sectionSummary("review", { task });
  assert.equal(s.text, "Reviewer failed");
  assert.notEqual(s.text, "All checks passed");
});

test("review section: findings + fixed-count when a previous attempt regresses to fewer failures", () => {
  const task = {
    status: "reviewing",
    attempts: [
      { review_checklist: { items: [{ passed: false }, { passed: false }, { passed: true }] } },
      { review_checklist: { items: [{ passed: false }, { passed: true }, { passed: true }] } },
    ],
  };
  const s = sectionSummary("review", { task });
  assert.equal(s.text, "1 finding · 1 fixed");
});

test("review section: all-passed reads as a clean summary", () => {
  const task = { attempts: [{ review_checklist: { items: [{ passed: true }, { passed: true }] } }] };
  const s = sectionSummary("review", { task });
  assert.equal(s.text, "All checks passed");
});

// ── attempt-scoping: SPEC/REVIEW state their scope, never a phantom link ──

test("review summary names its attempt and the prior review", () => {
  const task = {
    attempts: [
      { attempt_number: 1, review_checklist: { passed: false, items: [{ passed: false }] } },
      { attempt_number: 2, review_checklist: { passed: false, items: [{ passed: false }] } },
      { attempt_number: 3, review_checklist: { passed: false, items: [{ passed: false }] } },
      { attempt_number: 4, review_passed: false, review_checklist: { passed: false, items: [{ passed: false }] } },
      { attempt_number: 5 },
    ],
  };
  const s = sectionSummary("review", { task });
  assert.ok(s.text.includes("Attempt 5"), s.text);
  assert.ok(s.text.includes("attempt 4"), s.text);
  assert.ok(s.text.includes("FAIL"), s.text);
});

test("review summary with no prior review keeps the plain empty state", () => {
  const task = { attempts: [{ attempt_number: 1 }] };
  const s = sectionSummary("review", { task });
  assert.equal(s.text, "Not reviewed yet");
});

test("spec summary is attempt-scoped and invents no link", () => {
  const task = { status: "implementing", context: {}, attempts: [{ attempt_number: 1 }] };
  const s = sectionSummary("spec", { task });
  assert.equal(s.text, "No spec for this attempt yet");
  assert.ok(!/attempt \d/.test(s.text), s.text);
});

test("a reviewed current attempt is unaffected by the attempt-scoping change", () => {
  const passing = { attempts: [{ attempt_number: 1, review_checklist: { items: [{ passed: true }, { passed: true }] } }] };
  assert.equal(sectionSummary("review", { task: passing }).text, "All checks passed");

  const failing = {
    attempts: [{ attempt_number: 1, review_checklist: { passed: false, items: [{ passed: true }, { passed: true }] } }],
  };
  assert.equal(sectionSummary("review", { task: failing }).text, "Reviewer failed");
});

test("lastReviewedAttempt skips the current attempt and returns the nearest prior reviewed one", () => {
  const task = {
    attempts: [
      { attempt_number: 1, review_checklist: { passed: false, items: [{ passed: false }] } },
      { attempt_number: 2 },
    ],
  };
  const prior = lastReviewedAttempt(task);
  assert.equal(prior.attempt_number, 1);
  assert.equal(prior.passed, false);
});

test("lastReviewedAttempt is null when only the current attempt has ever existed", () => {
  assert.equal(lastReviewedAttempt({ attempts: [{ attempt_number: 1 }] }), null);
  assert.equal(lastReviewedAttempt({ attempts: [] }), null);
});

test("diff section: additions/deletions/file count, matching the +N -M across K files shape", () => {
  const diff = [
    "diff --git a/foo.js b/foo.js",
    "--- a/foo.js", "+++ b/foo.js",
    "@@ -1,2 +1,3 @@",
    "+line one", "+line two", "-old line",
    "diff --git a/bar.js b/bar.js",
    "--- a/bar.js", "+++ b/bar.js",
    "+added",
  ].join("\n");
  const stats = diffStats(diff);
  assert.equal(stats.added, 3);
  assert.equal(stats.removed, 1);
  assert.equal(stats.files, 2);
  const s = sectionSummary("diff", { task: { status: "reviewing" }, diff });
  assert.equal(s.text, "+3 −1 across 2 files");
});

test("no section micro-summary ever contains a raw status enum", () => {
  const task = {
    status: "awaiting_input", acceptance_criteria: ["a"], blocker: { question: "q?" },
    attempts: [{ review_checklist: { items: [{ passed: false }] } }],
    context: { spec: { files_to_change: ["a.js"] } },
  };
  for (const key of ["system", "activity", "subtasks", "details", "spec", "review", "diff", "attempts"]) {
    const s = sectionSummary(key, { task, diff: "" });
    if (s) assert.ok(!s.text.includes("_"), `${key} micro-summary leaked an enum: "${s.text}"`);
  }
});

// ── details micro: never a phantom "0/N criteria done" or a bare "Not tracked" ──

test("details micro: passed-review task with untracked criteria names the count, never Not tracked or 0/N", () => {
  const task = {
    status: "awaiting_approval",
    acceptance_criteria: ["a", "b"],
    // review_passed comes across the wire as an int (0/1), not a boolean —
    // see api/models.py `review_passed: int | None`.
    attempts: [{ review_passed: 1 }],
  };
  const s = sectionSummary("details", { task });
  assert.match(s.text, /^\d+ acceptance criteri/);
  assert.doesNotMatch(s.text, /Not tracked|0\/\d/);
  assert.equal(s.text, "2 acceptance criteria");
});

test("details micro: done task with untracked criteria names the count, never Not tracked or 0/N", () => {
  const task = { status: "done", acceptance_criteria: ["a", "b", "c"] };
  const s = sectionSummary("details", { task });
  assert.match(s.text, /^\d+ acceptance criteri/);
  assert.doesNotMatch(s.text, /Not tracked|0\/\d/);
  assert.equal(s.text, "3 acceptance criteria");
});

test("details micro: passed/done task with tracked criteria shows the real count", () => {
  const task = {
    status: "done",
    acceptance_criteria: ["a", "b"],
    context: { progress: { acceptance_criteria: [{ status: "done" }, { status: "done" }] } },
  };
  const s = sectionSummary("details", { task });
  assert.equal(s.text, "2/2 criteria done");
});

test("details micro: mid-flight task with untracked criteria names the count, never Not tracked or a phantom 0/N", () => {
  const task = { status: "coding", acceptance_criteria: ["a", "b"] };
  const s = sectionSummary("details", { task });
  assert.match(s.text, /^\d+ acceptance criteri/);
  assert.doesNotMatch(s.text, /Not tracked|0\/\d/);
  assert.equal(s.text, "2 acceptance criteria");
});

test("details micro: exactly one untracked criterion is singular, never '1 criteria'", () => {
  const task = { status: "done", acceptance_criteria: ["a"] };
  const s = sectionSummary("details", { task });
  assert.equal(s.text, "1 acceptance criterion");
});

test("details micro: mid-flight task with partially tracked criteria shows the real count", () => {
  const task = {
    status: "coding",
    acceptance_criteria: ["a", "b", "c"],
    context: { progress: { acceptance_criteria: [{ status: "done" }] } },
  };
  const s = sectionSummary("details", { task });
  assert.equal(s.text, "1/3 criteria done");
});

// ── SCRUM-80: terminal tasks neutralize a live blocker ask ─────────────────

test("isTerminalStatus is true only for done/failed", () => {
  assert.equal(isTerminalStatus("done"), true);
  assert.equal(isTerminalStatus("failed"), true);
  assert.equal(isTerminalStatus("blocked"), false);
  assert.equal(isTerminalStatus("awaiting_input"), false);
  assert.equal(isTerminalStatus("implementing"), false);
});

test("details micro neutralizes a blocker question once the task is terminal (failed)", () => {
  const s = sectionSummary("details", { task: { status: "failed", blocker: { question: "why?" } } });
  assert.equal(s.text, "Asked before it ended");
  assert.equal(s.colorVar, "var(--text-muted)");
});

test("details micro neutralizes a blocker question once the task is terminal (done)", () => {
  const s = sectionSummary("details", { task: { status: "done", blocker: { question: "why?" } } });
  assert.equal(s.text, "Asked before it ended");
  assert.equal(s.colorVar, "var(--text-muted)");
});

test("details micro keeps the live ask for a non-terminal (parked) blocker", () => {
  const s = sectionSummary("details", { task: { status: "blocked", blocker: { question: "why?" } } });
  assert.equal(s.text, "Has a question for you");
  assert.equal(s.colorVar, colorForStatus("blocked"));
});

// ── SCRUM-108: an answered blocker clears the question badge ───────────────
// reply_task (api/app.py) never clears task.blocker — it only appends to
// context.human_replies and flips status to implementing — so a raw
// `task.blocker?.question` check kept the badge lit long after the human
// answered.

function escalatedTask(overrides = {}) {
  return {
    status: "escalated",
    blocker: { question: "which approach?", raised_at: "2026-08-12T08:00:00Z" },
    context: {},
    attempts: [],
    ...overrides,
  };
}

test("answered blocker clears the question badge", () => {
  const task = escalatedTask();
  assert.equal(sectionSummary("details", { task }).text, "Has a question for you");
  assert.equal(questionState(task), "open");

  // The exact shape reply_task produces: status flips to implementing,
  // context.human_replies gains an entry, blocker is left untouched.
  const replied = {
    ...task,
    status: "implementing",
    context: {
      human_replies: [{ at: "2026-08-12T08:05:00Z", question: "which approach?", answer: "do X" }],
    },
  };
  assert.equal(questionState(replied), "answered");
  const s = sectionSummary("details", { task: replied });
  assert.notEqual(s.text, "Has a question for you");
});

test("a reply older than the blocker does not clear it", () => {
  const task = escalatedTask({
    context: {
      human_replies: [{ at: "2026-08-12T07:00:00Z", question: "which approach?", answer: "do X" }],
    },
  });
  assert.equal(questionState(task), "open");
  assert.equal(sectionSummary("details", { task }).text, "Has a question for you");
});

test("a terminal task still says it asked, even carrying an old reply (regression guard)", () => {
  const task = escalatedTask({
    status: "done",
    context: {
      human_replies: [{ at: "2026-08-12T08:05:00Z", question: "which approach?", answer: "do X" }],
    },
  });
  assert.equal(questionState(task), "terminal");
  assert.equal(sectionSummary("details", { task }).text, "Asked before it ended");
});

// ── gate-aware default section ──────────────────────────────────────────────

test("default-open section maps review-gate/parked/active exactly like the pre-1.4 tab logic", () => {
  assert.equal(defaultOpenSection({ status: "awaiting_approval" }), "review");
  for (const status of PARKED_STATUSES) {
    // No blocker record -> fall back to opening Details (nothing to build a
    // DecisionPanel from).
    assert.equal(defaultOpenSection({ status }), "details");
    // WITH a blocker, the DecisionPanel above the accordion carries the ask, so
    // the sections stay collapsed instead of dumping the description.
    assert.equal(defaultOpenSection({ status, blocker: { category: "X" } }), null);
  }
  assert.equal(defaultOpenSection({ status: "implementing" }), "system");
  assert.equal(defaultOpenSection(null), null);
});

test("compound_parent opens on Sub-tasks when the decomposition produced any", () => {
  const task = {
    status: "compound_parent",
    context: { decomposition: { subtasks: [{ title: "part 1" }, { title: "part 2" }] } },
  };
  assert.equal(defaultOpenSection(task), "subtasks");
});

test("compound_parent falls back to System when no sub-tasks exist yet (empty System pane bug)", () => {
  assert.equal(defaultOpenSection({ status: "compound_parent" }), "system");
  assert.equal(
    defaultOpenSection({ status: "compound_parent", context: { decomposition: { subtasks: [] } } }),
    "system",
  );
  assert.equal(
    defaultOpenSection({ status: "compound_parent", context: {} }),
    "system",
  );
});

test("colorForStatus covers every stage label and every parked status", () => {
  for (const status of Object.keys(STATUS_STAGE_LABEL)) {
    assert.ok(colorForStatus(status).startsWith("var(--"));
  }
  for (const status of PARKED_STATUSES) {
    assert.ok(colorForStatus(status).startsWith("var(--"));
  }
});

// ── static-source checks: the accordion/spacing/motion contract in the actual
//    component + stylesheet (mirrors the themeVars.test.mjs convention — these
//    properties can't be expressed as pure-function unit tests). ─────────────

const slideOverSrc = readFileSync(join(SRC, "SlideOver.jsx"), "utf8");
const stylesCss = readFileSync(join(SRC, "styles.css"), "utf8");

test("SlideOver renders a narrative summary + chips row (not the old tab strip as landing state)", () => {
  assert.match(slideOverSrc, /narrativeFor/);
  assert.match(slideOverSrc, /chipsFor/);
  assert.match(slideOverSrc, /tabular-nums|so-chip/);
});

test("SlideOver renders one accordion section per surviving tab component, closed by default", () => {
  for (const comp of ["SystemTab", "ActivityTab", "DetailsTab", "SpecTab", "ReviewTab", "DiffTab", "AttemptsTab"]) {
    assert.match(slideOverSrc, new RegExp(`<${comp}\\b`), `${comp} must still be rendered as a section body`);
  }
  // Single-open bookkeeping: exactly one state slot tracks which section is open.
  assert.match(slideOverSrc, /openSection/);
});

test("SlideOver uses defaultOpenSection (gate-aware) rather than always defaulting closed", () => {
  assert.match(slideOverSrc, /defaultOpenSection/);
});

test("the primary pane is a DIGEST (description + artifacts + status), NOT the raw log (redesign redo)", () => {
  // Operator feedback: the first redesign shipped the raw event stream as the
  // always-visible primary — "a ton of logs that is not even collapsible".
  // The redo makes the primary a tessl-style digest (short task description +
  // the artifacts it produced + one status line + the run summary), and DEMOTES
  // the raw event stream into the collapsed "Event log" accordion section.
  assert.match(slideOverSrc, /so-primary-stream/);
  // The primary pane feeds the digest the diff so it can show files-changed.
  assert.match(slideOverSrc, /<ActivityTab taskId=\{taskId\} task=\{task\} isActive=\{isLive\} diff=\{diff\} \/>/);
  // The digest renders the produced artifacts, not a wall of events.
  assert.match(slideOverSrc, /artifactsFor\(task, diff\)/);
  assert.match(slideOverSrc, /run-artifacts/);
  // The raw event stream is now a SEPARATE component, rendered by the accordion
  // (collapsed by default) — NOT in the primary pane.
  assert.match(slideOverSrc, /function ActivityLog\(/);
  assert.match(slideOverSrc, /case "activity":\s*return <ActivityLog/);
  // The old exclusion filter is GONE — "activity" (now labelled Event log) is a
  // normal collapsed accordion section like System/Diff/Review.
  assert.doesNotMatch(slideOverSrc, /\.filter\(\(s\) => s\.key !== "activity"\)/);
  assert.match(slideOverSrc, /\{ key: "activity", label: "Event log" \}/);
});

test("a coarse status chip renders at the top of the drawer summary (redesign)", () => {
  assert.match(slideOverSrc, /coarse-status/);
  assert.match(slideOverSrc, /coarseStatus\(task\)/);
});

test("coarseStatus gives a scannable plain-word label + a semantic color per status", () => {
  const cases = [
    ["implementing", "Working"], ["planning", "Working"], ["testing", "Working"],
    ["awaiting_input", "Needs you"], ["blocked", "Needs you"], ["escalated", "Needs you"],
    ["paused_quota", "Paused"], ["done", "Done"], ["failed", "Failed"],
    ["compound_parent", "Coordinating"],
  ];
  for (const [status, label] of cases) {
    const c = coarseStatus({ status });
    assert.equal(c.label, label, `${status} → ${label}`);
    assert.match(c.colorVar, /^var\(--/, "color is a semantic token, not a literal");
    assert.doesNotMatch(c.label, /_|[A-Z]{2,}/, "label is plain words, never a raw enum");
  }
  // awaiting_approval splits on whether it was already approved
  assert.equal(coarseStatus({ status: "awaiting_approval" }).label, "Ready for you");
  assert.equal(coarseStatus({ status: "awaiting_approval", context: { approved_at: "2026-08-30T00:00:00Z" } }).label, "Merging");
});

test("SpecTab pairs the product understanding with the technical plan (D6/T6)", () => {
  // The enriched acceptance criteria are surfaced as 'What we understood' beside
  // the technical spec 'How we'll build it', so the task view is not a black box
  // that only shows build steps. Both must be present and driven by real data.
  assert.match(slideOverSrc, /what-we-understood/);
  assert.match(slideOverSrc, /What we understood/);
  assert.match(slideOverSrc, /How we&rsquo;ll build it/);
  assert.match(slideOverSrc, /task\.acceptance_criteria/);
});

test("new --sp-1..8 spacing tokens are defined (theme-independent, like the font tokens)", () => {
  const expected = { 1: "4px", 2: "8px", 3: "12px", 4: "16px", 5: "24px", 6: "32px", 7: "48px", 8: "64px" };
  for (const [n, px] of Object.entries(expected)) {
    assert.match(stylesCss, new RegExp(`--sp-${n}\\s*:\\s*${px}`), `--sp-${n} must be ${px}`);
  }
});

test("accordion expand/collapse animates grid-template-rows with the shared timing tokens, exit faster than enter", () => {
  assert.match(stylesCss, /grid-template-rows:\s*0fr/);
  assert.match(stylesCss, /grid-template-rows:\s*1fr/);
  assert.match(stylesCss, /var\(--dur-base\)/);
  assert.match(stylesCss, /var\(--ease-out\)/);
  // exit ~65% of enter duration
  assert.match(stylesCss, /0\.65/);
});

test("SCRUM-80: blocker-history modifier is wired in both the component and the stylesheet", () => {
  assert.match(slideOverSrc, /blocker-history/, "SlideOver.jsx must apply the blocker-history class for terminal tasks");
  const rulesMatch = stylesCss.match(/\.blocker-history[^{]*\{[^}]*\}/g);
  assert.ok(rulesMatch && rulesMatch.length > 0, "styles.css must define .blocker-history rules");
  const rulesText = rulesMatch.join("\n");
  // No new/invented tokens and no hardcoded hex — only the existing neutral
  // tokens already defined in both :root and [data-theme="light"] (checked
  // directly below, against both theme blocks parsed from stylesCss).
  assert.doesNotMatch(rulesText, /#[0-9a-fA-F]{3,8}\b/, ".blocker-history must not hardcode a hex color");
  const usedVars = [...rulesText.matchAll(/var\((--[a-z-]+)\)/g)].map((m) => m[1]);
  assert.ok(usedVars.length > 0, ".blocker-history must reference at least one CSS var");
  const allowed = new Set(["--text-dim", "--text-muted", "--border", "--border-hi", "--text"]);
  for (const v of usedVars) {
    assert.ok(allowed.has(v), `.blocker-history uses an unexpected token ${v} — must reuse an existing neutral token`);
  }
  const rootBlock = stylesCss.match(/:root\s*\{([^}]*)\}/)?.[1] || "";
  const lightBlock = stylesCss.match(/\[data-theme="light"\]\s*\{([^}]*)\}/)?.[1] || "";
  for (const v of allowed) {
    assert.match(rootBlock, new RegExp(`${v}\\s*:`), `${v} must be defined in :root`);
    assert.match(lightBlock, new RegExp(`${v}\\s*:`), `${v} must be defined in [data-theme="light"]`);
  }
});

test("the new motion (accordion + pulse + crossfade) is prefers-reduced-motion guarded", () => {
  const guards = [...stylesCss.matchAll(/@media \(prefers-reduced-motion: reduce\)\s*\{([^]*?)\n\}/g)]
    .map((m) => m[1]).join("\n");
  assert.match(guards, /so-section|so-summary/, "the accordion/summary motion must have a reduced-motion override");
});

// ── SCRUM-16: ACTIVE means a live claimed session ──────────────────────────

test("unclaimed active task narrates as queued, never as an actor working", () => {
  for (const status of ["context", "planning", "implementing", "reviewing", "testing"]) {
    const n = narrativeFor({ status, kind: "feature", attempt_count: 2, claimed: false });
    const text = `${n.before} ${n.phrase}`;
    assert.match(text, /queued/i, `${status} unclaimed must read queued`);
    assert.doesNotMatch(text, /Coder is|reviewer is|planner is|orchestrator is|Tests are/i,
      `${status} unclaimed must not credit a live actor`);
    assert.match(n.phrase, /attempt 2/, "attempt number survives");
  }
});

test("claimed active task keeps today's actor attribution", () => {
  const n = narrativeFor({ status: "implementing", kind: "feature", attempt_count: 1, claimed: true });
  assert.match(`${n.before} ${n.phrase}`, /Coder is implementing/);
  const r = narrativeFor({ status: "reviewing", kind: "feature", claimed: true });
  assert.match(`${r.before} ${r.phrase}`, /reviewer is checking/i);
});

test("parked/terminal narratives are unchanged by the claimed field", () => {
  const e = narrativeFor({ status: "escalated", kind: "feature", claimed: false });
  assert.match(`${e.before} ${e.phrase}`, /waiting for your decision/);
  const d = narrativeFor({ status: "done", kind: "feature", claimed: false });
  assert.ok(d.phrase.length > 0);
});

// ── DEFECT 1: a PASSED review must not read as a failure ───────────────────
// Operator report: a review that PASSED under the severity-class rule (only
// blocking findings fail the gate) with 4 non-blocking findings and 1 passing
// criterion rendered "4 findings · 1/5 passed" — an 80% failure to read, on a
// legitimate PASS. The verdict word comes first and the findings are counted
// as advisory, never as a pass ratio.

test("blocking classification mirrors the backend's ADVISORY_SEVERITIES exactly", () => {
  const REVIEWER_PY = readFileSync(join(SRC, "../../src/no_human/review/reviewer.py"), "utf8");
  const m = REVIEWER_PY.match(/ADVISORY_SEVERITIES\s*=\s*frozenset\(\{([^}]*)\}\)/);
  assert.ok(m, "could not find ADVISORY_SEVERITIES in reviewer.py");
  const backend = new Set(m[1].match(/"([^"]+)"/g).map((s) => s.replace(/"/g, "")));
  assert.deepEqual([...ADVISORY_SEVERITIES].sort(), [...backend].sort());

  assert.equal(isBlockingFinding({ passed: true, severity: "high" }), false, "a PASSING item never blocks");
  for (const sev of backend) {
    assert.equal(isBlockingFinding({ passed: false, severity: sev }), false, `${sev} is advisory`);
    assert.equal(isBlockingFinding({ passed: false, severity: sev.toUpperCase() }), false, "case-insensitive");
  }
  // Unclassified degrades SAFE — blocking, same as the backend.
  assert.equal(isBlockingFinding({ passed: false, severity: "" }), true);
  assert.equal(isBlockingFinding({ passed: false }), true);
  assert.equal(isBlockingFinding({ passed: false, severity: "med" }), true);
  assert.equal(isBlockingFinding({ passed: false, severity: "high" }), true);
});

test("a PASSED review states PASSED first and counts its findings as non-blocking", () => {
  const checklist = {
    passed: true,
    items: [
      { label: "criterion met", passed: true },
      { label: "a", passed: false, severity: "low" },
      { label: "b", passed: false, severity: "low" },
      { label: "c", passed: false, severity: "nit" },
      { label: "d", passed: false, severity: "nit" },
    ],
  };
  const v = reviewVerdict(checklist);
  assert.equal(v.verdict, "PASSED");
  assert.equal(v.tone, "pass");
  assert.equal(v.detail, "4 non-blocking findings (2 low, 2 nit)");
  assert.equal(v.advisory, 4);
  assert.equal(v.blocking, 0);
  // The reported defect, pinned: no pass RATIO anywhere on a passed review.
  const line = `${v.verdict} ${v.detail}`;
  assert.doesNotMatch(line, /\d+\s*\/\s*\d+/, `a passed review must never render a ratio: "${line}"`);
  assert.doesNotMatch(v.detail, /passed/i, "a pass COUNT in the detail would re-create the 1/5 reading");
});

test("a PASSED review with a single finding is singular, and a clean one has no detail", () => {
  const one = reviewVerdict({ passed: true, items: [{ passed: false, severity: "nit" }] });
  assert.equal(one.detail, "1 non-blocking finding (1 nit)");
  const clean = reviewVerdict({ passed: true, items: [{ passed: true }, { passed: true }] });
  assert.equal(clean.verdict, "PASSED");
  assert.equal(clean.detail, null);
});

test("a FAILED review keeps today's failure-first presentation (verdict word, no detail)", () => {
  const v = reviewVerdict({
    passed: false,
    items: [{ passed: false, severity: "high" }, { passed: false, severity: "low" }],
  });
  assert.equal(v.verdict, "FAILED");
  assert.equal(v.tone, "fail");
  assert.equal(v.detail, null, "the failed header is unchanged");
  assert.equal(v.blocking, 1);
});

// ── test result verdict ─────────────────────────────────────────────────────
// orchestrator.py's change-aware gate has two distinct excuse paths that let
// an attempt with a nonzero failed count still open a PR: pre_existing_failures
// (a plain red run whose failing ids already fail on the base tree) and
// invocation_error + reproduces_on_base (the runner itself errored, and the
// same error reproduces on the base tree). Neither is "clean" and neither is
// a genuine "failed" — the card must name the excuse, never say bare "clean".

test("an excused-failure pass (pre_existing_failures) renders excused wording with the test ids, not clean", () => {
  const v = testResultVerdict({
    ran: true, ok: false, passed: 7637, failed: 1, errors: 0, tamper_flag: false,
    failing_tests: ["tests/test_flaky.py::test_x"],
    pre_existing_failures: ["tests/test_flaky.py::test_x"],
  });
  assert.equal(v.tone, "excused");
  assert.notEqual(v.label, "clean");
  assert.match(v.label, /pre-existing/);
  assert.match(v.label, /tests\/test_flaky\.py::test_x/);
});

test("a genuinely clean run still says clean", () => {
  const v = testResultVerdict({
    ran: true, ok: true, passed: 7638, failed: 0, errors: 0, tamper_flag: false,
  });
  assert.equal(v.tone, "clean");
  assert.equal(v.label, "clean");
});

// The second base-tree excuse path (orchestrator.py `_run_attempt`, the
// invocation-error branch): the test runner itself errored, but the SAME
// error reproduces on the base tree, so the orchestrator proceeds without
// failing the attempt. That record carries invocation_error/
// reproduces_on_base instead of pre_existing_failures — it must not fall
// through to the red "failed" bucket.
test("an invocation-error excused by reproduces_on_base renders excused, not the red failed bucket", () => {
  const v = testResultVerdict({
    ran: true, ok: false, passed: 2335, failed: 1, errors: 0, tamper_flag: false,
    invocation_error: true, reproduces_on_base: true,
  });
  assert.equal(v.tone, "excused");
  assert.notEqual(v.tone, "failed");
  assert.notEqual(v.label, "clean");
  assert.match(v.label, /invocation/);
});

test("an invocation error that does NOT reproduce on base is a genuine failure, never clean or excused", () => {
  const v = testResultVerdict({
    ran: true, ok: false, passed: 0, failed: 0, errors: 0, tamper_flag: false,
    invocation_error: true, reproduces_on_base: false,
  });
  assert.equal(v.tone, "failed");
});

test("a genuinely red run with no excuse recorded never renders clean", () => {
  const v = testResultVerdict({
    ran: true, ok: false, passed: 100, failed: 3, errors: 0, tamper_flag: false,
  });
  assert.equal(v.tone, "failed");
  assert.notEqual(v.label, "clean");
});

test("tamper-flagged results render no test-result verdict badge (the tamper banner owns that message)", () => {
  assert.equal(testResultVerdict({ ran: true, ok: false, failed: 1, tamper_flag: true }), null);
});

test("no test_results at all renders no verdict", () => {
  assert.equal(testResultVerdict(null), null);
  assert.equal(testResultVerdict(undefined), null);
});

test("severity chips render the reviewer's grade, and a PASSING row without one omits the chip", () => {
  assert.equal(severityChip({ passed: false, severity: "Low" }), "low");
  assert.equal(severityChip({ passed: false, severity: "nit" }), "nit");
  assert.equal(severityChip({ passed: false, severity: "high" }), "high");
  // A passing criterion carries no grade and needs none.
  assert.equal(severityChip({ passed: true }), null);
  assert.equal(severityChip({ passed: true, severity: "" }), null);
  assert.equal(severityChip(undefined), null);
  assert.equal(severityChip(null), null);
});

// The section header counted an "unrated" bucket for a failing finding the
// reviewer never graded, while the ROW rendered no chip at all — the header
// said "1 unrated" next to a row that showed nothing, and the `.cr-sev-unrated`
// rule in styles.css was reachable from no code path. An ungraded failing
// finding BLOCKS (reviewer.py's degrade-safe rule), so the honest fix is to
// show it, not to stop counting it.
test("an UNGRADED failing finding gets the same 'unrated' chip the header counts", () => {
  assert.equal(severityChip({ passed: false, severity: "" }), "unrated");
  assert.equal(severityChip({ passed: false, severity: "   " }), "unrated");
  assert.equal(severityChip({ passed: false }), "unrated");
  assert.equal(severityChip({}), "unrated");
  // …and the header's bucket name is the SAME token, so the chip class and the
  // count can never drift apart.
  const v = reviewVerdict({
    passed: true,
    items: [{ passed: true }, { passed: false }, { passed: false, severity: "nit" }],
  });
  assert.match(v.detail, /1 nit/);
  assert.match(v.detail, /1 unrated/);
  const buckets = v.detail.match(/\(([^)]*)\)/)[1].split(", ");
  assert.equal(
    buckets.reduce((n, b) => n + Number(b.split(" ")[0]), 0), 2,
    `every failing finding must appear in the breakdown: "${v.detail}"`,
  );
});

// styles.css carried `.cr-sev-med` and `.cr-sev-major` plus a comment claiming
// the reviewer emits "med". It does not: reviewer.py's schema is
// critical|high|medium|low|nit ("med" is history/analyzer.py's unrelated
// importance label). Both selectors were dead paint.
test("the .cr-sev-* rules cover exactly the severities that can reach the chip", () => {
  const css = readFileSync(join(SRC, "styles.css"), "utf8");
  const styled = new Set(
    [...css.matchAll(/\.cr-sev-([a-z0-9-]+)/g)].map((m) => m[1]),
  );
  const reviewerPy = readFileSync(
    join(SRC, "..", "..", "src", "no_human", "review", "reviewer.py"), "utf8",
  );
  // Read the schema line the reviewer actually sends to the model rather than
  // hand-copying the values here.
  //
  // ALL of them: reviewer.py carries THREE copies of this line (three prompts),
  // and a non-global `.match` reads only the first — so the other two could
  // drift to a vocabulary styles.css has no chip for, silently, with this test
  // green. Every copy is checked, and the count is asserted so deleting a
  // prompt (or adding a fourth) is a deliberate edit rather than a quiet loss
  // of coverage.
  const schemas = [...reviewerPy.matchAll(/"severity":\s*"([a-z|]+)"/g)];
  assert.ok(schemas.length > 0, "reviewer.py's severity schema line not found — retarget this test");
  assert.equal(schemas.length, 3,
    `reviewer.py now has ${schemas.length} severity schema lines, not 3 — retarget this test`);
  const vocabularies = schemas.map((m) => m[1].split("|"));
  for (const [i, emitted] of vocabularies.entries()) {
    assert.deepEqual(
      emitted, ["critical", "high", "medium", "low", "nit"],
      `reviewer.py's severity vocabulary changed in schema copy #${i + 1} — ` +
      "the three prompts must agree with each other and with styles.css",
    );
  }
  const emitted = vocabularies[0];
  const expected = new Set([...emitted, "unrated"]);
  assert.deepEqual(
    [...styled].filter((c) => !expected.has(c)), [],
    "dead severity paint: a .cr-sev-* rule no severity can produce",
  );
  assert.deepEqual(
    [...expected].filter((c) => !styled.has(c)), [],
    "an emittable severity with no chip style",
  );
  // Same vocabulary, same source of truth, for the header's sort order.
  const js = readFileSync(join(SRC, "slideOverSummary.js"), "utf8");
  const order = js.match(/SEVERITY_ORDER\s*=\s*\[([^\]]*)\]/);
  assert.ok(order, "SEVERITY_ORDER not found — retarget this test");
  const ranked = [...order[1].matchAll(/"([a-z]+)"/g)].map((m) => m[1]);
  assert.deepEqual(
    ranked.filter((c) => !expected.has(c)), [],
    "SEVERITY_ORDER ranks a value the reviewer cannot emit",
  );
  assert.deepEqual(
    emitted.filter((c) => !ranked.includes(c)), [],
    "an emittable severity the header cannot rank",
  );
});

test("a non-blocking finding row is visually distinct from a blocking failure", () => {
  assert.equal(checklistRowClass({ passed: true }), "pass");
  assert.equal(checklistRowClass({ passed: false, severity: "low" }), "advisory");
  assert.equal(checklistRowClass({ passed: false, severity: "nit" }), "advisory");
  assert.equal(checklistRowClass({ passed: false, severity: "high" }), "fail");
  assert.equal(checklistRowClass({ passed: false }), "fail");
  assert.notEqual(checklistRowClass({ passed: false, severity: "low" }),
    checklistRowClass({ passed: false, severity: "high" }),
    "an advisory row must not reuse the blocking red class");
});

test("the review micro-summary of a PASSED review with findings is not a failure ratio", () => {
  const task = {
    attempts: [{
      review_checklist: {
        passed: true,
        items: [
          { passed: true },
          { passed: false, severity: "low" }, { passed: false, severity: "low" },
          { passed: false, severity: "nit" }, { passed: false, severity: "nit" },
        ],
      },
    }],
  };
  const s = sectionSummary("review", { task });
  assert.doesNotMatch(s.text, /1\/5 passed/, "the reported string must be gone");
  assert.doesNotMatch(s.text, /\d+\s*\/\s*\d+/, `no ratio on a passed review: "${s.text}"`);
  assert.match(s.text, /^Passed/, `verdict first: "${s.text}"`);
  assert.match(s.text, /4 non-blocking findings/);
  assert.equal(s.colorVar, "var(--green)", "a pass is not painted as a failure");
});

test("the micro-summary still leads with the failure when the review FAILED", () => {
  const task = {
    attempts: [{
      review_checklist: {
        passed: false,
        items: [{ passed: true }, { passed: false, severity: "high" }],
      },
    }],
  };
  const s = sectionSummary("review", { task });
  assert.equal(s.colorVar, "var(--red)");
  assert.match(s.text, /1 finding/);
});

// ── DEFECT 2: clicking Approve must be observably confirmed ────────────────
// Operator report: Approve recorded server-side (context.approved_at was set)
// but the UI said nothing, so they believed it had failed.

test("the Approve button changes state the moment it is clicked and once it lands", () => {
  const idle = approveButtonState({});
  assert.equal(idle.label, "Approve and merge");
  assert.equal(idle.disabled, false);

  const busy = approveButtonState({ busy: true });
  assert.equal(busy.disabled, true, "an in-flight approval must not be re-clickable");
  assert.notEqual(busy.label, idle.label, "the label must change while it is in flight");

  const ok = approveButtonState({ outcome: "ok" });
  assert.equal(ok.disabled, true);
  // The label also names what is left to do — the same words the board uses,
  // and the same words a REOPENED drawer shows (see the derivation tests below).
  assert.equal(ok.label, "Approved — merge pending");
  assert.notEqual(ok.label, idle.label);

  const err = approveButtonState({ outcome: "error" });
  assert.equal(err.disabled, false, "a failed approval must be retryable");
  assert.match(err.label, /retry/i);
});

// ── DEFECT 2, second half: the confirmation must survive a drawer close ─────
// `approveOutcome` is session state, reset on every taskId change, and the
// approve endpoint leaves the task in awaiting_approval on the normal PR path
// (api/app.py stamps context.approved_at and stops there). So reopening the
// drawer on an already-approved task showed a plain, enabled "Approve" — the
// exact "did that do anything?" reading this defect was filed for — and a
// second click silently re-stamped approved_at over the real one.

test("an approval is derived from the task payload, not only from session state", () => {
  assert.equal(taskApprovedAt(null), null);
  assert.equal(taskApprovedAt({}), null);
  assert.equal(taskApprovedAt({ context: {} }), null);
  // TaskOut (the drawer's detail payload) carries it under context…
  assert.equal(
    taskApprovedAt({ context: { approved_at: "2026-07-30T10:00:00+00:00" } }),
    "2026-07-30T10:00:00+00:00",
  );
  // …TaskSummaryOut (the board's payload) hoists it to the top level. Both are
  // the same fact; the drawer must not care which shape it was handed.
  assert.equal(taskApprovedAt({ approved_at: "2026-07-30T10:00:00+00:00" }),
    "2026-07-30T10:00:00+00:00");
});

test("a send-back AFTER the approval spends it — the next PR is unapproved", () => {
  // send-back never clears context.approved_at (api/app.py's send_back only
  // appends feedback and resets the status), so a stale stamp would lock the
  // operator out of approving the NEXT attempt's PR.
  const stale = {
    context: {
      approved_at: "2026-07-30T10:00:00+00:00",
      send_back_feedback: [{ at: "2026-07-30T11:00:00+00:00", message: "redo it" }],
    },
  };
  assert.equal(taskApprovedAt(stale), null, "an approval predating a send-back is spent");
  const live = {
    context: {
      approved_at: "2026-07-30T12:00:00+00:00",
      send_back_feedback: [{ at: "2026-07-30T11:00:00+00:00", message: "redo it" }],
    },
  };
  assert.equal(taskApprovedAt(live), "2026-07-30T12:00:00+00:00",
    "an approval AFTER the last send-back still stands");
  // Unparseable timestamps must not silently discard a real approval.
  assert.equal(
    taskApprovedAt({ context: { approved_at: "2026-07-30T12:00:00+00:00", send_back_feedback: [{ at: "??" }] } }),
    "2026-07-30T12:00:00+00:00",
  );
});

test("reopening the drawer on an approved task shows the approved state, not a bare Approve", () => {
  const reopened = approveButtonState({ approvedAt: "2026-07-30T10:00:00+00:00" });
  assert.equal(reopened.disabled, true,
    "a second click would re-stamp approved_at over the operator's real approval");
  assert.equal(reopened.tone, "ok");
  assert.notEqual(reopened.label, "Approve", "a bare 'Approve' reads as nothing having happened");
  // Same words the board already uses for this state (Board.jsx actionHint).
  assert.match(reopened.label, /approved/i);
  assert.match(reopened.label, /merge pending/i);
  // The just-clicked path and the reopened path must not disagree about what
  // an approved task looks like.
  assert.deepEqual(approveButtonState({ outcome: "ok" }), reopened);
  // A task with no approval is untouched.
  assert.equal(approveButtonState({ approvedAt: null }).label, "Approve and merge");
  // An in-flight click still wins over the stale payload it is about to update.
  assert.match(approveButtonState({ busy: true, approvedAt: null }).label, /ing/i);
});

// ── DEAD CONTROL: an enabled "Retry approve" whose handler returns early ────
// `approveButtonState` used to exclude `outcome === "error"` from the approved
// state, while SlideOver's handleApprove returns immediately on `approvedAt`.
// Both conditions hold at once on a path with no server fault in it: the POST
// lands, `outcome` becomes "ok", the refetch in the same `try` fails, the catch
// rewrites `outcome` to "error", and the refresh then delivers `approved_at`.
// The operator is left with an enabled button that does nothing on click —
// the exact class of defect this drawer work exists to remove.
//
// The rule: `approvedAt` is the SERVER's record, `outcome` is this session's
// guess at it, and the server wins.

test("a server-recorded approval outranks a stale client error — no enabled control that cannot fire", () => {
  const stuck = approveButtonState({ outcome: "error", approvedAt: "2026-07-30T10:00:00+00:00" });
  assert.equal(stuck.disabled, true,
    "an enabled button whose handler returns early on approvedAt is a dead control");
  assert.doesNotMatch(stuck.label, /retry/i,
    "offering a retry for an approval the server already recorded is a lie about the state");
  // It is the SAME state a reopened drawer shows, by every field — the local
  // error must not leave a third, half-approved appearance behind.
  assert.deepEqual(stuck, approveButtonState({ approvedAt: "2026-07-30T10:00:00+00:00" }));
});

test("an approval error with NO server record is still fully retryable", () => {
  // The case the old exclusion was aimed at, unchanged: a genuinely refused or
  // dropped POST leaves no approved_at, and the operator must be able to click
  // again. Narrowing this is how the fix above would go wrong.
  const retry = approveButtonState({ outcome: "error", approvedAt: null });
  assert.equal(retry.disabled, false);
  assert.match(retry.label, /retry/i);
  assert.equal(retry.tone, "error");

  // And an approval SPENT by a later send-back is not a record either:
  // taskApprovedAt returns null for it, so the retry survives that path too.
  const spent = taskApprovedAt({
    context: {
      approved_at: "2026-07-30T10:00:00+00:00",
      send_back_feedback: [{ at: "2026-07-30T11:00:00+00:00", message: "redo" }],
    },
  });
  assert.equal(spent, null);
  assert.deepEqual(approveButtonState({ outcome: "error", approvedAt: spent }), retry);
});

test("the board's wording for an approved task is the wording the drawer reuses", () => {
  // The five-facts card rewrite (spec C2) moved this wording from Board.jsx's
  // own actionHint() into cardFacts.js's pure statusLine derivation.
  const cardFacts = readFileSync(join(SRC, "cardFacts.js"), "utf8");
  // `approvalLive(task)`, not a bare `task.approved_at` — the stale-approval
  // fix (approval_superseded_at) moved the guard onto the shared predicate
  // that also checks the status hasn't moved on. See approvalState.js.
  const hint = cardFacts.match(/approvalLive\(task\)\)\s*\{[^}]*?statusLine:\s*"([^"]+)"/);
  assert.ok(hint, "cardFacts.js's approved-task status line not found — retarget this test");
  const { label } = approveButtonState({ approvedAt: "2026-07-30T10:00:00+00:00" });
  assert.equal(label.toLowerCase(), hint[1].toLowerCase(),
    "the drawer must not invent new copy for a state the board already names");
});

test("SlideOver.jsx derives the Approve button from the task, not only from session state", () => {
  const src = readFileSync(join(SRC, "SlideOver.jsx"), "utf8");
  assert.match(src, /approveButtonState\(\{[^}]*approvedAt/,
    "the button must be handed the derived approval");
  assert.match(src, /taskApprovedAt\(/, "the derivation must come from the shared helper");
  // The click guard has to honour the same derivation, or the disabled button's
  // handler would still fire on a keyboard/programmatic activation.
  assert.doesNotMatch(src, /if \(!isAwaiting \|\| busy \|\| approveOutcome === "ok"\) return;/,
    "the re-approval guard cannot read session state alone");
});

test("a recorded approval says so and says who merges - never the agent", () => {
  const f = approvalFeedback({ ok: true });
  assert.equal(f.role, "status");
  assert.equal(f.tone, "ok");
  assert.match(f.text, /approval recorded/i);
  assert.match(f.text, /merge/i);
  assert.match(f.text, /agent never merges/i, "constraint #2 must be visible on the confirmation");
  assert.doesNotMatch(f.text, /—/, "hyphens, not em-dashes, in user-facing strings");
});

// ── operator directive 2026-08-12: approve now MERGES the PR ───────────────

test("test_idle_label_is_approve_and_merge", () => {
  const idle = approveButtonState({});
  assert.equal(idle.label, "Approve and merge");
  assert.equal(idle.disabled, false);
  assert.equal(idle.tone, "idle");
});

test("test_error_label_is_retry_approve_and_merge", () => {
  const retry = approveButtonState({ outcome: "error", approvedAt: null });
  assert.equal(retry.label, "Retry approve and merge");
  assert.equal(retry.disabled, false);
  assert.equal(retry.tone, "error");
});

test("test_success_feedback_mentions_merge", () => {
  const f = approvalFeedback({ ok: true });
  assert.match(f.text, /merge/i);
  assert.match(f.text, /agent never merges/i);
});

test("SlideOver.jsx's approve control tells the operator it merges the PR", () => {
  const src = readFileSync(join(SRC, "SlideOver.jsx"), "utf8");
  const btn = src.match(/btn btn-approve[\s\S]{0,400}?<\/button>/);
  assert.ok(btn, "approve button markup not found");
  assert.match(btn[0], /merge/i,
    "the confirmation/aria copy around the approve button must say it merges the PR");
});

test("the server's own message stays authoritative, with the queue remainder appended", () => {
  const msg = "Already satisfied claim confirmed - no code change was needed.";
  const f = approvalFeedback({ ok: true, message: msg, remaining: 2 });
  assert.ok(f.text.startsWith(msg), `server message must lead: "${f.text}"`);
  assert.match(f.text, /2 more waiting/);
  const alone = approvalFeedback({ ok: true, message: msg, remaining: 0 });
  assert.equal(alone.text, msg, "no queue suffix when nothing is waiting");
});

test("a failed approval is equally visible, and never claims it was recorded", () => {
  const f = approvalFeedback({ ok: false, error: "task is 'done', not awaiting_approval" });
  assert.equal(f.role, "alert");
  assert.equal(f.tone, "error");
  assert.match(f.text, /not (recorded|approved)/i);
  assert.match(f.text, /task is 'done'/, "the server's reason must reach the operator");
  assert.doesNotMatch(f.text, /approval recorded/i);
});

// ── wiring: the drawer must actually render these (behaviour is in e2e/drawer.mjs)

test("SlideOver.jsx renders the review header and the checklist rows from these helpers", () => {
  const src = readFileSync(join(SRC, "SlideOver.jsx"), "utf8");
  assert.match(src, /reviewVerdict/, "the review header must come from reviewVerdict");
  assert.match(src, /checklistRowClass\(/, "row styling must come from checklistRowClass");
  assert.match(src, /severityChip\(/, "each row's chip must come from severityChip");
  assert.match(src, /cr-sev cr-sev-/, "the chip must reuse the existing cr-sev styling");
  assert.doesNotMatch(src, /item\.passed \? "pass" : "fail"/,
    "the hardcoded pass/fail row class cannot survive - advisory rows need their own");
});

test("SlideOver.jsx drives the Approve button and its confirmation from these helpers", () => {
  const src = readFileSync(join(SRC, "SlideOver.jsx"), "utf8");
  assert.match(src, /approveButtonState\(/);
  assert.match(src, /approvalFeedback\(/);
  assert.doesNotMatch(src, /\{busy \? "…" : "Approve"\}/,
    "the bare busy-ellipsis label gave no post-click confirmation");
});

test("the flash banner is a live region so the confirmation is announced, not just painted", () => {
  const src = readFileSync(join(SRC, "SlideOver.jsx"), "utf8");
  const banner = src.match(/function FlashBanner\([\s\S]*?\n\}/);
  assert.ok(banner, "FlashBanner not found");
  assert.match(banner[0], /aria-live/);
  assert.match(banner[0], /role=/);
});

// ── fxCountsLabel: the group-header "N agents · N events" line ─────────────
// 2026-08-12 operator feedback: "under each agent in the ui it says <some
//_number> ev. what is this? it's very unclear." — the count was a cryptic
// "N ev" abbreviation; it must read "N events" (singular "event" for 1),
// matching how "agent"/"agents" already pluralizes on the same line.

test("fxCountsLabel spells out events in full, singular and plural", () => {
  assert.equal(fxCountsLabel(3, 1), "3 agents · 1 event");
  assert.equal(fxCountsLabel(3, 42), "3 agents · 42 events");
  assert.equal(fxCountsLabel(1, 0), "1 agent · 0 events");
});

test("fxCountsLabel never emits the old cryptic 'ev' abbreviation", () => {
  for (const [agents, events] of [[1, 1], [2, 2], [5, 1234]]) {
    const text = fxCountsLabel(agents, events);
    assert.doesNotMatch(text, /\bev\b/, `"${text}" still abbreviates events as "ev"`);
  }
});

test("SlideOver.jsx renders the group header count from fxCountsLabel, not the raw 'ev' abbreviation", () => {
  const src = readFileSync(join(SRC, "SlideOver.jsx"), "utf8");
  assert.match(src, /fxCountsLabel\(g\.agentCount, g\.eventCount\)/,
    "the fx-counts span must be built from fxCountsLabel");
  assert.doesNotMatch(src, /\{g\.eventCount\} ev\b/,
    "the old inline 'N ev' template must be gone");
});

// ── Live merge progress: the button must reflect the running merge ─────────
// Operator finding: a 2-4 minute synchronous land with zero feedback read as
// "doesn't work". `merging`/`elapsedMs`/`step` give approveButtonState a
// state for the whole window, not just click-and-eventual-result.

test("approveButtonState({merging}) shows an elapsed mm:ss timer and is disabled", () => {
  const merging = approveButtonState({ merging: true, elapsedMs: 42000 });
  assert.match(merging.label, /Merging… 0:42/);
  assert.equal(merging.disabled, true);
});

test("approveButtonState merging elapsed timer formats minutes correctly", () => {
  const merging = approveButtonState({ merging: true, elapsedMs: 125000 });
  assert.match(merging.label, /Merging… 2:05/);
});

test("approveButtonState merging never overrides an already-approved outcome", () => {
  const ok = approveButtonState({ merging: true, outcome: "ok" });
  assert.equal(ok.label, "Approved — merge pending");
  assert.equal(ok.disabled, true);

  const reopened = approveButtonState({
    merging: true, approvedAt: "2026-07-30T10:00:00+00:00",
  });
  assert.equal(reopened.label, "Approved — merge pending");
});

test("approveButtonState merging carries the current step through, when known", () => {
  const withStep = approveButtonState({ merging: true, elapsedMs: 1000, step: "push" });
  assert.equal(withStep.step, "push");
  const noStep = approveButtonState({ merging: true, elapsedMs: 1000 });
  assert.equal(noStep.step, null);
});

test("mergeStepLabel extracts the bare step name from a merge_step_<step> event kind", () => {
  assert.equal(mergeStepLabel("merge_step_push"), "push");
  assert.equal(mergeStepLabel("merge_step_verify"), "verify");
  assert.equal(mergeStepLabel("merge_step_close_pr"), "close_pr");
});

test("mergeStepLabel ignores unrelated frame kinds", () => {
  assert.equal(mergeStepLabel("merge_started"), null);
  assert.equal(mergeStepLabel("human_merged"), null);
  assert.equal(mergeStepLabel("task_updated"), null);
  assert.equal(mergeStepLabel(""), null);
  assert.equal(mergeStepLabel(undefined), null);
});

test("landFailureFeedback names the failed step and includes the stderr text", () => {
  const fb = landFailureFeedback({ step: "push", stderr: "remote rejected: non-fast-forward" });
  assert.equal(fb.role, "alert");
  assert.match(fb.text, /Failed: push/);
  assert.match(fb.text, /remote rejected: non-fast-forward/);
  assert.equal(fb.dismissible, true);
});

test("landFailureFeedback truncates stderr to the first 200 characters", () => {
  const long = "x".repeat(500);
  const fb = landFailureFeedback({ step: "verify", stderr: long });
  const shown = fb.text.split(" — ")[1];
  assert.equal(shown.length, 200);
});

test("an infra-stamped paused_quota park reads as a dead session, not quota", () => {
  const task = { status: "paused_quota", kind: "task", blocker: { category: "QUOTA", infra: true } };
  const text = narrativeText(narrativeFor(task));
  assert.match(text, /session died/i);
  assert.doesNotMatch(text, /quota/i);
  const badge = sectionSummary("system", { task });
  assert.match(badge.text, /session died/i);
  assert.doesNotMatch(badge.text, /quota/i);
});
