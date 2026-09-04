import { useState, useRef, useEffect } from "react";
import SlideOver from "./SlideOver.jsx";
import { BOARD_LANES, routeTask, isWaiting, cardActivity, waitingTagText } from "./boardLanes.js";
import { taskProgress } from "./taskProgress.js";
import { topPrioritised } from "./laneView.js";
import { partitionAnswerLane, shouldResetStaleOpen } from "./answerLane.js";
import { httpPrUrl } from "./prUrl.js";
import { mergeReadyChip } from "./slideOverSummary.js";
import {
  approveRefusalToast, setApproveError, dismissApproveError, pruneApproveErrors,
} from "./approveRefusal.js";
import { cardTitle } from "./cardTitle.js";
import { cardFacts } from "./cardFacts.js";
import { taskCost } from "./cost.js";
import { isFirstRun } from "./boardFirstRun.js";
import { timestampMs, compareAsc } from "./parseTimestamp.js";
import { elapsedChip } from "./cardElapsed.js";

// Toast lifetime — long enough to read a refusal sentence, short enough not
// to pile up. The persistent source of truth is the card banner (dismissed
// explicitly, or cleared once the task leaves awaiting_approval); the toast
// is just the "notice me now" nudge.
const APPROVE_TOAST_MS = 8_000;

// 5B: how many cards a collapsible lane shows before the expand arrow. 4 keeps
// every lane visible without vertical scroll on a typical viewport; the count
// badge still shows the true total, so nothing is hidden from awareness.
const LANE_TOP_N = 4;

// Board elapsed-chip tick — one shared interval for the whole board, not one
// per card. 60s is fine granularity for an "1h 42m" readout that only
// matters at the minute scale.
const ELAPSED_TICK_MS = 60_000;

export default function Board({ tasks, pendingOpenId, onPendingOpenHandled, tasksLoaded = true, outcomeCount = 0, onNewTask, onFollowUp = null }) {
  const [selectedId, setSelectedId] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const triggerRef = useRef(null);
  const prevUpdatedAtRef = useRef(null);
  // Per-task approve refusal state — the board's approve button used to fail
  // silently (task e24cee25/PR #643: an ancestry refusal reached the operator
  // nowhere at all). This is the persistent half; the toast below is the
  // "notice me now" half. Both read the same classified `{cause, text}`.
  const [approveErrors, setApproveErrors] = useState({});
  const [toast, setToast] = useState(null);

  const handleApproveRefused = (id, cls) => {
    setApproveErrors((m) => setApproveError(m, id, cls));
    setToast(approveRefusalToast(id, cls));
  };

  useEffect(() => {
    if (!toast) return undefined;
    const t = setTimeout(() => setToast((cur) => (cur?.id === toast.id ? null : cur)), APPROVE_TOAST_MS);
    return () => clearTimeout(t);
  }, [toast]);

  // Client-side tick for the elapsed-time chip - no server polling change.
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), ELAPSED_TICK_MS);
    return () => clearInterval(id);
  }, []);

  // A refusal error is stale once its task has left awaiting_approval — the
  // approve either landed on a retry or the task moved on some other way.
  useEffect(() => {
    setApproveErrors((m) => pruneApproveErrors(m, tasks));
  }, [tasks]);

  // Re-fetch the SlideOver whenever the selected task's updated_at changes via WS
  useEffect(() => {
    if (!selectedId) return;
    const selected = tasks.find((t) => t.id === selectedId);
    const stamp = selected?.updated_at ?? null;
    if (stamp && stamp !== prevUpdatedAtRef.current) {
      prevUpdatedAtRef.current = stamp;
      setRefreshKey((k) => k + 1);
    }
  }, [tasks, selectedId]);

  function openTask(id, domNode) {
    triggerRef.current = domNode;
    prevUpdatedAtRef.current = null; // reset so first open always fetches
    setSelectedId(id);
  }

  // A clicked notification lands here even when Board wasn't mounted at
  // click time: App parks the task id in state; this opens it on mount (or
  // immediately when already mounted) and hands the id back as consumed.
  useEffect(() => {
    if (pendingOpenId) {
      openTask(pendingOpenId, null);
      onPendingOpenHandled?.();
    }
  }, [pendingOpenId]);

  function closeTask() {
    setSelectedId(null);
    prevUpdatedAtRef.current = null;
    // Restore focus to the card that triggered the panel
    triggerRef.current?.focus();
    triggerRef.current = null;
  }

  // C6: an empty board (no lanes, no done, no failed) is a dead end, not a start —
  // show the loop and the one button in, instead of three empty lanes. Gated on
  // tasksLoaded (App only flips it once a real fetch/snapshot has landed) so a
  // board still loading its first paint never flashes this over real data — an
  // empty-lane view for a moment is the safe failure, not a false first-run.
  if (tasksLoaded && isFirstRun(tasks, outcomeCount)) {
    return (
      <section className="board-first-run" aria-labelledby="first-run-title">
        <h2 id="first-run-title">Your first task</h2>
        <p>Describe a change. no_human plans it, writes the code, runs your tests, has a second model try to refute it, and opens a pull request with the evidence. You approve.</p>
        <button type="button" className="btn btn-new-task" onClick={onNewTask}>New task</button>
      </section>
    );
  }

  return (
    <>
      <div className="nh-board">
        {/* 5D: only the GATE lanes. Done and Failed are outcomes — they live behind the two
            buttons above the connection indicator, which open the task table. */}
        {BOARD_LANES.map((lane) => (
          <Lane
            key={lane.key}
            lane={lane}
            tasks={tasks.filter((t) => routeTask(t) === lane.key)}
            onSelect={openTask}
            approveErrors={approveErrors}
            onDismissApproveError={(id) => setApproveErrors((m) => dismissApproveError(m, id))}
            nowMs={nowMs}
          />
        ))}
      </div>
      {selectedId && (
        <SlideOver
          // Remount on a task switch. Without this the drawer keeps the PREVIOUS task's
          // `task`/`diff` state while already bound to the new id, so "Next review →" showed
          // task A's header, id and diff with an enabled Approve that posted against B.
          // (A key change only fires on selectedId — a refreshKey bump still re-renders in
          // place, so a WS update never flashes an empty drawer.)
          key={selectedId}
          taskId={selectedId}
          onClose={closeTask}
          refreshKey={refreshKey}
          reviewQueue={tasks
            .filter((t) => t.status === "awaiting_approval")
            // Ascending (oldest first) through timestampMs, not a raw string
            // compare: `created_at` mixes naive-space and iso-offset formats
            // from the DB, and ' ' < 'T' lexically, so localeCompare always
            // ranked a naive-space row as older than an iso-offset row from
            // the same date regardless of actual age.
            .sort((a, b) => compareAsc(timestampMs(a.created_at), timestampMs(b.created_at)))
            .map((t) => t.id)}
          onJump={(id) => {
            prevUpdatedAtRef.current = null;
            setSelectedId(id);
          }}
          onApproveRefused={handleApproveRefused}
          onFollowUp={onFollowUp}
        />
      )}
      {toast && (
        <div className="nh-toast nh-toast-error" role="alert">
          <span className="nh-toast-text">{toast.text}</span>
          <button
            type="button"
            className="nh-toast-dismiss"
            aria-label="Dismiss notification"
            onClick={() => setToast(null)}
          >×</button>
        </div>
      )}
    </>
  );
}

// SCRUM-15: the Working lane header must agree with the card treatment below
// it — "N live · M queued" only ever comes from cardActivity's own mode, so
// the header cannot drift from what the cards actually render.
function workingBreakdown(tasks) {
  let running = 0;
  let queued = 0;
  for (const task of tasks) {
    const mode = cardActivity(task).mode;
    if (mode === "running") running += 1;
    else if (mode === "queued") queued += 1;
  }
  if (queued === 0) return String(tasks.length);
  // Review 2026-07-25: waiting tasks (paused_quota etc.) are neither running
  // nor queued — omitting them made the header count fewer tasks than the
  // cards below it whenever a queue formed.
  const waiting = tasks.length - running - queued;
  const base = `${running} live · ${queued} queued`;
  return waiting > 0 ? `${base} · ${waiting} waiting` : base;
}

function Lane({ lane, tasks, onSelect, approveErrors, onDismissApproveError, nowMs }) {
  const [expanded, setExpanded] = useState(false);
  // SCRUM-19: the Needs-Answer lane's OWN collapse — stale escalations (>24h,
  // by escalation recency) sink behind an expandable divider instead of
  // burying tonight's real ones. Independent of `collapsible` below: this
  // lane still shows every fresh card, never hides one behind a top-N arrow.
  const [staleOpen, setStaleOpen] = useState(false);
  // Human-gate lanes (Needs Answer, Review PR) NEVER collapse — every task there
  // needs action; hiding one behind an arrow defeats the board. Only the
  // in-flight/outcome lanes (Working, Failed, Done), which grow unbounded, do.
  const collapsible = !lane.needsYou;
  // 5D: the Failed lane left the board, and with it the same-title collapse — that graveyard
  // problem now lives on the Failed OUTCOME table, which is where the failures are.
  const rows = tasks;

  // Drop a stale expand once the lane fits within top-N: the Working lane churns
  // (tasks drain then a new batch arrives), and a lingering expanded=true would
  // re-inflate the re-grown lane the user never expanded. Resetting while it fits
  // is invisible (all rows already show) and re-growth then starts collapsed.
  useEffect(() => {
    if (expanded && rows.length <= LANE_TOP_N) setExpanded(false);
  }, [expanded, rows.length]);

  let visible = rows;
  let hiddenCount = 0;
  if (collapsible) {
    const r = topPrioritised(rows, expanded ? rows.length : LANE_TOP_N);
    visible = r.visible;
    hiddenCount = r.hiddenCount;
  }
  const showToggle =
    collapsible && (hiddenCount > 0 || (expanded && rows.length > LANE_TOP_N));

  // Never dismisses or filters anything — fresh.length + stale.length === rows.length,
  // same truthful lane-count badge below (tasks.length, unchanged).
  const { fresh, stale } = lane.staleCollapse
    ? partitionAnswerLane(rows, Date.now())
    : { fresh: rows, stale: [] };

  // Reset the stale-divider expansion once the stale group empties: a lingering
  // staleOpen=true would render the NEXT stale card pre-expanded, an expansion the
  // user never performed. Resetting only at length 0 is invisible (nothing shown).
  useEffect(() => {
    if (shouldResetStaleOpen(staleOpen, stale.length)) setStaleOpen(false);
  }, [staleOpen, stale.length]);

  return (
    <div className={`lane lane-${lane.key}${lane.loud ? " lane-loud" : ""}${tasks.length > 0 ? " lane-has-tasks" : ""}`}>
      <div className="lane-header">
        <div className="lane-dot" style={{ background: lane.accent }} />
        <div className="lane-title">{lane.label}</div>
        {tasks.length > 0 && (
          <div className="lane-count">
            {lane.key === "working" ? workingBreakdown(tasks) : tasks.length}
          </div>
        )}
      </div>
      <div className="lane-body">
        {tasks.length === 0 ? (
          <div className={`lane-empty${lane.needsYou ? " lane-empty-clear" : ""}`}>
            <span className="lane-empty-icon" aria-hidden="true">{lane.emptyIcon || "·"}</span>
            <span className="lane-empty-text">{lane.emptyHint || ""}</span>
          </div>
        ) : lane.staleCollapse ? (
          <>
            {fresh.map((task) => (
              <TaskCard
                key={task.id}
                task={task}
                isAwaiting={!!lane.needsYou}
                onClick={(e) => onSelect(task.id, e.currentTarget)}
                approveError={approveErrors?.[task.id]}
                onDismissApproveError={onDismissApproveError}
                nowMs={nowMs}
              />
            ))}
            {stale.length > 0 && (
              <button
                type="button"
                className={`lane-stale-divider${staleOpen ? " lane-more-open" : ""}`}
                aria-expanded={staleOpen}
                aria-label={staleOpen ? `Hide ${stale.length} older need answer${stale.length > 1 ? "s" : ""}` : `Show ${stale.length} older need answer${stale.length > 1 ? "s" : ""}`}
                onClick={() => setStaleOpen((v) => !v)}
              >
                <span className="lane-more-text">
                  {stale.length} older need answer{stale.length > 1 ? "s" : ""}
                </span>
                <span className="lane-more-arrow" aria-hidden="true">▾</span>
              </button>
            )}
            {staleOpen && stale.map((task) => (
              <TaskCard
                key={task.id}
                task={task}
                isAwaiting={!!lane.needsYou}
                staleAnswer
                onClick={(e) => onSelect(task.id, e.currentTarget)}
                approveError={approveErrors?.[task.id]}
                onDismissApproveError={onDismissApproveError}
                nowMs={nowMs}
              />
            ))}
          </>
        ) : (
          visible.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              isAwaiting={!!lane.needsYou}
              onClick={(e) => onSelect(task.id, e.currentTarget)}
              approveError={approveErrors?.[task.id]}
              onDismissApproveError={onDismissApproveError}
              nowMs={nowMs}
            />
          ))
        )}
        {showToggle && (
          <button
            type="button"
            className={`lane-more${expanded ? " lane-more-open" : ""}`}
            aria-expanded={expanded}
            aria-label={expanded ? `Show fewer in ${lane.label}` : `Show ${hiddenCount} more in ${lane.label}`}
            onClick={() => setExpanded((v) => !v)}
          >
            <span className="lane-more-text">
              {expanded ? "Show fewer" : `Show ${hiddenCount} more`}
            </span>
            <span className="lane-more-arrow" aria-hidden="true">▾</span>
          </button>
        )}
      </div>
    </div>
  );
}

const STALE_STATUSES = new Set(["context", "planning", "implementing", "reviewing", "testing", "awaiting_approval", "awaiting_input", "blocked"]);
const STALE_THRESHOLD_S = 16 * 3600;

function TaskCard({ task, isAwaiting, staleAnswer, onClick, approveError, onDismissApproveError, nowMs = Date.now() }) {
  const activityTs = task.last_activity || task.updated_at || task.created_at;
  const ageMs = Date.now() - timestampMs(activityTs, 0);
  const ageSec = ageMs / 1000;
  const age = relativeTime(activityTs);
  const isStale = STALE_STATUSES.has(task.status) && ageSec > STALE_THRESHOLD_S;
  const f = cardFacts(task, { cost: taskCost(task) });
  const elapsed = elapsedChip(task, nowMs);

  // SCRUM-15: the live pulse/progress must reflect the scheduler's actual claim,
  // not merely an "active" status — an unclaimed active-status task is queued,
  // and a queued task must never render as "agent is working on this".
  const activity = cardActivity(task);
  const isRunningNow = activity.mode === "running";
  const isQueuedNow = activity.mode === "queued";
  const waiting = isWaiting(task);

  // ph-no-capture: the ENTIRE card is operator content (title, live status,
  // description, blocker question, repo name, PR URL) — masked wholesale
  // from session replay rather than per-field.
  let cardCls = "task-card ph-no-capture";
  if (isAwaiting) cardCls += " awaiting";
  if (isStale) cardCls += " stale";
  if (isRunningNow) cardCls += " active-working";
  if (isQueuedNow) cardCls += " card-queued";
  if (waiting) cardCls += " waiting-parked";
  // Distinct concern from the in-flight `.stale` treatment above (STALE_STATUSES,
  // 16h): this is the escalation-age muting for the collapsed Needs-Answer divider.
  if (staleAnswer) cardCls += " answer-stale";

  return (
    <div
      className={cardCls}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        // Only when the CARD itself is focused: keydown from the PR-link descendant
        // bubbles here, and preventDefault would cancel the anchor's own activation —
        // Enter on a focused PR link would open the drawer instead of the PR.
        if (e.target !== e.currentTarget) return;
        // preventDefault is load-bearing: without it, Enter opens the drawer, the
        // drawer autofocuses its close button in the same event flush, and Enter's
        // default activation then CLICKS that button — the drawer opened and shut
        // within one keypress. (It also stops Space from scrolling the lane.)
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick(e);
        }
      }}
    >
      {isRunningNow && <div className="card-active-pulse" title="agent is working on this" />}
      {isQueuedNow && (
        <div className="card-queued-tag" title="active but not yet picked up by a worker">
          queued
        </div>
      )}
      {waiting && (
        <div className="card-waiting-tag" title={task.blocker_wake_condition || "will resume on its own"}>
          ◷ {waitingTagText(task)}
        </div>
      )}
      <div className="card-title" title={task.title}>{cardTitle(task)}</div>
      {f.statusLine && !isQueuedNow && <div className="card-status">{f.statusLine}</div>}
      {approveError && (
        // Persists until dismissed or the task leaves awaiting_approval
        // (pruneApproveErrors, above) — the toast fades on its own, but a
        // refusal must not be discoverable-only-if-you-were-looking.
        <div className="card-approve-error" role="alert">
          <span className="card-approve-error-text">{approveError.text}</span>
          <button
            type="button"
            className="card-approve-error-dismiss"
            aria-label="Dismiss approve error"
            onClick={(e) => { e.stopPropagation(); onDismissApproveError?.(task.id); }}
          >×</button>
        </div>
      )}
      {task.subtask_progress && (
        <div className="card-subtask-progress">sub-tasks {task.subtask_progress}</div>
      )}
      {(isRunningNow || isQueuedNow) && taskProgress(task.status) != null && (
        <div
          className={`card-progress${activity.mutedProgress ? " is-queued" : ""}`}
          title={`~${taskProgress(task.status)}% through the pipeline (${task.status}${isQueuedNow ? ", queued" : ""})`}
          role="progressbar"
          aria-valuenow={taskProgress(task.status)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div className="card-progress-fill"
               style={{ width: `${taskProgress(task.status)}%` }} />
        </div>
      )}
      <div className="card-meta">
        {f.metaLine && <span className="card-meta-line">{f.metaLine}</span>}
        {mergeReadyChip(task) && (
          <span
            className="card-merge-ready"
            title="the repo's merge-ready policy passed for this commit — advisory; you still merge"
          >
            {mergeReadyChip(task)}
          </span>
        )}
        {task.pr_url && (
          httpPrUrl(task.pr_url) ? (
            <a
              className="card-pr-badge"
              href={task.pr_url}
              target="_blank"
              rel="noreferrer noopener"
              title="open the pull request in a new tab"
              onClick={(e) => e.stopPropagation()}
            >View PR ↗</a>
          ) : (
            <span className="card-pr-badge">PR</span>
          )
        )}
        {elapsed && (
          <span
            className={`card-elapsed tone-${elapsed.tone}`}
            title="running time since this task was created"
          >{elapsed.text}</span>
        )}
        <span className="card-age">{age}</span>
      </div>
      {f.action && (
        // Visual call-to-action only — the whole card is the interactive element
        // (role="button" above); a nested <button> here would be an ARIA
        // anti-pattern (double announce, redundant tab stop) whose onClick did
        // nothing the card click doesn't already do (open the drawer).
        <span className={`card-action card-action-${f.action.kind}`}>
          {f.action.label}
        </span>
      )}
    </div>
  );
}

function relativeTime(iso) {
  if (!iso) return "";
  const diff = (Date.now() - timestampMs(iso, 0)) / 1000;
  if (diff < 60) return "<1m";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}
