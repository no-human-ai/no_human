import { makeEndpointGate } from "./queueHealthGate.js";
import { detailMessage } from "./apiError.js";

// Both arms of the old `import.meta.env.DEV ? "" : ""` were the empty string —
// vite folded it to "" in every build, so this is byte-identical at runtime.
// Written literally because `import.meta.env` is undefined outside vite, and
// that one expression made this whole module unimportable under `node --test`:
// api.test.mjs can now exercise these functions for real instead of asserting
// over their source text. (connectWS still reads import.meta.env, but only when
// called, and it needs a browser anyway.)
const BASE = "";

/** Guard against the SPA catch-all returning index.html instead of JSON. */
function _jsonSafe(r, fallback) {
  const ct = (r.headers.get("content-type") || "");
  if (!ct.includes("application/json")) return fallback;
  return r.json();
}

// Attempt-attributed "last 24h" spend (core/metrics.py:window_spend) — the
// board's ledger must consume this server figure verbatim, never re-derive a
// window total client-side from task timestamps (that re-derivation is the
// exact bug this endpoint fixes: closing an old task bumps `updated_at` with
// no new spend, sweeping its lifetime cost into "last 24h"). A 404 (older
// server) latches to null forever via makeEndpointGate — the sidebar simply
// hides the spend line rather than retrying every poll.
export const fetchWindowSpend = makeEndpointGate(() =>
  fetch(`${BASE}/api/metrics/window?hours=24`));

export async function fetchTasks() {
  const r = await fetch(`${BASE}/api/tasks`);
  if (!r.ok) throw new Error(`GET /api/tasks → ${r.status}`);
  return r.json();
}

/** The three heavy per-attempt blobs (`review_checklist`, `verifier_results`,
 * `test_results`) for one attempt — served lazily (P1, running-task page
 * slow-open) rather than inline on `GET /api/tasks/{id}`, because they are
 * multi-KB JSON per attempt on live tasks and the detail payload was heavy on
 * every open/poll whether or not the drawer needed them yet. */
export async function fetchAttemptDetails(taskId, attemptNumber) {
  const r = await fetch(`${BASE}/api/tasks/${taskId}/attempts/${attemptNumber}/details`);
  if (!r.ok) {
    throw new Error(
      `GET /api/tasks/${taskId}/attempts/${attemptNumber}/details → ${r.status}`);
  }
  return r.json();
}

// The P1 split moved the three heavy blobs off the inline task payload so an
// open/poll would not carry them when the drawer wasn't showing them — but
// `_hydrateAttemptDetails` below still re-fetched EVERY attempt's blobs on
// EVERY `fetchTask` call, so a SlideOver poll kept making 1+N requests for
// the same total payload the split was supposed to shrink.
//
// A TERMINAL attempt's blobs never change again (the review/verifier/test
// rows are written once, at that attempt's terminal transition, and never
// revised in place) so its detail response is safe to cache once and reuse.
// "Terminal" here is `attempt.status` in ("failed", "succeeded") — NOT
// `attempt.completed_at`: only the successful-delivery paths in the
// orchestrator's finalizer ever stamp `completed_at` (a handful of call
// sites), while the far more common failure paths (dozens of call sites)
// mark `status: "failed"` and leave `completed_at` null. Gating on
// `completed_at` alone would leave the ORDINARY non-final shape — a failed
// attempt — refetched on every poll forever, unrealizing the win this cache
// exists for.
//
// `status: "interrupted"` (a crashed worker's row, or a server-stop mid-run)
// is deliberately EXCLUDED even though it looks terminal: an interrupted
// attempt can still be resumed, and a resume can go on to write NEW
// review/verifier/test blobs onto that same attempt row — caching it would
// risk serving a stale pre-resume snapshot forever. A still-running attempt
// (the default in-progress status, not yet "failed"/"succeeded") is never
// cacheable either, for the same reason: its blobs genuinely change between
// polls.
//
// `completed_at` still rides along in the cache KEY (not the eligibility
// gate) as an extra invalidation signal when present: on a terminal row that
// does carry one, a later value (a resumed attempt reaching a new terminal
// state) produces a new key and busts the old one, rather than serving a
// previous run's blobs under the same key forever.
//
// Bounded FIFO/LRU at `_ATTEMPT_DETAILS_CACHE_CAP` entries: a long board
// session opening many tasks' drawers must not grow this without limit. A
// cache hit is moved to the end (most-recently-used) so eviction drops the
// entry nobody has touched in the longest time, not just the oldest write.
const _ATTEMPT_DETAILS_CACHE_CAP = 50;
const _attemptDetailsCache = new Map();

// Terminal `attempt.status` values whose heavy blobs are frozen and
// therefore safe to cache — see the block comment above for why
// "interrupted" and the in-flight default are excluded.
const _CACHEABLE_ATTEMPT_STATUSES = new Set(["failed", "succeeded"]);

function _attemptCacheKey(taskId, attempt) {
  return `${taskId}:${attempt.attempt_number}:${attempt.completed_at ?? ""}`;
}

function _rememberAttemptDetails(key, blobs) {
  _attemptDetailsCache.set(key, blobs);
  while (_attemptDetailsCache.size > _ATTEMPT_DETAILS_CACHE_CAP) {
    // Map iteration order is insertion order — the first key is the
    // least-recently-used one (see the `delete`+`set` re-insert on a hit
    // below).
    const oldest = _attemptDetailsCache.keys().next().value;
    _attemptDetailsCache.delete(oldest);
  }
}

// Test seam only — production code never inspects cache size or clears it;
// the cap above is what keeps a live session bounded.
export function _attemptDetailsCacheSizeForTests() {
  return _attemptDetailsCache.size;
}
export function _clearAttemptDetailsCacheForTests() {
  _attemptDetailsCache.clear();
}

/** Merge each attempt's lazily-fetched heavy blobs back onto `task.attempts`
 * in place, so callers see the exact shape the detail payload used to inline
 * — the drawer's own summary logic (`slideOverSummary.js`, `SlideOver.jsx`)
 * never has to know the split happened. A single attempt's fetch failing
 * (404, offline) degrades that ONE attempt to missing fields rather than
 * failing the whole task fetch — the same "no data" shape those consumers
 * already handle for an attempt that predates these columns. A terminal
 * ("failed"/"succeeded") attempt with a cached entry skips the network call
 * entirely — see the cache doc comment above `_attemptDetailsCache`. */
async function _hydrateAttemptDetails(task) {
  const attempts = task?.attempts;
  if (!Array.isArray(attempts) || attempts.length === 0) return task;
  await Promise.all(attempts.map(async (a) => {
    const cacheable = _CACHEABLE_ATTEMPT_STATUSES.has(a?.status);
    const key = cacheable ? _attemptCacheKey(task.id, a) : null;
    if (key && _attemptDetailsCache.has(key)) {
      const cached = _attemptDetailsCache.get(key);
      // Re-insert so this key becomes the most-recently-used for eviction.
      _attemptDetailsCache.delete(key);
      _attemptDetailsCache.set(key, cached);
      a.review_checklist = cached.review_checklist;
      a.verifier_results = cached.verifier_results;
      a.test_results = cached.test_results;
      return;
    }
    try {
      const details = await fetchAttemptDetails(task.id, a.attempt_number);
      const blobs = {
        review_checklist: details.review_checklist,
        verifier_results: details.verifier_results,
        test_results: details.test_results,
      };
      a.review_checklist = blobs.review_checklist;
      a.verifier_results = blobs.verifier_results;
      a.test_results = blobs.test_results;
      // Only a SUCCESSFUL fetch for a TERMINAL attempt is cached — a failed
      // fetch (404, offline) must not be remembered as "these fields are
      // absent forever" for what may be a transient blip.
      if (key) _rememberAttemptDetails(key, blobs);
    } catch {
      // Leave this attempt's heavy fields absent — see doc comment above.
    }
  }));
  return task;
}

export async function fetchTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}`);
  if (!r.ok) throw new Error(`GET /api/tasks/${id} → ${r.status}`);
  const task = await r.json();
  return _hydrateAttemptDetails(task);
}

export async function fetchDiff(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/diff`);
  if (!r.ok) return "";
  return r.text();
}

export async function createTask({ title, description, repo_path, project_id, kind, priority, acceptance_criteria, source, external_id, backend, follows_id }) {
  const r = await fetch(`${BASE}/api/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // `backend` is "" unless the composer's picker was touched — the server
    // treats a missing OR falsy value as "use worker.backend" (see
    // CreateTaskRequest.backend and the `if body.backend:` check in the POST
    // handler), so an untouched control's empty string is handled exactly
    // like an absent field and behaves exactly as it did before the picker
    // existed.
    body: JSON.stringify({ title, description, repo_path, project_id, kind, priority, acceptance_criteria, source, external_id, backend, follows_id }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `POST /api/tasks → ${r.status}`));
  }
  return r.json();
}

export async function uploadAttachment(taskId, file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`${BASE}/api/tasks/${taskId}/attachments`, {
    method: "POST", body: fd,
  });
  // detailMessage, like every other mutating call here: the server explains WHY
  // (a 409 reason, a 422 naming the offending field) and a bare status code
  // threw that away, leaving the operator unable to tell a REFUSED action from
  // a network blip. This one also has a 413/415 the operator can act on.
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `upload ${file.name} → ${r.status}`));
  }
  return r.json();
}

// approve_task lands the PR synchronously — a squash + push the sibling
// merge-progress feature documents as a normal 2-4 minute server call (see
// SlideOver.jsx's `merging` state and slideOverSummary.js's
// approveButtonState). An earlier version of this function raced that with a
// 30s client-side AbortController: on any land past 30s (the common case)
// the fetch aborted while the server kept merging, and the UI told the
// operator "Nothing was merged" while it in fact had — a false claim, and
// aborting the client fetch cannot cancel server-side work anyway. No
// client-side timeout here; the button's in-flight state (driven by
// `merging`, independent of this fetch) is what tells the operator it is
// still working, and the WS task-event stream (connectTaskProgress) shows
// live step progress for however long the land actually takes.
export async function approveTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/approve`, { method: "POST" });
  if (!r.ok) {
    // The server explains WHY (409: the task is no longer awaiting approval,
    // or a merge is already in progress; 500: a land failure with a
    // {step, stderr} detail). A bare status code left the operator with no
    // way to tell a rejected approval from a network blip — and the inline
    // failure panel needs the raw `detail` object (step/stderr), not just
    // the flattened message text, so both are attached to the thrown Error.
    const detail = await r.json().catch(() => ({}));
    const err = new Error(detailMessage(detail, `POST approve → ${r.status}`));
    err.status = r.status;
    err.detail = detail?.detail;
    throw err;
  }
  return r.json();
}

export async function finishReview(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/finish-review`, { method: "POST" });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `POST finish-review → ${r.status}`));
  }
  return r.json();
}

// Feature #1 — the 1-click split. `fetchSplitDrafts` lazily generates the 2-4
// proposed sub-tasks (a utility-model call runs server-side only when the split
// screen opens); `splitTask` creates the human-confirmed set as child tasks and
// cancels the original.
export async function fetchSplitDrafts(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/split-drafts`);
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `GET split-drafts → ${r.status}`));
  }
  return r.json();   // { drafts: [{title, description, contract}] }
}

export async function splitTask(id, drafts) {
  const r = await fetch(`${BASE}/api/tasks/${id}/split`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ drafts }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `POST split → ${r.status}`));
  }
  return r.json();   // [child TaskSummaryOut]
}

export async function replyTask(id, answer) {
  const r = await fetch(`${BASE}/api/tasks/${id}/reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answer }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `POST reply → ${r.status}`));
  }
  return r.json();
}

// W2.4: answer a blocker by choosing option N (1-based). The server applies
// the option's action (if any) and resumes — the only path that may apply
// actions, and it runs on a human's click.
export async function chooseBlockerOption(id, choose) {
  const r = await fetch(`${BASE}/api/tasks/${id}/reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ choose }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `POST reply(choose) → ${r.status}`));
  }
  return r.json();
}

export async function sendBack(id, message) {
  const r = await fetch(`${BASE}/api/tasks/${id}/send-back`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!r.ok) {
    // The 409s here say "task is already done" / "task is cancelled"
    // (api/app.py send_back) - the reason the send-back did not take.
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `POST send-back → ${r.status}`));
  }
  return r.json();
}

// ── Intake grill ────────────────────────────────────────────────────────────

export async function grillStep({ title, description, repo_path, project_id, qa_history }) {
  const r = await fetch(`${BASE}/api/grill`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, description, repo_path, project_id, qa_history: qa_history || [] }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `POST /api/grill → ${r.status}`));
  }
  return r.json();
}

// ── Grill SSE streaming ─────────────────────────────────────────────────────

export function grillStepSSE({ title, description, repo_path, project_id, qa_history }, onEvent, onResult, onError, onEval) {
  // POST-based SSE: we need to fetch as a stream since EventSource only does GET.
  const ctrl = new AbortController();
  (async () => {
    try {
      const r = await fetch(`${BASE}/api/grill/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, description, repo_path, project_id, qa_history: qa_history || [] }),
        signal: ctrl.signal,
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        if (onError) onError(new Error(detailMessage(d, `POST /api/grill/stream → ${r.status}`)));
        return;
      }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const data = JSON.parse(line.slice(6));
            if (data.kind === "done") { return; }
            if (data.kind === "eval_verdict") {
              if (onEval) onEval(data);
            } else if (data.kind === "grill_result" || data.kind === "grill_question") {
              if (onResult) onResult(data);
            } else if (data.kind === "error") {
              if (onError) onError(new Error(data.text || "grill error"));
            } else {
              if (onEvent) onEvent(data);
            }
          } catch { /* skip malformed */ }
        }
      }
    } catch (err) {
      if (err.name !== "AbortError" && onError) onError(err);
    }
  })();
  return { close: () => ctrl.abort() };
}

// ── Task lifecycle ──────────────────────────────────────────────────────────

export async function pauseTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/pause`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST pause → ${r.status}`)); }
  return r.json();
}

export async function resumeTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/resume`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST resume → ${r.status}`)); }
  return r.json();
}

export async function cancelTask(id, reason) {
  // No reason → the exact same no-body POST as before (server default:
  // "Cancelled from board"). A reason is posted as JSON so the operator's
  // typed explanation lands in the task's audit trail.
  const opts = reason
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }) }
    : { method: "POST" };
  const r = await fetch(`${BASE}/api/tasks/${id}/cancel`, opts);
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST cancel → ${r.status}`)); }
  return r.json();
}

export async function retryTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/retry`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST retry → ${r.status}`)); }
  return r.json();
}

// ── Knowledge management ────────────────────────────────────────────────────

export async function fetchRules({ includeArchived = false } = {}) {
  const r = await fetch(`${BASE}/api/rules${includeArchived ? "?include_archived=1" : ""}`);
  if (!r.ok) throw new Error(`GET /api/rules → ${r.status}`);
  return r.json();
}

export async function addRule({ title, content, tags, project }) {
  const r = await fetch(`${BASE}/api/rules`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, content, tags: tags || [], project }),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST rules → ${r.status}`)); }
  return r.json();
}

export async function removeRule(id) {
  const r = await fetch(`${BASE}/api/rules/${id}`, { method: "DELETE" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `DELETE rules → ${r.status}`)); }
  return r.json();
}

export async function fetchSkills({ includeArchived = false } = {}) {
  const r = await fetch(`${BASE}/api/skills${includeArchived ? "?include_archived=1" : ""}`);
  if (!r.ok) throw new Error(`GET /api/skills → ${r.status}`);
  return r.json();
}

export async function addSkill({ title, content, tags, project }) {
  const r = await fetch(`${BASE}/api/skills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, content, tags: tags || [], project }),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST skills → ${r.status}`)); }
  return r.json();
}

export async function removeSkill(id) {
  const r = await fetch(`${BASE}/api/skills/${id}`, { method: "DELETE" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `DELETE skills → ${r.status}`)); }
  return r.json();
}

// `includePaused` (D3.2 review deferral): the Second-brain panel's list must
// keep showing a paused row (its own `Paused` chip, so Restore is
// discoverable) even though `list_memories`'s default hides it from every
// OTHER caller, injection included. `includeArchived` (D3.2 review-round
// fix): the same panel's Delete only archives, never really deletes — a
// caller that never asks for the archived row back makes that
// recoverability theoretical, so the panel's own archived-count footer
// passes this to get it back. Both are only meaningful with `active: true` —
// see `GET /api/learnings`'s docstring for why either flag is a harmless
// no-op on the pending branch rather than something this client has to gate
// on.
export async function fetchLearnings({ active = false, includePaused = false, includeArchived = false } = {}) {
  const r = await fetch(`${BASE}/api/learnings?active=${active}&include_paused=${includePaused}&include_archived=${includeArchived}`);
  if (!r.ok) throw new Error(`GET /api/learnings → ${r.status}`);
  return r.json();
}

export async function fetchQuarantineCounts() {
  const r = await fetch(`${BASE}/api/memories/quarantine`);
  if (!r.ok) throw new Error(`GET /api/memories/quarantine → ${r.status}`);
  return r.json();
}

export async function confirmLearning(id) {
  const r = await fetch(`${BASE}/api/learnings/${id}/confirm`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST confirm → ${r.status}`)); }
  return r.json();
}

export async function rejectLearning(id) {
  const r = await fetch(`${BASE}/api/learnings/${id}/reject`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST reject → ${r.status}`)); }
  return r.json();
}

// D3: the Second-brain UI's Pause action — the row stays (recoverable),
// never injected again. Idempotent on the server (pausing an already-paused
// row is a no-op 200, not an error).
export async function pauseLearning(id) {
  const r = await fetch(`${BASE}/api/learnings/${id}/pause`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST pause → ${r.status}`)); }
  return r.json();
}

// D3: the Second-brain UI's Delete action — archives the row, never a real
// delete (mirrors the curator's never-deletes invariant); recoverable via
// restoreLearning.
export async function deleteLearning(id) {
  const r = await fetch(`${BASE}/api/learnings/${id}/delete`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST delete → ${r.status}`)); }
  return r.json();
}

// Memory lifecycle C: the retire? section — stale ACTIVE (confirmed) rules,
// suggest-only. Read-only; nothing here archives anything.
export async function fetchRetireCandidates({ days = 90 } = {}) {
  const r = await fetch(`${BASE}/api/learnings/retire-candidates?days=${days}`);
  if (!r.ok) throw new Error(`GET retire-candidates → ${r.status}`);
  return r.json();
}

// The human's explicit yes to a retire? suggestion. Reversible server-side
// (archive, never delete); idempotent (a second call reports
// `already_archived` rather than erroring).
export async function retireLearning(id) {
  const r = await fetch(`${BASE}/api/learnings/${id}/retire`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST retire → ${r.status}`)); }
  return r.json();
}

// Rules/Skills UI triage action (Memory lifecycle C part B): undo an
// archive/retire/supersede/sweep. Idempotent on the server (already_active),
// same convention as retireLearning above.
export async function restoreLearning(id) {
  const r = await fetch(`${BASE}/api/learnings/${id}/restore`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST restore → ${r.status}`)); }
  return r.json();
}

export async function fetchConfig() {
  const r = await fetch(`${BASE}/api/config`);
  if (!r.ok) throw new Error(`GET /api/config → ${r.status}`);
  return r.json();
}

// Persist the opt-out telemetry consent (`telemetry.enabled`) to config.yaml.
// (The onboarding step + Settings pane that once called this were removed,
// operator 2026-08-26.) The server mints the anonymous instance id on first
// enable — the browser only ever sends the boolean.
export const saveTelemetryConsent = (enabled) =>
  _put("/api/telemetry/consent", { enabled });

// The current consent state, and ONLY that — Settings deliberately has no
// whole-config reader any more (the Config panel was removed; its absence is
// pinned by settingsOverlay.test.mjs), so the Usage insights panel gets a
// projection that cannot grow back into one.
export async function fetchTelemetryConsent() {
  const cfg = await fetchConfig();
  return { enabled: Boolean(cfg?.telemetry?.enabled) };
}

// The running `nh` version and distribution channel, for the browser path
// where there is no desktop bridge to read it from. Never throws a version
// out of thin air: the caller treats a failure as "unknown", which is what it
// was before this existed. `published` fails closed — an older server that
// only ever returned `{version}` (or a malformed body) reads as unpublished,
// never as a command that might not resolve.
export async function fetchVersion() {
  const r = await fetch(`${BASE}/api/version`);
  if (!r.ok) throw new Error(`GET /api/version → ${r.status}`);
  const d = await r.json();
  const version = typeof d?.version === "string" && d.version ? d.version : null;
  const distName = typeof d?.dist_name === "string" && d.dist_name ? d.dist_name : null;
  const published = d?.published === true;
  return { version, distName, published };
}

export async function fetchProfiles() {
  const r = await fetch(`${BASE}/api/profiles`);
  if (!r.ok) return [];
  return r.json();
}

// Auth status for the Settings Account panel. Returns null when the endpoints
// are absent (a build without the auth endpoints) so the panel degrades to an
// "unavailable" note instead of throwing. The payload never contains a token.
export async function fetchAuthStatus() {
  try {
    const r = await fetch(`${BASE}/api/auth/status`);
    if (!r.ok) return null;
    return r.json();
  } catch { return null; }
}

// Write an OAuth token for a profile. On a 422 refusal (a metered API key, an
// empty token, or a newline) `_put` throws Error(detail) — a human-facing
// message written to be shown verbatim. Never returns the token.
export async function setAuthToken(profile, token) {
  return _put("/api/auth/token", { profile, token });
}

// Set the Codex coder backend's auth mode ("api_key" | "subscription"). Writes
// llm.codex_auth_mode to config.yaml server-side; the KEY never travels here.
// `_put` throws Error(detail) verbatim on a 422. Returns the auth status shape.
export async function setCodexMode(mode) {
  return _put("/api/auth/codex-mode", { mode });
}

// Write the OpenAI API key to ~/.no_human/.env (never config). Same write-only
// discipline as setAuthToken: the value is cleared on submit and never returned
// by the server (the response carries the variable NAME only). `_put` throws
// Error(detail) verbatim on a 422 (empty/newline-injected key).
export async function setCodexKey(key) {
  return _put("/api/auth/codex-key", { key });
}

// Settings → Models pane. Returns null when the endpoint is absent (an older
// server build) OR when the SPA catch-all answers with `index.html` at status
// 200 instead of real JSON — `r.json()` on an HTML body throws, and that
// throw is caught here the same way a network failure is, so the pane always
// degrades to its "unavailable" note rather than rendering an empty select.
export async function fetchModels() {
  try {
    const r = await fetch(`${BASE}/api/models`);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

// Write a `{config_key: model_id}` subset to llm.*. `_put` throws
// Error(detail) verbatim on a 422 — a single unattributed string naming
// nothing was written, since apply_model_changes validates every submitted
// key before writing any of them.
export const saveModels = (body) => _put("/api/config/models", body);

// Settings → Models pane's coder-backend row (core/backend_settings.py ::
// backend_payload). Same degrade-to-null convention as fetchModels: an
// older server without the endpoint, or the SPA catch-all answering with
// index.html at 200, both fail `r.json()` and land here as null rather than
// throwing into the render path.
export async function fetchCoderBackend() {
  try {
    const r = await fetch(`${BASE}/api/coder-backend`);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

// Write `{backend: <name>}` to worker.backend — the GLOBAL default coder
// backend. `_put` throws Error(detail) verbatim on a 422 (an unsupported
// name, or one this install cannot currently run — the same reason the
// dropdown already greyed it out with, never a second message).
export const saveCoderBackend = (body) => _put("/api/config/coder-backend", body);

// Settings → Models pane's worker-count row (config.set_concurrency ::
// concurrency.max_workers / .enabled). Same degrade-to-null convention as
// fetchModels for an older server without the endpoint.
export async function fetchWorkers() {
  try {
    const r = await fetch(`${BASE}/api/config/workers`);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

// Write `{max_workers?, enabled?}` to concurrency.*. `_put` throws
// Error(detail) verbatim on a 422 (out of range, wrong type).
export const saveWorkers = (body) => _put("/api/config/workers", body);

// C3-G3: the repos the operator knows (for the repo-understanding picker).
export async function fetchRepos() {
  try {
    const r = await fetch(`${BASE}/api/repos`);
    if (!r.ok) return [];
    // await so a malformed body rejects INSIDE this try (the fetchMetrics
    // fix, PR #111; its review found these two siblings).
    return await r.json();
  } catch {
    return [];
  }
}

// C3-G3: what no_human understands about one known repo (profile + cached
// repo map + matched playbooks). Null when the repo is unknown/unavailable.
export async function fetchRepoUnderstanding(path) {
  try {
    const r = await fetch(`${BASE}/api/repo?path=${encodeURIComponent(path)}`);
    if (!r.ok) return null;
    // await so a malformed body rejects INSIDE this try (the fetchMetrics
    // fix, PR #111; its review found these two siblings).
    return await r.json();
  } catch {
    return null;
  }
}

// C3-G4: cross-task full-text search over the failure/fix record. [] on any
// error or empty query so the search box degrades quietly.
export async function searchEvents(q) {
  if (!q || !q.trim()) return [];
  try {
    const r = await fetch(`${BASE}/api/search?q=${encodeURIComponent(q)}`);
    if (!r.ok) return [];
    return await r.json();   // await so a malformed body rejects INSIDE this try
  } catch {
    return [];
  }
}

// The north-star numbers straight from the record (PRs merged/opened,
// tokens-per-PR, review verdicts, cache economics). Null when unavailable so
// Stats degrades to its client-side aggregates.
export async function fetchMetrics() {
  try {
    const r = await fetch(`${BASE}/api/metrics`);
    if (!r.ok) return null;
    // await so a malformed body rejects INSIDE this try (same fix as
    // searchEvents) — un-awaited, a 200-with-bad-JSON rejected outside the
    // catch and left Stats' loader spinning forever (PR #108 review, low).
    return await r.json();
  } catch {
    return null;
  }
}


export async function fetchProjects() {
  const r = await fetch(`${BASE}/api/projects`);
  if (!r.ok) return [];
  return r.json();
}

export async function createProject({ name, repo_paths, primary_repo }) {
  const r = await fetch(`${BASE}/api/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, repo_paths, primary_repo }),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST projects → ${r.status}`)); }
  return r.json();
}

export async function scaffoldRepo(parent, name) {
  const r = await fetch(`${BASE}/api/repos/scaffold`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parent, name }),
  });
  // The backend's `detail` names WHICH validation failed - surface it verbatim.
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST repos/scaffold → ${r.status}`)); }
  return r.json();
}

export async function updateProject(id, body) {
  const r = await fetch(`${BASE}/api/projects/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `PUT projects → ${r.status}`)); }
  return r.json();
}

export async function deleteProject(id) {
  const r = await fetch(`${BASE}/api/projects/${id}`, { method: "DELETE" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `DELETE projects → ${r.status}`)); }
  return r.json();
}

export async function fetchTaskEvents(taskId) {
  const r = await fetch(`${BASE}/api/tasks/${taskId}/events`);
  if (!r.ok) return [];
  return r.json();
}

export async function fetchSubtasks(taskId) {
  const r = await fetch(`${BASE}/api/tasks/${taskId}/subtasks`);
  if (!r.ok) return [];
  return r.json();
}

export async function postReviewComments(taskId, items = null) {
  const r = await fetch(`${BASE}/api/tasks/${taskId}/post-review-comments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `POST post-review-comments → ${r.status}`));
  }
  return r.json();
}

export const fetchQueueHealth = makeEndpointGate(
  () => fetch(`${BASE}/api/queue/health`));

export async function fetchWorkerStatus() {
  const r = await fetch(`${BASE}/api/worker/status`);
  if (!r.ok) return { running: false, inflight: 0, max_workers: 0 };
  return r.json();
}

// ── Onboarding wizard ────────────────────────────────────────────────────────

async function _put(path, body) {
  const r = await fetch(`${BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `PUT ${path} → ${r.status}`));
  }
  return r.json();
}

async function _post(path, body) {
  const r = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST ${path} → ${r.status}`)); }
  return r.json();
}

async function _get(path) {
  const r = await fetch(`${BASE}${path}`);
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `GET ${path} → ${r.status}`)); }
  return r.json();
}

export async function fetchOnboardingStatus() {
  const r = await fetch(`${BASE}/api/onboarding/status`);
  if (!r.ok) throw new Error(`GET onboarding/status → ${r.status}`);
  return r.json();
}
// Auto-discovery over home + the conventional clone roots. `root` scans just
// that one folder, wherever it resolves — even outside home, the user typed
// it on purpose; `limit` caps the rows. Both optional — a plain
// `discoverRepos()` is the default scan.
export const discoverRepos = async ({ root, limit } = {}) => {
  const qs = new URLSearchParams();
  if (limit) qs.set("limit", limit);
  if (root)  qs.set("root", root);
  const q = qs.toString();
  const r = await fetch(`${BASE}/api/repos/discover${q ? `?${q}` : ""}`);
  if (!r.ok) throw new Error(`GET repos/discover → ${r.status}`);
  return r.json();
};

// The old single-root POST /repos/detect was retired (it 404s now); this is a
// thin shim over the one scanner so callers passing a folder path keep working.
export const detectRepos       = (root)    => discoverRepos({ root });

export const onboardRepo       = (repo_path) => _post("/api/onboarding/repos/onboard", { repo_path });

// PROVE a repo's commands by really running them, streaming the real output.
// POST-based SSE (same shape as grillStepSSE above — EventSource is GET-only).
// `onFrame` receives every frame; the caller decides what to render. Returns a
// handle whose close() aborts the stream (the run itself is bounded server-side).
export function proveRepoSSE({ repo_path, test_cmd, install_cmd, timeout }, onFrame, onError) {
  const ctrl = new AbortController();
  (async () => {
    try {
      const r = await fetch(`${BASE}/api/onboarding/repos/prove`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_path, test_cmd, install_cmd, timeout }),
        signal: ctrl.signal,
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        if (onError) onError(new Error(detailMessage(d, `POST /api/onboarding/repos/prove → ${r.status}`)));
        return;
      }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const data = JSON.parse(line.slice(6));
            if (data.kind === "stream_end") return;
            if (data.kind === "error") {
              if (onError) onError(new Error(data.text || "prove failed"));
              return;
            }
            if (onFrame) onFrame(data);
          } catch { /* skip malformed */ }
        }
      }
    } catch (err) {
      if (err.name !== "AbortError" && onError) onError(err);
    }
  })();
  return { close: () => ctrl.abort() };
}

export const confirmRepoProfile = (repo_path) => _post("/api/onboarding/repos/confirm", { repo_path });
export const setRepoUiEvidence  = (repo_path, enabled) => _post("/api/onboarding/repos/ui-evidence", { repo_path, enabled });

export async function fetchReadiness() {
  const r = await fetch(`${BASE}/api/onboarding/readiness`);
  if (!r.ok) throw new Error(`GET onboarding/readiness → ${r.status}`);
  return r.json();
}
export const extractHistory    = ()        => _post("/api/onboarding/history/extract", {});
export const analyzeHistory    = (days = 30, repo_paths = []) => _post("/api/onboarding/history/analyze", { days, repo_paths });
export const confirmRules      = (ids)     => _post("/api/onboarding/rules/confirm", { ids });
export const completeOnboarding = (payload) => _post("/api/onboarding/complete", payload);
// Minimal path (spec §3 B1): the deferred steps carried on the board's Finish-setup card.
export const fetchDeferred     = ()        => _get("/api/onboarding/deferred");
export const markDeferredDone  = (step)    => _post(`/api/onboarding/deferred/${encodeURIComponent(step)}/done`, {});
export const generateDocs      = (repo_path) => _post("/api/onboarding/docs/generate", { repo_path });
export const getDocsJob        = (id)        => _get(`/api/onboarding/docs/jobs/${encodeURIComponent(id)}`);
export const detectDocs        = (repo)      => _get(`/api/onboarding/docs/detect?repo=${encodeURIComponent(repo)}`);

// ── Integrations (status registry; secrets never returned) ──────────────────
/**
 * The configured-integrations registry.
 *
 * THROWS on a failed request. It used to swallow every non-ok response into
 * `{integrations: []}`, which is indistinguishable from a healthy server
 * answering "nothing is configured" — so a 500, a dead server or a proxy error
 * all rendered as "Jira is not configured", sending the operator to Settings to
 * fix a token that was never the problem. "I could not ask" and "the answer is
 * none" are different facts and the caller has to be able to tell them apart.
 *
 * The callers that genuinely don't care (the composer's optional Backlog
 * pointer, the settings list) keep their own `.catch`.
 */
export async function fetchIntegrations() {
  let r;
  try {
    r = await fetch(`${BASE}/api/integrations`);
  } catch {
    // fetch() rejects only when the request never got an answer at all.
    throw new Error("the no_human server did not answer");
  }
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `GET /api/integrations → ${r.status}`));
  }
  return _jsonSafe(r, { integrations: [] });
}
// Run a live health check for one integration; returns the updated status.
export const testIntegration = (name) =>
  _post(`/api/integrations/${encodeURIComponent(name)}/test`, {});
// Save an integration's settings-form fields (dirty fields only — see
// Integrations.jsx). Returns the refreshed status card + its `fields` array;
// never a secret value. Throws with the server's 422/404 `detail` message.
export const saveIntegrationConfig = (name, fields) =>
  _put(`/api/integrations/${encodeURIComponent(name)}/config`, { fields });

// ── Onboarding "Connect your tools" (config.yaml only, NEVER a secret) ──────
// One card per block under DEFAULT_CONFIG["integrations"], discovered server-
// side, so a new integration appears with no change here. `secrets` carries
// only env-var NAMES + a `set` bool.
export async function fetchIntegrationSetup() {
  const r = await fetch(`${BASE}/api/integrations/setup`);
  if (!r.ok) return { integrations: [] };
  return _jsonSafe(r, { integrations: [] });
}
// Persist one integration's non-secret settings. The server refuses (422) any
// field that would put a credential in config.yaml.
export const saveIntegrationSetup = (name, values) =>
  _put(`/api/integrations/${encodeURIComponent(name)}/setup`, { values });

// Task 1.6: browse/pick a configured tracker's tickets for the Backlog page.
// Throws with the server's 503 (unconfigured) / 502 (upstream error) `detail`
// message — the page surfaces that text as-is.
//
// ONE implementation for both trackers: /api/integrations/{tracker}/issues have
// the same contract and return the same row shape (TrackerIssueOut), so the
// page has one code path per row whichever tracker it came from.
async function _trackerIssues(tracker, q, limit) {
  const params = new URLSearchParams({ q: q || "", limit: String(limit) });
  const r = await fetch(`${BASE}/api/integrations/${tracker}/issues?${params}`);
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `GET ${tracker}/issues → ${r.status}`));
  }
  return r.json();
}

async function _trackerIssue(tracker, key) {
  const r = await fetch(
    `${BASE}/api/integrations/${tracker}/issues/${encodeURIComponent(key)}`);
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `GET ${tracker}/issues/${key} → ${r.status}`));
  }
  return r.json();
}

export async function searchJiraIssues(q, limit = 50) {
  return _trackerIssues("jira", q, limit);
}
export async function searchLinearIssues(q, limit = 50) {
  return _trackerIssues("linear", q, limit);
}

// SCRUM-9: fetch ONE issue in full when it's picked — the browse list above
// truncates description to 2000 chars (list payload), so the composer prefill
// must not be built from that brief alone. Throws with the server's 503/502
// `detail`, same convention as searchJiraIssues.
export async function fetchJiraIssue(key) {
  return _trackerIssue("jira", key);
}
export async function fetchLinearIssue(key) {
  return _trackerIssue("linear", key);
}

/** Browse both trackers, or whichever ones are configured. `trackers` is the
 * list of names from the integrations registry. */
export function searchTrackerIssues(tracker, q, limit = 50) {
  return tracker === "linear" ? searchLinearIssues(q, limit) : searchJiraIssues(q, limit);
}
export function fetchTrackerIssue(tracker, key) {
  return tracker === "linear" ? fetchLinearIssue(key) : fetchJiraIssue(key);
}

export async function suggestPaths(path) {
  const r = await fetch(`${BASE}/api/fs/suggest?path=${encodeURIComponent(path || "")}`);
  if (!r.ok) return { suggestions: [] };
  return r.json();
}

// ── Phase 4a: SSE live event stream ──────────────────────────────────────────
export function connectTaskSSE(taskId, onEvent, onDone) {
  const url = `${BASE}/api/tasks/${encodeURIComponent(taskId)}/events/stream`;
  const es = new EventSource(url);
  // W2.3: transient errors must NOT end the stream — closing here silently
  // froze long-running tasks in the UI. EventSource reconnects natively and
  // replays Last-Event-ID (the server keys frames by event ts), so we only
  // give up after sustained failure with nothing received in between.
  let consecutiveErrors = 0;
  es.onmessage = (msg) => {
    consecutiveErrors = 0;
    try {
      const data = JSON.parse(msg.data);
      if (data.kind === "done") { es.close(); if (onDone) onDone(); return; }
      if (onEvent) onEvent(data);
    } catch { /* ignore malformed */ }
  };
  es.onerror = () => {
    consecutiveErrors += 1;
    if (consecutiveErrors >= 10) { es.close(); if (onDone) onDone(); }
    // else: let the native reconnect (with Last-Event-ID) do its job.
  };
  return es; // caller can es.close() to unsubscribe
}

// ── WebSocket ───────────────────────────────────────────────────────────────

export function connectWS(onMessage, { onOpen, onClose, makeSocket } = {}) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const host = import.meta.env.DEV ? "127.0.0.1:8420" : location.host;
  const url = `${proto}://${host}/ws`;
  const ws = (makeSocket || ((u) => new WebSocket(u)))(url);
  ws.onmessage = (e) => {
    try {
      onMessage(JSON.parse(e.data));
    } catch {
      /* ignore malformed frame */
    }
  };
  ws.onopen = () => { if (onOpen) onOpen(); };
  // Both routed to the same handler: a paired error+close from the same
  // socket is ONE disconnect, deduped by the reconnector (wsReconnect.js),
  // not here.
  ws.onclose = () => { if (onClose) onClose(); };
  ws.onerror = () => { if (onClose) onClose(); };
  return ws;
}

// A `connectWS` subscription filtered to the additive `task_event` frame
// kind (merge_started/merge_step_*/human_merged/merge_failed) for one task —
// rides the existing broadcast socket rather than opening a second
// connection. Matches on the full id first (the drawer holds the full task
// id); the prefix fallback covers a caller that only has the short id.
export function connectTaskProgress(taskId, onEvent) {
  return connectWS((msg) => {
    if (msg?.type !== "task_event") return;
    const msgId = msg.task_id || "";
    if (msgId !== taskId && !msgId.startsWith(String(taskId).slice(0, 8))) return;
    if (onEvent) onEvent(msg.event);
  });
}
