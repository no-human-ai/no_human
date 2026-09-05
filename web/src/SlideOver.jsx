import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import {
  approveTask, cancelTask, chooseBlockerOption, fetchDiff, fetchSubtasks,
  fetchTask, fetchTaskEvents, finishReview,
  pauseTask, postReviewComments, replyTask, resumeTask, retryTask, sendBack,
  connectTaskSSE, connectTaskProgress, fetchSplitDrafts, splitTask,
} from "./api.js";
import Markdown from "./Markdown.jsx";
import { keepFocusInDialog } from "./keepFocusInDialog.js";
import { eventLabel } from "./eventLabels.js";
import { classifyApproveError } from "./approveRefusal.js";
import {
  CANCEL_TITLE, CANCEL_BODY, CANCEL_CONFIRM_LABEL, CANCEL_KEEP_LABEL,
  CANCEL_REASON_PLACEHOLDER, REASON_MAX, submitCancel,
} from "./cancelFlow.js";
import { ROLE_LABEL, discoverSubagents, eventSource, modelsByNode } from "./eventRoles.js";
import { deriveAgentStatus } from "./pipelineStatus.js";
import { taskProgress } from "./taskProgress.js";
import { hasAction, normalizeOption } from "./blockerOptions.js";
import { planApproveIndex } from "./planGate.js";
import { decisionFor } from "./blockerDecision.js";
import { clampAgentState, currentFunctionality, groupFunctionalities, laneAgentRows } from "./functionalities.js";
import { agentSummary, taskSummary } from "./summaries.js";
import { escalationLatencyLine } from "./escalationLatency.js";
import { checkoutCommand } from "./checkoutCommand.js";
import { followUpSeed } from "./followUpSeed.js";
import {
  PARKED_STATUSES, narrativeFor, chipsFor, milestonesFor, artifactsFor, sectionSummary, coarseStatus,
  defaultOpenSection, isTerminalStatus, lastReviewedAttempt,
  reviewVerdict, severityChip, checklistRowClass, isBlockingFinding,
  approveButtonState, approvalFeedback, taskApprovedAt, testResultVerdict,
  fxCountsLabel, mergeStepLabel, landFailureFeedback, verifierRows,
  mergePolicyRows,
} from "./slideOverSummary.js";

// ── Inline SVG icons — consistent, scalable, theme-aware ──────────────────
const IconCheck = ({ size = 14, className = "" }) => (
  <svg className={`nh-icon ${className}`} width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 8 7 12 13 4" /></svg>
);
const IconX = ({ size = 14, className = "" }) => (
  <svg className={`nh-icon ${className}`} width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="4" y1="4" x2="12" y2="12" /><line x1="12" y1="4" x2="4" y2="12" /></svg>
);
const IconChevronDown = ({ size = 14 }) => (
  <svg className="nh-icon" width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="4 6 8 10 12 6" /></svg>
);
const IconChevronRight = ({ size = 14 }) => (
  <svg className="nh-icon" width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="6 4 10 8 6 12" /></svg>
);
const IconAlertTriangle = ({ size = 14 }) => (
  <svg className="nh-icon" width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2L1.5 13.5h13L8 2z" /><line x1="8" y1="6.5" x2="8" y2="9.5" /><circle cx="8" cy="11.5" r="0.5" fill="currentColor" stroke="none" /></svg>
);
const IconInfo = ({ size = 14 }) => (
  <svg className="nh-icon" width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="8" r="6.5" /><line x1="8" y1="7" x2="8" y2="11" /><circle cx="8" cy="5" r="0.5" fill="currentColor" stroke="none" /></svg>
);

export default function SlideOver({ taskId, onClose, refreshKey = 0,
                                    reviewQueue = [], onJump = null, onApproveRefused = null,
                                    onFollowUp = null, authMode = undefined }) {
  // W2.5: the review queue — the next awaiting-approval task after this one,
  // so five reviews feel like one pass instead of five board round-trips.
  const nextInQueue = reviewQueue.find((id) => id !== taskId) || null;
  const [task, setTask] = useState(null);
  const [diff, setDiff] = useState("");
  // 1.4: the tab strip is gone — the drawer is summary-first, with one lazy
  // accordion section open at a time below it. `openSection` is that section's
  // key, or null (summary only, all details collapsed).
  const [openSection, setOpenSection] = useState(null);
  // U3: the drawer opens on the section that can CLEAR this task's gate — the diff +
  // approve for a review, the question + its canned answers for a parked task. System
  // is one click away.
  const openedFirstSection = useRef(false);
  const sectionRefs = useRef({});
  const [busy, setBusy] = useState(false);
  const [sbOpen, setSbOpen] = useState(false);
  const [sbMsg, setSbMsg] = useState("");
  const [replyOpen, setReplyOpen] = useState(false);
  const [replyMsg, setReplyMsg] = useState("");
  const [cancelOpen, setCancelOpen] = useState(false);
  const [cancelReason, setCancelReason] = useState("");
  const [splitOpen, setSplitOpen] = useState(false);   // feature #1 split-review
  const closeSb = useCallback(() => setSbOpen(false), []);
  const closeReply = useCallback(() => setReplyOpen(false), []);
  const closeCancel = useCallback(() => setCancelOpen(false), []);
  const sbRef = useNestedModalKeys(sbOpen, closeSb);
  const replyRef = useNestedModalKeys(replyOpen, closeReply);
  const cancelRef = useNestedModalKeys(cancelOpen, closeCancel);
  const [flash, setFlash] = useState(null);
  // null before the operator clicks Approve, "ok" once the server recorded it,
  // "error" if it did not. Drives the button's label + disabled state so the
  // click is confirmed on the control itself, not only in the banner.
  const [approveOutcome, setApproveOutcome] = useState(null);
  // The land is a synchronous 2-4 minute server call with zero feedback
  // otherwise (operator finding: it read as "doesn't work"). `merging` flips
  // true SYNCHRONOUSLY on click, before the `approveTask` await, so the
  // button's disabled "Merging…" state lands in the same commit as the click.
  const [merging, setMerging] = useState(false);
  const [mergeStartedAt, setMergeStartedAt] = useState(null);
  const [mergeElapsedMs, setMergeElapsedMs] = useState(0);
  const [mergeStep, setMergeStep] = useState(null);
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const scrollRef = useRef(null);
  const primaryStreamRef = useRef(null);

  // G-2: the primary digest (ActivityTab) sits ABOVE the accordion inside the
  // one shared `.so-scroll` region (styles.css FINDING G) and grows on its own
  // as live events stream in — a turn counter and a live-status line appear
  // where there were none a moment ago. Nothing here changes scrollTop, but a
  // reader who has scrolled PAST the digest into the Details & tools inspector
  // still gets visually yanked: the document reflows underneath them and the
  // thing they were reading moves down the screen. A ResizeObserver on the
  // digest is the only thing that can see this (it fires regardless of WHY the
  // digest grew — new events, a task refetch, a diff arriving) and compensate
  // scrollTop by the same delta so the reader's view never moves. Skipped
  // while ANY part of the digest is still on screen: there the reader is
  // watching it (or close enough to see something new arrive), so growth
  // landing in view is the correct, expected behavior — compensating would
  // scroll PAST content they have not seen yet (review finding #2: a coarse
  // `scrollTop > 0` check over-compensated in the 0 < scrollTop < digestHeight
  // band, hiding growth the reader should have seen appear).
  //
  // Reads `entries[].contentRect`, never `getBoundingClientRect()`, for the
  // SIZE delta: the drawer itself mounts with a `transform: scale(0.97 → 1)`
  // entrance animation (styles.css `.slideover { animation: modal-in … }`),
  // and getBoundingClientRect() reports that ancestor transform — so a
  // baseline taken with it mid-animation reads ~3% short of the settled size
  // and every later delta is skewed by that same amount. contentRect is the
  // element's own untransformed layout box, immune to an ancestor's scale.
  // The "is it still visible" check below DOES use getBoundingClientRect —
  // safely, because it only compares two positions at the same instant under
  // the same transform, which preserves ordering regardless of scale.
  //
  // Keyed on `Boolean(task)`, NOT `task` itself (review finding #1): `task` is
  // a fresh object on every fetch, and a live task update — delivered over
  // Board's WS `sync` path, which diffs the task's `updated_at` and bumps
  // `refreshKey` on a change (Board.jsx:58-66) — makes SlideOver refetch and
  // hand ActivityTab a NEW `task` object, all while the drawer stays open.
  // An effect keyed on `[task]` tears down and re-arms the observer on every
  // one of those refetches, resetting `lastHeight` to
  // null right as it happens — any digest-height change landing in that same
  // tick is silently swallowed as "just the new baseline", reintroducing the
  // drift intermittently in a live session. `Boolean(task)` only flips once,
  // false→true, on the first successful fetch — the observer then stays
  // armed, tracking the SAME `lastHeight`, across every later refetch for as
  // long as this drawer instance is mounted (Board remounts SlideOver wholesale
  // on a task switch via `key={selectedId}`, which re-runs this from scratch).
  //
  // `.so-scroll` also carries `overflow-anchor: none` (styles.css) — measured
  // directly while building this fix: once the digest is fully scrolled past,
  // Chromium's OWN native scroll anchoring tries to compensate the same
  // growth on its own, and with both active `scrollTop` moved by the delta
  // TWICE (an exact, reproducible overshoot). Disabling the native mechanism
  // makes this effect the sole authority — deterministic, and not dependent
  // on a browser's own anchoring heuristics or timing.
  useEffect(() => {
    const scrollEl = scrollRef.current;
    const streamEl = primaryStreamRef.current;
    // `.so-primary-stream` only mounts once `task` is truthy (it's `{task &&
    // (<div ref={primaryStreamRef}>…)}` below) — the FIRST render always has
    // task=null, so an effect keyed on `[]` captures a null ref forever and
    // this guard silently never re-arms once the digest actually mounts.
    // Keying on `Boolean(task)` re-runs this exactly once, when the ref goes live.
    if (!scrollEl || !streamEl || typeof ResizeObserver === "undefined") return undefined;
    let lastHeight = null;   // established by the observer's own first callback, below
    const ro = new ResizeObserver((entries) => {
      const h = entries[entries.length - 1].contentRect.height;
      if (lastHeight === null) { lastHeight = h; return; }   // baseline only — no prior size to diff against
      const delta = h - lastHeight;
      lastHeight = h;
      if (delta === 0) return;
      // Fully scrolled past BEFORE this growth, not merely "scrolled some" —
      // and not "still fully past AFTER growing", which is the wrong question:
      // by the time this callback runs the DOM already reflects the NEW
      // (grown) size, so `streamEl`'s current bottom edge already includes
      // content the reader never had a chance to see. Appending can grow the
      // digest well past whatever margin the reader had scrolled by (e.g. 40px
      // past, then it grows 172px) — read post-growth, that tail pokes back
      // into the viewport and this would wrongly read as "still visible",
      // refusing to compensate a case it must. Subtracting `delta` back out
      // recovers the PRE-growth bottom edge — the position the check actually
      // needs, since the box's top edge does not move as it grows downward.
      const streamBottom = streamEl.getBoundingClientRect().bottom - delta;
      const viewTop = scrollEl.getBoundingClientRect().top;
      if (streamBottom <= viewTop) scrollEl.scrollTop += delta;
    });
    ro.observe(streamEl);
    return () => ro.disconnect();
  }, [Boolean(task)]);

  // Elapsed-time ticker for the "Merging… m:ss" label.
  useEffect(() => {
    if (!merging || !mergeStartedAt) return undefined;
    setMergeElapsedMs(Date.now() - mergeStartedAt);
    const id = setInterval(() => setMergeElapsedMs(Date.now() - mergeStartedAt), 1000);
    return () => clearInterval(id);
  }, [merging, mergeStartedAt]);

  // Live merge-step subscription — rides the existing WS broadcast
  // (task_event frames: merge_started/merge_step_*/human_merged/merge_failed),
  // the same wiring the SlideOver already has for other WS-driven updates.
  useEffect(() => {
    if (!merging) return undefined;
    const ws = connectTaskProgress(taskId, (ev) => {
      const step = mergeStepLabel(ev?.kind);
      if (step) setMergeStep(step);
    });
    return () => { try { ws.close(); } catch { /* already closed */ } };
  }, [merging, taskId]);

  // A NEW task resets the review-first latch; a refreshKey bump must not —
  // it would yank the user back to the review tab on every WS update.
  useEffect(() => { openedFirstSection.current = false; }, [taskId]);

  // "Next review →" swaps taskId in place; the previous task's Approved state
  // must not carry over onto the new one's button.
  useEffect(() => { setApproveOutcome(null); setFlash(null); }, [taskId]);

  // Re-fetch whenever taskId changes OR when Board signals a WS update
  useEffect(() => {
    // A late response from the PREVIOUS task must never paint under the new one's
    // header: "Next review →" swaps taskId while these are in flight, and Approve
    // would then post against a task whose diff the operator never saw.
    let stale = false;
    fetchTask(taskId).then((t) => {
      if (stale) return;
      setTask(t);
      if (!openedFirstSection.current && t) {
        // The section that explains the gate starts open. Review was already
        // handled; a parked task's blocker — its question, category and evidence — lives in
        // Details, so opening on System buried the question behind a pipeline diagram. (The
        // Reply button is in the persistent action bar on every section, and no parked task in
        // the live DB carries one-click `options` — so this surfaces the QUESTION, which is
        // the honest claim.)
        const dest = defaultOpenSection(t);
        if (dest) {
          openedFirstSection.current = true;  // once per open; user clicks win after
          setOpenSection(dest);
          // Summary stays visible above; scroll the gate section's header into
          // view too, so a long drawer doesn't hide the one thing to act on.
          requestAnimationFrame(() => {
            sectionRefs.current[dest]?.scrollIntoView({ behavior: "smooth", block: "nearest" });
          });
        }
      }
    }).catch(() => {});
    fetchDiff(taskId).then((d) => { if (!stale) setDiff(d); }).catch(() => {});
    return () => { stale = true; };
  }, [taskId, refreshKey]);

  // Move focus into the drawer when it OPENS — mount-only, and deliberately so.
  // This call used to live in the [onClose] effect below, and Board passes a NEW
  // onClose identity on every render, so EVERY background re-render (the 10s
  // worker-status poll, and every WS task frame) yanked focus out of whatever the
  // operator was typing into and parked it on this button — where the next SPACE
  // keystroke activated it, closing the drawer and discarding the answer.
  // Board keys the drawer on selectedId, so a task switch still remounts it and
  // re-runs this; the "Next review →" jump goes through that same key change.
  useEffect(() => { closeRef.current?.focus(); }, []);

  // Self-heal the focus trap — but ONLY when the focused control was REMOVED
  // from the DOM, which is the one case that actually drops focus out of an
  // aria-modal dialog against the operator's wishes.
  //
  // Two earlier forms of this ran from a RENDER effect and asked where focus IS,
  // which cannot answer the question that matters — WHY it is there:
  //   `activeElement === document.body`  fires when the operator clicks plain
  //       text (a heading, a label). Focus legitimately sits on <body>, the heal
  //       yanks it to the close button, and the next SPACE closes the drawer and
  //       discards the typed answer. That is the originally reported bug, intact.
  //   `not inside any dialog`            additionally yanks focus back from a
  //       control the operator deliberately Tabbed to outside the drawer.
  // Remembering WHICH control had focus supplies the missing signal: if that
  // element is still in the document, the operator moved focus on purpose —
  // leave them be. Only if it has been removed is focus genuinely orphaned.
  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return undefined;
    // Remember the last control focus was in. `focusin` fires reliably; the
    // `focusout`/`blur` that a REMOVAL would raise does not — Chromium does not
    // fire it when the focused node is detached, which is exactly our case.
    // The nested modals (reply / send-back) are SIBLINGS of this dialog, not
    // descendants — so both the listener and the observer must sit one level up,
    // or the one unmount that actually orphans focus is invisible here.
    const scope = el.parentNode || el;
    const ours = (n) => Boolean(
      n && (el.contains(n) || (n.closest && n.closest("[data-nested-modal]"))));
    let last = null;
    const onFocusIn = (e) => { if (ours(e.target)) last = e.target; };
    scope.addEventListener("focusin", onFocusIn);
    // A DOM mutation is the only honest trigger: it fires when something is
    // removed, and never merely because the component re-rendered.
    const obs = new MutationObserver(() => {
      if (!last || document.contains(last)) return;   // nothing was orphaned
      // Forget it FIRST, whichever way this goes: leaving it set kept the heal
      // armed against a detached node for the rest of the drawer's life, so an
      // unrelated mutation later stole focus (and retained the dead subtree).
      last = null;
      const active = document.activeElement;
      if (active && active !== document.body && !el.contains(active)) return;
      closeRef.current?.focus();
    });
    obs.observe(scope, { childList: true, subtree: true });
    return () => { scope.removeEventListener("focusin", onFocusIn); obs.disconnect(); };
  }, []);

  // Escape-to-close + focus trap
  useEffect(() => {
    function onKeyDown(e) {
      if (e.key === "Escape") {
        // A modal ABOVE the drawer owns the key: closing both would also destroy the
        // feedback the operator just typed into send-back/reply. The agent-log modal
        // is a child component, so its state is not visible here — the DOM is.
        if (document.querySelector("[data-nested-modal]")) return;
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const el = dialogRef.current;
      if (!el) return;
      const focusable = Array.from(
        el.querySelectorAll('button:not([disabled]), [href], input, textarea, [tabindex]:not([tabindex="-1"])')
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey) {
        if (document.activeElement === first) { e.preventDefault(); last.focus(); }
      } else {
        if (document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const isAwaiting = task?.status === "awaiting_approval";
  const isParked = PARKED_STATUSES.has(task?.status);
  // The landing-view decision: what's blocking, in plain language, and what the
  // human can do about it — surfaced ABOVE the accordion so the ask is never
  // buried under the description. null unless the task is parked with a blocker.
  const decision = useMemo(() => decisionFor(task), [task]);

  const isActive = ["pending", "context", "planning", "implementing", "reviewing", "testing"].includes(task?.status);
  // SCRUM-16: active STATUS gates polling/actions; a live SESSION (the pulse,
  // ACTIVE agent lanes) additionally requires the scheduler to have CLAIMED
  // the task — an unclaimed active-status task is queued, nothing is running.
  const isLive = isActive && task?.claimed === true;
  const isFailed = task?.status === "failed";
  const isDone = task?.status === "done";
  const isTerminal = isTerminalStatus(task?.status);

  // When the DecisionPanel is showing, it owns the actions (Resume/Reply/Stop),
  // so the bottom bar suppresses its duplicates. Keyed on whether the panel
  // actually renders (`!!decision`) — not `isParked`, since decisionFor also
  // covers paused_quota, which PARKED_STATUSES doesn't. The bar renders only
  // when it will hold at least one button — no more empty bordered bar on
  // parked-with-panel or on a terminal `done` task.
  const hasDecisionPanel = !!decision;
  const showParkedActions = isParked && !hasDecisionPanel;   // Reply + Resume
  const showQuietCancel = !isTerminal && !hasDecisionPanel;
  // Task 7: `done` joins the bar too — it used to render nothing at all for a
  // done task (a terminal, non-failed, non-parked status matched none of the
  // other conditions), which was fine before "Follow up" gave it a reason to.
  const showActionBar = isAwaiting || isActive || isFailed || isDone || showParkedActions || showQuietCancel;

  // Already approved according to the SERVER, not merely according to this
  // drawer session — survives closing and reopening the drawer, which
  // `approveOutcome` (reset on every taskId change) does not.
  const approvedAt = taskApprovedAt(task);
  // ONE derivation, read by both the button and its click guard. Computing the
  // state at the render site and the guard separately is what let an enabled
  // "Retry approve" sit on top of a handler that returns immediately; sharing
  // the object makes that class of divergence unrepresentable.
  const approveBtn = approveButtonState({
    busy, outcome: approveOutcome, approvedAt,
    merging, elapsedMs: mergeElapsedMs, step: mergeStep,
  });

  async function handleApprove() {
    if (!isAwaiting || approveBtn.disabled || merging) return;
    // Synchronous — before the first `await` — so this state change and the
    // click land in the same React commit (the <50ms latency criterion).
    setMerging(true);
    setMergeStartedAt(Date.now());
    setMergeElapsedMs(0);
    setMergeStep(null);
    setFlash(null);
    setBusy(true);
    setApproveOutcome(null);
    let posted = false;
    try {
      const res = await approveTask(taskId);
      posted = true;
      const remaining = reviewQueue.filter((id) => id !== taskId).length;
      // The server's message is authoritative: an already-satisfied approval
      // completes the task with no PR to merge (PR #101 round-2 review) —
      // hardcoding the merge instruction here lied on that path.
      setApproveOutcome("ok");
      setFlash(approvalFeedback({ ok: true, message: res?.message, remaining }));
    } catch (e) {
      setApproveOutcome("error");
      // A genuine land failure carries {step, stderr} as `detail` (see
      // api.js's approveTask) — the persistent inline alert names the step
      // and the first 200 chars of stderr. Anything else (a plain 409/500
      // string detail, a client timeout, a network error) falls back to the
      // classifier's text so the refusal is never silent (task e24cee25/PR
      // #643 — the operator saw nothing at all on an ancestry refusal).
      const detail = e && typeof e.detail === "object" && e.detail && e.detail.step
        ? e.detail : null;
      const cls = classifyApproveError(e);
      setFlash(detail ? landFailureFeedback(detail) : approvalFeedback({ ok: false, error: cls.text }));
      // Bubble the classified refusal up to Board so it survives this drawer
      // closing — a toast + a persistent card banner, not just this inline flash.
      onApproveRefused?.(taskId, cls);
    } finally {
      setBusy(false);
      setMerging(false);
    }
    // The refresh is OUTSIDE the approval's try on purpose. It used to sit
    // inside it, so a transient GET failure AFTER the POST had already landed
    // ran the catch: the operator was told "Approval NOT recorded" about an
    // approval the server had recorded, and the button offered a retry. The
    // POST's own result is the only thing that can decide the outcome; failing
    // to re-read the task is a display problem, and the WS-driven refresh
    // delivers the updated task anyway.
    if (posted) {
      try {
        setTask(await fetchTask(taskId));
      } catch {
        /* the socket's own refresh will bring it — the approval stands either way */
      }
    }
  }

  async function handleCancel() {
    if (busy) return;
    setBusy(true);
    try {
      const res = await submitCancel({ taskId, reason: cancelReason, api: cancelTask });
      if (res.ok) {
        setCancelOpen(false);
        setCancelReason("");
        setFlash(res.message);
        const updated = await fetchTask(taskId);
        setTask(updated);
      } else {
        setFlash(`Error: ${res.error}`);
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleSendBack() {
    if (!sbMsg.trim() || busy) return;
    setBusy(true);
    try {
      const res = await sendBack(taskId, sbMsg.trim());
      setSbOpen(false);
      setSbMsg("");
      setFlash(
        res?.budget_warning
          ? `⚠ ${res.budget_warning.message}`
          : "Feedback stored. Task returned to queue."
      );
      const updated = await fetchTask(taskId);
      setTask(updated);
    } catch (e) {
      setFlash(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleReply() {
    if (!replyMsg.trim() || busy) return;
    setBusy(true);
    try {
      const res = await replyTask(taskId, replyMsg.trim());
      setReplyOpen(false);
      setReplyMsg("");
      setFlash(
        res?.budget_warning
          ? `⚠ ${res.budget_warning.message}`
          : res.message || "Reply stored. Run `nh watch` to resume."
      );
      const updated = await fetchTask(taskId);
      setTask(updated);
    } catch (e) {
      setFlash(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleLifecycle(action) {
    if (busy) return;
    // "cancel" is not here: it opens the confirm modal (openCancelModal)
    // instead of firing straight through this handler.
    const actions = { pause: pauseTask, resume: resumeTask, retry: retryTask };
    const fn = actions[action];
    if (!fn) return;
    setBusy(true);
    try {
      const res = await fn(taskId);
      setFlash(res.message);
      const updated = await fetchTask(taskId);
      setTask(updated);
    } catch (e) {
      setFlash(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {/* No backdrop close. This was kept as a deliberate decision on the
          grounds that closing the drawer is "non-destructive" — a review
          disproved it: a stray click here destroyed a typed spec
          change-request with no confirm, the same loss that motivated
          removing backdrop-close from the other five overlays. */}
      <div className="slideover-backdrop" onMouseDown={keepFocusInDialog} />
      {/* ph-no-capture on the WHOLE drawer: every tab renders operator
          content (spec, diffs — in Review and Diff tabs both — blockers,
          activity, agent logs). Three review rounds proved per-tab masking
          misses surfaces; the drawer records as one blocked box. */}
      <div
        className="slideover ph-no-capture"
        role="dialog"
        aria-modal="true"
        aria-labelledby="so-dialog-title"
        ref={dialogRef}
        // Most of this panel is headings, summaries and labels. A mousedown on
        // any of them while the operator is typing an answer strands the caret
        // and drops every keystroke after it — the reported bug, reached by a
        // click rather than by a re-render.
        onMouseDown={keepFocusInDialog}
      >
        {/* header */}
        <div className="so-header">
          <div className="so-header-text">
            <div className="so-id">{task?.id ?? taskId}</div>
            {/* ph-no-capture: task titles are operator content — excluded from
                session replay (PostHog block-level suppression class). */}
            <div className="so-title ph-no-capture" id="so-dialog-title">{task?.title ?? "Loading…"}</div>
          </div>
          <button className="so-close" onClick={onClose} ref={closeRef} aria-label="Close"><IconX size={16} /></button>
        </div>
        {task && taskProgress(task.status) != null && (
          <div className="so-progress"
               title={task.status === "awaiting_approval"
                 ? "The agent has finished — a PR is up, awaiting your review/approval"
                 : `~${taskProgress(task.status)}% through the pipeline (${task.status})`}
               role="progressbar" aria-valuenow={taskProgress(task.status)}
               aria-valuemin={0} aria-valuemax={100}>
            <div className="so-progress-fill"
                 style={{ width: `${taskProgress(task.status)}%` }} />
            <span className="so-progress-label">{task.status === "awaiting_approval"
              ? "Ready — awaiting your approval"
              : `${taskProgress(task.status)}%`}</span>
          </div>
        )}

        {/* ONE scroll region: header + progress stay pinned above, the action
            bar stays pinned below, and everything between — summary, decision,
            the primary stream, and the Details & tools inspector — scrolls
            together. Without this the inspector (a flex:1 child) collapsed to a
            0px sliver under the tall header + the 46vh-capped stream, so its
            sections were unreachable on any task with a live stream (FINDING G). */}
        <div className="so-scroll" ref={scrollRef}>
        {/* Why it failed — the orchestrator's own failure_reason, verbatim. A
            failed task otherwise showed only a Retry button, so a quota stop
            ("...returned an error result: success") was indistinguishable from a
            real capability failure. Rendered as text (React-escaped), shown
            whenever the field is set. */}
        {task?.failure_reason && (
          <div className="so-failure" role="alert">
            <div className="so-failure-label">Why it failed</div>
            <div className="so-failure-reason ph-no-capture">{task.failure_reason}</div>
          </div>
        )}

        {/* summary — the landing state: a plain-language narrative, live chips,
            and a small milestone timeline. Replaces the tab strip; the action
            bar below still leads with Approve/Reply/etc. */}
        {task && <TaskSummary task={task} isActive={isLive} authMode={authMode} />}

        {/* Pre-flight feasibility (feature #1): only while the task is still
            PENDING and the create-time hint offered a mitigation — a task
            already running is past the point of splitting. Advisory: it offers
            a 1-click split, never blocks. */}
        {task && task.status === "pending"
          && (task.context || {}).feasibility_hint?.offer === "split" && (
          <FeasibilityCard
            hint={task.context.feasibility_hint}
            onSplit={() => setSplitOpen(true)}
          />
        )}

        {/* Decision panel — the one thing to act on, promoted above the
            accordion. Only when the task is parked with a blocker; carries the
            plain-language ask + one-click actions so the operator never has to
            open Details and scroll past the description to find the question. */}
        {task && decision && (
          <DecisionPanel
            decision={decision}
            taskId={taskId}
            taskStatus={task.status}
            busy={busy}
            onAction={(id) => {
              if (id === "reply") setReplyOpen(true);
              else if (id === "cancel") setCancelOpen(true);
              else handleLifecycle(id); // "resume"
            }}
          />
        )}

        {/* PRIMARY digest — the redesign's answer to "no one understands what's
            going on": a tessl-style glance (short task description, the
            artifacts it produced, one live-status line, the run summary), NOT a
            wall of logs. The raw event stream is demoted to the collapsed
            "Event log" accordion section below, alongside System/Diff/Review. */}
        {task && (
          <div className="so-primary-stream" ref={primaryStreamRef} data-testid="primary-stream">
            <ActivityTab taskId={taskId} task={task} isActive={isLive} diff={diff} />
          </div>
        )}

        {/* Secondary inspector: the remaining sections as a lazy accordion, one
            open at a time — including the raw Event log, collapsed by default.
            Each section still mounts (and fetch/SSEs) only when open. */}
        <div className="so-body">
          <div className="so-inspector-label">Details &amp; tools</div>
          <div className="so-accordion">
            {SECTION_LIST
              .filter((s) => s.key !== "subtasks" || task?.parent_id || task?.status === "compound_parent")
              .map((s) => {
                const isOpen = openSection === s.key;
                return (
                  <AccordionSection
                    key={s.key}
                    sectionKey={s.key}
                    label={s.label}
                    summary={task ? sectionSummary(s.key, { task, diff }) : null}
                    isOpen={isOpen}
                    onToggle={() => setOpenSection(isOpen ? null : s.key)}
                    headerRef={(el) => { sectionRefs.current[s.key] = el; }}
                  >
                    {isOpen && sectionBody(s.key, {
                      taskId, task, diff, isActive,
                      onSpecRefresh: () => fetchTask(taskId).then(setTask),
                      onOpenSection: (key) => setOpenSection(key),
                      onJump,
                    })}
                  </AccordionSection>
                );
              })}
          </div>
        </div>
        </div>{/* /so-scroll */}

        {/* The result of the last action, pinned OUTSIDE the scrolling body and
            directly above the buttons that cause it. Inside .so-body it scrolled
            away from the action bar, so clicking Approve at the bottom of a long
            drawer showed its confirmation off-screen — the operator saw nothing
            happen. */}
        {flash && <FlashBanner msg={flash} onDismiss={() => setFlash(null)} />}

        {/* action bar — contextual based on task status. Suppressed entirely
            when the DecisionPanel above owns the actions (parked-with-blocker),
            and never rendered empty. */}
        {task && showActionBar && (
          <div className="so-actions">
            {isAwaiting && (
              // The click has to change something on the control itself: an
              // approval that recorded server-side but left the button reading
              // "Approve" was read as a dead button.
              // `approvedAt` is the payload's own record, so reopening the
              // drawer on an approved task still reads "Approved — merge
              // pending" instead of offering a bare Approve that would
              // silently re-stamp approved_at.
              // `approveBtn` is the SAME object handleApprove guards on, so an
              // enabled label can never outlive the handler that backs it.
              <>
                <button
                  className={`btn btn-approve btn-approve-${approveBtn.tone}`}
                  onClick={handleApprove}
                  disabled={approveBtn.disabled || busy}
                  title="Squash-merges the PR onto the default branch and closes it - the agent never merges on its own."
                  aria-label={approveBtn.label}
                >
                  {approveBtn.label}
                </button>
                {/* The live step name streamed from the server (merge_step_*
                    WS frames) — what actually makes the multi-minute window
                    read as progress rather than a frozen button. */}
                {approveBtn.step && <span className="approve-merge-step">{approveBtn.step}</span>}
              </>
            )}
            {isAwaiting && (
              <button className="btn btn-sendback" onClick={() => setSbOpen(true)} disabled={busy || merging}>
                Send back
              </button>
            )}
            {isAwaiting && nextInQueue && onJump && (
              // Review-queue navigation belongs to the review flow only. It used
              // to show on ANY drawer with a pending review in the queue — so a
              // BLOCKED task's action bar led with "Next review →" instead of the
              // Reply/Resume it actually needs.
              <button className="btn btn-next-review" disabled={busy}
                      onClick={() => onJump(nextInQueue)}
                      title="Jump to the next task awaiting approval">
                Next review →
              </button>
            )}
            {showParkedActions && (
              <button className="btn btn-reply" onClick={() => setReplyOpen(true)} disabled={busy}>
                Reply
              </button>
            )}
            {showParkedActions && (
              <button className="btn btn-lifecycle btn-resume" onClick={() => handleLifecycle("resume")} disabled={busy}>
                Resume
              </button>
            )}
            {isActive && (
              <button className="btn btn-lifecycle btn-pause" onClick={() => handleLifecycle("pause")} disabled={busy}>
                Pause
              </button>
            )}
            {isFailed && (
              <button className="btn btn-lifecycle btn-retry" onClick={() => handleLifecycle("retry")} disabled={busy}>
                Retry
              </button>
            )}
            {(isDone || isFailed) && (
              // Task 7: a follow-up is a NEW sibling task (follows_id), never a
              // re-run of this one — that is Retry, right above. Seeds the New
              // Task composer via the same `initial` mechanism the backlog
              // start flow already uses (App.jsx's newTaskSeed/onFollowUp).
              <button
                className="btn btn-lifecycle btn-follow-up"
                onClick={() => onFollowUp && onFollowUp(followUpSeed(task))}
                disabled={busy}
              >
                Follow up
              </button>
            )}
            {showQuietCancel && (
              // Destructive action: spatially separated from the primary CTA
              // (it used to sit as an equal sibling next to Approve — the
              // classic danger-adjacency mistake) and demoted to a quiet
              // text button. Opens the confirm modal rather than acting
              // immediately — cancelling is irreversible. Suppressed when the
              // DecisionPanel is present — it carries its own "Stop this task".
              <button
                className="btn-cancel-quiet"
                onClick={() => setCancelOpen(true)}
                disabled={busy}
                title="Cancel this task (destructive)"
              >
                Cancel task
              </button>
            )}
          </div>
        )}
      </div>

      {/* send-back modal */}
      {sbOpen && (
        <div
          // No backdrop-click close: this discarded typed feedback on a stray
          // click. Escape and the Cancel button below are the deliberate exits.
          // onMouseDown keeps the caret in the textarea when the backdrop is
          // clicked — otherwise the modal stays open but silently stops typing.
          className="sendback-overlay" data-nested-modal
          onMouseDown={keepFocusInDialog}>
          <div className="sendback-modal" ref={sbRef} role="dialog" aria-modal="true"
               aria-label="Send back with feedback">
            <div className="sendback-label">Send back with feedback</div>
            <textarea
              className="sendback-textarea"
              placeholder="What needs to change?"
              value={sbMsg}
              onChange={(e) => setSbMsg(e.target.value)}
              autoFocus
            />
            <div className="sendback-actions">
              <button className="btn btn-sendback" onClick={() => setSbOpen(false)}>
                Cancel
              </button>
              <button
                className="btn btn-approve"
                onClick={handleSendBack}
                disabled={!sbMsg.trim() || busy}
              >
                {busy ? "…" : "Send"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* reply modal */}
      {replyOpen && (
        <div
          // No backdrop-click close — see the send-back modal above. This is the
          // box a friend was typing a blocker answer into when a stray click
          // discarded it.
          className="sendback-overlay" data-nested-modal
          onMouseDown={keepFocusInDialog}>
          <div className="sendback-modal" ref={replyRef} role="dialog" aria-modal="true"
               aria-label="Reply to blocker question">
            <div className="sendback-label">
              {decision?.question ? "Answering the task's question" : "Reply to guide the task"}
            </div>
            {decision && (decision.question || decision.ask) && (
              // Context so the box isn't blank: what the human is actually
              // answering, quoted from the blocker.
              <p className="reply-context ph-no-capture">{decision.question || decision.ask}</p>
            )}
            <textarea
              className="sendback-textarea"
              placeholder="Your answer…"
              value={replyMsg}
              onChange={(e) => setReplyMsg(e.target.value)}
              autoFocus
            />
            <div className="sendback-actions">
              <button className="btn btn-sendback" onClick={() => setReplyOpen(false)}>
                Cancel
              </button>
              <button
                className="btn btn-approve"
                onClick={handleReply}
                disabled={!replyMsg.trim() || busy}
              >
                {busy ? "…" : "Send reply"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* cancel-task confirm modal — irreversible, so it always requires an
          explicit confirm click; the reason is optional and never blocks
          submit. The sha + justification "landed elsewhere" override this
          replaced stays CLI-only (`nh approve --landed`), unreachable here. */}
      {cancelOpen && (
        <div
          className="sendback-overlay" data-nested-modal
          onMouseDown={keepFocusInDialog}>
          <div className="sendback-modal" ref={cancelRef} role="dialog" aria-modal="true"
               aria-label={CANCEL_TITLE}>
            <div className="sendback-label">{CANCEL_TITLE}</div>
            <p className="reply-context">{CANCEL_BODY}</p>
            <textarea
              className="sendback-textarea"
              placeholder={CANCEL_REASON_PLACEHOLDER}
              value={cancelReason}
              onChange={(e) => setCancelReason(e.target.value)}
              maxLength={REASON_MAX}
              autoFocus
            />
            <div className="sendback-actions">
              <button className="btn btn-sendback" onClick={() => setCancelOpen(false)} disabled={busy}>
                {CANCEL_KEEP_LABEL}
              </button>
              <button
                className="btn btn-cancel-danger"
                onClick={handleCancel}
                disabled={busy}
              >
                {busy ? "…" : CANCEL_CONFIRM_LABEL}
              </button>
            </div>
          </div>
        </div>
      )}

      {splitOpen && task && (
        <SplitReview
          taskId={taskId}
          onClose={() => setSplitOpen(false)}
          onDone={() => { setSplitOpen(false); onClose?.(); }}
        />
      )}
    </>
  );
}

/* ── sub-components ───────────────────────────────────────────────────────── */

// Feature #1: the pre-flight feasibility card. Advisory — a band and one honest
// line ("X% of similar tasks finished in one pass") + a single button. Never a
// gate; the user can always ignore it and run the task.
function FeasibilityCard({ hint, onSplit }) {
  return (
    <div className="feasibility-card" role="region" aria-label="Task size">
      <div className="feasibility-eyebrow">Before you start</div>
      <p className="feasibility-msg ph-no-capture">{hint.message}</p>
      <div className="feasibility-actions">
        <button className="btn btn-approve" onClick={onSplit}>
          Split into sub-tasks
        </button>
      </div>
    </div>
  );
}

// Feature #1: the split-review modal. Lazily fetches the proposed 2-4 sub-tasks
// (generated server-side only now), lets the human edit them, and creates the
// confirmed set with one click. On success the original is cancelled, so the
// drawer closes.
function SplitReview({ taskId, onClose, onDone }) {
  const [drafts, setDrafts] = useState(null);   // null=loading, []=none, [...]=ready
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchSplitDrafts(taskId)
      .then((r) => { if (!cancelled) setDrafts(r.drafts || []); })
      .catch((e) => { if (!cancelled) { setErr(e.message); setDrafts([]); } });
    return () => { cancelled = true; };
  }, [taskId]);

  function edit(i, field, value) {
    setDrafts((ds) => ds.map((d, j) => (j === i ? { ...d, [field]: value } : d)));
  }

  async function create() {
    if (!drafts || drafts.length < 2 || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await splitTask(taskId, drafts.map((d) => ({
        title: (d.title || "").trim(),
        description: d.description || null,
        contract: d.contract || null,
      })));
      onDone();
    } catch (e) {
      setErr(e.message || "split failed");
      setBusy(false);
    }
  }

  const ready = drafts && drafts.length >= 2;
  const canCreate = ready && drafts.every((d) => (d.title || "").trim()) && !busy;
  return (
    <div className="sendback-overlay" data-nested-modal onMouseDown={keepFocusInDialog}>
      <div className="sendback-modal split-modal" role="dialog" aria-modal="true"
           aria-label="Split into sub-tasks">
        <div className="sendback-label">Split into sub-tasks</div>
        {drafts === null && <p className="reply-context">Drafting a split…</p>}
        {drafts && drafts.length === 0 && (
          <p className="reply-context">
            Couldn’t draft a split — cancel and run the task as it is, or edit it first.
          </p>
        )}
        {ready && (
          <div className="split-drafts ph-no-capture">
            {drafts.map((d, i) => (
              <div className="split-draft" key={i}>
                <input
                  className="split-draft-title"
                  value={d.title || ""}
                  onChange={(e) => edit(i, "title", e.target.value)}
                  aria-label={`Sub-task ${i + 1} title`}
                />
                <textarea
                  className="split-draft-desc"
                  value={d.description || ""}
                  onChange={(e) => edit(i, "description", e.target.value)}
                  rows={2}
                  aria-label={`Sub-task ${i + 1} description`}
                />
                {d.contract && (
                  <div className="split-draft-contract">Why this split: {d.contract}</div>
                )}
              </div>
            ))}
          </div>
        )}
        {err && <div className="decision-choice-err">{err}</div>}
        <div className="sendback-actions">
          <button className="btn btn-sendback" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          {ready && (
            <button className="btn btn-approve" onClick={create} disabled={!canCreate}>
              {busy ? "Creating…" : `Create these ${drafts.length} tasks`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// `msg` is either a plain string (the lifecycle/reply flows) or the
// { text, role, tone } shape approvalFeedback returns. The role makes it a live
// region: an approval that only PAINTS a confirmation is invisible to a
// screen reader and easy to miss on a long drawer.
function FlashBanner({ msg, onDismiss }) {
  const { text, role, tone } = typeof msg === "string"
    ? { text: msg, role: "status", tone: "info" }
    : { text: msg?.text ?? "", role: msg?.role || "status", tone: msg?.tone || "info" };
  return (
    <div
      className={`flash-banner flash-banner-${tone}`}
      role={role}
      aria-live={role === "alert" ? "assertive" : "polite"}
    >
      <span>{text}</span>
      <button className="flash-banner-dismiss" onClick={onDismiss} aria-label="Dismiss"><IconX size={14} /></button>
    </div>
  );
}

// ── 1.4: summary-first drawer — narrative + chips + milestones ────────────
// The one glance that answers "what is this and what happens next" in plain
// language, before any accordion section is opened. All derivation is pure
// (slideOverSummary.js) — this component only lays it out.
function TaskSummary({ task, isActive, authMode }) {
  const n = useMemo(() => narrativeFor(task), [task]);
  const chips = useMemo(() => chipsFor(task, authMode), [task, authMode]);
  const milestones = useMemo(() => milestonesFor(task), [task]);
  const status = useMemo(() => coarseStatus(task), [task]);
  return (
    <div className="so-summary">
      <div className="so-status-chip" data-testid="coarse-status"
           style={{ "--chip-c": status.colorVar }}>
        <span className={`so-status-chip-dot${isActive ? " pulsing" : ""}`} aria-hidden="true" />
        {status.label}
      </div>
      <p className="so-summary-narrative">
        {n.before}{" "}
        <span className="so-summary-phrase" style={{ color: n.colorVar }}>
          <span
            className={`so-summary-dot${isActive ? " pulsing" : ""}`}
            style={{ background: n.colorVar }}
            aria-hidden="true"
          />
          {n.phrase}
        </span>
        {n.after}
      </p>
      {chips.length > 0 && (
        <div className="so-chips">
          {chips.map((c) => (
            <div className="so-chip" key={c.key}>
              {c.href ? (
                <a href={c.href} target="_blank" rel="noreferrer" className="so-chip-value" key={c.label}>
                  {c.label} ↗
                </a>
              ) : (
                // key={c.label}: a live SSE-driven update changes the label text, which
                // remounts this element and replays the crossfade (styles.css) instead of
                // the number silently snapping to its new value.
                <span className="so-chip-value" key={c.label}>{c.label}</span>
              )}
              <span className="so-chip-sub">{c.sub}</span>
            </div>
          ))}
        </div>
      )}
      {milestones.length > 0 && (
        <div className="so-milestones" role="list" aria-label="Task timeline">
          {milestones.map((m, i) => (
            <div key={m.key} className={`so-milestone${m.done ? " done" : ""}${m.current ? " current" : ""}`} role="listitem">
              <span className="so-milestone-dot" aria-hidden="true" />
              <span className="so-milestone-label">{m.label}</span>
              {i < milestones.length - 1 && <span className="so-milestone-connector" aria-hidden="true" />}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// The action panel for a parked task — rendered above the accordion. It leads
// with what's blocking in plain language, then the concrete moves: the
// blocker's own one-click options if it has any, plus resume / reply / stop.
// The intelligence (category -> copy, which actions apply) lives in the pure,
// tested `decisionFor`; this component only renders it and owns the one-click
// option submit (idempotent: disabled after the first click so a double-click
// can't double-resume).
function DecisionPanel({ decision, taskId, taskStatus, busy, onAction }) {
  const [chosen, setChosen] = useState(null);   // option index being submitted
  const [choiceErr, setChoiceErr] = useState(null);
  const parked = ["blocked", "awaiting_input", "escalated", "paused_quota"]
    .includes(taskStatus);
  async function choose(i) {
    if (chosen != null || busy) return;
    setChosen(i);
    setChoiceErr(null);
    try {
      await chooseBlockerOption(taskId, i + 1);   // API is 1-based
    } catch (e) {
      setChoiceErr(e?.message || "reply failed");
      setChosen(null);                            // let the human retry
    }
  }
  const { categoryLabel, headline, ask, question, detail, options, wake, actions } = decision;
  const frozen = busy || chosen != null;
  return (
    <div className="decision-panel" role="region" aria-label="Action needed">
      <div className="decision-head">
        <span className="decision-icon" aria-hidden="true"><IconAlertTriangle size={15} /></span>
        <span className="decision-eyebrow">Needs your decision</span>
        {categoryLabel && <span className="decision-badge">{categoryLabel}</span>}
      </div>
      <p className="decision-headline">{headline}</p>
      {detail && <p className="decision-detail">{detail}</p>}
      {question ? (
        <p className="decision-ask">
          <span className="decision-ask-label">It’s asking:</span> {question}
        </p>
      ) : (
        <p className="decision-ask">{ask}</p>
      )}
      {options.length > 0 && parked && taskId && (
        <ul className="decision-options">
          {options.map((opt, i) => (
            <li key={i}>
              <button
                className="decision-option-btn"
                disabled={frozen}
                onClick={() => choose(i)}
              >
                {chosen === i ? "…" : opt.label}{hasAction(opt) ? " ⚡" : ""}
              </button>
            </li>
          ))}
        </ul>
      )}
      {choiceErr && <div className="decision-choice-err">{choiceErr}</div>}
      <div className="decision-actions">
        {actions.map((a) => (
          <button
            key={a.id}
            className={`decision-btn decision-btn-${a.variant}`}
            disabled={frozen}
            onClick={() => onAction(a.id)}
          >
            {a.label}
          </button>
        ))}
      </div>
      {wake && (
        <p className="decision-wake">Otherwise it resumes on its own when {wake}.</p>
      )}
    </div>
  );
}

// The ordered accordion sections — one body component each, matching the 8
// original tabs exactly (Subtasks is filtered in when the task qualifies).
const SECTION_LIST = [
  { key: "system",   label: "System" },
  { key: "activity", label: "Event log" },
  { key: "subtasks", label: "Sub-tasks" },
  { key: "details",  label: "Details" },
  { key: "spec",     label: "Spec" },
  { key: "review",   label: "Review" },
  { key: "diff",     label: "Diff" },
  { key: "attempts", label: "Attempts" },
];

// Resolves a section key to its (unchanged) tab component. Only called while
// the section is open — this IS the lazy-render boundary the old tab switch
// had; a closed section mounts nothing and fetches nothing.
function sectionBody(key, { taskId, task, diff, isActive, onSpecRefresh, onOpenSection, onJump }) {
  switch (key) {
    case "system":   return <SystemTab taskId={taskId} task={task} isActive={isActive} />;
    case "activity": return <ActivityLog taskId={taskId} task={task} isActive={isActive} />;
    case "subtasks": return <SubtasksTab taskId={taskId} />;
    case "details":  return <DetailsTab task={task} onJump={onJump} />;
    case "spec":     return <SpecTab task={task} onRefresh={onSpecRefresh} />;
    case "review":   return <ReviewTab task={task} diff={diff} onOpenSection={onOpenSection} />;
    case "diff":     return <DiffTab diff={diff} />;
    case "attempts": return <AttemptsTab task={task} />;
    default: return null;
  }
}

// One accordion row: header (label + colored micro-summary + chevron) always
// visible; body animates open/closed via a grid-template-rows 0fr↔1fr track
// (styles.css), so it works without knowing the content's height up front.
function AccordionSection({ sectionKey, label, summary, isOpen, onToggle, headerRef, children }) {
  return (
    <div className={`so-section${isOpen ? " open" : ""}`} ref={headerRef}>
      <button
        type="button"
        className="so-section-header"
        onClick={onToggle}
        aria-expanded={isOpen}
        aria-controls={`so-section-body-${sectionKey}`}
      >
        <span className="so-section-title">{label}</span>
        {summary && (
          <span className="so-section-micro" style={{ color: summary.colorVar }}>
            {summary.text}
          </span>
        )}
        <span className={`so-section-chev${isOpen ? " open" : ""}`} aria-hidden="true">
          <IconChevronDown size={14} />
        </span>
      </button>
      <div className="so-section-body-wrap" id={`so-section-body-${sectionKey}`}>
        <div className="so-section-body-inner">{children}</div>
      </div>
    </div>
  );
}


// ── Agent definitions for the system diagram ───────────────────────────────
const AGENTS = [
  { id: "worker",     label: "Orchestrator", type: "ORCHESTRATOR", icon: "⚙",  desc: "Drives the task pipeline: context, planning, attempts, review",
    color: "var(--agent-worker)" },
  { id: "planner",    label: "Planner",      type: "PLANNER",      icon: "◈",  desc: "Fans out independent plan proposals, then synthesizes one",
    color: "var(--agent-planner)" },
  { id: "agent",      label: "Coder",        type: "AGENT",        icon: "⌨",  desc: "The Claude session that reads & edits code",
    color: "var(--agent-agent)" },
  { id: "supervisor", label: "Supervisor",   type: "MONITOR",      icon: "◉",  desc: "Course-corrects the worker every N tool calls",
    color: "var(--agent-supervisor)" },
  { id: "reviewer",   label: "Reviewer",     type: "QUALITY GATE", icon: "✓",  desc: "Adversarial review, tests, tamper check",
    color: "var(--agent-reviewer)" },
  // The post-PR watcher (W2.1): without this node its agentState was never
  // derived, so the Shepherding stage could not light up even after the
  // events reached the client (the second half of the same starvation bug).
  { id: "watcher",    label: "Watcher",      type: "SHEPHERD",     icon: "☂",  desc: "Post-PR: merge watch, feedback injection, CI fixes, Enterprise CI gate",
    color: "var(--agent-worker)" },
];

// Parse URLs in text and return React elements with clickable links
function linkify(text) {
  if (!text) return text;
  const urlRe = /(https?:\/\/[^\s<>)"',]+)/g;
  const urlTest = /^https?:\/\//;
  const parts = text.split(urlRe);
  if (parts.length === 1) return text;
  return parts.map((part, i) =>
    urlTest.test(part)
      ? <a key={i} href={part} target="_blank" rel="noreferrer">{part.length > 80 ? part.slice(0, 77) + "…" : part}</a>
      : part
  );
}

// Group consecutive same-tool events for collapsed display.
// A modal above the drawer owns both Escape and Tab while it is open: the drawer's handlers
// stand down for it (see the [data-nested-modal] guard), and the drawer's focus trap only
// covers .slideover, of which these modals are siblings.
function useNestedModalKeys(open, onClose) {
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    function onKey(e) {
      if (e.key === "Escape") {
        // No stopPropagation: the drawer's listener is on the same target and already ran —
        // it stands down via the [data-nested-modal] guard, which is what actually protects
        // the drawer (and the text typed into this modal).
        onClose();
        return;
      }
      if (e.key !== "Tab" || !ref.current) return;
      const items = [...ref.current.querySelectorAll("button, textarea, input, select, [href]")]
        .filter((el) => !el.disabled && el.offsetParent !== null);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  return ref;
}

// PARKED_STATUSES now lives in slideOverSummary.js (imported above) — the statuses
// whose gate the operator clears IN the drawer (Reply / Resume / the blocker's
// options). Deliberately NOT paused_quota: the backend parks it without a blocker
// record (orchestrator._park_quota) and `isParked` gives it no Reply/Resume buttons,
// so sending it to Details would strand it on a section emptier than System — its
// gate is a budget raise, not an answer. One definition; `defaultOpenSection` reads
// it too.

function groupConsecutiveEvents(events) {
  const result = [];
  let i = 0;
  while (i < events.length) {
    const e = events[i];
    // Only group consecutive Read/View calls — most common noise source.
    if (e.kind === "tool_use" && (e.tool_name === "Read" || e.tool_name === "View")) {
      const group = [e];
      let j = i + 1;
      while (j < events.length && events[j].kind === "tool_use" && events[j].tool_name === e.tool_name) {
        group.push(events[j]);
        j++;
      }
      if (group.length >= 3) {
        result.push({ _group: true, tool: e.tool_name, events: group, ts: e.ts, firstIdx: i });
        i = j;
        continue;
      }
    }
    result.push(e);
    i++;
  }
  return result;
}

// Extract file basename from a tool_use event.
function toolFile(event) {
  const inp = event.tool_input || {};
  const path = inp.file_path || inp.path || inp.notebook_path || "";
  return path ? path.split("/").pop() : "";
}

// Collapsed group of consecutive same-tool events.
function GroupedEvents({ group, role }) {
  const [expanded, setExpanded] = useState(false);
  const files = group.events.map(toolFile).filter(Boolean);
  const shown = files.slice(0, 3);
  const rest = files.length - shown.length;
  const elapsed = group.events[group.events.length - 1].ts - group.events[0].ts;

  return (
    <div className={`activity-event-rich role-${role} ak-tool_use event-group`}>
      <div className="rich-meta">
        <span className="rich-ts">{fmtTs(group.ts)}</span>
        <span className="rich-role">{ROLE_LABEL[role]}</span>
        <span className="rich-kind ak-tool_use">{group.tool}</span>
        {elapsed > 2 && <span className="rich-elapsed">+{fmtDuration(elapsed)}</span>}
      </div>
      <div
        className="rich-tool-group"
        onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
      >
        <span className="rich-tool-name">{group.tool}</span>
        <span className="rich-group-count">{group.events.length} files</span>
        {shown.map((f, i) => <span key={i} className="rich-file-chip">{f}</span>)}
        {rest > 0 && <span className="rich-group-more">+{rest}</span>}
        <span className="rich-group-toggle">{expanded ? "▾" : "▸"}</span>
      </div>
      {expanded && (
        <div className="rich-group-detail">
          {group.events.map((e, i) => (
            <div key={i} className="rich-group-item">
              <span className="rich-file-chip">{toolFile(e) || "file"}</span>
              {e.result_preview && <pre className="rich-tool-result">{e.result_preview}</pre>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Collapsed-by-default reasoning block ("Thought for Ns") — extended-thinking
// content is verbose and low-signal at a glance, but shouldn't be discarded.
function ThinkingBlock({ text, elapsed }) {
  const [expanded, setExpanded] = useState(false);
  const label = elapsed > 1 ? `Thought for ${fmtDuration(elapsed)}` : "Thinking";
  return (
    <div className="rich-thinking">
      <button
        type="button"
        className="rich-thinking-toggle"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        {expanded ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
        <span>{label}</span>
      </button>
      {expanded && (
        <div className="rich-thinking-body">
          <Markdown>{text}</Markdown>
        </div>
      )}
    </div>
  );
}

// "What knowledge was applied to this task" — the learning loop's only visible
// output while a task is running.
//
// The orchestrator has emitted `knowledge_accessed` all along, carrying the
// TITLES of the rules it injected (`injected`), and the timeline rendered it
// through the catch-all branch: one line of counts, titles discarded. So the
// product could say "44 rule(s) injected" and could not say which 44 — which
// is the question an operator asks when the coder does something surprising.
//
// A button and a conditional body, NOT <details>/<summary>. The Settings
// overlay's focus trap had to grow a `summary` selector and a collapsed-details
// filter after exactly that markup leaked Tab out of the dialog; this drawer's
// trap (see keepFocusInDialog) still has neither, so a <summary> here would
// reintroduce a fixed bug in a component that never fixed it. `ThinkingBlock`
// above is the same shape for the same reason.
function KnowledgeApplied({ event }) {
  const [expanded, setExpanded] = useState(false);
  const injected = Array.isArray(event.injected) ? event.injected : [];
  const held = Array.isArray(event.held_for_terms) ? event.held_for_terms : [];
  const text = event.text || "";
  if (!injected.length && !held.length) {
    // "0 rule(s) injected, 71 held (trigger not matched)" is the honest and
    // common case. It gets the plain line it always had — a disclosure control
    // that opens onto nothing is worse than no control.
    return <span className="rich-body">{text}</span>;
  }
  return (
    <div className="rich-knowledge">
      <button
        type="button"
        className="rich-thinking-toggle"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        {expanded ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
        <span>{text}</span>
      </button>
      {expanded && (
        <div className="rich-knowledge-body">
          {injected.map((title, i) => (
            <div className="rich-knowledge-item" key={`i${i}`}>{title}</div>
          ))}
          {/* Held-for-terms is named separately and looks different on
              purpose: one is the trigger filter working, the other is a rule
              the operator needs to clean. The backend has always distinguished
              them in the count line; discarding the distinction here would
              make a screened rule look like it simply did not match. */}
          {held.map((title, i) => (
            <div className="rich-knowledge-item held" key={`h${i}`}>
              {title} <span className="rich-knowledge-why">held — banned term</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Render a single event with rich formatting
function RichEvent({ event, elapsed, role }) {
  const kind = event.kind;
  const text = event.text || "";

  let body;
  if (kind === "tool_use") {
    const toolName = event.tool_name || text.split(" ")[0] || "tool";
    const file = toolFile(event);
    // `text` is already a human-readable one-liner from the backend
    // (e.g. "Read <repo>/Jenkinsfile", "Run `wc -l ...`") — show
    // the remainder after the tool name/file chip rather than re-deriving
    // raw tool_input (which would just repeat absolute paths verbosely).
    const args = text.replace(toolName, "").trim();
    const preview = (event.result_preview || "").trim();
    body = (
      <div className="rich-tool">
        <div className="rich-tool-row">
          <span className="rich-tool-name">{toolName}</span>
          {file && <span className="rich-file-chip">{file}</span>}
          <span className="rich-tool-args" title={args}>
            {file ? args.replace(new RegExp(`[^=]*${file.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}[^,]*,?\\s*`), "").trim() : args}
          </span>
        </div>
        {preview && <pre className="rich-tool-result">{preview}</pre>}
      </div>
    );
  } else if (kind === "state") {
    body = (
      <div className="rich-status-change">
        <span className="rich-status-arrow">→</span>
        <span className="rich-status-badge">{text}</span>
      </div>
    );
  } else if (kind === "commit") {
    const hashMatch = text.match(/([a-f0-9]{7,40})/);
    body = (
      <div className="rich-commit">
        {hashMatch && <span className="rich-commit-hash">{hashMatch[1].slice(0, 7)}</span>}
        <span className="rich-commit-msg">{linkify(text.replace(hashMatch?.[0] || "", "").trim())}</span>
      </div>
    );
  } else if (kind === "review") {
    const passed = /pass/i.test(text);
    body = (
      <div className="rich-review-verdict">
        <span className={`rich-verdict-chip ${passed ? "pass" : "fail"}`}>
          {passed ? "PASSED" : "FAILED"}
        </span>
        <span className="rich-body">{linkify(text)}</span>
      </div>
    );
  } else if (kind === "tests" || kind === "lint" || kind === "quality_gate" || kind === "quality_gate_failed") {
    const passed = kind !== "quality_gate_failed" && (/pass/i.test(text) || /clean/i.test(text));
    const label = kind.startsWith("quality_gate") ? "QUALITY GATE" : kind === "tests" ? "TESTS" : "LINT";
    body = (
      <div className="rich-review-verdict">
        <span className={`rich-verdict-chip ${passed ? "pass" : "fail"}`}>
          {label} {passed ? "PASS" : "FAIL"}
        </span>
        <span className="rich-body">{linkify(text)}</span>
      </div>
    );
  } else if (kind === "env_setup_failed") {
    body = (
      <div className="rich-review-verdict">
        <span className="rich-verdict-chip fail">ENV SETUP FAILED</span>
        <span className="rich-body">{linkify(text)}</span>
      </div>
    );
  } else if (kind === "agent_text") {
    body = <div className="rich-agent-prose"><Markdown>{text}</Markdown></div>;
  } else if (kind === "thinking") {
    body = <ThinkingBlock text={text} elapsed={elapsed} />;
  } else if (kind === "knowledge_accessed") {
    body = <KnowledgeApplied event={event} />;
  } else if (kind === "supervisor_decision") {
    // `text` is only the verdict word; the guidance lives in `message`.
    // Rendering just the word made 33 real corrections look like noise.
    const isCorrection = text === "correct";
    body = (
      <div className="rich-supervisor">
        <span className={`rich-verdict-chip ${isCorrection ? "fail" : "pass"}`}>
          {isCorrection ? "COURSE-CORRECT" : "ON TRACK"}
        </span>
        {event.message && <span className="rich-body">{linkify(event.message)}</span>}
      </div>
    );
  } else {
    body = <span className="rich-body">{linkify(text)}</span>;
  }

  return (
    <div className={`activity-event-rich role-${role} ak-${kind}`}>
      <div className="rich-meta">
        <span className="rich-ts">{fmtTs(event.ts)}</span>
        <span className="rich-role">{ROLE_LABEL[role]}</span>
        <span className={`rich-kind ak-${kind}`}>{eventLabel(kind)}</span>
        {elapsed > 2 && <span className="rich-elapsed">+{fmtDuration(elapsed)}</span>}
      </div>
      {body}
    </div>
  );
}

// A single node card in the tree diagram

// Agent log modal — opens when clicking a node
// One digest card for both surfaces: an agent's modal and the Activity page.
// Deterministic (summaries.js) — no model call, computed from the same events
// the raw feed shows, so it can't say anything the feed doesn't back up.
function SummaryCard({ summary }) {
  if (!summary) return null;
  return (
    <div className="digest-card">
      <div className="digest-headline">{summary.headline}</div>
      <div className="digest-facts">
        {summary.facts.map(([label, value]) => (
          <div className="digest-fact" key={label}>
            <span className="digest-fact-label">{label}</span>
            <span className="digest-fact-value">{value}</span>
          </div>
        ))}
      </div>
      {summary.highlights.length > 0 && (
        <ul className="digest-highlights">
          {summary.highlights.map((h, i) => <li key={i}>{h}</li>)}
        </ul>
      )}
      {summary.issues.length > 0 && (
        <ul className="digest-issues">
          {summary.issues.map((h, i) => {
            // An issue is either a plain string (still open — red) or an object
            // carrying a resolved flag. A resolved finding was found-and-fixed by
            // the loop, so it renders green with a "fixed" marker instead of red.
            const text = typeof h === "string" ? h : h.text;
            const resolved = typeof h === "object" && h !== null && h.resolved;
            return (
              <li key={i} className={resolved ? "digest-issue-resolved" : undefined}>
                {resolved ? "✓ " : ""}{text}{resolved ? " — fixed" : ""}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// The expanded agent's log — a full-width reading pane below the pipeline
// (replaces the old pop-up modal). Rendered wide because a stage column is far
// too narrow for a readable log; a header ties it back to the exact agent and
// stage that is open. Same deterministic digest (summaries.js) + raw event feed.
function AgentLogPanel({ agent, stage, events, onClose }) {
  const agentEvents = agent.subagentTaskId
    ? events.filter(e => (e.kind === "subagent_start" || e.kind === "subagent_progress" || e.kind === "subagent_done") && e.task_id === agent.subagentTaskId)
    : events.filter(e => eventSource(e) === agent.id);
  const elapsed = agentEvents.length > 1 ? agentEvents[agentEvents.length - 1].ts - agentEvents[0].ts : 0;

  // Keep the newest event in view as a live agent streams — the SYSTEM tab's
  // primary use is watching a running agent (restores the behavior the old
  // AgentLogModal had via its end-anchor). Sticky-bottom: scroll only the
  // events list (never the page/drawer), and ONLY when the operator is already
  // at the bottom — so scrolling up to read history is never yanked back down.
  const logRef = useRef(null);
  const stickRef = useRef(true);
  const onLogScroll = () => {
    const el = logRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  };
  useEffect(() => {
    const el = logRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [agentEvents.length]);

  return (
    <div className="fx-log-panel" style={{ "--node-color": agent.color }}>
      <div className="fx-log-head">
        <span className="fx-log-icon">{agent.icon}</span>
        <div className="fx-log-titles">
          <div className="fx-log-type">{agent.type}{stage ? ` · ${stage}` : ""}</div>
          <div className="fx-log-name">{agent.label}</div>
        </div>
        <div className="fx-log-stats">
          <span className="fx-log-stat">Events <b>{agentEvents.length}</b></span>
          {elapsed > 0 && <span className="fx-log-stat">Duration <b>{fmtDuration(elapsed)}</b></span>}
        </div>
        {onClose && (
          <button className="fx-log-close" onClick={onClose} aria-label="Close agent log">
            <IconX size={15} />
          </button>
        )}
      </div>
      {agent.desc && <div className="fx-log-desc">{agent.desc}</div>}
      <SummaryCard summary={agentSummary(events, agent)} />
      <div className="fx-log-events" ref={logRef} onScroll={onLogScroll}>
        {agentEvents.length === 0 ? (
          <div className="fx-log-empty">No events from this agent yet.</div>
        ) : (
          agentEvents.map((e, i) => {
            const prev = i > 0 ? agentEvents[i - 1] : null;
            const dt = prev ? e.ts - prev.ts : 0;
            return <RichEvent key={i} event={e} elapsed={dt} role={eventSource(e)} />;
          })
        )}
      </div>
    </div>
  );
}

const clipText = (s, n) => {
  const x = (s || "").replace(/\s+/g, " ").trim();
  return x.length > n ? x.slice(0, n - 1) + "…" : x;
};

// One lane of the functionality board: the stage's status header on top,
// its full agent tree always visible beneath — nothing hidden behind clicks.
// The arrow out of a header flows when the NEXT stage is the one running.

// M7 / TS: one compact row per agent — the stage head carries status/counts/
// model, so the row only needs identity + a live status dot + a chevron that
// toggles the agent's log (the wide pane below the board). Replaces the
// AgentNode cards that repeated the head's facts at ~150px each.
function RoleRow({ agent, state, onClick, laneModel, expanded }) {
  if (!agent) return null;
  const status = (state && state.status) || "idle";
  // The lane head already prints its primary's model in full; repeating it
  // here truncates to "op…" in a narrow lane — show a row's model only when
  // it differs from the lane's (e.g. a haiku researcher under the coder).
  const showModel = agent.model && agent.model !== laneModel;
  return (
    <button type="button" className={`fx-role-row s-${status}${expanded ? " expanded" : ""}`}
            onClick={onClick} aria-expanded={expanded}
            title={`${agent.label} — ${expanded ? "hide" : "show"} log`}>
      <span className="fx-role-icon" aria-hidden="true">{agent.icon}</span>
      <span className="fx-role-name">{agent.label}</span>
      {showModel && <span className="fx-role-model">{agent.model.replace("claude-", "")}</span>}
      <span className={`fx-role-dot s-${status}`} aria-label={status} />
      <span className="fx-role-chev" aria-hidden="true">›</span>
    </button>
  );
}

// One agent in the tree: its row. Depth 0 is the stage's primary agent; depth 1
// is a nested child (the Coding Supervisor, a planner's investigation
// subagents). The expanded agent's log renders wide below the whole board.
function AgentRowNode({ agent, depth, isLastChild, state, laneModel, expanded, onToggle }) {
  if (!agent) return null;
  return (
    <div className={`fx-node${depth ? " fx-node--child" : ""}${isLastChild ? " is-last" : ""}`}>
      <RoleRow agent={agent} state={state} laneModel={laneModel}
               expanded={expanded} onClick={onToggle} />
    </div>
  );
}

function FxLane({ g, isCurrent, flowOut, agentStates, node, expandedId, onToggle }) {
  // Resolve the pure row plan (functionalities.js) into agent objects: role ids
  // get their model binding via node(); discovered subagents carry their own.
  const nodes = laneAgentRows(g)
    .map((r) => ({ ...r, agent: r.kind === "role" ? node(r.id) : r.sub }))
    .filter((r) => r.agent);
  const primaries = nodes.filter((r) => r.depth === 0);
  const children  = nodes.filter((r) => r.depth === 1);
  const hasPrimary = primaries.length > 0;
  const rowFor = (r) => (
    <AgentRowNode key={r.agent.id} agent={r.agent} depth={r.depth} isLastChild={r.isLastChild}
                  state={agentStates[r.agent.id]} laneModel={g.model}
                  expanded={expandedId === r.agent.id}
                  onToggle={() => onToggle(r.agent.id)} />
  );
  return (
    <div className={`fx-lane s-${g.status}${isCurrent ? " current" : ""}`}
         style={{ "--fx-color": g.color }}>
      <div className={`fx-head${flowOut ? " flow-out" : ""}${hasPrimary ? " clickable" : ""}`} title={g.desc}
           role={hasPrimary ? "button" : undefined} tabIndex={hasPrimary ? 0 : undefined}
           onClick={() => hasPrimary && onToggle(primaries[0].agent.id)}
           onKeyDown={(e) => { if ((e.key === "Enter" || e.key === " ") && hasPrimary) { e.preventDefault(); onToggle(primaries[0].agent.id); } }}>
        <div className="fx-head-top">
          <span className="fx-icon" aria-hidden="true">{g.icon}</span>
          <span className="fx-label">{g.label}</span>
          {isCurrent && <span className="fx-live-dot" role="img" aria-label="running now" />}
        </div>
        <div className="fx-head-meta">
          <span className={`fx-status s-${g.status}`}>{g.status}</span>
          {g.present && (
            <span className="fx-counts" title={fxCountsLabel(g.agentCount, g.eventCount)}>
              {fxCountsLabel(g.agentCount, g.eventCount)}
            </span>
          )}
        </div>
        {g.model && <div className="fx-model" title={g.model}>{g.model}</div>}
      </div>
      {nodes.length > 0 ? (
        <div className="fx-role-list">
          {primaries.map(rowFor)}
          {children.length > 0 && (
            <div className="fx-children">{children.map(rowFor)}</div>
          )}
        </div>
      ) : (
        <div className="fx-lane-empty">not started yet</div>
      )}
    </div>
  );
}

function SystemTab({ taskId, task, isActive }) {
  const [events, setEvents] = useState([]);
  // Single-open accordion: the id of the agent whose inline log is expanded,
  // or null. Clicking an agent (or its stage head, for the primary) toggles it.
  const [expandedId, setExpandedId] = useState(null);
  const toggleAgent = (id) => setExpandedId((cur) => (cur === id ? null : id));
  const currentFx = currentFunctionality(events, isActive);

  useEffect(() => {
    let cancelled = false;
    if (isActive) {
      let seedTs = 0;
      fetchTaskEvents(taskId).then((evts) => {
        if (cancelled) return;
        seedTs = evts.length > 0 ? Math.max(...evts.map(e => e.ts || 0)) : 0;
        // MERGE, don't replace: `seedTs` is 0 until this resolves, so anything the stream
        // delivers during the fetch window is appended — and a plain setEvents(evts) then
        // threw those away permanently (the stream never re-sends them).
        setEvents((streamed) => {
          const duringFetch = streamed.filter((e) => (e.ts || 0) > seedTs);
          return duringFetch.length > 0 ? [...evts, ...duringFetch] : evts;
        });
      }).catch(() => {});
      const es = connectTaskSSE(taskId,
        (evt) => {
          if (cancelled) return;
          if ((evt.ts || 0) > seedTs) {
            setEvents((prev) => [...prev, evt]);
          }
        },
        () => {},
      );
      return () => { cancelled = true; es.close(); };
    }
    async function poll() {
      while (!cancelled) {
        try {
          const evts = await fetchTaskEvents(taskId);
          if (!cancelled) setEvents(evts);
        } catch { /* ignore */ }
        await new Promise((r) => setTimeout(r, 10000));
      }
    }
    poll();
    return () => { cancelled = true; };
  }, [taskId, isActive]);

  // Derive per-agent status. On a task that is NOT running (parked,
  // escalated, awaiting approval, done, failed) nothing is executing — a
  // badge stuck on "active" is a lie the operator acts on (task 6cfdb936
  // showed 5 ACTIVE stages while escalated with zero live sessions).
  const agentStates = {};
  for (const a of AGENTS) {
    // SCRUM-16: ACTIVE chips require a live claimed session, not merely an
    // active status — an unclaimed queued task has zero running agents.
    agentStates[a.id] = clampAgentState(
      deriveAgentStatus(events, a.id), isActive && task?.claimed === true);
  }

  // Discover dynamically-spawned subagents from events, grouped by the role
  // that spawned them — each role column owns its own children.
  const subagents = discoverSubagents(events);
  // Subagent status comes from the discovery function, not deriveAgentStatus.
  for (const sub of subagents) {
    const subEvents = events.filter(e =>
      (e.kind === "subagent_start" || e.kind === "subagent_progress" || e.kind === "subagent_done")
      && e.task_id === sub.subagentTaskId
    );
    agentStates[sub.id] = clampAgentState(
      { status: sub.status, count: subEvents.length, lastText: subEvents.length > 0 ? subEvents[subEvents.length - 1].text || "" : "" },
      isActive && task?.claimed === true);
  }

  const totalElapsed = events.length > 1 ? events[events.length - 1].ts - events[0].ts : 0;

  // Label each node with the model that actually ran it (from the `models`
  // event), so "the coder is Sonnet 5" is verifiable at a glance rather than
  // assumed from a config file that may be shadowing the real default.
  const nodeModels = modelsByNode(events);
  const withModel  = (a) => (a && nodeModels[a.id] ? { ...a, model: nodeModels[a.id] } : a);
  const node       = (id) => withModel(AGENTS.find(a => a.id === id));

  // The page's top layer: the five functionalities of the pipeline, in the
  // order the orchestrator drives them. Each groups its roles and their
  // subagents; clicking one expands that stage's agent tree.
  const groups = groupFunctionalities({ agentStates, subagents, models: nodeModels, events });

  // Resolve the expanded agent (a primary role or a discovered subagent) and the
  // stage it belongs to, so its log can render in the wide pane below the board.
  const agentIndex = {};
  for (const g of groups) {
    for (const r of laneAgentRows(g)) {
      const a = r.kind === "role" ? node(r.id) : r.sub;
      if (a) agentIndex[a.id] = { agent: a, stage: g.label };
    }
  }
  const openAgent = expandedId ? agentIndex[expandedId] : null;

  if (events.length === 0) {
    return (
      <div className="sys-view sys-view-empty">
        <div className="so-diff-empty">
          {isActive ? "Waiting for events…" : "No events recorded for this task."}
        </div>
      </div>
    );
  }

  // The one line the operator needs first: running where, or waiting on what.
  const banner = (() => {
    const st = task?.status || "";
    if (st === "awaiting_approval") return {
      cls: "waiting", text: "Waiting for you — review & merge the PR", icon: "⏸" };
    if (st === "escalated" || st === "awaiting_input") return {
      cls: "waiting",
      text: `Waiting for you — ${clipText((task?.blocker || {}).question || "a decision is needed (see Activity)", 110)}`,
      icon: "⏸" };
    if (isActive) {
      const fx = groups.find((g) => g.id === currentFx);
      return { cls: "running", text: `Running — ${fx ? fx.label : "starting"}`, icon: "●" };
    }
    if (st === "done") return { cls: "done", text: "Done — PR merged", icon: "✓" };
    return null;
  })();

  return (
    <div className="sys-view">
      <div className="sys-tree">
        {banner && (
          <div className={`fx-banner ${banner.cls}`}>
            <span className="fx-banner-icon" aria-hidden="true">{banner.icon}</span>
            {banner.text}
          </div>
        )}
        {/* The functionality board: five lanes, every agent and subagent
            visible at once. The arrow between headers flows toward the stage
            that is running right now. */}
        <div className="fx-board">
          {groups.map((g, i) => (
            <FxLane key={g.id} g={g}
                    isCurrent={isActive && currentFx === g.id}
                    flowOut={isActive && i + 1 < groups.length
                             && currentFx === groups[i + 1].id}
                    agentStates={agentStates} node={node}
                    expandedId={expandedId} onToggle={toggleAgent} />
          ))}
        </div>

        {/* The expanded agent's log — wide, readable, below the pipeline. */}
        {openAgent && (
          <AgentLogPanel key={openAgent.agent.id} agent={openAgent.agent}
                         stage={openAgent.stage} events={events}
                         onClose={() => setExpandedId(null)} />
        )}

        {/* Summary stats */}
        <div className="sys-summary">
          <div className="sys-summary-item">
            <span>Events:</span>
            <span className="sys-summary-value">{events.length}</span>
          </div>
          {totalElapsed > 0 && (
            <div className="sys-summary-item">
              <span>Duration:</span>
              <span className="sys-summary-value">{fmtDuration(totalElapsed)}</span>
            </div>
          )}
          {task?.attempt_count > 0 && (
            <div className="sys-summary-item">
              <span>Attempt:</span>
              <span className="sys-summary-value">#{task.attempt_count}</span>
            </div>
          )}
          {events.length === 0 && (
            <div className="sys-summary-item" style={{ color: 'var(--text-dim)' }}>
              {isActive ? "Waiting for events…" : "No events recorded."}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


// ── Sub-tasks tab for compound parents ───────────────────────────────────
const STATUS_ICON = { done: "[ok]", failed: "[x]", pending: "[.]", implementing: "[~]", reviewing: "[?]", blocked: "[!]", compound_parent: "[+]" };

function SubtasksTab({ taskId }) {
  const [subs, setSubs] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const data = await fetchSubtasks(taskId);
      if (!cancelled) { setSubs(data); setLoading(false); }
    }
    load();
    const iv = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(iv); };
  }, [taskId]);

  if (loading) return <div className="so-diff-empty">Loading sub-tasks…</div>;
  if (subs.length === 0) return <div className="so-diff-empty">No sub-tasks.</div>;

  const done = subs.filter(s => s.status === "done").length;

  return (
    <div className="subtasks-tab">
      <div className="subtasks-header">
        <span className="subtasks-progress">{done} / {subs.length} complete</span>
        <div className="subtasks-bar">
          <div className="subtasks-bar-fill" style={{ width: `${(done / subs.length) * 100}%` }} />
        </div>
      </div>
      <div className="subtasks-list">
        {subs.map(s => (
          <div key={s.id} className={`subtask-card status-${s.status}`}>
            <span className="subtask-icon">{STATUS_ICON[s.status] || "·"}</span>
            <div className="subtask-info">
              <span className="subtask-title">{s.title}</span>
              <span className="subtask-meta">
                {s.status}{s.kind !== "feature" ? ` · ${s.kind}` : ""}
                {s.live_status ? ` · ${s.live_status}` : ""}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}


// One SSE (live) / poll (inactive) / fetch-once (terminal) event feed for a
// task, with correct teardown. Each consumer that mounts gets its own
// subscription: the primary digest always holds one; the collapsed "Event log"
// section opens a second only while it is expanded (it mounts lazily), and both
// tear down on unmount. Not a shared/deduped feed — if that ever matters, lift
// it into a Context.
function useTaskEvents(taskId, task, isActive) {
  const [events, setEvents] = useState([]);
  useEffect(() => {
    let cancelled = false;
    // Phase 4a: use SSE for active tasks (real-time), poll for inactive.
    if (isActive) {
      // Seed with existing events, then stream new ones.
      // Track seed watermark to avoid duplicates from SSE.
      let seedTs = 0;
      fetchTaskEvents(taskId).then((evts) => {
        if (cancelled) return;
        setEvents(evts);
        seedTs = evts.length > 0 ? Math.max(...evts.map(e => e.ts || 0)) : 0;
      }).catch(() => {});
      const es = connectTaskSSE(taskId,
        (evt) => {
          if (cancelled) return;
          // Only append events newer than the seed watermark (dedup).
          if ((evt.ts || 0) > seedTs) {
            setEvents((prev) => [...prev, evt]);
          }
        },
        () => { /* stream ended — will fall back on next render cycle */ },
      );
      return () => { cancelled = true; es.close(); };
    }
    // A terminal task's events never change — fetch once, never poll.
    if (isTerminalStatus(task?.status)) {
      fetchTaskEvents(taskId).then((evts) => { if (!cancelled) setEvents(evts); }).catch(() => {});
      return () => { cancelled = true; };
    }
    // Inactive but not terminal (queued/parked — can still resume): slow poll.
    async function poll() {
      while (!cancelled) {
        try {
          const evts = await fetchTaskEvents(taskId);
          if (!cancelled) setEvents(evts);
        } catch { /* ignore */ }
        await new Promise((r) => setTimeout(r, 10000));
      }
    }
    poll();
    return () => { cancelled = true; };
  }, [taskId, isActive]);
  return events;
}

// PRIMARY digest — the redesign's answer to "no one understands what's going
// on". Below the agent legend it shows, tessl-style: a short description of the
// task, the ARTIFACTS it produced (PR / files / review verdict), ONE compact
// live-status line, and the digested run summary. The raw event stream is NOT
// here — it lives in the collapsed "Event log" accordion section below, so the
// glance is a digest, never a wall of logs.
function ActivityTab({ taskId, task, isActive, diff }) {
  const events = useTaskEvents(taskId, task, isActive);
  const artifacts = useMemo(() => artifactsFor(task, diff), [task, diff]);

  const lastEvent = events.length > 0 ? events[events.length - 1] : null;
  const isWorking = isActive && !!lastEvent;
  const lastRole = lastEvent ? eventSource(lastEvent) : null;
  const totalElapsed = events.length > 1 ? events[events.length - 1].ts - events[0].ts : 0;

  // Turn counter: count tool_use events from the agent since the last attempt_start.
  let turnCount = 0;
  let maxTurns = null;
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].kind === "attempt_start") {
      maxTurns = events[i].max_turns || null;
      break;
    }
    if (events[i].kind === "tool_use" && eventSource(events[i]) === "agent") {
      turnCount++;
    }
  }
  const turnPct = maxTurns ? Math.min(100, (turnCount / maxTurns) * 100) : null;

  return (
    <div className="activity-feed ph-no-capture">
      <div className="activity-legend">
        <span className="al-role role-worker"><i />Orchestrator — drives pipeline</span>
        <span className="al-role role-agent"><i />Worker — reads &amp; edits code</span>
        <span className="al-role role-supervisor"><i />Supervisor — course-corrects</span>
        <span className="al-role role-reviewer"><i />Reviewer — gates quality</span>
        {/* fontSize below was 0.7rem, which resolves to 9.8px against this app's
            14px root — the one inline style left under the 12px functional-text
            floor on the four surfaces the 2026-08-29 craft pass covers. */}
        {totalElapsed > 0 && (
          <span style={{ marginLeft: 'auto', fontSize: '12px', color: 'var(--fg-dim)' }}>
            {events.length} events · {fmtDuration(totalElapsed)}
          </span>
        )}
      </div>
      {task?.title && (
        <div className="run-desc">
          <span className="run-desc-label">Task</span>
          <span className="run-desc-text">{task.title}</span>
        </div>
      )}
      {artifacts.length > 0 && (
        <div className="run-artifacts" data-testid="run-artifacts">
          <div className="run-artifacts-label">Artifacts</div>
          {artifacts.map((a) => (
            <div className="run-artifact" key={a.key}>
              <span className="run-artifact-key">{a.label}</span>
              {a.href ? (
                <a className="run-artifact-val" href={a.href} target="_blank" rel="noreferrer">{a.value} ↗</a>
              ) : (
                <span className={`run-artifact-val${a.tone ? ` tone-${a.tone}` : ""}`}>{a.value}</span>
              )}
            </div>
          ))}
        </div>
      )}
      {isWorking && (
        <div className={`activity-status-bar role-${lastRole}`}>
          <span className="activity-pulse" />
          <span className="activity-status-text">
            {ROLE_LABEL[lastRole]} · {eventLabel(lastEvent.kind)}: {lastEvent.text}
          </span>
        </div>
      )}
      {isWorking && turnCount > 0 && (
        <div className="turn-counter">
          <span className="turn-label">Turn {turnCount}{maxTurns ? ` / ${maxTurns}` : ""}</span>
          {turnPct !== null && (
            <div className="turn-bar">
              <div className="turn-bar-fill" style={{ width: `${turnPct}%` }} />
            </div>
          )}
        </div>
      )}
      {(() => {
        const planFiles = (task?.context?.spec?.files_to_change) || [];
        if (planFiles.length === 0) return null;
        const editedBasenames = new Set(
          events
            .filter(e => e.kind === "tool_use" && ["Edit", "Write", "MultiEdit", "NotebookEdit"].includes(e.tool_name))
            .map(e => toolFile(e))
            .filter(Boolean)
        );
        const extraFiles = [...editedBasenames].filter(f => !planFiles.some(pf => pf.endsWith(f)));
        return (
          <div className="plan-tracker">
            <span className="plan-tracker-label">Files:</span>
            {planFiles.map((f, i) => {
              const base = f.split("/").pop();
              const done = editedBasenames.has(base);
              return <span key={i} className={`plan-file ${done ? "done" : ""}`}>{done ? "[x]" : "[ ]"} {base}</span>;
            })}
            {extraFiles.map((f, i) => (
              <span key={`x-${i}`} className="plan-file extra">[+] {f}</span>
            ))}
          </div>
        );
      })()}
      <SummaryCard summary={taskSummary(events)} />
      {events.length === 0 && (
        <div className="so-diff-empty">
          {isActive ? "Waiting for events…" : "No events recorded for this task yet."}
        </div>
      )}
    </div>
  );
}

// The raw, chronological event stream — every tool call and message. Demoted
// out of the primary pane into the collapsed "Event log" accordion section so
// it is available on demand without burying the digest. Mounts (and streams)
// only when the section is opened.
function ActivityLog({ taskId, task, isActive }) {
  const events = useTaskEvents(taskId, task, isActive);
  // U1: only the newest N events enter the DOM. Rendering a long task's full
  // history (2015 events on 84251cb2) froze the tab exactly when the task was
  // interesting.
  const [windowSize, setWindowSize] = useState(150);
  const endRef = useRef(null);

  useEffect(() => {
    const el = endRef.current;
    if (!el) return;
    // Only follow the live stream when the reader is already near the bottom.
    // The section shares one scroll container (.so-scroll) with the rest of the
    // inspector, so an unconditional scroll-to-latest would yank a reader who
    // scrolled up on every streamed event.
    //
    // Interaction with G-2's digest-growth compensation (this component's
    // `scrollRef`/`primaryStreamRef` effect, above): both write `.so-scroll`'s
    // `scrollTop`, but their arm conditions are near-disjoint by construction.
    // G-2 only fires once the digest (`.so-primary-stream`, which sits ABOVE
    // this section in the document) is scrolled ENTIRELY out of view — which
    // this "near the bottom" check (within 160px of the content's end) implies
    // in practice, since the digest is nowhere near .so-scroll's bottom on any
    // task with more than a screenful of content. When both are armed they
    // agree on direction (both are following new content DOWN the page), so a
    // same-tick G-2 `scrollTop +=` lands as an instant jump the in-flight
    // smooth-scroll animation resumes from — it does not fight it, because
    // `scrollIntoView`'s target is `el`'s position, which is unaffected by an
    // upstream `scrollTop` write; the animation simply continues toward the
    // same element from wherever the jump left it. No guard added: an
    // occasional single extra frame of adjustment here is not the drift class
    // this fix exists for (a static reading position moving on its own) — it
    // is the two features cooperating to keep the SAME reader at the bottom.
    const sc = el.closest(".so-scroll");
    if (sc && sc.scrollHeight - sc.scrollTop - sc.clientHeight > 160) return;
    el.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  if (events.length === 0) {
    return (
      <div className="activity-feed ph-no-capture">
        <div className="so-diff-empty">
          {isActive ? "Waiting for events…" : "No events recorded for this task."}
        </div>
      </div>
    );
  }

  return (
    <div className="activity-feed ph-no-capture">
      <div className="activity-log">
        {events.length > windowSize && (
          <button
            className="activity-load-older"
            onClick={() => setWindowSize((s) => s + 300)}
          >
            Show older events ({events.length - windowSize} hidden)
          </button>
        )}
        {(() => {
          const visible = events.length > windowSize
            ? events.slice(-windowSize) : events;
          // Keys are the event's ABSOLUTE index in the (append-only) feed, not its index
          // in the sliding window: a window index shifts on every streamed event, which
          // silently re-attached an expanded reasoning block to a different event.
          const offset = events.length - visible.length;
          // `prevTs` walks forward with the render instead of `visible.indexOf(e)` — that
          // was a linear scan per row, per render, on every SSE frame (a real task here
          // has 2,015 events).
          let prevTs = visible.length > 0 ? visible[0].ts : 0;
          let idx = 0;  // position in `visible`, walked forward — never searched for.
          return groupConsecutiveEvents(visible).map((item) => {
            if (item._group) {
              const role = eventSource(item.events[0]);
              const key = `g-${offset + idx}`;
              idx += item.events.length;
              prevTs = item.events[item.events.length - 1].ts;
              return <GroupedEvents key={key} group={item} role={role} />;
            }
            const e = item;
            const key = `e-${offset + idx}`;
            idx += 1;
            const elapsed = e.ts - prevTs;
            prevTs = e.ts;
            const role = eventSource(e);
            return <RichEvent key={key} event={e} elapsed={elapsed} role={role} />;
          });
        })()}
        <div ref={endRef} />
      </div>
    </div>
  );
}

function fmtTs(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtDuration(secs) {
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

// Desktop only — window.nhDesktop.openPath is not injected in the web build,
// so this renders nothing there. shell.openPath (main.mjs) hands the folder to
// the OS default handler; we don't know the user's editor, so the label says
// "repo", not "editor".
function OpenRepoButton({ repoPath }) {
  const [msg, setMsg] = useState(null);
  useEffect(() => {
    if (!msg) return;
    const t = setTimeout(() => setMsg(null), 2000);
    return () => clearTimeout(t);
  }, [msg]);
  if (!window.nhDesktop?.openPath) return null;
  return (
    <button
      type="button"
      className="btn btn-post-one"
      data-testid="open-repo-btn"
      onClick={async () => {
        const res = await window.nhDesktop.openPath(repoPath);
        if (!res?.ok) setMsg(res?.error || "could not open");
      }}
    >
      {msg || "Open repo"}
    </button>
  );
}

function DetailsTab({ task, onJump = null }) {
  // Task 7: the predecessor's title, for the "Follows: <id8> <title>" line
  // below — fetched lazily, only while Details is open (this component's own
  // lazy-render boundary, same as the accordion's), and only when there IS a
  // follows_id to resolve.
  const [followed, setFollowed] = useState(null);
  useEffect(() => {
    if (!task?.follows_id) { setFollowed(null); return undefined; }
    let cancelled = false;
    fetchTask(task.follows_id).then((t) => { if (!cancelled) setFollowed(t); }).catch(() => {});
    return () => { cancelled = true; };
  }, [task?.follows_id]);

  if (!task) return <div className="so-diff-empty">Loading…</div>;
  const findings = task.context?.findings;
  const followedTitle = followed?.title
    ? (followed.title.length > 60 ? `${followed.title.slice(0, 57)}…` : followed.title)
    : null;
  return (
    <>
      {task.description && (
        <section>
          <div className="so-section-label">Description</div>
          {/* ph-no-capture: the spec/description is operator content. */}
          <div className="so-description ph-no-capture">{task.description}</div>
        </section>
      )}
      {task.acceptance_criteria?.length > 0 && (
        <section>
          <div className="so-section-label">Acceptance criteria</div>
          <ul className="so-criteria ph-no-capture" data-testid="criteria-list">
            {task.acceptance_criteria.map((c, i) => {
              const progress = task.context?.progress?.acceptance_criteria?.[i];
              const status = progress?.status;
              return (
                <li key={i} className={`criterion-tracked ${status || "not_started"}`}>
                  <span className="criterion-status-icon">
                    {status === "done" && <IconCheck size={12} />}
                    {status === "in_progress" && <span className="criterion-spinner" />}
                  </span>
                  <span>{c}</span>
                  {progress?.evidence && <div className="criterion-evidence">{progress.evidence}</div>}
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {findings && (
        <section>
          <div className="so-section-label">
            {task.kind === "design_doc" ? "Design document" : "Investigation findings"}
          </div>
          <pre className="so-findings">{findings}</pre>
        </section>
      )}
      {task.blocker && <BlockerSection blocker={task.blocker} terminal={isTerminalStatus(task.status)} />}
      {task.repo_path && (
        <section>
          <div className="so-section-label">Repo</div>
          <div className="so-repo-path">{task.repo_path}</div>
          <OpenRepoButton repoPath={task.repo_path} />
        </section>
      )}
      {/* Task-level facts the five-facts board card no longer carries as its
          own chips (spec C2) — source, kind, priority, an operator cancel, and
          the escalation latency that used to be its own card line. */}
      {(task.source || (task.kind && task.kind !== "feature") || task.priority
        || task.cancelled || task.follows_id || escalationLatencyLine(task)) && (
        <section>
          <div className="so-section-label">Task info</div>
          <div className="so-task-info">
            {task.source && <div>Source: {task.source}</div>}
            {task.kind && task.kind !== "feature" && <div>Kind: {task.kind}</div>}
            {task.priority && <div>Priority: {task.priority}</div>}
            {task.cancelled && <div>Cancelled by operator</div>}
            {task.follows_id && (
              <div>
                Follows:{" "}
                {onJump ? (
                  <button
                    type="button"
                    className="so-follows-link"
                    onClick={() => onJump(task.follows_id)}
                  >
                    {task.follows_id.slice(0, 8)}{followedTitle ? ` ${followedTitle}` : ""}
                  </button>
                ) : (
                  <span>{task.follows_id.slice(0, 8)}{followedTitle ? ` ${followedTitle}` : ""}</span>
                )}
              </div>
            )}
            {escalationLatencyLine(task) && <div>Escalation: {escalationLatencyLine(task)}</div>}
          </div>
        </section>
      )}
    </>
  );
}

function SpecTab({ task, onRefresh }) {
  const [requestingChanges, setRequestingChanges] = useState(false);
  const [changeNote, setChangeNote] = useState("");
  const [specBusy, setSpecBusy] = useState(false);
  if (!task) return <div className="so-diff-empty">Loading…</div>;
  const spec = task.context?.spec;
  const criteria = task.acceptance_criteria || [];
  // D6: the product plan ("What we understood") and the technical plan ("How
  // we'll build it") are paired here. The understanding is the enriched
  // acceptance criteria from intake; the build plan is context.spec.
  const understood = criteria.length > 0 && (
    <section data-testid="what-we-understood">
      <div className="so-section-label">What we understood</div>
      <ul className="so-criteria">{criteria.map((c, i) => <li key={i}><Markdown>{c}</Markdown></li>)}</ul>
    </section>
  );
  const hasSpec = !!(spec && (spec.approach || spec.files_to_change?.length ||
    spec.test_plan || spec.out_of_scope?.length || spec.verification));
  if (!hasSpec) {
    const isEarlyStatus = ["pending", "context", "planning"].includes(task.status);
    return (
      <div data-testid="spec-tab">
      {understood}
      <div className="so-diff-empty" data-testid="spec-empty">
        {understood && <div className="so-section-label" style={{ marginTop: '0.6rem' }}>How we&rsquo;ll build it</div>}
        {["code_review", "investigation", "design_doc", "ci_fix"].includes(task.kind)
          ? `Spec generation doesn't apply to ${task.kind} tasks.`
          : isEarlyStatus
          ? "Spec not generated yet."
          : "No spec recorded for this attempt."}
        {/* orchestrator._persist_plan overwrites context.spec on every attempt —
            there is no per-attempt spec artifact, so this states scope but never
            invents a "view attempt N's spec" link. */}
        {!isEarlyStatus && !["code_review", "investigation", "design_doc", "ci_fix"].includes(task.kind) && (
          // Inherits .so-diff-empty's muted color/size — no new CSS class needed.
          <div>Specs are kept for the latest plan only — earlier attempts&rsquo; specs are not retained.</div>
        )}
      </div>
      </div>
    );
  }
  const planSize = spec.files_to_change?.length || 0;
  return (
    <div data-testid="spec-tab">
      {understood}
      {understood && <div className="so-section-label">How we&rsquo;ll build it</div>}
      {spec.files_to_change?.length > 0 && (
        <section>
          <div className="so-section-label">Files to Change</div>
          <div className="spec-file-list">
            {spec.files_to_change.map((f, i) => (
              <div key={i} className="spec-file-item">
                <span className="spec-file-path">{f}</span>
              </div>
            ))}
          </div>
        </section>
      )}
      {spec.approach && (
        <section>
          <div className="so-section-label">Approach</div>
          <Markdown>{spec.approach}</Markdown>
        </section>
      )}
      {spec.test_plan && (
        <section>
          <div className="so-section-label">Test Plan</div>
          <Markdown>{spec.test_plan}</Markdown>
        </section>
      )}
      {spec.out_of_scope?.length > 0 && (
        <section>
          <div className="so-section-label">Out of Scope</div>
          <ul className="so-criteria spec-out-of-scope">{spec.out_of_scope.map((r, i) => <li key={i}><Markdown>{r}</Markdown></li>)}</ul>
        </section>
      )}
      {spec.verification && (
        <section>
          <div className="so-section-label">Verification</div>
          <Markdown>{spec.verification}</Markdown>
        </section>
      )}
      {(task.context?.plan_size_warning || planSize > 8) && (
        <div className="spec-plan-warning" data-testid="plan-size-warning">
          <IconAlertTriangle size={14} /> Large plan ({planSize} files) — consider decomposing
        </div>
      )}
      {task.status === "awaiting_input" && spec && (
        <div className="spec-approval-gate" data-testid="spec-approval-gate">
          {!requestingChanges ? (
            <div className="spec-approval-actions">
              <button className="btn-approve" disabled={specBusy} onClick={async () => {
                setSpecBusy(true);
                // At the plan-approval gate the approval is the option, not the
                // words: a free-text reply there is a correction and re-plans.
                const approveIdx = planApproveIndex(task);
                try {
                  if (approveIdx) await chooseBlockerOption(task.id, approveIdx);
                  else await replyTask(task.id, "spec approved");
                  if (onRefresh) onRefresh();
                }
                catch { /* handled by parent */ }
                finally { setSpecBusy(false); }
              }}>{planApproveIndex(task) ? "Approve Plan" : "Approve Spec"}</button>
              <button className="btn-request-changes" disabled={specBusy}
                onClick={() => setRequestingChanges(true)}>Request Changes</button>
            </div>
          ) : (
            <div className="spec-change-request">
              <textarea className="spec-change-textarea" rows={3}
                placeholder="Describe what needs to change…"
                value={changeNote} onChange={(e) => setChangeNote(e.target.value)} />
              <div className="spec-approval-actions">
                <button className="btn-approve" disabled={specBusy || !changeNote.trim()} onClick={async () => {
                  setSpecBusy(true);
                  try { await replyTask(task.id, changeNote.trim()); if (onRefresh) onRefresh(); }
                  catch { /* handled by parent */ }
                  finally { setSpecBusy(false); setRequestingChanges(false); setChangeNote(""); }
                }}>Submit Changes</button>
                <button className="btn-cancel" onClick={() => { setRequestingChanges(false); setChangeNote(""); }}>Cancel</button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function BlockerSection({ blocker: b, terminal }) {
  const cat = b.category ? String(b.category).replace(/_/g, " ") : null;
  const pct = b.confidence != null ? `${Math.round(b.confidence * 100)}%` : null;
  // Read-only record. The live, one-click actions for a parked task now live in
  // the DecisionPanel above the accordion (a single action surface — buttons in
  // two places invited a double-submit and split the operator's attention).
  // This section is the full evidence trail: goal, what happened, the diagnosis,
  // any question, and the wake condition — for the operator who wants the detail.
  // Once the task is terminal there's no decision left to make: the section
  // stays in the DOM (SCRUM-80 — it's still the evidence trail) but renders as
  // settled history, not a live ask.
  return (
    <section className={terminal ? "blocker-history" : undefined}>
      <div className="so-section-label blocker-label">Blocker</div>
      {cat && (
        <div className="blocker-meta">
          <span className="blocker-cat">{cat}</span>
          {pct && <span className="blocker-confidence">{pct} confidence</span>}
          {b.transient && <span className="blocker-transient">transient</span>}
        </div>
      )}
      {b.goal && (
        <div className="blocker-field">
          <div className="blocker-field-label">Goal</div>
          <div className="blocker-field-body">{b.goal}</div>
        </div>
      )}
      {b.evidence && (
        <div className="blocker-field">
          <div className="blocker-field-label">What happened</div>
          <div className="blocker-field-body">{b.evidence}</div>
        </div>
      )}
      {b.root_cause_hypothesis && b.root_cause_hypothesis !== b.question && (
        // When a task is operator-paused with a single reason, the same string
        // lands in both root_cause_hypothesis and question — don't print it
        // twice under two labels. Keep the actionable "Question for you" block
        // (it carries the reply/options) and drop the redundant "Why blocked".
        <div className="blocker-field">
          <div className="blocker-field-label">Why blocked</div>
          <div className="blocker-field-body">{b.root_cause_hypothesis}</div>
        </div>
      )}
      {b.question && (
        <div className="blocker-field blocker-question">
          <div className="blocker-field-label">{terminal ? "Asked before it ended" : "Question for you"}</div>
          <div className="blocker-field-body">{b.question}</div>
          {!terminal && b.options?.length > 0 && (
            <ul className="blocker-options">
              {b.options.map(normalizeOption).map((opt, i) => (
                <li key={i}>[{i + 1}] {opt.label}{hasAction(opt) ? " ⚡" : ""}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {b.wake_condition && (
        <div className="blocker-field">
          <div className="blocker-field-label">{terminal ? "Was waiting for" : "Wake when"}</div>
          <div className="blocker-field-body blocker-wake">{b.wake_condition}</div>
        </div>
      )}
    </section>
  );
}

// Extract the unified-diff hunk for `file:line` and trim to ±CTX lines around
// the target so each review comment shows only the relevant snippet.
const HUNK_CTX = 7;

function trimToTarget(body, targetLine) {
  if (body.length <= HUNK_CTX * 2 + 2) return body;
  const m = body[0].match(/@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/);
  if (!m || !targetLine || targetLine <= 0) return body.slice(0, HUNK_CTX * 2 + 1);
  let cur = parseInt(m[1], 10) - 1;
  let idx = -1;
  for (let i = 1; i < body.length; i++) {
    if (!body[i].startsWith("-")) cur++;
    if (cur === targetLine) { idx = i; break; }
    if (cur > targetLine + 2) break;
  }
  if (idx < 0) return body.slice(0, HUNK_CTX * 2 + 1);
  const from = Math.max(1, idx - HUNK_CTX);
  const to   = Math.min(body.length - 1, idx + HUNK_CTX);
  return [body[0], ...body.slice(from, to + 1)];
}

function extractHunk(diff, file, line) {
  if (!diff || !file) return null;
  const lines = diff.split("\n");
  const want = String(file).replace(/^[ab]\//, "");
  let secStart = -1;
  for (let j = 0; j < lines.length; j++) {
    const l = lines[j];
    if ((l.startsWith("+++ ") || l.startsWith("diff --git")) && l.includes(want)) { secStart = j; break; }
  }
  if (secStart === -1) return null;
  let secEnd = lines.length;
  for (let j = secStart + 1; j < lines.length; j++) {
    if (lines[j].startsWith("diff --git")) { secEnd = j; break; }
  }
  const hunkRe = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/;
  let best = null, bestDist = Infinity;
  for (let j = secStart; j < secEnd; j++) {
    const m = lines[j].match(hunkRe);
    if (!m) continue;
    const start = parseInt(m[1], 10);
    const count = m[2] ? parseInt(m[2], 10) : 1;
    const end   = start + count - 1;
    const body  = [lines[j]];
    let k = j + 1;
    while (k < secEnd && !lines[k].startsWith("@@") && !lines[k].startsWith("diff --git")) {
      body.push(lines[k]); k++;
    }
    if (!line || line <= 0) { if (!best) best = trimToTarget(body, -1); continue; }
    if (line >= start - 2 && line <= end + 2) return trimToTarget(body, line);
    const dist = Math.min(Math.abs(line - start), Math.abs(line - end));
    if (dist < bestDist) { bestDist = dist; best = trimToTarget(body, line); }
  }
  return best;
}

function CommentHunk({ diff, file, line }) {
  const hunk = extractHunk(diff, file, line);
  if (!hunk) return null;
  return (
    <div className="ci-hunk">
      <pre className="diff-pre">
        {hunk.map((l, idx) => (
          <div key={idx} className={`diff-line ${diffLineClass(l)}`}>{l || " "}</div>
        ))}
      </pre>
    </div>
  );
}

function ReviewTab({ task, diff, onOpenSection }) {
  const [rawOpen, setRawOpen] = useState(false);
  const [posted, setPosted] = useState({});  // index → "ok" | "error" | "busy"
  const [postingAll, setPostingAll] = useState(false);
  const [finishing, setFinishing] = useState(false);

  async function handleFinishReview() {
    setFinishing(true);
    try {
      await finishReview(task.id);  // → done; the WS task_updated refreshes the board/drawer
    } catch {
      setFinishing(false);
    }
  }

  if (!task) return <div className="so-diff-empty">Loading…</div>;

  const lastAttempt = task.attempts?.[task.attempts.length - 1];
  const checklist = lastAttempt?.review_checklist;

  if (!checklist?.items?.length) {
    // The stage strip aggregates attempt HISTORY ("Reviewing DONE" once any
    // attempt was reviewed) while this section is scoped to the CURRENT
    // attempt only — so both can be true at once and this must say which
    // attempt it means, and point at the prior one instead of reading as
    // data loss.
    const attempts = task.attempts || [];
    const n = attempts.length || 1;
    const prior = lastReviewedAttempt(task);
    const isFreshStart = n <= 1 && !prior
      && (task.status === "pending" || task.status === "context" || task.status === "implementing");
    return (
      <div className="so-diff-empty" data-testid="review-empty">
        {isFreshStart ? "Review not started yet." : `Attempt ${n} not reviewed yet.`}
        {prior && (
          <div>
            {`Attempt ${prior.attempt_number}'s review: ${prior.passed ? "PASS" : "FAIL"} — `}
            <button
              type="button"
              className="raw-toggle"
              data-testid="view-prior-review"
              onClick={() => onOpenSection && onOpenSection("attempts")}
            >
              view
            </button>
          </div>
        )}
      </div>
    );
  }

  const verdict = reviewVerdict(checklist);
  const vr = verifierRows(lastAttempt?.verifier_results);
  const mp = mergePolicyRows(task.context?.merge_policy?.[lastAttempt?.commit_sha]);
  const testResults = lastAttempt?.test_results;
  const tamperFlag = testResults?.tamper_flag;
  const testVerdict = testResultVerdict(testResults);
  const ciUrl = lastAttempt?.ci_pipeline_url;
  const rawOutput = checklist.raw_output;
  const hasPrUrl = !!(task.context?.pr_url);
  const failedIndices = checklist.items
    .map((it, i) => (!it.passed ? i : -1))
    .filter((i) => i >= 0);

  async function handlePostOne(index) {
    setPosted((p) => ({ ...p, [index]: "busy" }));
    try {
      const res = await postReviewComments(task.id, [index]);
      const r = res.results?.[0];
      setPosted((p) => ({ ...p, [index]: r?.ok ? "ok" : "error" }));
    } catch {
      setPosted((p) => ({ ...p, [index]: "error" }));
    }
  }

  async function handlePostAll() {
    setPostingAll(true);
    const toPost = failedIndices.filter((i) => posted[i] !== "ok");
    toPost.forEach((i) => setPosted((p) => ({ ...p, [i]: "busy" })));
    try {
      const res = await postReviewComments(task.id, toPost);
      for (const r of res.results || []) {
        setPosted((p) => ({ ...p, [r.index]: r.ok ? "ok" : "error" }));
      }
    } catch {
      toPost.forEach((i) => setPosted((p) => ({ ...p, [i]: "error" })));
    }
    setPostingAll(false);
  }

  const allFailedPosted = failedIndices.length > 0 &&
    failedIndices.every((i) => posted[i] === "ok");

  // A standalone code_review of someone else's PR is NOT a build gate — the job
  // is "here are the comments, approve the ones to post in your name". The
  // pass/fail-stage-criteria framing below is for the build gate; give code
  // reviews their own plain, unambiguous surface.
  if (task.kind === "code_review") {
    const comments = checklist.items
      .map((it, i) => ({ it, i }))
      .filter(({ it }) => !it.passed);
    const prUrl = task.context?.pr_url;
    const prShort = prUrl ? prUrl.replace(/^https?:\/\//, "") : null;
    const allPosted = comments.length > 0 && comments.every(({ i }) => posted[i] === "ok");
    const nUnposted = comments.filter(({ i }) => posted[i] !== "ok").length;
    return (
      <div className="cr-approve">
        <div className="cr-approve-head">
          <div className="cr-approve-title">
            {comments.length} review comment{comments.length !== 1 ? "s" : ""}
          </div>
          {prShort && (
            <a href={prUrl} target="_blank" rel="noreferrer" className="cr-approve-pr">
              on {prShort} ↗
            </a>
          )}
        </div>
        <div className="cr-approve-warn">
          These post as comments <strong>in your name</strong>. Nothing is sent
          until you approve — read each one, then post the ones you agree with.
        </div>
        {comments.length === 0 ? (
          <div className="so-diff-empty">
            The reviewer found nothing to comment on — no comments to post.
          </div>
        ) : (
          <>
            <div className="cr-approve-actions">
              {prUrl && !allPosted && (
                <button className="btn btn-approve" onClick={handlePostAll} disabled={postingAll}>
                  {postingAll ? "Posting…" : `Approve & post all ${nUnposted}`}
                </button>
              )}
              {allPosted && (
                <span className="review-posted-badge">✓ All {comments.length} posted</span>
              )}
              {!prUrl && <span className="cr-nopr">No PR URL — cannot post from here.</span>}
              {/* You decide when the review is done — post all, some, or none.
                  A code_review has no PR of its own to merge, so it won't
                  self-complete; this takes it to Done. */}
              <button
                className="btn btn-finish-review"
                onClick={handleFinishReview}
                disabled={finishing}
                title="Mark this review done — whether or not you posted every comment"
              >
                {finishing ? "Finishing…" : "Finish review →"}
              </button>
            </div>
            <div className="cr-comment-list">
              {comments.map(({ it, i }) => (
                <div key={i} className="cr-comment">
                  <div className="cr-comment-loc">
                    {it.file ? (
                      <span className="ci-filechip" title={it.file}>
                        {it.file.split("/").slice(-2).join("/")}{it.line > 0 ? `:${it.line}` : ""}
                      </span>
                    ) : (
                      <span className="cr-general">general comment</span>
                    )}
                    {it.severity && (
                      <span className={`cr-sev cr-sev-${String(it.severity).toLowerCase()}`}>
                        {it.severity}
                      </span>
                    )}
                    {posted[i] === "ok" && <span className="post-badge post-ok">✓ posted</span>}
                  </div>
                  {it.file && <CommentHunk diff={diff} file={it.file} line={it.line} />}
                  <div className="cr-comment-body">{it.comment || it.evidence || it.label}</div>
                  {prUrl && posted[i] !== "ok" && (
                    <div className="ci-post-row">
                      <button
                        className="btn btn-post-one"
                        onClick={() => handlePostOne(i)}
                        disabled={posted[i] === "busy"}
                      >
                        {posted[i] === "busy" ? "Posting…"
                          : posted[i] === "error" ? "Retry post"
                          : "Approve & post this"}
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
        {rawOutput && (
          <section>
            <button className="raw-toggle" onClick={() => setRawOpen((o) => !o)}>
              {rawOpen ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />} Reviewer's full reasoning
            </button>
            {rawOpen && <pre className="raw-output">{rawOutput}</pre>}
          </section>
        )}
      </div>
    );
  }

  return (
    <>
      {tamperFlag && (
        <div className="tamper-banner">
          TAMPER DETECTED — test count reduced between attempts
        </div>
      )}
      <section>
        <div className="so-section-label so-section-label-row">
          <span>Reviewer verdict —{" "}
            {verdict.tone === "pass"
              ? <span className="verdict-pass">{verdict.verdict}</span>
              : <span className="verdict-fail">{verdict.verdict}</span>
            }
            {/* On a PASS the findings are counted as findings with their
                severities. The old "N/M passed" ratio read as an 80% failure
                on a review that passed with 4 advisory findings. */}
            {verdict.detail && (
              <span className="verdict-detail" data-testid="verdict-detail"> - {verdict.detail}</span>
            )}
          </span>
          {checklist.items.length >= 5 && (
            <span className="review-cap-indicator" data-testid="review-cap">(capped at 5 items)</span>
          )}
          <div className="review-header-actions">
            {ciUrl && (
              <a href={ciUrl} target="_blank" rel="noreferrer" className="ci-link">
                CI pipeline →
              </a>
            )}
            {hasPrUrl && failedIndices.length > 0 && !allFailedPosted && (
              <button
                className="btn btn-post-all"
                onClick={handlePostAll}
                disabled={postingAll}
              >
                {postingAll ? "Posting…" : `Post All Comments (${failedIndices.length})`}
              </button>
            )}
            {allFailedPosted && (
              <span className="review-posted-badge">All comments posted</span>
            )}
          </div>
        </div>
        {checklist.stages && (
          <div className="review-stages" data-testid="review-stages">
            <span className={`review-stage-badge ${checklist.stages.spec_compliance?.passed ? "pass" : "fail"}`}>
              Stage 1: Spec Compliance — {checklist.stages.spec_compliance?.passed ? "PASSED" : "FAILED"}
            </span>
            <span className={`review-stage-badge ${checklist.stages.code_quality?.passed ? "pass" : "fail"}`}>
              Stage 2: Code Quality — {checklist.stages.code_quality?.passed ? "PASSED" : "FAILED"}
            </span>
          </div>
        )}
        {checklist.stages && !checklist.stages.spec_compliance?.passed && (
          <div className="unmet-criteria" data-testid="unmet-criteria">
            <div className="so-section-label">Unmet Criteria</div>
            <ul className="unmet-list">
              {checklist.items.filter(it => !it.passed).map((it, i) => (
                <li key={i} className="unmet-item">
                  <span className="ci-icon fail"><IconX size={12} /></span>
                  <span>{it.label}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {vr && (
          <div className="so-verifiers" data-testid="verifier-rows">
            <div className="so-section-label">Verifiers — {vr.summary}</div>
            <ul className="unmet-list">
              {vr.rows.map((r, i) => (
                <li key={i} className={`unmet-item ${r.ok ? "pass" : "fail"}`}>
                  <span className={`ci-icon ${r.ok ? "" : "fail"}`}>
                    {r.ok ? <IconCheck size={12} /> : <IconX size={12} />}
                  </span>
                  <span>
                    {r.id}
                    {r.ok
                      ? ` — ${r.filesChecked} file${r.filesChecked === 1 ? "" : "s"}`
                      : `${r.location ? ` — ${r.location}` : ""}${r.comment ? ` — ${r.comment}` : ""}`}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {mp && (
          <div className="so-merge-policy" data-testid="merge-policy">
            <div className="so-section-label">Merge policy — {mp.summary}</div>
            {mp.policyChanged && (
              <div className="so-merge-policy-warn" data-testid="merge-policy-changed-warning">
                <span className="ci-icon fail"><IconAlertTriangle size={12} /></span>
                <span>POLICY FILE CHANGED IN THIS PR</span>
              </div>
            )}
            <ul className="unmet-list">
              {mp.rows.map((r, i) => (
                <li key={i} className={`unmet-item ${r.ok ? "pass" : "fail"}`}>
                  <span className={`ci-icon ${r.ok ? "" : "fail"}`}>
                    {r.ok ? <IconCheck size={12} /> : <IconX size={12} />}
                  </span>
                  <span>
                    {r.name}
                    {r.detail ? ` — ${r.detail}` : ""}
                  </span>
                </li>
              ))}
            </ul>
            <div className="so-merge-policy-source">{mp.sourceLine}</div>
            {mp.problems.length > 0 && (
              <div className="so-merge-policy-warn" data-testid="merge-policy-problems">
                {mp.problems.map((p, i) => <div key={i}>{p}</div>)}
              </div>
            )}
          </div>
        )}
        <div className="so-checklist">
          {checklist.items.map((item, i) => {
            // A low/nit finding is advisory: it did not fail the gate, so it
            // gets its own row modifier and icon instead of the blocking red.
            const rowClass = checklistRowClass(item);
            const sev = severityChip(item);
            const blocking = isBlockingFinding(item);
            return (
            <div key={i} className={`checklist-item ${rowClass}`}>
              {/* header row: icon + title + severity chip + file chip */}
              <div className="ci-header">
                <span className="ci-icon">
                  {item.passed ? <IconCheck size={14} /> : blocking ? <IconX size={14} /> : <IconInfo size={14} />}
                </span>
                <span className="ci-title">{item.label}</span>
                {/* Older attempts carry no severity — omit the chip. */}
                {sev && <span className={`cr-sev cr-sev-${sev}`}>{sev}</span>}
                {item.file && (
                  <span className="ci-filechip" title={item.file + (item.line > 0 ? `:${item.line}` : "")}>
                    {item.file.split("/").slice(-2).join("/")}{item.line > 0 ? `:${item.line}` : ""}
                  </span>
                )}
                {hasPrUrl && !item.passed && posted[i] === "ok" && (
                  <span className="post-badge post-ok ci-badge">Posted</span>
                )}
              </div>
              {/* failed items get code hunk + comment */}
              {!item.passed && item.file && (
                <CommentHunk diff={diff} file={item.file} line={item.line} />
              )}
              {!item.passed && item.comment && (
                <div className="ci-comment">{item.comment}</div>
              )}
              {!item.passed && item.evidence && item.evidence !== item.comment && (
                <div className="ci-evidence">{item.evidence}</div>
              )}
              {hasPrUrl && !item.passed && posted[i] !== "ok" && (
                <div className="ci-post-row">
                  {posted[i] === "error" ? (
                    <button className="btn btn-post-retry" onClick={() => handlePostOne(i)}>
                      Retry posting
                    </button>
                  ) : (
                    <button
                      className="btn btn-post-one"
                      onClick={() => handlePostOne(i)}
                      disabled={posted[i] === "busy"}
                    >
                      {posted[i] === "busy" ? "Posting…" : "Post to PR"}
                    </button>
                  )}
                </div>
              )}
            </div>
            );
          })}
        </div>
      </section>
      {testResults && (
        <section>
          <div className="so-section-label so-section-label-row">
            <span>Test results</span>
            {testVerdict && (
              <span
                className={`verdict-${testVerdict.tone}`}
                data-testid="test-result-verdict"
              >
                {testVerdict.label}
              </span>
            )}
          </div>
          <TestResultCard result={testResults} />
        </section>
      )}
      {rawOutput && (
        <section>
          <button
            className="raw-toggle"
            onClick={() => setRawOpen((o) => !o)}
          >
            {rawOpen ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />} Reviewer reasoning
          </button>
          {rawOpen && (
            <pre className="raw-output">{rawOutput}</pre>
          )}
        </section>
      )}
      {(checklist.suggested_next || lastAttempt?.suggested_next) && (
        <section>
          <div className="review-suggested-next" data-testid="suggested-next">
            <IconInfo size={14} />
            <span>{checklist.suggested_next || lastAttempt.suggested_next}</span>
          </div>
        </section>
      )}
    </>
  );
}

function TestResultCard({ result }) {
  const passed = result.passed ?? 0;
  const failed = result.failed ?? 0;
  const errors = result.errors ?? 0;
  // node --test reports no explicit total, which rendered a contradictory
  // "55 passed · 0 total"; derive it from the parts when absent.
  const total = result.total ?? (passed + failed + errors);
  return (
    <div className="test-result-card">
      <div className="test-result-stats">
        <span className="test-result-pass"><IconCheck size={12} /> {passed} passed</span>
        {failed > 0 && <span className="test-result-fail"><IconX size={12} /> {failed} failed</span>}
        {errors > 0 && <span className="test-result-fail"><IconX size={12} /> {errors} errors</span>}
        {total > 0 && <span className="test-result-dim">{total} total</span>}
      </div>
      {result.output && <pre className="raw-output">{result.output.slice(0, 2000)}</pre>}
    </div>
  );
}

// Self-contained, offline diff renderer. A read-only unified diff doesn't need
// a 5MB CDN-loaded editor — colorized monospace lines are lighter, work without
// network, and match the operator-terminal aesthetic.
function diffLineClass(line) {
  if (line.startsWith("+++") || line.startsWith("---")) return "diff-file";
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("diff ") || line.startsWith("index ")) return "diff-meta";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  return "diff-ctx";
}

function DiffTab({ diff }) {
  if (!diff) {
    return <div className="so-diff-empty">No diff available yet.</div>;
  }
  const lines = diff.split("\n");
  return (
    // ph-no-capture: the diff renders the operator's code — never recorded.
    <div className="so-diff-wrap ph-no-capture" data-testid="diff-view">
      <pre className="diff-pre">
        {lines.map((line, i) => (
          <div key={i} className={`diff-line ${diffLineClass(line)}`}>
            {line || " "}
          </div>
        ))}
      </pre>
    </div>
  );
}

function AttemptsTab({ task }) {
  const [copiedId, setCopiedId] = useState(null);
  if (!task) return <div className="so-diff-empty">Loading…</div>;
  if (!task.attempts?.length) {
    return <div className="so-diff-empty">No attempts yet.</div>;
  }

  const getAttemptPassRate = (a) => {
    const items = a.review_checklist?.items;
    if (!items?.length) return null;
    return items.filter(it => it.passed).length;
  };
  const attempts = task.attempts;
  let stagnant = false;
  if (attempts.length >= 2) {
    const last = getAttemptPassRate(attempts[attempts.length - 1]);
    const prev = getAttemptPassRate(attempts[attempts.length - 2]);
    const total = attempts[attempts.length - 1]?.review_checklist?.items?.length || 0;
    if (last !== null && prev !== null && last === prev && last < total) stagnant = true;
  }
  const passRates = attempts.map(a => {
    const items = a.review_checklist?.items;
    if (!items?.length) return null;
    return { passed: items.filter(it => it.passed).length, total: items.length };
  });
  const hasRates = passRates.filter(Boolean).length >= 2;

  return (
    <>
      {stagnant && (
        <div className="stagnation-warning" data-testid="stagnation-warning">
          <IconAlertTriangle size={14} />
          <span>No progress between last two attempts — review pass rate unchanged</span>
        </div>
      )}
      {hasRates && (
        <div className="pass-rate-trend" data-testid="pass-rate-trend">
          <span className="pass-rate-label">Review pass rate:</span>
          {passRates.map((r, i) => r && (
            <span key={i} className="pass-rate-item">
              {i > 0 && <span className="pass-rate-arrow">→</span>}
              <span className={r.passed === r.total ? "pass-rate-full" : "pass-rate-partial"}>
                {r.passed}/{r.total}
              </span>
            </span>
          ))}
        </div>
      )}
      {(() => {
        const withTests = attempts.filter(a => a.test_results?.test_count > 0).length;
        return withTests > 0 && (
          <div className="tdd-metric" data-testid="tdd-metric">
            <span className="pass-rate-label">Test adoption:</span>
            <span className={withTests === attempts.length ? "pass-rate-full" : "pass-rate-partial"}>
              {withTests}/{attempts.length} attempts ran tests
            </span>
          </div>
        );
      })()}
      {[...task.attempts].reverse().map((a) => (
        <div key={a.id} className="attempt-row">
          <div className="attempt-number">Attempt #{a.attempt_number}</div>
          {a.branch_name && (
            <div className="attempt-branch-row">
              <div className="attempt-branch">{a.branch_name}</div>
              <button
                type="button"
                className="btn btn-post-one"
                data-testid="copy-checkout-btn"
                onClick={() => {
                  navigator.clipboard.writeText(checkoutCommand(a.branch_name));
                  setCopiedId(a.id);
                  setTimeout(() => setCopiedId((cur) => (cur === a.id ? null : cur)), 1500);
                }}
              >
                {copiedId === a.id ? "Copied" : "Copy checkout command"}
              </button>
            </div>
          )}
          {a.cache_read_share != null && (
            <div
              className="attempt-cache"
              data-testid="attempt-cache"
              title={`${(a.cache_read_tokens ?? 0).toLocaleString()} read · ${(a.cache_creation_tokens ?? 0).toLocaleString()} rebuilt`}
            >
              cache read {(a.cache_read_share * 100).toFixed(0)}%
            </div>
          )}
          {a.pr_url && (
            <div className="attempt-pr">
              <a href={a.pr_url} target="_blank" rel="noreferrer">{a.pr_url}</a>
            </div>
          )}
          <div className="attempt-status">
            {a.review_passed != null && (() => {
              const passed = !!a.review_passed;
              let label = passed ? "review passed" : "issues found";
              if (!passed && a.review_checklist?.items) {
                const n = a.review_checklist.items.filter(it => !it.passed).length;
                if (n > 0) label = `${n} issue${n > 1 ? "s" : ""} found`;
              }
              return (
                <span className={`attempt-badge ${passed ? "pass" : "fail"}`}>
                  {label}
                </span>
              );
            })()}
            {a.ci_status && (
              <span className={`attempt-badge ${a.ci_status === "success" ? "pass" : "fail"}`}>
                CI {a.ci_status}
              </span>
            )}
          </div>
          {a.resume_checkpoint_lost && (
            <div className="attempt-resume-lost" data-testid="attempt-resume-lost">
              {a.resume_checkpoint_lost}
            </div>
          )}
          {a.failure_reason && (
            <div className="test-result-fail-msg">{a.failure_reason}</div>
          )}
        </div>
      ))}
    </>
  );
}
