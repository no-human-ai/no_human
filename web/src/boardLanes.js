// Lanes are organized by WHAT ACTION THE HUMAN NEEDS TO TAKE:
//   Needs Answer — awaiting_input / escalated / blocked-without-wake: the agent
//                  needs a human decision, answer, or clarification to proceed
//   Working      — agent is in-flight OR parked-but-self-resolving (blocked with
//                  a wake condition, paused_quota). The old separate "Waiting"
//                  lane held only auto-resolving tasks and was empty most of the
//                  time — dead horizontal space. That "wakes itself, no human
//                  needed" distinction now lives on the CARD (isWaiting →
//                  "◷ waits for its own signal"), not a whole column.
//   Review PR    — awaiting_approval: a PR is up, review and approve/merge
//   Failed       — terminal
//   Done         — completed
//
// Pure so the routing is node --test'd. Board.jsx consumes LANES + routeTask.
// Order is left→right by narrative: attention-now → in-flight → outcomes.
// "Review PR" is the last POSITIVE step before Done (approve → merge → done),
// so it sits right beside Done and is coloured with the semantic review purple
// (--c-review) — NOT the blue of Working.
import { approvalLive } from "./approvalState.js";

export const LANES = [
  { key: "answer",  label: "Needs Answer", accent: "var(--c-answer)",    statuses: ["awaiting_input", "escalated"], loud: true, needsYou: true, staleCollapse: true, emptyIcon: "✓", emptyHint: "All caught up — nothing needs your input" },
  { key: "working", label: "Working",      accent: "var(--c-building)",  statuses: ["pending", "context", "planning", "implementing", "reviewing", "testing", "compound_parent", "paused_quota"], emptyIcon: "○", emptyHint: "No tasks in flight" },
  // partial_success: a crash stranded a real commit after it landed but
  // before a PR existed (core/lanes.py LANE_STATUSES carries the matching
  // comment). It is terminal and reads on the board exactly where a human
  // already looks for "what happened to my task" — the Failed lane — not a
  // fourth outcome lane. isRealFailure/isSalvaged below tell the two apart.
  { key: "failed",  label: "Failed",       accent: "var(--c-escalated)", statuses: ["failed", "partial_success"], outcome: true, emptyIcon: "○", emptyHint: "No failures" },
  { key: "review",  label: "Review PR",    accent: "var(--c-review)",    statuses: ["awaiting_approval"], loud: true, needsYou: true, emptyIcon: "○", emptyHint: "No PRs waiting for review" },
  { key: "done",    label: "Done",         accent: "var(--c-done)",      statuses: ["done"], outcome: true, emptyIcon: "○", emptyHint: "Nothing shipped yet" },
];

// 5D: the board shows only the lanes that represent a GATE — something the human owes. Done and
// Failed are OUTCOMES: they competed for width with the three lanes that actually need attention,
// and an outcome list reads better as a sortable table than as a column of cards. They move to two
// buttons above the connection indicator, which open that table.
//
// They stay in LANES on purpose: LANES is also the ROUTING table (routeTask iterates it), so
// filtering it here would make a done task fall through to "working" — finished work would come
// back as in-flight.
export const BOARD_LANES = LANES.filter((l) => !l.outcome);
export const OUTCOME_LANES = LANES.filter((l) => l.outcome);

const LANE_KEYS = new Set(LANES.map((l) => l.key));

// The lane DECISION now lives server-side, in src/no_human/core/lanes.py, and
// arrives on the task payload as `lane`. This file keeps the PRESENTATION half
// (labels, order, colours) and prefers what the server said, so the board and a
// CLI reading the same API cannot disagree — the drift this repo already
// shipped once (PR-007: correct counts, lying lane label).
//
// computeLane stays as the fallback for a payload with no `lane` field: an
// older server, or a summary built outside the board path (POST /api/tasks).
// Deleting it is what would make this unsafe to deploy.
// testdata/lane_conformance.json runs the same cases through both.
export function routeTask(task) {
  const served = task?.lane;
  if (typeof served === "string" && LANE_KEYS.has(served)) return served;
  return computeLane(task);
}

// "blocked" routes dynamically: WITH a wake_condition it self-resolves → Working
// (shown as parked on the card); WITHOUT, a human must act → Needs Answer.
//
// The split is on TRUTHINESS, deliberately, so "" routes like absent. The two
// languages' falsy sets are NOT the same: [] and {} are truthy here and FALSY in
// core/lanes.py, so a wake condition of [] would route to Working here and to
// Needs Answer there. Unreachable today — the field is typed
// `blocker_wake_condition: str | None` on TaskSummaryOut and pydantic rejects a
// list or a dict before routing sees it, which is why the shared fixture carries
// no such case. Widen that field and the two implementations diverge; add the
// case to testdata/lane_conformance.json if you ever do.
export function computeLane(task) {
  if (task?.status === "blocked") {
    return task.blocker_wake_condition ? "working" : "answer";
  }
  for (const lane of LANES) {
    if (lane.statuses.includes(task?.status)) return lane.key;
  }
  return "working";
}

// A task sitting in Working that is parked on its own signal (not actively being
// processed) — so the card can say "waits for its own signal" instead of looking
// like live work. This is the distinction the old Waiting column carried.
export function isWaiting(task) {
  return (
    task?.status === "paused_quota" ||
    (task?.status === "blocked" && !!task.blocker_wake_condition)
  );
}

// The card's waiting tag. A paused_quota park stamped `blocker.infra` came
// from a dead agent session, not a wall (core/orchestrator.py _park_quota):
// nothing about quota is true of it, so the card must not say quota.
export function waitingTagText(task) {
  if (task?.status !== "paused_quota") return "waits for its own signal";
  if (task?.blocker && task.blocker.infra === true) return "waits to retry — session died";
  return "waits for quota";
}

const NEEDS_YOU_LANES = new Set(LANES.filter((l) => l.needsYou).map((l) => l.key));

// SINGLE source of truth for "this task needs a human" — the same routing the
// board uses. A status-only set drifted from the lanes (blocked-without-wake
// sits in Needs Answer but a status set missed it, so the header said "6 need
// you" while the lanes showed 7). Count, badge, and notifications all use this.
//
// BLAST RADIUS, now that routeTask prefers a SERVED lane: isNeedsYou routes
// through routeTask, so a wrong `lane` on the payload can SILENCE "N need you"
// for a PR genuinely waiting on a human — the count, the badge, and the
// notification all go quiet together. That was impossible while routing was
// purely local: the worst a bad server field could do was mislabel a column.
// The design is still right (one definition beats two), but the server value is
// now load-bearing for an alert, not just for a label — which is why the
// conformance fixture and the bogus-lane fallback in routeTask exist, and why
// TaskSummaryOut.lane defaults to None rather than to any real lane key.
export function isNeedsYou(task) {
  // B2 #19: an APPROVED PR still sits in awaiting_approval until the merge
  // lands — it is not waiting on you any more, so it must stop shouting in
  // "N need you". It STAYS in the Review lane (routing is untouched: LANES is
  // also the routing table) but renders as "approved — merge pending".
  // `approvalLive`, not a bare `task.approved_at`: a STALE approval —
  // superseded by a later escalation/send-back/attempt — must NOT silence a
  // task that has since moved into a genuinely needs-you lane (that was the
  // bug this predicate exists to close: 16 rows counted as answered while
  // sitting in Needs Answer).
  if (approvalLive(task)) return false;
  // A human who explicitly stopped a parked task already gave their answer
  // (blocker.human_stopped, flattened as blocker_human_stopped). Stop it
  // shouting in "N need you", but keep its lane — routing untouched, exactly
  // like the approved_at guard above.
  if (task?.blocker_human_stopped) return false;
  return NEEDS_YOU_LANES.has(routeTask(task));
}

// A cancelled task ends in FAILED status but is NOT a capability failure. The board's
// overview strip counted every `failed` row, so a board with 1 real failure and 10
// operator-cancelled tasks shouted "11 failed" — a permanent red alarm competing with
// the actual gates. Stats already excluded cancels; this is now the one definition both
// surfaces share.
export function isRealFailure(task) {
  return Boolean(task) && task.status === "failed" && !task.cancelled;
}

// A partial_success task is not a bare failure and not a cancel: the
// scheduler's pool-crash handler found a real commit on a real branch before
// it marked the task, and recorded exactly that (top-level `salvaged_branch`/
// `salvaged_commit_sha` on the served payload — see api/models.py). Kept
// distinct from isRealFailure so neither predicate has to lie about which
// shape it is describing.
export function isSalvaged(task) {
  return Boolean(task) && task.status === "partial_success";
}

// SCRUM-15: the scheduler's in-flight set is the ONLY thing that means "the
// agent is actually working on this right now" — an active-status task the
// scheduler hasn't picked up yet is queued, not running, no matter how it looks.
export function isRunning(task) {
  return task?.claimed === true;
}

// Queued = routed to Working, but not actually claimed, and not parked on its
// own signal (that's `waiting`, a distinct, non-active state with its own tag).
export function isQueued(task) {
  return routeTask(task) === "working" && !isRunning(task) && !isWaiting(task);
}

// Card treatment decision, extracted so JSX just consumes it instead of
// re-deriving "is this active" from raw status (the bug this ticket fixes).
export function cardActivity(task) {
  if (isRunning(task)) return { mode: "running", showPulse: true, showQueuedChip: false, mutedProgress: false };
  if (isQueued(task)) return { mode: "queued", showPulse: false, showQueuedChip: true, mutedProgress: true };
  if (isWaiting(task)) return { mode: "waiting", showPulse: false, showQueuedChip: false, mutedProgress: true };
  return { mode: "idle", showPulse: false, showQueuedChip: false, mutedProgress: false };
}

// SINGLE source of truth for the top strip / lane headers / sidebar counts —
// they must all read from here so they cannot disagree (the bug this ticket
// fixes: three surfaces derived "working" from three different vocabularies).
export function deriveCounts(tasks) {
  const t = Array.isArray(tasks) ? tasks : [];
  const running = t.filter(isRunning).length;
  const queued = t.filter(isQueued).length;
  const waiting = t.filter(isWaiting).length;
  return {
    running,
    queued,
    waiting,
    working: running + queued + waiting, // == Working-lane header, by construction
    needsYou: t.filter(isNeedsYou).length,
  };
}
