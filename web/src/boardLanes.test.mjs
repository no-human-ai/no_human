import test from "node:test";
import assert from "node:assert/strict";
import {
  LANES, routeTask, isNeedsYou, isWaiting, waitingTagText, isRealFailure, isSalvaged, BOARD_LANES, OUTCOME_LANES,
  isRunning, isQueued, cardActivity, deriveCounts,
} from "./boardLanes.js";

test("awaiting_approval routes to its OWN 'Review PR' lane, not a catch-all", () => {
  assert.equal(routeTask({ status: "awaiting_approval" }), "review");
});

test("clarification/decision states route to 'Needs Answer', separate from PRs", () => {
  assert.equal(routeTask({ status: "awaiting_input" }), "answer");
  assert.equal(routeTask({ status: "escalated" }), "answer");
});

test("a PR-approval task and a clarification task land in DIFFERENT lanes", () => {
  // The whole point of the split: these must not share a column.
  assert.notEqual(
    routeTask({ status: "awaiting_approval" }),
    routeTask({ status: "awaiting_input" }),
  );
});

test("blocked splits on wake_condition: auto-resolving → Working, else → Needs Answer", () => {
  // No separate Waiting column: a self-resolving blocked task sits in Working,
  // marked parked on the card (isWaiting), not in a mostly-empty column.
  assert.equal(routeTask({ status: "blocked", blocker_wake_condition: "ci_green_on:main" }), "working");
  assert.equal(routeTask({ status: "blocked" }), "answer");
});

test("working / done / failed route as before; paused_quota joins Working", () => {
  assert.equal(routeTask({ status: "implementing" }), "working");
  assert.equal(routeTask({ status: "done" }), "done");
  assert.equal(routeTask({ status: "failed" }), "failed");
  assert.equal(routeTask({ status: "paused_quota" }), "working");
});

test("there is no separate Waiting lane anymore", () => {
  assert.equal(LANES.find((l) => l.key === "waiting"), undefined);
  assert.equal(LANES.length, 5);
});

test("isWaiting flags parked-but-self-resolving tasks (the old Waiting column, on the card)", () => {
  assert.equal(isWaiting({ status: "paused_quota" }), true);
  assert.equal(isWaiting({ status: "blocked", blocker_wake_condition: "ci_green_on:main" }), true);
  assert.equal(isWaiting({ status: "blocked" }), false);         // needs a human → not waiting
  assert.equal(isWaiting({ status: "implementing" }), false);    // actively working
  assert.equal(isWaiting({ status: "done" }), false);
});

test("an unknown status falls back to Working, never lost", () => {
  assert.equal(routeTask({ status: "some_new_state" }), "working");
});

// A crash that salvaged a commit (Scheduler._salvage_committed_work) is still
// terminal and still belongs where a human already looks for "what happened
// to my task" — the Failed lane — not a fourth outcome lane of its own.
test("partial_success (a salvaged post-commit crash) routes to the Failed lane", () => {
  assert.equal(routeTask({ status: "partial_success" }), "failed");
  const failed = LANES.find((l) => l.key === "failed");
  assert.ok(failed.statuses.includes("partial_success"));
});

test("Review PR and Needs Answer are both loud, human-facing lanes", () => {
  const review = LANES.find((l) => l.key === "review");
  const answer = LANES.find((l) => l.key === "answer");
  assert.ok(review.loud && review.needsYou);
  assert.ok(answer.loud && answer.needsYou);
  assert.equal(review.statuses.length, 1);            // only awaiting_approval
  assert.ok(answer.statuses.includes("escalated"));
});

test("Review PR sits immediately before Done and is review-coloured", () => {
  const keys = LANES.map((l) => l.key);
  assert.equal(keys.indexOf("review") + 1, keys.indexOf("done"));  // beside Done
  const review = LANES.find((l) => l.key === "review");
  const working = LANES.find((l) => l.key === "working");
  assert.equal(review.accent, "var(--c-review)");     // its own colour…
  assert.notEqual(review.accent, working.accent);      // …not Working's blue
});

test("Needs Answer (act to unblock) is a different colour from Failed (dead)", () => {
  const answer = LANES.find((l) => l.key === "answer");
  const failed = LANES.find((l) => l.key === "failed");
  assert.notEqual(answer.accent, failed.accent);  // both were identical red before
});

test("isNeedsYou matches the board's lanes exactly (the header-count fix)", () => {
  assert.equal(isNeedsYou({ status: "awaiting_approval" }), true);   // Review PR
  assert.equal(isNeedsYou({ status: "escalated" }), true);          // Needs Answer
  assert.equal(isNeedsYou({ status: "awaiting_input" }), true);     // Needs Answer
  // the case the status-only count missed: blocked WITHOUT a wake condition
  // sits in Needs Answer, so it DOES need you (header said 6, lanes showed 7).
  assert.equal(isNeedsYou({ status: "blocked" }), true);
  // …but blocked WITH a wake condition auto-resolves → Waiting → not "needs you".
  assert.equal(isNeedsYou({ status: "blocked", blocker_wake_condition: "ci_green_on:main" }), false);
  assert.equal(isNeedsYou({ status: "implementing" }), false);
  assert.equal(isNeedsYou({ status: "done" }), false);
  assert.equal(isNeedsYou({ status: "failed" }), false);
});

// A cancelled task ends in FAILED status but is not a capability failure. The board's
// overview strip counted it as one, so "11 failed" was really 1 failure + 10 cancels —
// a permanent red alarm, 91% self-inflicted, competing with the real gates. Stats got
// this right (Stats.jsx) while the board did not; this is the shared definition.
test("isRealFailure excludes operator-cancelled tasks", () => {
  assert.equal(isRealFailure({ status: "failed", cancelled: false }), true);
  assert.equal(isRealFailure({ status: "failed" }), true);
  assert.equal(isRealFailure({ status: "failed", cancelled: true }), false);
  assert.equal(isRealFailure({ status: "done" }), false);
  assert.equal(isRealFailure({ status: "awaiting_input" }), false);
  assert.equal(isRealFailure(null), false);
  assert.equal(isRealFailure(undefined), false);
});

// isRealFailure must not lie about a salvaged crash either: partial_success
// is not "failed" at all, so it was already excluded by the status check —
// this pins that down explicitly, next to isSalvaged, so neither predicate
// can silently start double-counting the other's shape.
test("isRealFailure excludes partial_success; isSalvaged is its own predicate", () => {
  assert.equal(isRealFailure({ status: "partial_success" }), false);
  assert.equal(isSalvaged({ status: "partial_success" }), true);
  assert.equal(isSalvaged({ status: "failed" }), false);
  assert.equal(isSalvaged(null), false);
  assert.equal(isSalvaged(undefined), false);
});

// 5D (operator): the board shows only the three GATE lanes. Done and Failed are outcomes, not
// gates — they leave the board and live behind two buttons that open a table.
test("the board renders exactly three lanes: needs-answer, working, review", () => {
  assert.deepEqual(BOARD_LANES.map((l) => l.key), ["answer", "working", "review"]);
});

test("the outcome lanes are still LANES — dropping them from the board must not drop them from ROUTING", () => {
  // The trap: filter LANES itself and routeTask can no longer place a done/failed task, so it
  // falls through to "working" and finished work reappears as in-flight.
  assert.equal(routeTask({ status: "done" }), "done");
  assert.equal(routeTask({ status: "failed" }), "failed");
  assert.deepEqual(OUTCOME_LANES.map((l) => l.key), ["failed", "done"]);
  // ...and the gate lanes still route as before.
  assert.equal(routeTask({ status: "awaiting_input" }), "answer");
  assert.equal(routeTask({ status: "implementing" }), "working");
  assert.equal(routeTask({ status: "awaiting_approval" }), "review");
});

test("each outcome lane carries the outline colour its button uses", () => {
  const failed = OUTCOME_LANES.find((l) => l.key === "failed");
  const done = OUTCOME_LANES.find((l) => l.key === "done");
  assert.match(failed.accent, /--c-escalated/);   // red
  assert.match(done.accent, /--c-done/);          // green
});

test("B2 #19: an approved PR stops shouting in 'need you' but keeps its lane", () => {
  const approved = { id: "a", status: "awaiting_approval", approved_at: "2026-07-15T00:00:00Z" };
  const unreviewed = { id: "b", status: "awaiting_approval" };

  assert.equal(isNeedsYou(approved), false, "approved must leave the need-you count");
  assert.equal(isNeedsYou(unreviewed), true, "un-reviewed still needs the human");

  // Routing is UNTOUCHED — LANES is also the routing table (the known trap):
  // the approved task stays visible in the Review lane.
  assert.equal(routeTask(approved), routeTask(unreviewed));
});

test("a STALE approval (superseded by a later escalation) shouts again in 'need you'", () => {
  // The bug this ticket fixes: 16 rows carried approved_at while sitting in
  // Needs Answer/Working, and isNeedsYou (like the board) kept reading them
  // as answered because it only checked task.approved_at. `approvalLive`
  // re-checks status too, so a task that has since moved on counts again.
  const staleEscalated = {
    id: "e", status: "escalated",
    approved_at: "2026-07-15T00:00:00Z",
    approval_superseded_at: "2026-07-16T00:00:00Z",
  };
  const staleImplementing = {
    id: "i", status: "implementing",
    approved_at: "2026-07-15T00:00:00Z",
    approval_superseded_at: "2026-07-16T00:00:00Z",
  };
  const liveApproval = {
    id: "a", status: "awaiting_approval",
    approved_at: "2026-07-15T00:00:00Z",
  };

  assert.equal(isNeedsYou(staleEscalated), true, "escalated + stale approval must shout again");
  assert.equal(routeTask(staleEscalated), "answer");
  assert.equal(isNeedsYou(staleImplementing), false, "implementing was never a needs-you lane");
  assert.equal(routeTask(staleImplementing), "working");
  assert.equal(isNeedsYou(liveApproval), false, "a genuinely live approval still stays quiet");
});

test("a human-stopped blocked task stops shouting in 'need you' but keeps its lane", () => {
  const stopped = { id: "s", status: "blocked", blocker_human_stopped: true };
  assert.equal(isNeedsYou(stopped), false, "human already gave their answer: stop, keep parked");
  assert.equal(routeTask(stopped), "answer", "routing is untouched — it stays in Needs Answer");
});

test("NEGATIVE CONTROL: a blocked task WITHOUT the human-stopped flag still needs you", () => {
  const blocked = { id: "b", status: "blocked" };
  assert.equal(isNeedsYou(blocked), true);
  assert.equal(routeTask(blocked), "answer");
});

test("deriveCounts.needsYou drops by exactly one when a single task gains blocker_human_stopped", () => {
  const tasks = [
    { id: "1", status: "blocked" },
    { id: "2", status: "awaiting_input" },
    { id: "3", status: "implementing" },
  ];
  const before = deriveCounts(tasks).needsYou;
  tasks[0] = { ...tasks[0], blocker_human_stopped: true };
  const after = deriveCounts(tasks).needsYou;
  assert.equal(after, before - 1);
});

// SCRUM-15: claimed=true from the scheduler's in-flight set is the ONLY signal
// for "actually running" — an active status alone must never read as running.
test("isRunning is true only when claimed, regardless of status", () => {
  assert.equal(isRunning({ status: "implementing", claimed: true }), true);
  assert.equal(isRunning({ status: "implementing", claimed: false }), false);
  assert.equal(isRunning({ status: "implementing" }), false);
  assert.equal(isRunning({ status: "compound_parent", claimed: true }), true);
});

test("isQueued is a Working-lane task that is not claimed and not waiting", () => {
  assert.equal(isQueued({ status: "implementing", claimed: false }), true);
  assert.equal(isQueued({ status: "implementing", claimed: true }), false, "claimed → running, not queued");
  assert.equal(isQueued({ status: "paused_quota", claimed: false }), false, "waiting has its own state");
  assert.equal(isQueued({ status: "done", claimed: false }), false, "not routed to working");
  assert.equal(isQueued({ status: "compound_parent", claimed: false }), true);
});

test("cardActivity: running gets the pulse; queued/waiting/idle never do", () => {
  assert.deepEqual(
    cardActivity({ status: "implementing", claimed: true }),
    { mode: "running", showPulse: true, showQueuedChip: false, mutedProgress: false },
  );
  assert.deepEqual(
    cardActivity({ status: "implementing", claimed: false }),
    { mode: "queued", showPulse: false, showQueuedChip: true, mutedProgress: true },
  );
  assert.deepEqual(
    cardActivity({ status: "paused_quota", claimed: false }),
    { mode: "waiting", showPulse: false, showQueuedChip: false, mutedProgress: true },
  );
  // A pending task sits in the Working lane awaiting a slot — that IS queued
  // (the whole point of this ticket); idle is for terminal/gate states only.
  assert.deepEqual(
    cardActivity({ status: "pending", claimed: false }),
    { mode: "queued", showPulse: false, showQueuedChip: true, mutedProgress: true },
  );
  assert.deepEqual(
    cardActivity({ status: "done", claimed: false }),
    { mode: "idle", showPulse: false, showQueuedChip: false, mutedProgress: false },
  );
  // The exact bug: a queued (unclaimed active) task must never carry the pulse.
  for (const t of [
    { status: "implementing", claimed: false },
    { status: "paused_quota", claimed: false },
    { status: "pending", claimed: false },
  ]) {
    assert.equal(cardActivity(t).showPulse, false);
  }
  // compound_parent must never read as running unless actually claimed.
  assert.equal(cardActivity({ status: "compound_parent", claimed: false }).mode, "queued");
  assert.equal(cardActivity({ status: "compound_parent", claimed: true }).mode, "running");
});

test("deriveCounts: one definition — strip/lane/sidebar cannot disagree", () => {
  const tasks = [
    { id: "1", status: "implementing", claimed: true },   // running
    { id: "2", status: "implementing", claimed: false },  // queued
    { id: "3", status: "pending", claimed: false },        // queued
    { id: "4", status: "reviewing", claimed: false },      // queued
    { id: "5", status: "testing", claimed: false },        // queued
    { id: "6", status: "planning", claimed: false },       // queued
    { id: "7", status: "context", claimed: false },        // queued
    { id: "8", status: "compound_parent", claimed: false },// queued
    { id: "9", status: "awaiting_approval" },              // needs-you
    { id: "10", status: "escalated" },                     // needs-you
    { id: "11", status: "paused_quota" },                  // waiting
  ];
  const counts = deriveCounts(tasks);
  assert.deepEqual(counts, { running: 1, queued: 7, waiting: 1, working: 9, needsYou: 2 });
  assert.equal(counts.running, tasks.filter(isRunning).length);
  assert.equal(counts.working, tasks.filter((t) => routeTask(t) === "working").length);
  assert.equal(counts.working, counts.running + counts.queued + counts.waiting);
});

test("deriveCounts defaults safely on non-array input", () => {
  assert.deepEqual(deriveCounts(null), { running: 0, queued: 0, waiting: 0, working: 0, needsYou: 0 });
  assert.deepEqual(deriveCounts(undefined), { running: 0, queued: 0, waiting: 0, working: 0, needsYou: 0 });
});

test("the card's waiting tag says the session died for an infra-stamped park, quota otherwise", () => {
  assert.equal(waitingTagText({ status: "paused_quota", blocker: { category: "QUOTA" } }), "waits for quota");
  const dead = waitingTagText({ status: "paused_quota", blocker: { category: "QUOTA", infra: true } });
  assert.match(dead, /session died/i);
  assert.doesNotMatch(dead, /quota/i);
  assert.equal(waitingTagText({ status: "blocked", blocker_wake_condition: "after:30m" }), "waits for its own signal");
});
