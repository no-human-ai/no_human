import { useEffect, useReducer, useRef, useState } from "react";
import { connectWS, createTask, uploadAttachment, fetchTasks, fetchWorkerStatus, fetchQueueHealth, fetchOnboardingStatus, fetchAuthStatus, fetchTrackerIssue, fetchConfig, grillStep, grillStepSSE, fetchDeferred, markDeferredDone, fetchWindowSpend } from "./api.js";
import FinishSetupCard from "./FinishSetupCard.jsx";
import { initTelemetry, captureScreen } from "./telemetry.js";
import Board from "./Board.jsx";
import Backlog from "./Backlog.jsx";
import SettingsOverlay from "./Settings.jsx";
import Stats from "./Stats.jsx";
import Onboarding from "./Onboarding.jsx";
import {
  isAiConfigDone, markAiConfigDone, isPopupDismissed, markPopupDismissed,
} from "./aiConfigNudge.js";
import TaskComposer from "./TaskComposer.jsx";
import Outcomes from "./Outcomes.jsx";
import About from "./About.jsx";
import { keepFocusInDialog } from "./keepFocusInDialog.js";
import { LegionLogo } from "./Logo.jsx";
import { newlyNeedsYou, notificationBody, titleWithBadge } from "./notifications.js";
import { setFavicon } from "./favicon.js";
import { needsPrUrl } from "./composerKinds.js";
import { hasPrRef } from "./prRefs.js";
import { shouldTriggerNewTask } from "./keyboardShortcut.js";
import { matchShortcut } from "./shortcuts.js";
import ShortcutsDialog from "./ShortcutsDialog.jsx";
import { isNeedsYou, isRealFailure, deriveCounts } from "./boardLanes.js";
import { overviewState } from "./overviewStrip.js";
import { ledgerSummary } from "./nightLedger.js";
import { fmtCost } from "./cost.js";
import { deriveSpendDisplay, perShippedCost } from "./ledgerSpend.js";
import { tasksReducer } from "./tasksReducer.js";
import { createReconnector } from "./wsReconnect.js";
import { connectionBanner } from "./connectionBanner.js";
import { drainChip, formatPausedUntil } from "./drainChip.js";
import { initialDrainReadout, nextDrainReadout, readoutPayload } from "./drainReadout.js";
import { useEscapeKey } from "./useEscapeKey.js";
import { promptFromIssue, externalIdFromIssue } from "./jiraImport.js";
import {
  backlogQueueReducer, initialQueue, queueHead, queueNotice, queueRemaining,
} from "./backlogSelection.js";
import QueueNotice from "./QueueNotice.jsx";
import { feasibilityCreateToast } from "./feasibilityToast.js";

// P3: how long the create-time feasibility toast stays up — long enough to
// read one sentence, short enough not to pile up. Mirrors Board.jsx's own
// APPROVE_TOAST_MS constant (same idiom, different toast).
const FEASIBILITY_TOAST_MS = 8_000;

// Header brand: logo + wordmark + tagline. Used in the main and error headers.
// The mark is the way home, the way it is on every other product: clicking it
// returns to the board. A real <button> rather than a click handler on a div,
// so it is reachable by keyboard and announced as a control.
function Brand({ onHome }) {
  if (!onHome) {
    return (
      <div className="legion-brand">
        <LegionLogo size={56} />
        <div className="legion-wordmark">
          <span className="legion-name">no_human</span>
          <span className="legion-tag">stop babysitting Claude</span>
        </div>
      </div>
    );
  }
  return (
    <button type="button" className="legion-brand legion-brand-home" onClick={onHome}
            title="Back to work in progress" aria-label="no_human — back to work in progress">
      <LegionLogo size={56} />
      <span className="legion-wordmark">
        <span className="legion-name">no_human</span>
        <span className="legion-tag">stop babysitting Claude</span>
      </span>
    </button>
  );
}

// ── 1.5: sidebar nav icon set — inline SVG only (CSP forbids remote icon
// fonts/images). Every icon below is paired with a text label by NavRow —
// never rendered alone. 16px, single stroke weight, no fills except the
// small "done" check and the tiny alert dot, both currentColor.
function IconBoard() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1.5" y="2.5" width="3.4" height="11" rx="1" />
      <rect x="6.3" y="2.5" width="3.4" height="7.5" rx="1" />
      <rect x="11.1" y="2.5" width="3.4" height="9.5" rx="1" />
    </svg>
  );
}
function IconDone() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="6.25" />
      <path d="M5.3 8.2l1.9 1.9 3.5-4.2" />
    </svg>
  );
}
function IconFailed() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 1.6l6.7 11.6a1 1 0 0 1-.86 1.5H2.16a1 1 0 0 1-.86-1.5L8 1.6z" />
      <path d="M8 6.2v3.1" />
      <circle cx="8" cy="11.5" r="0.9" fill="currentColor" stroke="none" />
    </svg>
  );
}
// Backlog: a stack of tickets waiting to be picked up — three lines with a
// leading tick mark, the "inbox list" reading, distinct from IconBoard's
// three vertical lanes at a glance.
function IconBacklog() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1.5" y="2.4" width="3" height="3" rx="0.8" />
      <rect x="1.5" y="10.5" width="3" height="3" rx="0.8" />
      <path d="M1.5 8h3" />
      <path d="M7 3.9h7.5M7 8h7.5M7 12h7.5" />
    </svg>
  );
}
function IconStats() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 13.5h12" />
      <path d="M4.5 13.5V9" />
      <path d="M8 13.5V5.5" />
      <path d="M11.5 13.5V7" />
    </svg>
  );
}
function IconBrain() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 3.2v9.6" />
      <path d="M8 4.4A2 2 0 0 0 4.3 5.2 1.9 1.9 0 0 0 3 7a1.9 1.9 0 0 0 .5 3.1A1.9 1.9 0 0 0 6 12.4" />
      <path d="M8 4.4A2 2 0 0 1 11.7 5.2 1.9 1.9 0 0 1 13 7a1.9 1.9 0 0 1-.5 3.1A1.9 1.9 0 0 1 10 12.4" />
    </svg>
  );
}
function IconAbout() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="6.2" />
      <path d="M8 7.2v4" />
      <path d="M8 4.9h.01" />
    </svg>
  );
}
function IconGear() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="2.3" />
      <path d="M8 1.8v1.6M8 12.6v1.6M14.2 8h-1.6M3.4 8H1.8M12.1 3.9l-1.1 1.1M5 11.1l-1.1 1.1M12.1 12.1l-1.1-1.1M5 4.9L3.9 3.9" />
    </svg>
  );
}

// A single sidebar nav row: inline-SVG icon + text label, always both — an
// icon-only row is never rendered (label is not conditionally hidden). The
// active row gets a soft filled pill (see .nh-navrow.active in styles.css),
// animated on --dur-fast/--ease-out and prefers-reduced-motion guarded.
function NavRow({
  icon, label, active, current, haspopup, expanded, badge, badgeVariant, badgeAriaLabel,
  onClick, onBadgeClick, title, className = "",
}) {
  // D2.1: when the badge is given its own click handler (the Settings row's
  // "!"), it must be its own click target — opening the Second-brain pane
  // directly, distinct from the row body's own destination — and must not
  // fire the row's onClick too. A <button> cannot nest inside another
  // <button> (invalid HTML, and the nested control would be unreachable to
  // assistive tech), so that case renders two SIBLING buttons instead of the
  // badge-as-span layout below; every other caller (Done/Failed/Finish-setup
  // counts) is untouched.
  if (onBadgeClick) {
    // `className` carries the caller's own layout rules, targeting ".nh-navrow"
    // specifically — including a mobile breakpoint that flips this row from
    // column to row layout — so it stays on the ROW button,
    // not the wrapper; the wrapper only carries the split's own flex layout.
    //
    // Review fix (M3): no e.stopPropagation() here — the badge is a SIBLING
    // of the row button (never nested inside it, see the structural test in
    // secondBrainBadge.test.mjs), so its click has nothing to bubble into;
    // the wrapper <div> below carries no onClick of its own to intercept.
    return (
      <div className="nh-navrow-split">
        <button
          className={`nh-navrow${active ? " active" : ""}${className ? ` ${className}` : ""}`}
          aria-current={current ? "page" : undefined}
          aria-haspopup={haspopup || undefined}
          aria-expanded={expanded === undefined ? undefined : expanded}
          onClick={onClick}
        >
          <span className="nh-navrow-icon" aria-hidden="true">{icon}</span>
          <span className="nh-navrow-label">{label}</span>
        </button>
        {badge != null && (
          <button
            type="button"
            className={`nh-navrow-badge nh-navrow-badge-btn${badgeVariant ? ` nh-navrow-badge-${badgeVariant}` : ""}`}
            aria-label={badgeAriaLabel}
            title={title}
            onClick={onBadgeClick}
          >
            {badge}
          </button>
        )}
      </div>
    );
  }

  return (
    <button
      className={`nh-navrow${active ? " active" : ""}${className ? ` ${className}` : ""}`}
      // aria-current marks the current PAGE for a screen reader — only the page-nav
      // rows pass `current`. Settings opens an overlay dialog, not a page, so it is
      // `active` (filled pill) without claiming to be the current page.
      aria-current={current ? "page" : undefined}
      // A row that opens a dialog announces it (aria-haspopup) and its open state
      // (aria-expanded) — page-nav rows pass neither, so both stay off for them.
      aria-haspopup={haspopup || undefined}
      aria-expanded={expanded === undefined ? undefined : expanded}
      onClick={onClick}
      title={title}
    >
      <span className="nh-navrow-icon" aria-hidden="true">{icon}</span>
      <span className="nh-navrow-label">{label}</span>
      {badge != null && (
        <span className={`nh-navrow-badge${badgeVariant ? ` nh-navrow-badge-${badgeVariant}` : ""}`}>{badge}</span>
      )}
    </button>
  );
}

// A muted, uppercase group header over a set of NavRows — the Claude-app
// "Work" / "Insights" sidebar grouping. Visual grouping only: every row still
// carries its own original handler, so no nav-model change hides here.
function NavGroup({ title, children }) {
  return (
    <div className="nh-navgroup">
      <div className="nh-navgroup-title">{title}</div>
      <div className="nh-navgroup-rows">{children}</div>
    </div>
  );
}

function fmtAge(seconds) {
  if (seconds < 60) return "<1m";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function NightLedger({ tasks, authMode, spend }) {
  // The signature module: what no_human did in the last 24h, in the sidebar's
  // dead space. Counts are derived from the same task list as the lanes (one
  // source, one truth) — a quiet board should read "calm instrument", never
  // "dead app". The dollar/token figures are NOT re-derived here: `spend` is
  // the server's attempt-attributed window total (`/api/metrics/window`,
  // fetched by the caller on the existing 10s poll) — consuming it verbatim
  // is the fix for the board sweeping a closed task's lifetime cost into
  // "last 24h" on a bare `updated_at` touch. `spend` is null before the first
  // fetch resolves or on an old server (fetchWindowSpend's 404 gate); either
  // way the figures below fall back to zero and the row simply hides.
  const s = ledgerSummary(tasks, undefined, undefined, spend);
  const tokensSpent = s.tokens;
  const windowCost = s.cost;
  const perPr = perShippedCost(s.done, windowCost);
  const perPrCost = perPr != null ? fmtCost(perPr) : null;
  // In subscription/OAuth mode (default/absent) the dollar figure is an
  // API-rate ESTIMATE, never money that changed hands — only api_key mode
  // pays Anthropic per token for real (SCRUM-20).
  const spendDisplay = deriveSpendDisplay(tokensSpent, windowCost, windowCost, authMode);
  const showSpend = spend != null && (windowCost > 0 || tokensSpent > 0);
  return (
    <div className="nh-ledger" aria-label="last 24 hours summary">
      <div className="nh-ledger-title">last 24h</div>
      {s.quiet
        ? <div className="nh-ledger-quiet">quiet — nothing shipped, nothing owed</div>
        : (
          <div className="nh-ledger-rows">
            <div className="nh-ledger-row">
              {/* Review 2026-07-25: in subscription mode the dollar is an
                  API-rate estimate — a naked "$X/PR" claimed real spend in the
                  one mode where it isn't (SCRUM-20's whole point). */}
              <b>{s.done}</b> shipped
              {perPrCost && (authMode === "api_key"
                ? ` (~${perPrCost}/PR)` : ` (~${perPrCost}/PR est.)`)}
            </div>
            <div className="nh-ledger-row"><b>{s.parked}</b> waiting on you</div>
            {s.failed > 0 && (
              <div className="nh-ledger-row nh-ledger-bad"><b>{s.failed}</b> failed</div>
            )}
            {showSpend && (
              <div className="nh-ledger-row nh-ledger-cost"><b>{spendDisplay.primary}</b> {spendDisplay.secondary}</div>
            )}
          </div>
        )}
    </div>
  );
}

function OverviewStrip({ tasks }) {
  // SCRUM-84: this strip is live-state only (overviewStrip.js) — all-time
  // outcome tallies (failed/cancelled) are dropped here; they already live in
  // the Failed nav badge and Stats page, and mixing an unlabeled all-time
  // count in with now-metrics put a second, differently-scoped "failed"
  // number on screen next to the sidebar's 24h ledger figure.
  const s = overviewState(tasks);

  return (
    <div className="nh-overview">
      {s.allClear
        ? <span className="ov-clear">nothing needs you</span>
        : <span className="ov-awaiting">{s.needsYouCount} need you</span>
      }
      {s.oldestWaitingSec !== null && (
        <>
          <span className="ov-sep">·</span>
          <span className={`ov-oldest${s.oldestWaitingSec > 24 * 3600 ? " ov-oldest-stale" : ""}`}>
            oldest waiting {fmtAge(s.oldestWaitingSec)}
          </span>
        </>
      )}
      {/* "0 working" is noise on a phone — the Working lane already says so. The gate AGE is not. */}
      <span className="ov-sep ov-hide-narrow">·</span>
      <span className="ov-hide-narrow">
        {s.running} running{s.queued > 0 ? ` · ${s.queued} queued` : ""}
      </span>
    </div>
  );
}

// SCRUM-67 3/3: header drain readout. `readout` is the poll wiring state
// (drainReadout.js) — renders nothing until the first successful fetch so it
// never shows phantom "0/0 workers busy" zeros, and switches to the honest
// unreachable tone the moment a poll fails after a prior success.
function DrainReadoutChip({ readout }) {
  const payload = readoutPayload(readout);
  if (payload == null) return null;
  const chip = drainChip(payload);
  return <span className={`drain-chip tone-${chip.tone}`} title={chip.text}>{chip.text}</span>;
}

function Spinner() {
  return <span className="grill-spinner" />;
}

// Strips the SSE envelope (`kind: "eval_verdict"`, `source: "grill"`) off an
// eval_verdict frame down to the `EvalResult.as_dict()` shape
// CreateTaskRequest.eval_result expects — null when no grill round produced
// a verdict, so createTask's payload omits the key exactly as before.
function _evalResultField(evalData) {
  if (!evalData) return null;
  const { kind: _kind, source: _source, ...verdictFields } = evalData;
  return verdictFields;
}

function NewTaskModal({
  onClose, onCreated, initial = null,
  notice = null, queueLeft = 0, onStopQueue = null,
  onOpenBacklog = null,
}) {
  // The composed spec, handed over by TaskComposer when the operator hits Next.
  // Null until then — the composer owns its own field state (see TaskComposer.jsx).
  //
  // `initial` seeds it for a ticket started from the Backlog page: the composer
  // opens with the ticket's prompt (and the hidden source/external_id markers)
  // already filled, and from there this is the SAME flow a typed task runs —
  // composer → grill → createTask. There is no second create path. Note the
  // seed never reaches handleSubmit directly: that branch is reachable only
  // after startGrill, which overwrites `fields` with the composer's own spec.
  const [fields, setFields] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // B2: grill state
  const [grillMode, setGrillMode] = useState(false);
  const [grillQA, setGrillQA] = useState([]);
  const [grillQuestion, setGrillQuestion] = useState(null);
  const [grillAnswer, setGrillAnswer] = useState("");
  const [grillResult, setGrillResult] = useState(null);
  const [grillEvents, setGrillEvents] = useState([]);
  const [evalVerdict, setEvalVerdict] = useState(null);
  // Mirrors evalVerdict for _startGrillSSE's onResult callback, which closes
  // over the render that created it (a stale one, since the eval_verdict SSE
  // frame — which triggers setEvalVerdict — arrives and re-renders BEFORE the
  // grill_result frame that consumes it here). A ref reads its CURRENT value,
  // not the one from whichever render captured this closure.
  const evalVerdictRef = useRef(null);
  const grillStreamRef = useRef(null);
  // Escape closes the dialog — same escape route the overlay-click already gives,
  // but for keyboard users. Suppressed while a submit is in flight so Escape can't
  // discard a task that's already being created. Bound ONLY on the grill branches:
  // the composer binds its own, and a state where both are mounted (a failed grill)
  // would otherwise fire onClose twice.
  const showingGrill = grillMode || Boolean(grillResult);
  useEscapeKey(onClose, !busy && showingGrill);

  // The grill/intake modals are dialogs (role/aria set on each modal div below):
  // pull focus into the current one and trap Tab within it, matching the app's
  // other modals. Escape is handled above. The effect re-runs on every grill
  // state transition so that when a step's focused control unmounts (e.g.
  // loading -> question) focus is pulled back in rather than lost to <body>,
  // which would let the next Tab escape the trap.
  const grillRef = useRef(null);
  useEffect(() => {
    const el = grillRef.current;
    if (!el) return undefined;
    const focusables = () => Array.from(el.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    )).filter((n) => !n.disabled && n.getClientRects().length > 0);
    if (!el.contains(document.activeElement)) (focusables()[0] || el).focus();
    function onKeyDown(e) {
      if (e.key !== "Tab") return;
      const list = focusables();
      if (!list.length) return;
      const first = list[0];
      const last = list[list.length - 1];
      if (e.shiftKey) {
        if (document.activeElement === first) { e.preventDefault(); last.focus(); }
      } else if (document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [showingGrill, grillResult, grillMode, grillQuestion, busy]);

  async function handleSubmit(e) {
    if (e) e.preventDefault();
    if (!fields || busy) return;
    setBusy(true);
    setError(null);
    try {
      const title = grillResult?.title || fields.title;
      let description = grillResult?.description || fields.description;
      // The grill REWRITES the spec, and its prompt has no instruction to preserve
      // URLs. A code_review task whose refined text dropped the PR link would be
      // failed at the gate (orchestrator: parse_pr_refs over title+description), so
      // re-attach the operator's original reference when the rewrite lost it.
      if (needsPrUrl(fields.kind) && !hasPrRef(`${title} ${description || ""}`)) {
        const original = [fields.prUrl?.trim(), fields.description].find(hasPrRef);
        if (original) description = [description, original].filter(Boolean).join("\n\n");
      }
      const created = await createTask({
        title,
        description,
        repo_path: fields.repoPath,
        project_id: fields.projectId,
        kind: fields.kind,
        priority: fields.priority,
        acceptance_criteria: grillResult?.acceptance_criteria || [],
        // The grill's intake-eval verdict, carried on `grillResult` (stripped
        // of the SSE envelope's `kind`/`source` down to the
        // `EvalResult.as_dict()` shape CreateTaskRequest.eval_result
        // documents) by `_startGrillSSE`'s onResult handler below. Undefined
        // for a create with no grill round, same as every other optional
        // field here.
        eval_result: grillResult?.eval_result,
        // Task 1.6: the hidden marker TaskComposer sets when the task came
        // from a picked Jira ticket — "board" for every typed task, unchanged.
        source: fields.source,
        external_id: fields.externalId,
        // GAP 1: the composer's "review plan first" toggle. Omitted-as-false
        // keeps every other caller on the unattended path.
        plan_approval: !!fields.planApproval,
        // Coder-backend picker (issue: board can't select claude|codex|local).
        // Untouched control → fields.backend is "" → sent as an empty string,
        // which CreateTaskRequest.backend and the POST handler's `if
        // body.backend:` both treat exactly like an absent field → server
        // falls back to worker.backend, unchanged from today. Forwarded
        // verbatim: never recomputed from other state.
        backend: fields.backend,
        // Task 7: "Follow up" — the predecessor task's id, or null for every
        // other create path. The server 404s if it doesn't resolve.
        follows_id: fields.followsId || null,
      });
      // Attach any screenshots/documents to the new task (best-effort — a failed
      // upload must not lose the task that was already created).
      for (const f of fields.files || []) {
        try { await uploadAttachment(created.id, f); }
        catch (err) { console.error("attachment upload failed", f.name, err); }
      }
      // P3: hand the raw create response up so App can surface its
      // feasibility_hint as a toast — this modal unmounts right after
      // onClose(), so nothing here can render the hint itself.
      onCreated(created);
      onClose();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  // `spec` defaults to the committed fields; startGrill passes them explicitly
  // because it runs in the same tick as setFields (state is not yet visible).
  function _grillParams(qaOverride, spec = fields) {
    return {
      title: spec.title, description: spec.description,
      // A project resolves its own repo server-side, so repo_path is sent only
      // when there is no project — preserving the pre-composer behaviour.
      repo_path: spec.projectId ? null : spec.repoPath,
      project_id: spec.projectId,
      qa_history: qaOverride ?? [],
    };
  }

  function _startGrillSSE(params) {
    if (grillStreamRef.current) grillStreamRef.current.close();
    setGrillEvents([]);
    grillStreamRef.current = grillStepSSE(
      params,
      (evt) => setGrillEvents((prev) => [...prev.slice(-30), evt]),
      (result) => {
        setBusy(false);
        if (result.type === "done") {
          setGrillResult({ ...result, eval_result: _evalResultField(evalVerdictRef.current) });
          setGrillQuestion(null);
        } else { setGrillQuestion(result); }
      },
      (err) => {
        // SSE failed — fall back to sync POST. No eval_verdict frame exists on
        // this path (grillStep is the non-streaming endpoint), so eval_result
        // stays whatever evalVerdictRef already holds from an earlier round.
        grillStep(params)
          .then((step) => {
            if (step.type === "done") {
              setGrillResult({ ...step, eval_result: _evalResultField(evalVerdictRef.current) });
            } else { setGrillQuestion(step); }
          })
          .catch((e) => { setError(e.message); setGrillMode(false); })
          .finally(() => setBusy(false));
      },
      (evalData) => { evalVerdictRef.current = evalData; setEvalVerdict(evalData); },
    );
  }

  function startGrill(spec) {
    if (!spec?.title || busy) return;
    setFields(spec);
    setBusy(true); setError(null); setGrillMode(true);
    setGrillQA([]); setGrillQuestion(null); setGrillResult(null); setEvalVerdict(null);
    evalVerdictRef.current = null;
    _startGrillSSE(_grillParams([], spec));
  }

  function submitGrillAnswer() {
    if (!grillAnswer.trim() || busy) return;
    setBusy(true); setError(null);
    const newQA = [...grillQA, { question: grillQuestion.question, answer: grillAnswer.trim() }];
    setGrillQA(newQA); setGrillAnswer("");
    _startGrillSSE(_grillParams(newQA));
  }

  if (grillResult) {
    // No backdrop-click close: a stray click mid-intake discarded the whole
    // refined spec. Escape and the explicit Cancel button are the ways out.
    // (The grill's question-while-busy branch further below never had one, for
    // this same reason; the other three, including the loading branch, did and
    // no longer do.)
    return (
      <div className="sendback-overlay" onMouseDown={keepFocusInDialog}>
        <div className="new-task-modal" role="dialog" aria-modal="true" aria-label="Refined spec" tabIndex={-1} ref={grillRef}>
          <QueueNotice notice={notice} remaining={queueLeft} onStopAll={onStopQueue} />
          <div className="sendback-label">Refined Spec</div>
          {/* ph-no-capture: the refined spec (title, description, acceptance
              criteria, eval rationale) is operator content. */}
          <div className="grill-spec ph-no-capture">
            <div className="grill-spec-title">{grillResult.title}</div>
            {evalVerdict && (
              <div className="eval-verdict-card">
                <span className={`eval-badge eval-${evalVerdict.verdict}`}>
                  {evalVerdict.verdict.toUpperCase()}
                </span>
                {evalVerdict.dimensions && (
                  <span className="eval-dims">
                    {Object.entries(evalVerdict.dimensions).map(([k, v]) => (
                      <span key={k} className={`eval-dim ${v ? 'dim-pass' : 'dim-fail'}`}>
                        {k.replace(/_/g, ' ')}
                      </span>
                    ))}
                  </span>
                )}
                {evalVerdict.rationale && (
                  <div className="eval-rationale">{evalVerdict.rationale}</div>
                )}
              </div>
            )}
            {grillResult.description && <div className="grill-spec-desc">{grillResult.description}</div>}
            <div className="grill-spec-section-label">Acceptance Criteria</div>
            <ul className="grill-ac-list">
              {grillResult.acceptance_criteria.map((ac, i) => (
                <li key={i} className="grill-ac-item">
                  <span className="grill-ac-num">{i + 1}.</span>
                  <span>{ac}</span>
                </li>
              ))}
            </ul>
          </div>
          {error && <div className="new-task-error ph-no-capture">{error}</div>}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            <button className="btn btn-approve" disabled={busy} onClick={handleSubmit}>{busy ? "\u2026" : "Create Task"}</button>
          </div>
        </div>
      </div>
    );
  }

  // Loading overlay while grill is exploring (no question yet)
  if (grillMode && !grillQuestion && busy) {
    return (
      // No backdrop-click close — see the Refined Spec branch above.
      <div className="sendback-overlay" onMouseDown={keepFocusInDialog}>
        <div className="new-task-modal" role="dialog" aria-modal="true" aria-label="Let's scope this" tabIndex={-1} ref={grillRef}>
          <QueueNotice notice={notice} remaining={queueLeft} onStopAll={onStopQueue} />
          <div className="sendback-label">Let's scope this</div>
          <div className="grill-loading">
            <Spinner />
            <div className="grill-loading-text">Reading your code so the questions are worth asking...</div>
            {grillEvents.length > 0 && (
              <div className="grill-explore-log ph-no-capture">
                {grillEvents.map((ev, i) => (
                  <div key={i} className="grill-explore-entry" style={{ opacity: i === grillEvents.length - 1 ? 1 : 0.5 }}>
                    {ev.kind === 'tool_use' ? `\u2699 ${ev.text}` : ev.text}
                  </div>
                ))}
              </div>
            )}
            {grillEvents.length === 0 && (
              <div className="grill-loading-hint">This usually takes 15–30 seconds</div>
            )}
          </div>
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
          </div>
        </div>
      </div>
    );
  }

  if (grillMode && grillQuestion) {
    const maxRounds = 5;
    const progressPct = Math.min(100, (grillQuestion.round / maxRounds) * 100);
    if (busy) {
      return (
        <div className="sendback-overlay" onMouseDown={keepFocusInDialog}>
          <div className="new-task-modal" role="dialog" aria-modal="true" aria-label="Let's scope this" tabIndex={-1} ref={grillRef}>
            <QueueNotice notice={notice} remaining={queueLeft} onStopAll={onStopQueue} />
            <div className="grill-header">
              <div className="sendback-label">Let's scope this</div>
              <span className="grill-round-badge">Round {grillQuestion.round}/{maxRounds}</span>
            </div>
            <div className="grill-progress-bar">
              <div className="grill-progress-fill" style={{ width: `${progressPct}%` }} />
            </div>
            <div className="grill-loading">
              <Spinner />
              <div className="grill-loading-text">Thinking about that...</div>
              <div className="grill-loading-hint">Refining the next question based on your input</div>
            </div>
            {error && <div className="new-task-error ph-no-capture">{error}</div>}
            <div className="sendback-actions">
              <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            </div>
          </div>
        </div>
      );
    }
    return (
      // No backdrop-click close — a stray click mid-grill discarded every answer
      // the operator had already given. See the Refined Spec branch above.
      <div className="sendback-overlay" onMouseDown={keepFocusInDialog}>
        <div className="new-task-modal" role="dialog" aria-modal="true" aria-label="Let's scope this" tabIndex={-1} ref={grillRef}>
          <QueueNotice notice={notice} remaining={queueLeft} onStopAll={onStopQueue} />
          <div className="grill-header">
            <div className="sendback-label">Let's scope this</div>
            <span className="grill-round-badge">Round {grillQuestion.round}/{maxRounds}</span>
          </div>
          <div className="grill-progress-bar">
            <div className="grill-progress-fill" style={{ width: `${progressPct}%` }} />
          </div>
          {grillQA.length > 0 && (
            <div className="grill-qa-history ph-no-capture">
              {grillQA.map((qa, i) => (
                <div key={i} className="grill-qa-item">
                  <div><strong>Q{i+1}:</strong> {qa.question}</div>
                  <div className="grill-qa-answer">{"\u2192"} {qa.answer}</div>
                </div>
              ))}
            </div>
          )}
          <div className="ntm-field">
            <div className="ntm-label">Question</div>
            {/* ph-no-capture: the question paraphrases the operator's spec. */}
            <div className="ph-no-capture" style={{ fontWeight: 500, fontSize: '14px', lineHeight: 1.5, margin: '4px 0 12px' }}>{grillQuestion.question}</div>
          </div>
          {grillQuestion.suggestions.map((s, i) => (
            <button key={i} type="button" className="grill-suggestion ph-no-capture" onClick={() => setGrillAnswer(s.replace(/^[A-D]:\s*/, ''))}>{s}</button>
          ))}
          <textarea className="sendback-textarea" placeholder="Your answer (or click a suggestion above)" value={grillAnswer} onChange={(e) => setGrillAnswer(e.target.value)} rows={2} style={{ marginTop: '8px' }} />
          {error && <div className="new-task-error ph-no-capture">{error}</div>}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            <button type="button" className="btn btn-sendback btn-sm"
              onClick={async () => {
                setBusy(true); setError(null);
                try {
                  const step = await grillStep(_grillParams([...grillQA, { question: grillQuestion.question, answer: "(skip \u2014 use what you have)" }]));
                  if (step.type === "done") { setGrillResult(step); setGrillQuestion(null); } else { setGrillResult({ title: step.title || fields.title, description: step.description || fields.description, acceptance_criteria: step.acceptance_criteria || [] }); setGrillQuestion(null); }
                } catch (err) { setError(err.message); }
                finally { setBusy(false); }
              }}>Skip &amp; finish</button>
            <button className="btn btn-approve" disabled={!grillAnswer.trim()} onClick={submitGrillAnswer}>Answer</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <TaskComposer
      busy={busy}
      error={error}
      // Re-seed after a failed grill: this component was unmounted for the grill's
      // duration, so without `initial` the operator would come back to an empty
      // composer with their prompt, attachments and kind gone.
      initial={fields}
      notice={notice}
      queueRemaining={queueLeft}
      onStopQueue={onStopQueue}
      onOpenBacklog={onOpenBacklog}
      onStart={startGrill}
      onClose={onClose}
    />
  );
}

// The window between "the operator started a ticket" and "the composer has its
// full spec": one detail GET, up to 30s if the tracker is slow.
//
// It used to be a bare spinner. Every OTHER branch of this flow has a way out —
// Cancel buttons on all four grill steps, Escape on the composer — and this one
// had neither, so a slow Jira held the whole screen with no key and no control
// that did anything. It gets the same two exits as its neighbours, and they mean
// what Cancel means everywhere else in the queue: skip THIS ticket, keep the
// rest (backlogSelection.js).
function BacklogSeedOverlay({ issueKey, tracker, notice, queueLeft, onSkip, onStopQueue }) {
  const ref = useRef(null);
  useEscapeKey(onSkip, true);
  useEffect(() => { ref.current?.focus(); }, []);
  return (
    <div className="sendback-overlay" onMouseDown={keepFocusInDialog}>
      {/* ph-no-capture: the tracker issue key names the operator's
          project, in the aria-label attribute and the body text both. */}
      <div
        className="new-task-modal ph-no-capture"
        role="dialog"
        aria-modal="true"
        aria-label={`Reading ${issueKey}`}
        tabIndex={-1}
        ref={ref}
      >
        <QueueNotice notice={notice} remaining={queueLeft} onStopAll={onStopQueue} />
        <div className="grill-loading" role="status" aria-live="polite">
          <Spinner />
          <div className="grill-loading-text">Reading {issueKey} from {tracker === "linear" ? "Linear" : "Jira"}…</div>
        </div>
        <div className="sendback-actions">
          <button type="button" className="btn btn-sendback" onClick={onSkip}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [tasks, dispatch] = useReducer(tasksReducer, []);
  // C6: an empty `tasks` array is ambiguous — genuinely no tasks ever filed,
  // OR the first fetch/snapshot just hasn't landed yet. Flipped true only once
  // real data has arrived, so the board's first-run empty state (which reads
  // tasks.length===0) can never fire during that loading window.
  const [tasksLoaded, setTasksLoaded] = useState(false);
  const [wsLive, setWsLive] = useState(false);
  // Drives the stale-data banner (incident 2026-08-12): "connecting" until
  // the first open, "resyncing" once open but before the snapshot lands,
  // "live" only after a fresh snapshot is delivered — a socket that is open
  // over stale data is exactly the incident, not a healthy state.
  const [wsPhase, setWsPhase] = useState("connecting");
  const prevTasksRef = useRef([]);
  const [fetchError, setFetchError] = useState(null);
  const [showNewTask, setShowNewTask] = useState(false);
  // What the composer opens pre-filled with, when something upstream already
  // knows the answer. Today that is onboarding handing over the repo it just
  // PROVED — the wizard's last button says "Create your first task in <repo>"
  // and the composer used to open on an empty path field, which is the exact
  // moment the setup's payoff was supposed to land. Cleared on close so the
  // next "+ New Task" opens blank. Deliberately NOT the Backlog path's `key`
  // remount: this modal is unmounted while closed, so `initial` is read fresh
  // at mount anyway, and a shared key would couple the two seeds.
  const [newTaskSeed, setNewTaskSeed] = useState(null);
  // Task 7: "Follow up" on a done/failed task (SlideOver's action bar) reuses
  // this SAME composer-open path, seeded via followUpSeed(task) instead of the
  // onboarding repo hint above.
  function openFollowUp(seed) {
    setNewTaskSeed(seed);
    setShowNewTask(true);
  }
  // P3: the create-time feasibility toast (feature #1). Dispatch takes ~9s
  // and SlideOver's FeasibilityCard only renders while status === "pending",
  // so an operator opening the drawer after that almost never sees the hint.
  // This reads it straight off the CREATE RESPONSE instead, so it shows
  // immediately regardless of what the task's live status becomes — lifted
  // to App (not NewTaskModal state) because the modal that created the task
  // unmounts on success, before there is anywhere left to render it.
  const [createToast, setCreateToast] = useState(null);
  useEffect(() => {
    if (!createToast) return undefined;
    const t = setTimeout(
      () => setCreateToast((cur) => (cur?.id === createToast.id ? null : cur)),
      FEASIBILITY_TOAST_MS,
    );
    return () => clearTimeout(t);
  }, [createToast]);
  const [page, setPage] = useState("board");
  // ── Backlog → intake queue ────────────────────────────────────────────────
  // The tickets the operator selected on the Backlog page, still to be started.
  // The intake flow is interactive and PER TASK (scoping questions about THIS
  // spec), so N selected tickets are not a batch: they run through the same
  // composer→grill→create flow one at a time, head of the queue first. The
  // Backlog page says so before the first question is asked
  // (backlogSelection.js: multiStartNotice).
  //
  // The transitions live in backlogSelection.js as a pure reducer, NOT inline
  // here — read the "the queue itself" block there for why (this handler is
  // where nine of ten tickets could go missing without a test noticing, and
  // where one Escape used to discard the whole run).
  const [backlog, setBacklog] = useState(initialQueue);
  const backlogQueue = backlog.queue;
  // The head ticket resolved to a composer seed. null while the full issue is
  // being fetched: the browse list truncates description at 2000 chars, and a
  // task must carry the whole spec, so the detail GET happens BEFORE the
  // composer mounts (its `initial` is read once, at mount — upgrading it after
  // would either be ignored or clobber what the operator had started typing).
  const [backlogSeed, setBacklogSeed] = useState(null);
  // Bumped after each created task so the Backlog page re-reads its list and
  // the ticket it just started shows its "imported" chip.
  const [backlogNonce, setBacklogNonce] = useState(0);
  // The ticket currently being started (queue head), or null when the queue is
  // empty. Declared here, above every effect that reads it.
  const backlogHeadKey = queueHead(backlog)?.key ?? null;
  // ONE ticket is finished with — created or cancelled, the queue does not care
  // which — so move to the next. The whole run is abandoned only by `stopQueue`,
  // which is reachable solely from the "Stop the rest" button.
  const nextTicket = () => setBacklog((s) => backlogQueueReducer(s, { type: "next" }));
  const stopQueue = () => setBacklog((s) => backlogQueueReducer(s, { type: "stop" }));
  // Settings is an overlay dialog (Claude macOS desktop app model), not a
  // routed page — it can open on top of whatever page is showing.
  const [settingsOpen, setSettingsOpen] = useState(false);
  // The ⌘/ cheat-sheet dialog (task C5) — same "overlay on top of whatever
  // page is showing" model as Settings above.
  const [showShortcuts, setShowShortcuts] = useState(false);
  // Which Settings pane to open on — set when a Finish-setup item is clicked so
  // the overlay lands on the matching pane; null means "wherever it last was".
  const [settingsTab, setSettingsTab] = useState(null);
  // The Settings "!" nudge (AI-learnings left onboarding 2026-08-30) is TWO
  // separate acknowledgements, each its own localStorage bit (aiConfigNudge.js)
  // — review fix round, 2026-09-01: collapsing them into one made the popup
  // nag forever, because a user who opens Settings via the row body or a
  // Finish-setup deep link and never visits the Second-brain pane would keep
  // seeing the popup on every subsequent visit.
  //   - `aiConfigDone` (the BADGE): shown until the Second-brain pane has
  //     actually been opened once (D2.1) — NOT merely because Settings opened
  //     on some other pane. Settings.jsx clears it from that pane's own
  //     first-mount effect, which calls handleSecondBrainOpened below so the
  //     badge disappears immediately without a reload.
  //   - `popupDismissed` (the POPUP): shown until Settings opens on ANY pane,
  //     or its own × is clicked — a one-time prompt pointing at Settings, a
  //     strictly weaker condition than the badge's.
  const [aiConfigDone, setAiConfigDone] = useState(() => isAiConfigDone());
  const [popupDismissed, setPopupDismissed] = useState(() => isPopupDismissed());
  const openSettings = (tab = null) => {
    if (tab !== null) setSettingsTab(tab);
    setSettingsOpen(true);
    if (!popupDismissed) { markPopupDismissed(); setPopupDismissed(true); }
  };
  const handleSecondBrainOpened = () => {
    if (!aiConfigDone) { markAiConfigDone(); setAiConfigDone(true); }
  };
  // The popup's own × — dismissing it without opening Settings still counts
  // as acknowledged (a one-time prompt, not a gate), but must NOT satisfy the
  // badge's stricter "the Second-brain pane was actually seen" condition.
  const dismissAiConfig = () => { markPopupDismissed(); setPopupDismissed(true); };
  // The onboarding steps deferred by the minimal path (spec §3 B1). The board's
  // FinishSetupCard renders them; empty once every step is done.
  const [deferred, setDeferred] = useState([]);
  const [pendingOpenId, setPendingOpenId] = useState(null);
  const [workerStatus, setWorkerStatus] = useState(null);
  const [queueHealth, setQueueHealth] = useState(null);
  // Attempt-attributed "last 24h" spend (core/metrics.py:window_spend), fetched
  // on the existing 10s poll below — no second interval. null before the first
  // resolve, or forever on an old server (fetchWindowSpend's 404 gate); the
  // sidebar's spend line just hides in either case.
  const [windowSpend, setWindowSpend] = useState(null);
  // SCRUM-67 3/3: the header drain chip's own view of the SAME poll (reuses
  // fetchQueueHealth below rather than adding a second poller) — kept
  // separate from queueHealth above so a poll failure can flip the chip to
  // "unreachable" without disturbing the sidebar's stuck/eta indicators,
  // which already have their own (independent) staleness tolerance.
  const [drainReadout, setDrainReadout] = useState(initialDrainReadout);
  // Mode-aware LAST 24H spend line (SCRUM-20): the same auth-status endpoint
  // Settings' Account panel already queries. undefined until it resolves —
  // the sidebar treats that exactly like an absent field (subscription
  // behavior), never api_key, so it can't flash a false "real dollars" line.
  const [authMode, setAuthMode] = useState(undefined);
  const doneCount = tasks.filter((t) => t.status === "done").length;
  const failedCount = tasks.filter(isRealFailure).length;
  const cancelledCount = tasks.filter((t) => t.status === "failed" && t.cancelled).length;

  // Theme: the operator's persisted choice, else DARK. Light mode is fully built
  // and reachable via the toggle.
  const [theme, setTheme] = useState(() =>
    // DARK BY DEFAULT, regardless of the OS setting. Following
    // prefers-color-scheme meant a user whose Mac is in light mode saw a light
    // app on first run -- which is what the first external tester got, and it is
    // not the product's intended look. An explicit choice still wins: the
    // toggle writes "nh-theme" and that is read first.
    localStorage.getItem("nh-theme") || "dark");
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("nh-theme", theme);
    // Desktop shell only. The main process paints the window frame (and, on
    // Windows, the min/max/close buttons) before any renderer exists, so it
    // cannot read the line above — it keeps its own copy and uses it on the
    // NEXT launch. A browser has no bridge and skips this.
    // Unawaited AND deliberately not `.catch(() => {})`-ed: the handler
    // resolves for every theme and every write failure alike, so a rejection
    // can only mean a broken bridge, and an empty catch would bury it.
    window.nhDesktop?.setTheme?.(theme);
  }, [theme]);
  // Opt-OUT usage telemetry + masked replay (telemetry.js): the config fetch
  // decides consent (default on); without it posthog-js is never even
  // imported. Screen
  // views report the lane NAME only (board/backlog/done/failed/stats/about).
  useEffect(() => {
    fetchConfig().then((cfg) => initTelemetry(cfg)).catch(() => {});
  }, []);
  useEffect(() => { captureScreen(page); }, [page]);
  useEffect(() => { if (settingsOpen) captureScreen("settings"); }, [settingsOpen]);
  // Desktop shell (Electron) marks itself via the preload bridge; the class
  // gates the inset-title-bar accommodations (drag region + traffic-light
  // clearance) so the browser experience is untouched.
  useEffect(() => {
    document.documentElement.classList.toggle(
      "nh-in-shell", Boolean(window.nhDesktop?.shell));
    // Sided accommodation: Windows window controls live top-RIGHT (the
    // titleBarOverlay), so the main bar needs right clearance there the way
    // the sidebar brand zone needs top clearance for macOS traffic lights.
    // Without it, "+ New Task" sat under the overlay — clipped and unclickable.
    document.documentElement.classList.toggle(
      "nh-shell-win32",
      Boolean(window.nhDesktop?.shell) && window.nhDesktop?.platform === "win32");
  }, []);
  // null = checking; false = needs onboarding; true = onboarded. Fail-open so a
  // missing/old endpoint never blocks an existing user at the board.
  //
  // Fetched ONCE, on mount, and deliberately not polled: POST
  // /api/onboarding/reset clears the flag on the server, but a board that is
  // already open will not notice — the wizard reappears on the next load of
  // this page. The desktop File → "Re-run Setup…" item therefore resets AND
  // reloads the window (desktop/main.mjs); a reset driven by the API alone
  // needs a browser reload to take effect. A poll would buy nothing: the only
  // writer is a deliberate human action that already has a reload attached.
  const [onboarded, setOnboarded] = useState(null);

  // onboarding gate
  useEffect(() => {
    fetchOnboardingStatus()
      .then((s) => setOnboarded(!!s.completed))
      .catch(() => setOnboarded(true));
  }, []);

  // The minimal-path deferred steps for the Finish-setup card. Fetched once the
  // board is showing; an empty list simply renders no card.
  useEffect(() => {
    if (onboarded !== true) return;
    fetchDeferred().then((r) => setDeferred(r.deferred || [])).catch(() => {});
  }, [onboarded]);

  // initial load
  useEffect(() => {
    fetchTasks()
      .then((ts) => { setFetchError(null); dispatch({ type: "set", tasks: ts }); setTasksLoaded(true); })
      .catch((err) => setFetchError(err?.message || "Cannot reach the no_human API."));
  }, []);

  // Auth mode for the sidebar's mode-aware spend line — fetched once, not
  // polled: it only changes on an operator-driven profile switch + restart.
  useEffect(() => {
    fetchAuthStatus().then((s) => setAuthMode(s?.auth_mode)).catch(() => {});
  }, []);

  // Worker status poll
  useEffect(() => {
    function poll() {
      fetchWorkerStatus().then(setWorkerStatus).catch(() => {});
      fetchQueueHealth()
        .then((h) => { setQueueHealth(h); setDrainReadout((s) => nextDrainReadout(s, h)); })
        .catch(() => setDrainReadout((s) => nextDrainReadout(s, "error")));
      fetchWindowSpend().then(setWindowSpend).catch(() => {});
    }
    poll();
    const id = setInterval(poll, 10000);
    return () => clearInterval(id);
  }, []);

  // Needs-You notifications (W2.2): the tab title always carries the count;
  // the Notification API fires per NEW arrival when granted and the tab is
  // hidden. Permission is requested on the first user gesture (browsers
  // block gesture-less requests); denied/unsupported degrades to the badge.
  useEffect(() => {
    const needy = tasks.filter(isNeedsYou);
    document.title = titleWithBadge(needy.length);
    const fresh = newlyNeedsYou(prevTasksRef.current, tasks, isNeedsYou);
    prevTasksRef.current = tasks;
    if (fresh.length && typeof Notification !== "undefined"
        && Notification.permission === "granted" && document.hidden) {
      for (const t of fresh.slice(0, 3)) {
        try {
          const n = new Notification("no_human — needs you", { body: notificationBody(t) });
          // Clicking the toast must LAND somewhere: focus the window (the
          // desktop shell restores it from the tray) and open the task that
          // fired it — the operator's next action is always answering it.
          n.onclick = () => {
            window.focus();
            setPage("board");
            // Board is conditionally mounted: an event dispatched here is
            // lost when the operator was on another page (PR #104 review,
            // medium). A pending id survives until Board mounts and opens it.
            setPendingOpenId(t.id);
          };
        } catch { /* a notification is a bonus, never an error */ }
      }
    }
  }, [tasks]);

  // Favicon dot: surface an awaiting_approval task even when the tab title
  // (badge) isn't visible, e.g. a pinned tab.
  useEffect(() => {
    setFavicon(tasks.some((t) => t.status === "awaiting_approval"));
  }, [tasks]);

  useEffect(() => {
    if (typeof Notification === "undefined"
        || Notification.permission !== "default") return undefined;
    const ask = () => { Notification.requestPermission().catch(() => {}); };
    window.addEventListener("pointerdown", ask, { once: true });
    return () => window.removeEventListener("pointerdown", ask);
  }, []);

  // Global 'n' opens the New Task modal (unless typing or a modal is open).
  useEffect(() => {
    function onKeyDown(e) {
      // The drawer autofocuses its close BUTTON, which is not an editable tag — so without
      // this the "n" shortcut opened the composer *behind* the open drawer (z-50 under the
      // drawer's 101), where it held focus invisibly and one Escape then closed both.
      const drawerOpen = Boolean(document.querySelector(".slideover"));
      // A backlog-started ticket has its own modal open — "n" must not stack a
      // second composer behind it (same trap the drawer check above closed).
      // showShortcuts too: the cheat-sheet is reachable from the browser (About
      // page), where bare "n" is live, and the same stacking trap applies.
      const modalOpen = showNewTask || drawerOpen || Boolean(backlogHeadKey) || showShortcuts;
      if (shouldTriggerNewTask(e, { modalOpen })) {
        // Swallow the keystroke: the composer autofocuses its textarea, so an
        // un-prevented "n" types itself into the prompt it just opened.
        e.preventDefault();
        setShowNewTask(true);
        return;
      }
      // ⌘, (Settings) and ⌘/ (this cheat-sheet) — desktop-only, and the ONLY
      // two accelerators this handler owns. ⌘N/⌘1-4 stay the Electron
      // application menu's alone (desktop/menu.mjs already handles them and
      // posts "nh:menu" below); matching them here too would double-fire a
      // single keystroke. Escape is deliberately NOT handled here: every
      // dialog that can be open (composer, drawer, Settings, and now this
      // one) already closes itself on Escape via its own listener, so
      // reacting to "close" here would either be a no-op or a double-close.
      // Same modalOpen guard as "n" above: without it, ⌘, / ⌘/ could open
      // Settings or the cheat-sheet OVER the composer/drawer/backlog modal,
      // and one Escape would then close both stacked dialogs at once.
      if (modalOpen) return;
      const action = matchShortcut(e, { isDesktop: Boolean(window.nhDesktop) });
      if (action === "settings") { e.preventDefault(); setSettingsOpen(true); }
      else if (action === "help") { e.preventDefault(); setShowShortcuts(true); }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [showNewTask, backlogHeadKey, showShortcuts]);

  // Desktop application menu → the board's own navigation. The Electron shell
  // (main process) posts "nh:menu"; drive the SAME page state the tab bar does,
  // so File ▸ New Task and View ▸ Board/Stats/Settings need no parallel UI.
  useEffect(() => {
    const off = window.nhDesktop?.onMenu?.((action) => {
      if (action === "new-task") setShowNewTask(true);
      else if (action === "settings") openSettings();
      else if (action === "board" || action === "backlog" || action === "stats") setPage(action);
    });
    return off;
  }, []);

  // Resolve the head of the backlog queue into a composer seed. The FULL issue
  // is fetched first (the browse list truncates description at 2000 chars);
  // if that fetch fails the list brief already in hand stands — a truncated
  // spec beats a dead end, and the operator can still edit it in the composer.
  useEffect(() => {
    if (!backlogHeadKey) { setBacklogSeed(null); return undefined; }
    const head = queueHead(backlog);
    // Which tracker the row came from. It decides the detail endpoint AND the
    // task's `source`, and the two must agree: dedupe keys on
    // (source, external_id), so stamping a Linear ticket "jira" would let it
    // collide with a Jira ticket of the same key.
    const tracker = head.tracker === "linear" ? "linear" : "jira";
    let ignore = false;
    setBacklogSeed(null);
    const seedFrom = (issue) => ({
      prompt: promptFromIssue(issue),
      // The hidden markers a tracker-sourced task carries — identical to what
      // the composer's own picker used to set, so the dedup key reaching the
      // backend is unchanged.
      source: tracker,
      externalId: externalIdFromIssue(issue),
    });
    fetchTrackerIssue(tracker, head.key)
      .then((full) => { if (!ignore) setBacklogSeed(seedFrom(full)); })
      .catch(() => { if (!ignore) setBacklogSeed(seedFrom(head)); });
    return () => { ignore = true; };
    // Keyed on the head KEY, not the array: the queue re-renders on every
    // dequeue, and re-running this on identity alone would re-fetch the same
    // ticket. `backlogQueue[0]` is read inside and is always the row that key
    // belongs to.
  }, [backlogHeadKey]);

  // WebSocket — exponential backoff + snapshot re-fetch on reconnect
  // (wsReconnect.js; incident 2026-08-12). The old effect here closed over a
  // fixed three-second retry timer and never re-fetched the snapshot, so a
  // socket that reconnected after a server restart sat open over stale state
  // with nothing visibly wrong.
  useEffect(() => {
    const reconnector = createReconnector({
      connect: (handlers) => connectWS((msg) => {
        if (msg.tasks) { dispatch({ type: "sync", tasks: msg.tasks }); setTasksLoaded(true); }
        // inflight rides the WS frame; `running` stays the REST poll's
        // authority (a WS frame from a worker-less server said running:true).
        if (msg.worker) setWorkerStatus(prev => ({ ...prev, ...msg.worker }));
      }, handlers),
      fetchSnapshot: fetchTasks,
      onSnapshot: (ts) => { setFetchError(null); dispatch({ type: "set", tasks: ts }); setTasksLoaded(true); },
      onStatus: (phase) => { setWsPhase(phase); setWsLive(phase === "live"); },
    });
    reconnector.start();
    return () => {
      reconnector.stop();
    };
  }, []);

  if (onboarded === null) {
    return (
      <div className="nh-shell">
        <header className="nh-header"><Brand /></header>
        <div className="nh-board board-skeleton" aria-busy="true" aria-label="Loading board">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div className="sk-lane" key={i}>
              <div className="skeleton sk-head" />
              {[0, 1, 2].map((j) => <div className="skeleton sk-card" key={j} />)}
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (onboarded === false) {
    // Onboarding hands back the repo it proved, so setup ends on a first task
    // rather than an empty board.
    return (
      <Onboarding
        onComplete={(res) => {
          setOnboarded(true);
          // The path itself, not just the fact that there is one: TaskComposer
          // already seeds `repoPath` from `initial` (and pins free-text mode
          // once a path is present), so this is all it takes for the composer
          // to open on the repo the wizard just proved.
          if (res && res.firstTaskRepo) {
            setNewTaskSeed({ repoPath: res.firstTaskRepo });
            setShowNewTask(true);
          }
        }}
      />
    );
  }

  if (fetchError) {
    return (
      <div className="nh-shell">
        <header className="nh-header">
          <Brand />
          <span className="legion-credit">Developed by eyalgolan</span>
        </header>
        <div className="nh-center">
          <div className="nh-error">
            <div>API unavailable: {fetchError}</div>
            <button
              className="btn btn-sendback btn-mt"
              onClick={() => window.location.reload()}
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  const needYou = tasks.filter(isNeedsYou).length;
  // SCRUM-15: same derivation as OverviewStrip/lane headers so the sidebar
  // "Working (N)" figure agrees with the board instead of its own count.
  const sidebarCounts = deriveCounts(tasks);
  const banner = connectionBanner(wsPhase);
  return (
    <div className="nh-shell nh-shell-cc">
      {banner && (
        <div className={banner.className} role={banner.role}>{banner.text}</div>
      )}
      <aside className="nh-sidebar">
        <div className="nh-sidebar-brand"><Brand onHome={() => setPage("board")} /></div>
        {/* 1.5: grouped nav, Claude-app style — muted uppercase group headers over
            icon+label rows. Same destinations/handlers as before (Board/Done/Failed/
            Stats all still call setPage); this is visual grouping only. Done/Failed
            move up from the old outlined outcome-pill bar into the Work group. */}
        <nav className="nh-sidenav" aria-label="Primary">
          <NavGroup title="Work">
            <NavRow
              icon={<IconBoard />}
              label="In progress"
              active={page === "board"}
              current={page === "board"}
              onClick={() => setPage("board")}
              badge={needYou > 0 ? needYou : null}
              badgeVariant="alert"
              title={needYou > 0 ? `${needYou} need you` : undefined}
            />
            {/* The tracker's open tickets — the work that hasn't started yet,
                one row above the two outcome lists. This is where a ticket is
                picked up; the composer no longer hides a Jira picker inside it. */}
            <NavRow
              icon={<IconBacklog />}
              label="Backlog"
              active={page === "backlog"}
              current={page === "backlog"}
              onClick={() => setPage("backlog")}
              title="Open tickets from your tracker"
            />
            <NavRow
              icon={<IconDone />}
              label="Done"
              active={page === "done"}
              current={page === "done"}
              onClick={() => setPage("done")}
              badge={doneCount}
              badgeVariant="done"
              title="Tasks that shipped"
            />
            <NavRow
              icon={<IconFailed />}
              label="Failed"
              active={page === "failed"}
              current={page === "failed"}
              onClick={() => setPage("failed")}
              badge={failedCount}
              badgeVariant={failedCount > 0 ? "failed" : undefined}
              title={cancelledCount > 0 ? `${failedCount} failed · ${cancelledCount} cancelled` : "Tasks that failed"}
            />
          </NavGroup>
          <NavGroup title="Insights">
            <NavRow
              icon={<IconStats />}
              label="Stats"
              active={page === "stats"}
              current={page === "stats"}
              onClick={() => setPage("stats")}
            />
          </NavGroup>
        </nav>
        <NightLedger tasks={tasks} authMode={authMode} spend={windowSpend} />
        <div className="nh-sidebar-foot">
          {/* SCRUM-16: render-gate and figure share ONE source (deriveCounts
              over the websocket board payload) — gating on the polled
              workerStatus while displaying the WS count let a transient
              drift show "Working (0)". workerStatus stays only as slot
              capacity in the tooltip. */}
          {sidebarCounts.running > 0 && (
            <div className="nh-status-indicator" title={workerStatus?.max_workers ? `${sidebarCounts.running} of ${workerStatus.max_workers} worker slots in use` : `${sidebarCounts.running} running`}>
              <div className="nh-ws-dot live" style={{ background: 'var(--accent)' }} />
              <span className="nh-status-label">Working ({sidebarCounts.running})</span>
            </div>
          )}
          {queueHealth?.stuck && (
            <div className="nh-alarm" role="alert" title={queueHealth.stuck_reason}>
              Queue stuck — {queueHealth.open_tasks} open, nothing finishing
            </div>
          )}
          {/* 2026-08-20 evidence: /api/queue/health reported "not stuck, 0
              busy, 7 queued, ETA 210 min" while the pool sat behind a quota
              wall — every field individually true, the picture false. A
              pause is deliberate, not a wedge (`stuck` stays false), so it
              gets its own line rather than piggybacking on the alarm. */}
          {!queueHealth?.stuck && queueHealth?.paused && queueHealth?.paused_reason === "infra" && (
            <div className="nh-status-indicator" role="status"
                 title="Pool-wide pause — repeated SDK/auth failures">
              <div className="nh-ws-dot" />
              <span className="nh-status-label">
                Paused — SDK/auth failures, resumes {formatPausedUntil(queueHealth.paused_until)}
              </span>
            </div>
          )}
          {!queueHealth?.stuck && queueHealth?.paused && queueHealth?.paused_reason !== "infra" && (
            <div className="nh-status-indicator" role="status"
                 title={queueHealth.paused_profile ? `${queueHealth.paused_profile} profile hit its quota` : "Pool-wide quota cooldown"}>
              <div className="nh-ws-dot" />
              <span className="nh-status-label">
                Paused — quota resets {formatPausedUntil(queueHealth.paused_until)}
              </span>
            </div>
          )}
          {!queueHealth?.stuck && !queueHealth?.paused && queueHealth?.eta_minutes != null && queueHealth.open_tasks > 0 && (
            <div className="nh-status-indicator" title={`${queueHealth.completed_in_window} finished in the last ${queueHealth.window_minutes} min`}>
              <div className="nh-ws-dot live" />
              <span className="nh-status-label">
                Drains in ~{queueHealth.eta_minutes < 60
                  ? `${Math.round(queueHealth.eta_minutes)}m`
                  : `${(queueHealth.eta_minutes / 60).toFixed(1)}h`}
              </span>
            </div>
          )}
          {workerStatus && workerStatus.running === false && (
            <div className="nh-alarm" role="alert"
                 title="The API is up but no worker claims tasks — replies and retries will sit as Working forever until nh start runs with a worker.">
              Worker offline — tasks won't progress
            </div>
          )}
          {workerStatus?.watcher_error && (
            <div className="nh-alarm" role="alert"
                 title={`WakeWatcher failed to start: ${workerStatus.watcher_error}. Parked tasks will not wake until the server restarts cleanly.`}>
              Wake watcher down — parked tasks won't wake
            </div>
          )}
          {/* The server loads its backend once and never reloads it, so a
              merged fix is not live until a restart — a task can be judged by
              code that was superseded hours ago. The startup log line cannot
              show this: by definition it matters on a server that has been up
              a long time, and that line scrolled away at boot. role="status"
              rather than "alert" — advisory, and nothing here blocks a claim. */}
          {workerStatus?.loaded_code_stale && (
            <div className="nh-alarm nh-stale" role="status"
                 title={`${workerStatus.loaded_code_stale}. Loaded: ${workerStatus.loaded_code || "unknown"}.`}>
              Running superseded code — restart to pick up merged fixes
            </div>
          )}
          <div className="nh-status-indicator" title={wsLive ? "Browser is connected to the no_human server" : "Connection lost — reconnecting…"}>
            <div className={`nh-ws-dot${wsLive ? " live" : ""}`} />
            <span className="nh-status-label">{wsLive ? "Connected" : "Reconnecting…"}</span>
          </div>
          <div className="nh-sidebar-row">
            <button
              className="nh-theme-toggle"
              onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
              title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
              aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
            >
              {theme === "dark" ? "☀" : "☾"}
            </button>
            <span className="legion-credit">by eyalgolan</span>
          </div>
          {/* Settings pinned at the very bottom, Claude-app placement — opens the
              overlay dialog from task 1.1 (page routing is unchanged). */}
          <NavRow
            icon={<IconAbout />}
            label="About"
            active={page === "about"}
            current={page === "about"}
            onClick={() => setPage("about")}
            title="What no_human is, docs, and contact"
          />
          {/* One-time nudge from the "!" — shown after onboarding until
              Settings opens on ANY pane or the popup is dismissed
              (`popupDismissed`, a strictly weaker condition than the
              badge's own `aiConfigDone` below — the badge needs the
              Second-brain pane specifically; this popup does not). Not
              while onboarding is still checking (null) or in progress
              (false). Rendered as a flow sibling here (not inside
              .nh-settings-navwrap) so it can never absolutely-overlap the
              Finish-setup row below it. */}
          {!popupDismissed && onboarded === true && !settingsOpen && (
            <div className="nh-aiconfig-nudge" role="dialog" aria-label="Complete AI configuration">
              <button
                type="button"
                className="nh-aiconfig-nudge-x"
                aria-label="Dismiss"
                onClick={dismissAiConfig}
              >×</button>
              <div className="nh-aiconfig-nudge-title">Complete AI configuration</div>
              <div className="nh-aiconfig-nudge-body">
                Review your rules, pick the model for each role, and seed your
                second brain — all in Settings.
              </div>
              <button
                type="button"
                className="btn btn-approve nh-aiconfig-nudge-cta"
                onClick={() => openSettings("learnings")}
              >Open Settings</button>
            </div>
          )}
          {/* The minimal path's leftover steps, as a compact affordance directly
              above Settings — not the old board-body card (real-user feedback:
              it "took half the screen" and every row deep-linked to the wrong
              pane). Renders only while something is deferred; gone once the last
              item is marked done. */}
          {deferred.length > 0 && (
            <FinishSetupCard
              deferred={deferred}
              onNavigate={({ tab }) => openSettings(tab)}
              onDone={(step) => markDeferredDone(step).then((r) => setDeferred(r.deferred || [])).catch(() => {})}
            />
          )}
          {/* Settings row wrapper — no longer scopes the AI-config popup (moved
              above as a flow sibling); kept for the row's own positioning. */}
          <div className="nh-settings-navwrap">
            <NavRow
              icon={<IconGear />}
              label="Settings"
              active={settingsOpen}
              haspopup="dialog"
              expanded={settingsOpen}
              badge={aiConfigDone ? null : "!"}
              badgeVariant="warn"
              // D2.1: the badge is its OWN click target — it opens Settings
              // straight on the Second-brain pane, distinct from a click on
              // the row body (which still opens on whatever pane was last
              // shown, and no longer clears the flag itself — see
              // handleSecondBrainOpened).
              badgeAriaLabel="Complete AI configuration — open the Second-brain pane"
              onBadgeClick={aiConfigDone ? undefined : () => openSettings("learnings")}
              title={aiConfigDone ? undefined : "Complete AI configuration"}
              onClick={() => openSettings()}
              className="nh-settings-row"
            />
          </div>
        </div>
      </aside>
      <main className="nh-main">
        <h1 className="sr-only">
          {page === "board" ? "Task board"
            : page === "backlog" ? "Backlog"
            : page === "done" ? "Done tasks"
            : page === "failed" ? "Failed tasks"
            : page === "stats" ? "Performance"
            : page === "about" ? "About no_human"
            : "Settings"}
        </h1>
        {page === "board" && (
          <div className="nh-main-bar">
            <OverviewStrip tasks={tasks} />
            <DrainReadoutChip readout={drainReadout} />
            <button className="btn btn-new-task" aria-haspopup="dialog" aria-expanded={showNewTask} onClick={() => setShowNewTask(true)}>+ New Task</button>
          </div>
        )}
        {page === "board" && (
          <Board
            tasks={tasks}
            pendingOpenId={pendingOpenId}
            onPendingOpenHandled={() => setPendingOpenId(null)}
            tasksLoaded={tasksLoaded}
            outcomeCount={doneCount + failedCount}
            onNewTask={() => setShowNewTask(true)}
            onFollowUp={openFollowUp}
          />
        )}
        {page === "backlog" && (
          <Backlog
            refreshNonce={backlogNonce}
            onStart={(list) => setBacklog((s) => backlogQueueReducer(s, { type: "start", issues: list }))}
          />
        )}
        {page === "done" && <Outcomes tasks={tasks} lane="done" onFollowUp={openFollowUp} />}
        {page === "failed" && <Outcomes tasks={tasks} lane="failed" onFollowUp={openFollowUp} />}
        {page === "stats" && <Stats tasks={tasks} />}
        {page === "about" && <About onShowShortcuts={() => setShowShortcuts(true)} />}
      </main>
      {showNewTask && (
        <NewTaskModal
          initial={newTaskSeed}
          onClose={() => { setShowNewTask(false); setNewTaskSeed(null); }}
          onCreated={(created) => {
            setCreateToast(feasibilityCreateToast(created));
            fetchTasks().then((ts) => dispatch({ type: "set", tasks: ts }));
          }}
          onOpenBacklog={() => { setShowNewTask(false); setPage("backlog"); }}
        />
      )}
      {/* A ticket started from the Backlog page runs the SAME modal — composer
          (pre-filled from the ticket) → "Let's scope this" → createTask. `key`
          remounts it per ticket so nothing of the previous one leaks in. */}
      {backlogHeadKey && backlogSeed && (
        <NewTaskModal
          key={backlogHeadKey}
          initial={backlogSeed}
          notice={queueNotice(backlog)}
          queueLeft={queueRemaining(backlog)}
          onStopQueue={stopQueue}
          onCreated={(created) => {
            setCreateToast(feasibilityCreateToast(created));
            setBacklogNonce((n) => n + 1);
            fetchTasks().then((ts) => dispatch({ type: "set", tasks: ts }));
          }}
          // Created and cancelled are the SAME queue transition on purpose:
          // whichever it was, this ticket is done with and the ones behind it
          // are not. (onCreated fires first on the create path; it only
          // refreshes the board.)
          onClose={nextTicket}
        />
      )}
      {backlogHeadKey && !backlogSeed && (
        <BacklogSeedOverlay
          issueKey={backlogHeadKey}
          tracker={queueHead(backlog)?.tracker}
          notice={queueNotice(backlog)}
          queueLeft={queueRemaining(backlog)}
          onSkip={nextTicket}
          onStopQueue={stopQueue}
        />
      )}
      {settingsOpen && (
        <SettingsOverlay
          initialTab={settingsTab}
          onClose={() => { setSettingsOpen(false); setSettingsTab(null); }}
          // Second-brain row's "origin task" link: close Settings, land on the
          // board, and open that task — the same pendingOpenId hand-off the
          // "needs you" desktop notification already uses (Board is
          // conditionally mounted, so a direct openTask() call here would be
          // lost whenever Settings was opened from a page other than the
          // board).
          onOpenTask={(id) => {
            setSettingsOpen(false);
            setSettingsTab(null);
            setPage("board");
            setPendingOpenId(id);
          }}
          // D2.1: the AI-config nudge clears only once the Second-brain pane
          // has actually rendered — called from that pane's own first-mount
          // effect in Settings.jsx, not from openSettings() here.
          onSecondBrainOpened={handleSecondBrainOpened}
        />
      )}
      {showShortcuts && <ShortcutsDialog isDesktop={Boolean(window.nhDesktop)} onClose={() => setShowShortcuts(false)} />}
      {/* P3: the create-time feasibility toast — advisory (role="status", not
          "alert"), so it never reads as an error. Reuses the accent-tinted
          visual language of SlideOver's FeasibilityCard rather than a second
          style; the card itself still gates on live pending status, unchanged. */}
      {createToast && (
        <div className="nh-toast nh-toast-feasibility" role="status" aria-label="Task size hint">
          <span className="nh-toast-text ph-no-capture">{createToast.message}</span>
          <button
            type="button"
            className="nh-toast-dismiss"
            aria-label="Dismiss task size hint"
            onClick={() => setCreateToast(null)}
          >
            ×
          </button>
        </div>
      )}
    </div>
  );
}
