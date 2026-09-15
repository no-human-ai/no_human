import { formatDuration } from "./formatDuration.js";

// MEASURED 2026-09-14 on the operator's live dogfood server (pid 52752,
// uptime 2 days): the scheduler's tick loop stopped ticking and nothing told
// anyone. Two /api/worker/status samples ~75s apart showed
// seconds_since_last_tick growing from 125.7 to 200.6 (threshold 60.0) while
// every error field (watcher_error, worker_error, lease_lost) stayed None —
// zero ticks happened in that window, and 10 coder/worktree processes were
// still alive, so work was completing into a void that nobody watched.
//
// The backend already computes and exposes this (scheduler.py health_snapshot,
// merged into /api/worker/status by app.py's `out.update(snapshot())`). The
// defect was purely the missing consumer: `loaded_code_stale` gets a banner,
// `tick_stalled` got none. This module is that consumer.
//
// DECISION (surface only, no self-heal / restart / event emission):
//   - No bounded restart: the measured incident had inflight 3/4 with live
//     worker processes and an UNKNOWN cause (every error field was None).
//     Restarting a loop in an unknown state risks re-dispatching attempts
//     that are still running — exactly the disturbance a fix here must not
//     cause. A heal built on an unknown cause is also untestable.
//   - No event: the only place that ever observes `tick_stalled` is the
//     GET /api/worker/status handler the board polls every 10s per open tab.
//     Emitting a DB row / event from a read endpoint is non-idempotent event
//     spam, and the honest emitter would be the tick loop itself — the very
//     thing that is dead. Surfacing on the existing poll is the only signal
//     path that actually exists.
// Options 2 (event stream) and 3 (bounded restart) remain open follow-ups;
// this change intentionally does neither.
export function tickStallNotice(w) {
  if (!w) return null;
  if (w.running === false) return null;
  if (!w.tick_stalled) return null;

  const neverTicked = !!w.never_ticked;
  const rawAge = neverTicked ? w.seconds_since_start : w.seconds_since_last_tick;
  const age = Number.isFinite(rawAge)
    ? rawAge
    : Number.isFinite(w.seconds_since_start)
      ? w.seconds_since_start
      : null;
  const ageText = age == null ? "for an unknown time" : `for ${formatDuration(age)}`;

  const text = neverTicked
    ? `Scheduler never started ticking (${ageText}) — no task will progress`
    : `Scheduler stalled (${ageText}) — finished work isn't being picked up`;

  const threshold = w.tick_stall_threshold_s;
  const inflight = w.inflight;
  const maxWorkers = w.max_workers;
  const consequence =
    "In-flight attempts will finish into a void — their results are not " +
    "picked up until the loop ticks again.";

  const detail = neverTicked
    ? `The tick loop has never ticked` +
      `${age != null ? ` (running ${age}s` : ""}${age != null && threshold != null ? `, threshold ${threshold}s)` : age != null ? ")" : ""}.`
    : `Last tick was ${w.seconds_since_last_tick != null ? `${w.seconds_since_last_tick}s` : "an unknown time"} ago` +
      `${threshold != null ? ` (threshold ${threshold}s)` : ""}.`;
  const inflightPart =
    inflight != null && maxWorkers != null
      ? ` ${inflight}/${maxWorkers} workers inflight.`
      : "";
  const title = `${detail}${inflightPart} ${consequence}`;

  return { text, title, className: "nh-alarm", role: "alert" };
}
