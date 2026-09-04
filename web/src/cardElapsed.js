// Operator finding: tasks ran 4-5.6h with nothing on the board saying so -
// the operator found out by asking. `TaskSummaryOut` exposes `created_at`/
// `updated_at`/`last_activity` only; there is no dispatch/started stamp on
// the summary (attempt/phase `started_at` exist in core/db.py but are not
// shipped there), so elapsed is measured from `created_at`. That deliberately
// includes queue time - named here, not a silent approximation.

import { timestampMs } from "./parseTimestamp.js";

// In-flight statuses only. Allow-list, not deny-list, so a status added
// server-side tomorrow renders no chip rather than a wrong one. Mirrors the
// active half of Board.jsx's STALE_STATUSES minus the human/queued gates
// (awaiting_approval, awaiting_input, blocked, pending never get a chip).
export const ELAPSED_STATUSES = new Set([
  "context", "planning", "implementing", "reviewing", "testing",
]);

export const WARN_MS = 2 * 3600_000;
export const ERROR_MS = 4 * 3600_000;

/** "0m", "59m", "1h 42m", "5h 0m" - never fabricates sub-minute precision. */
export function formatElapsed(ms) {
  const totalMin = Math.floor(ms / 60_000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

/**
 * `{text, tone, ms}` for an active task's elapsed wall time since
 * `created_at`, or `null` when no chip should render: terminal/parked/queued
 * status, a human-stop/cancel flag, or a missing/unparseable/future
 * timestamp (clock skew never fabricates a duration).
 */
export function elapsedChip(task, nowMs = Date.now()) {
  if (!task) return null;
  if (task.cancelled || task.blocker_human_stopped) return null;
  if (!ELAPSED_STATUSES.has(task.status)) return null;

  const startMs = timestampMs(task.created_at);
  if (Number.isNaN(startMs)) return null;

  const ms = nowMs - startMs;
  if (ms < 0) return null;

  const tone = ms >= ERROR_MS ? "error" : ms >= WARN_MS ? "warn" : "ok";
  return { text: formatElapsed(ms), tone, ms };
}
