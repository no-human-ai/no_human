// SCRUM-71 (2/3 of SCRUM-67's split): pure formatting for the board header's
// live drain readout. Kept out of the JSX so the handful of states (idle /
// partial / full / no-estimate / unreachable) are unit-testable with
// `node --test` — the same pattern as integrationChip.js's statusChip().
//
// Signature is the flat AC-mandated shape: {workers_busy, max_workers,
// queue_depth, est_drain_seconds|null, error?}. The real backend payload
// (src/no_human/core/health.py: eta_minutes/open_tasks) uses different field
// names — mapping that payload onto this shape, and wiring the chip into the
// header, is SCRUM-71 3/3's job, not this module's.
//
// Never fabricates a number: an unknown ETA renders as "no estimate", not a
// guess. tone is a closed string token ("ok"/"warn"/"error"), consumed
// elsewhere as `tone-${tone}` classes — no CSS vars are introduced here.

import React from "react";
import { parseTimestamp } from "./parseTimestamp.js";

export function formatDrainEta(seconds) {
  if (seconds == null) return "no estimate";
  if (seconds < 3600) return `~${Math.max(1, Math.round(seconds / 60))} min to drain`;
  return `~${(seconds / 3600).toFixed(1)} h to drain`;
}

// 2026-08-20 evidence: `/api/queue/health` reported "not stuck, 0 busy, 7
// queued, ETA 210 min" while the pool sat behind a quota wall — every field
// individually true, the picture false. `paused_until` is the scheduler's
// own cooldown clock (never re-derived here); this only formats it.
export function formatPausedUntil(iso) {
  if (!iso) return "unknown time";
  const d = parseTimestamp(iso);
  if (!d) return "unknown time";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

// One place naming what each `paused_reason` MEANS to an operator — the
// single spot both the header chip (drainChip, below) and the sidebar
// indicator (App.jsx) read, so a reason added here is never explained one
// way in one surface and a different way (or not at all) in the other.
//
// The DEFAULT branch matters as much as the named ones: a `paused_reason`
// this code has never heard of — including a genuinely absent/null one on
// a truthy `paused` — must render as UNKNOWN, never fall through to "quota"
// by virtue of merely not being "infra". That fallthrough is the exact bug
// a lost pool lease exposed (task 92e48491): a permanent, restart-only
// failure rendered as a cooldown that would reset itself. Every reason this
// module does not explicitly recognise gets the same honest "unknown" —
// nothing here infers a specific cause from silence.
export function pausedPresentation(reason, { paused_until = null, paused_profile = null } = {}) {
  const at = formatPausedUntil(paused_until);
  if (reason === "infra") {
    return {
      text: `Paused — SDK/auth failures, resumes ${at}`,
      title: "Pool-wide pause — repeated SDK/auth failures",
      tone: "warn",
    };
  }
  if (reason === "quota") {
    return {
      text: `Paused — quota resets ${at}`,
      title: paused_profile ? `${paused_profile} profile hit its quota` : "Pool-wide quota cooldown",
      tone: "warn",
    };
  }
  if (reason === "lease_lost") {
    // No `at`: nothing resumes this on its own — only a restart does — so
    // there is deliberately no time in the text, unlike the two cooldowns
    // above.
    return {
      text: "Paused — pool lease lost; restart required",
      title: "This scheduler lost the pool lease and will not dispatch again until it is restarted",
      tone: "error",
    };
  }
  return {
    text: "Paused — reason unknown",
    title: `Unrecognised paused_reason (${JSON.stringify(reason)}) — see /api/worker/status`,
    tone: "warn",
  };
}

// The sidebar's own pause indicator (App.jsx), factored out so its rendered
// output is testable directly (`renderToStaticMarkup`) instead of only via a
// source-text guard on App.jsx. Written with React.createElement rather than
// JSX so this stays a plain .js module: no build-time transform is needed to
// import and render it from a `node --test` file, and App.jsx still renders
// it exactly as any other component (`<PausedIndicator .../>`).
export function PausedIndicator({ paused_reason = null, paused_until = null, paused_profile = null } = {}) {
  const p = pausedPresentation(paused_reason, { paused_until, paused_profile });
  return React.createElement(
    "div",
    { className: "nh-status-indicator", role: "status", title: p.title },
    React.createElement("div", { className: "nh-ws-dot" }),
    React.createElement("span", { className: "nh-status-label" }, p.text)
  );
}

// input: the flat {workers_busy, max_workers, queue_depth, est_drain_seconds,
// error, paused, paused_until} object (the raw /api/queue/health payload —
// field names match 1:1, see core/health.py QueueHealth.as_dict). error is
// the most recent poll's failure (if any) — that always wins over any cached
// counts, per the AC's explicit unreachable state. A quota pause wins over
// the normal busy/queued/ETA readout: reporting "0/4 workers busy · 7
// queued · ~3.5 h to drain" with no word of a wall is the defect this exists
// to close.
export function drainChip({
  workers_busy = 0,
  max_workers = 0,
  queue_depth = 0,
  est_drain_seconds = null,
  error = null,
  paused = false,
  paused_until = null,
  paused_reason = null,
  paused_profile = null,
} = {}) {
  if (error) return { text: "server unreachable", tone: "error" };
  if (paused) {
    const { text, tone } = pausedPresentation(paused_reason, { paused_until, paused_profile });
    return { text, tone };
  }

  const parts = [`${workers_busy}/${max_workers} workers busy`, `${queue_depth} queued`];
  if (queue_depth > 0) parts.push(formatDrainEta(est_drain_seconds));

  const tone = max_workers > 0 && workers_busy >= max_workers && queue_depth > 0 ? "warn" : "ok";
  return { text: parts.join(" · "), tone };
}
