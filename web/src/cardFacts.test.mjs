import test from "node:test";
import assert from "node:assert/strict";
import { cardFacts } from "./cardFacts.js";

// `cost` is a caller-computed dollar number (TaskSummaryOut has no cost_usd —
// see cardFacts.js's header comment), not a task field.

test("awaiting_input yields an Answer question action and the blocker as status line", () => {
  const f = cardFacts(
    { title: "T", status: "awaiting_input", blocker_question: "Which repo?", repo_name: "app", attempt_count: 2 },
    { cost: 1.234, authMode: "api_key" },
  );
  assert.equal(f.statusLine, "Which repo?");
  assert.deepEqual(f.action, { label: "Answer question", kind: "answer" });
  assert.equal(f.metaLine, "app · att 2 · $1.23");
});

test("awaiting_approval yields Review PR", () => {
  const f = cardFacts({ title: "T", status: "awaiting_approval", pr_url: "https://x/pr/1", repo_name: "app" });
  assert.deepEqual(f.action, { label: "Review PR", kind: "review" });
});

test("working shows the live status and no action", () => {
  const f = cardFacts({ title: "T", status: "implementing", live_status: "running: pytest", repo_name: "app" });
  assert.equal(f.statusLine, "running: pytest");
  assert.equal(f.action, null);
});

test("meta line omits absent parts", () => {
  assert.equal(cardFacts({ title: "T", status: "pending" }).metaLine, "");
});

test("escalated yields Advise or split, and reshapes a budget-exhausted question the same way as awaiting_input", () => {
  const f = cardFacts({
    title: "T", status: "escalated",
    blocker_question: "This task has exhausted its lifetime budget (tokens 15,324,491/12,000,000). Spend more, or stop here?",
  });
  assert.deepEqual(f.action, { label: "Advise or split", kind: "answer" });
  assert.equal(f.statusLine, "Budget: 15.32M of 12.00M tokens. Spend more, or stop here?");
});

test("a blocked task WITH a wake condition self-resolves — no action, no blocker text", () => {
  const f = cardFacts({ title: "T", status: "blocked", blocker_question: "irrelevant", blocker_wake_condition: "quota resets" });
  assert.equal(f.action, null);
  assert.equal(f.statusLine, "");
});

test("a blocked task with NO wake condition needs a human, same as awaiting_input", () => {
  const f = cardFacts({ title: "T", status: "blocked", blocker_question: "Which repo?" });
  assert.deepEqual(f.action, { label: "Answer question", kind: "answer" });
  assert.equal(f.statusLine, "Which repo?");
});

test("cancelled, approved and human-stopped end the story regardless of status, with no action", () => {
  assert.equal(cardFacts({ title: "T", status: "failed", cancelled: true }).statusLine, "Cancelled");
  assert.equal(cardFacts({ title: "T", status: "awaiting_approval", approved_at: "2026-08-01T00:00:00Z" }).statusLine,
    "Approved — merge pending");
  assert.equal(cardFacts({ title: "T", status: "escalated", blocker_human_stopped: true }).statusLine,
    "Stopped by you — parked");
  for (const t of [
    { title: "T", status: "failed", cancelled: true },
    { title: "T", status: "awaiting_approval", approved_at: "2026-08-01T00:00:00Z" },
    { title: "T", status: "escalated", blocker_human_stopped: true },
  ]) {
    assert.equal(cardFacts(t).action, null);
  }
});

test("a bounded merge-conflict rebase round leads the status line instead of a chip", () => {
  const f = cardFacts({ title: "T", status: "implementing", pr_conflict_rounds: 2, max_pr_conflict_rounds: 3 });
  assert.equal(f.statusLine, "resolving merge conflict — round 2/3");
  assert.equal(f.action, null);
});

test("title falls back from title_short to title", () => {
  assert.equal(cardFacts({ title: "Long title", status: "pending" }).title, "Long title");
  assert.equal(cardFacts({ title: "Long title", title_short: "Short", status: "pending" }).title, "Short");
});

// The bug this ticket fixes: 16 rows carried approved_at while sitting in a
// status other than awaiting_approval (failed/escalated/implementing), and
// the card kept saying "Approved — merge pending". A STALE approval
// (approval_superseded_at stamped, or the status has simply moved on) must
// fall through to the real status line/action instead.
test("an escalated task with a stale (superseded) approval shows the escalation, not the stale chip", () => {
  const f = cardFacts({
    title: "T", status: "escalated",
    approved_at: "2026-08-01T00:00:00Z",
    approval_superseded_at: "2026-08-02T00:00:00Z",
    blocker_question: "Which branch?",
  });
  assert.notEqual(f.statusLine, "Approved — merge pending");
  assert.equal(f.statusLine, "Which branch?");
  assert.deepEqual(f.action, { label: "Advise or split", kind: "answer" });
});

test("an implementing task with a stale approval shows the working state, not the stale chip", () => {
  const f = cardFacts({
    title: "T", status: "implementing", live_status: "running: pytest",
    approved_at: "2026-08-01T00:00:00Z",
    approval_superseded_at: "2026-08-02T00:00:00Z",
  });
  assert.notEqual(f.statusLine, "Approved — merge pending");
  assert.equal(f.statusLine, "running: pytest");
  assert.equal(f.action, null);
});

test("awaiting_approval with a LIVE approval (no supersession stamp) still shows the chip", () => {
  const f = cardFacts({ title: "T", status: "awaiting_approval", approved_at: "2026-08-01T00:00:00Z" });
  assert.equal(f.statusLine, "Approved — merge pending");
  assert.equal(f.action, null);
});

test("awaiting_approval with approved_at but ALSO superseded (contradictory shape) falls through to Review PR", () => {
  // Shouldn't occur via the real write path (supersession only fires when a
  // row LEAVES awaiting_approval), but the predicate is belt-and-suspenders:
  // it re-checks status directly rather than trusting either stamp alone.
  const f = cardFacts({
    title: "T", status: "awaiting_approval",
    approved_at: "2026-08-01T00:00:00Z",
    approval_superseded_at: "2026-08-02T00:00:00Z",
  });
  assert.notEqual(f.statusLine, "Approved — merge pending");
  assert.deepEqual(f.action, { label: "Review PR", kind: "review" });
});

test("the dual context.* payload shape also suppresses the stale chip", () => {
  const f = cardFacts({
    title: "T", status: "failed",
    context: { approved_at: "2026-08-01T00:00:00Z", approval_superseded_at: "2026-08-02T00:00:00Z" },
  });
  assert.notEqual(f.statusLine, "Approved — merge pending");
});
