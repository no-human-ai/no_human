// PostHog session replay runs with `recordBody: true` / `recordHeaders: true`
// (telemetry.js — an explicit, documented operator choice). Historically this
// file only ever *excluded* a handful of onboarding paths and passed every
// other request through by identity — fail-OPEN: a brand new endpoint added
// to api.js (say, one that echoes back a repo's absolute filesystem path,
// like `/api/profiles` does) was captured into replay by default, with
// nobody having to opt it in. That is how repo names and absolute local
// paths ended up reachable from stored PostHog recordings.
//
// This file is now fail-CLOSED / default-deny: every `/api/*` response body
// is redacted unless it is named on `REPLAY_BODY_ALLOWLIST` below. Silence —
// an endpoint nobody has classified — means "redact", not "pass through".
//
// Three tiers, checked in order:
//   1. DROP (`REPLAY_EXCLUDED_PATHS`): the request is excluded from replay
//      capture entirely (posthog-js drops it when this function returns
//      `null`). Reserved for cases where even a redacted *request line* is
//      unsafe to keep — right now that's only the onboarding email address,
//      whose REQUEST body the server can't redact because it never chose
//      what the browser sent it. `/api/onboarding/status` and
//      `/api/onboarding/reset` ride along here as defence-in-depth: the
//      server already keeps their response bodies clean via
//      `_onboarding_public()`, but dropping them from replay costs nothing.
//      (`/api/onboarding/reset` is not currently called from the UI at all —
//      it is kept on this list purely as a standing guard in case that
//      changes; see replayScrub.test.mjs's drift test for why that does not
//      count against "every live endpoint must be classified".)
//   2. ALLOW (`REPLAY_BODY_ALLOWLIST`): the request is passed through to
//      replay completely unchanged — request/response body and headers
//      included. Reserved for endpoints whose response bodies were read
//      end-to-end (see `API_BODY_CLASSIFICATION`'s `why` for each) and
//      contain no filesystem paths, no repo/user-chosen names, no operator
//      text and nothing credential-shaped: `/api/version`, `/api/queue/health`.
//      Matching is exact-pathname only — never a prefix or substring check —
//      so `/api/version` does not accidentally cover some future
//      `/api/version/history`. `/api/worker/status` was originally on this
//      list but is NOT: its `watcher_error`/`worker_error`/`health_error`
//      fields embed raw exception text (`str(exc)`, `f"{type(exc).__name__}:
//      {exc}"`), and an exception raised while touching a repo's filesystem
//      path routinely puts that path inside the exception's own message —
//      see app.py's `worker_status()`.
//   3. REDACT (everything else, the default): the request LINE (method,
//      pathname with its query string stripped, status, timing) is kept —
//      losing that would make replay useless for debugging network activity
//      — but `requestBody`/`responseBody`/`requestHeaders`/`responseHeaders`
//      are replaced. The query string is stripped from `name`/`url` too:
//      several endpoints (`/api/repo?path=...`, `/api/search?q=...`,
//      `/api/fs/suggest?path=...`) carry the exact same filesystem paths or
//      operator text in the URL that Tier 3 exists to keep out of the body,
//      and the request line is explicitly NOT dropped, so it needs its own
//      scrub. Nothing is mutated in place — `data` is shallow-cloned first —
//      and this path never throws, so an endpoint nobody has thought about
//      yet still lands here safely by default.
//
// `API_BODY_CLASSIFICATION` documents *why* each of the ~82 `/api/*`
// endpoints `api.js` actually calls sits where it sits (drop / allow /
// redact). It is not consulted at runtime — `maskCapturedNetworkRequest`
// only ever needs the two literal arrays above — but replayScrub.test.mjs
// sweeps api.js's real source for every `/api/*` call site and asserts each
// one has an entry here, so a newly added endpoint fails CI with a clear
// "classify me" message instead of silently defaulting to captured-in-full
// the way the old fail-open version did.
//
// `maskCapturedNetworkRequestFn` is posthog-js's own sanctioned seam for
// dropping a request from replay entirely (returning `null` — distinct from
// the deprecated `maskNetworkRequestFn`, which only redacted fields within a
// captured request but could not exclude the request itself). Wired into
// telemetry.js's `client.init(...)` call exactly like deadClickFilter.js's
// `DEAD_CLICK_IGNORE_SELECTORS`/`deadClickBeforeSend` are wired into
// `capture_dead_clicks`/`before_send`: a pure, exported, unit-testable
// function handed to posthog-js as an init option. `.ph-no-capture`
// (used throughout Onboarding.jsx) is a separate, DOM-only mechanism — it
// stops rrweb from recording rendered *elements*, but has no effect on the
// network-capture plugin, which records request/response bodies regardless
// of what CSS class wraps the element that triggered the fetch. That is why
// this file exists as a second, independent mechanism.

export const REPLAY_EXCLUDED_PATHS = [
  "/api/onboarding/email", // request body carries the address (load-bearing: server can't redact an inbound request)
  "/api/onboarding/status", // response body: defence-in-depth, server already redacts via _onboarding_public
  "/api/onboarding/reset", // response body: defence-in-depth, server already redacts via _onboarding_public; not currently called from the UI
];

// Tier 2 — exact pathname match only. Every entry here has had its response
// body read end-to-end (see API_BODY_CLASSIFICATION) and contains nothing
// filesystem- or identity-shaped.
export const REPLAY_BODY_ALLOWLIST = [
  "/api/version", // {version, distName, published} — no paths, no names
  "/api/queue/health", // pure timestamps via core.health.queue_health
];

// One entry per normalized `/api/*` pathname that api.js actually calls
// (`:param` stands in for an interpolated path segment; query strings are
// not part of the key). `tier` is "drop" | "allow" | "redact"; `why` is a
// one-line, human-checkable justification. This is the source of truth the
// two arrays above are hand-kept consistent with — replayScrub.test.mjs
// checks that consistency, and separately sweeps api.js's real source so a
// newly added endpoint without an entry here fails loudly instead of
// silently defaulting to "captured in full".
export const API_BODY_CLASSIFICATION = {
  // --- Tier 1: dropped from replay entirely ---
  "/api/onboarding/email": { tier: "drop", why: "request body is the address the user just typed; no server-side redaction is possible for an inbound request" },
  "/api/onboarding/status": { tier: "drop", why: "defence-in-depth alongside server-side _onboarding_public() redaction" },
  "/api/onboarding/reset": { tier: "drop", why: "defence-in-depth alongside server-side _onboarding_public() redaction; not currently called from the UI" },

  // --- Tier 2: allowlisted, body passed through unchanged ---
  "/api/version": { tier: "allow", why: "response is {version, distName, published} only — verified against app.py, no paths or names" },
  "/api/queue/health": { tier: "allow", why: "response is queue timestamps only, via core.health.queue_health — verified against app.py" },

  // --- Tier 3: redacted. Filesystem paths / repo identity ---
  "/api/worker/status": { tier: "redact", why: "watcher_error/worker_error/health_error embed raw exception text (str(exc), f\"{type(exc).__name__}: {exc}\") which routinely contains absolute filesystem paths — see app.py worker_status()" },
  "/api/profiles": { tier: "redact", why: "returns name + absolute repo_path for every configured repo — the original leak this fix addresses" },
  "/api/repos": { tier: "redact", why: "repo listing carries repo paths/names" },
  "/api/repos/discover": { tier: "redact", why: "discovery results carry filesystem paths" },
  "/api/repos/scaffold": { tier: "redact", why: "scaffolds a repo at a filesystem path" },
  "/api/repo": { tier: "redact", why: "single-repo lookup; also carries the path in its query string (`?path=`), stripped separately from the request line" },
  "/api/fs/suggest": { tier: "redact", why: "filesystem path autocomplete; the query string itself (`?path=`) carries a path fragment" },
  "/api/projects": { tier: "redact", why: "projects are backed by repo paths" },
  "/api/projects/:param": { tier: "redact", why: "projects are backed by repo paths" },
  "/api/config": { tier: "redact", why: "full operator config echo, including repo-path-shaped fields" },
  "/api/tasks": { tier: "redact", why: "task bodies carry repo paths, titles, specs" },
  "/api/tasks/:param": { tier: "redact", why: "task detail carries repo path, title, spec" },
  "/api/tasks/:param/approve": { tier: "redact", why: "task-scoped; may echo task/repo context" },
  "/api/tasks/:param/attachments": { tier: "redact", why: "attachment bodies/paths" },
  "/api/tasks/:param/attempts/:param/details": { tier: "redact", why: "attempt detail carries repo/task context" },
  "/api/tasks/:param/cancel": { tier: "redact", why: "task-scoped; may echo task/repo context" },
  "/api/tasks/:param/diff": { tier: "redact", why: "diffs carry repo-relative and absolute file paths" },
  "/api/tasks/:param/events": { tier: "redact", why: "activity log entries carry file paths and operator text" },
  "/api/tasks/:param/events/stream": { tier: "redact", why: "same content as /events, streamed" },
  "/api/tasks/:param/finish-review": { tier: "redact", why: "task-scoped; may echo task/repo context" },
  "/api/tasks/:param/pause": { tier: "redact", why: "task-scoped; may echo task/repo context" },
  "/api/tasks/:param/post-review-comments": { tier: "redact", why: "review comment text plus repo/file context" },
  "/api/tasks/:param/reply": { tier: "redact", why: "operator reply text plus repo/task context" },
  "/api/tasks/:param/resume": { tier: "redact", why: "task-scoped; may echo task/repo context" },
  "/api/tasks/:param/retry": { tier: "redact", why: "task-scoped; may echo task/repo context" },
  "/api/tasks/:param/send-back": { tier: "redact", why: "operator text plus repo/task context" },
  "/api/tasks/:param/split": { tier: "redact", why: "new task titles/specs plus repo context" },
  "/api/tasks/:param/split-drafts": { tier: "redact", why: "draft titles/specs plus repo context" },
  "/api/tasks/:param/subtasks": { tier: "redact", why: "subtask titles/specs plus repo context" },
  "/api/onboarding/complete": { tier: "redact", why: "onboarding payload includes repo selections/paths" },
  "/api/onboarding/deferred": { tier: "redact", why: "deferred onboarding steps reference repo context" },
  "/api/onboarding/deferred/:param/done": { tier: "redact", why: "deferred onboarding steps reference repo context" },
  "/api/onboarding/docs/detect": { tier: "redact", why: "detects docs by filesystem path (`?repo=`)" },
  "/api/onboarding/docs/generate": { tier: "redact", why: "generates docs at filesystem paths" },
  "/api/onboarding/docs/jobs/:param": { tier: "redact", why: "job status references filesystem paths" },
  "/api/onboarding/history/analyze": { tier: "redact", why: "analyzes repo history at a filesystem path" },
  "/api/onboarding/history/extract": { tier: "redact", why: "extracts repo history/rules content" },
  "/api/onboarding/readiness": { tier: "redact", why: "repo readiness carries repo path/name context" },
  "/api/onboarding/repos/confirm": { tier: "redact", why: "confirms repo selection by path" },
  "/api/onboarding/repos/onboard": { tier: "redact", why: "onboards a repo at a filesystem path" },
  "/api/onboarding/repos/prove": { tier: "redact", why: "proves repo setup at a filesystem path" },
  "/api/onboarding/repos/ui-evidence": { tier: "redact", why: "captures onboarding UI evidence tied to a repo path" },
  "/api/onboarding/rules/confirm": { tier: "redact", why: "confirms repo rules content" },

  // --- Tier 3: redacted. User text / operator content ---
  "/api/search": { tier: "redact", why: "search query + results are operator text (also present in the `?q=` query string, stripped from the request line)" },
  "/api/rules": { tier: "redact", why: "operator-authored rule text" },
  "/api/rules/:param": { tier: "redact", why: "operator-authored rule text" },
  "/api/skills": { tier: "redact", why: "operator-authored skill text" },
  "/api/skills/:param": { tier: "redact", why: "operator-authored skill text" },
  "/api/learnings": { tier: "redact", why: "learning text derived from operator activity" },
  "/api/learnings/:param/confirm": { tier: "redact", why: "learning text derived from operator activity" },
  "/api/learnings/:param/delete": { tier: "redact", why: "learning text derived from operator activity" },
  "/api/learnings/:param/pause": { tier: "redact", why: "learning text derived from operator activity" },
  "/api/learnings/:param/reject": { tier: "redact", why: "learning text derived from operator activity" },
  "/api/learnings/:param/restore": { tier: "redact", why: "learning text derived from operator activity" },
  "/api/learnings/:param/retire": { tier: "redact", why: "learning text derived from operator activity" },
  "/api/learnings/retire-candidates": { tier: "redact", why: "learning text derived from operator activity" },
  "/api/memories/quarantine": { tier: "redact", why: "quarantined memory content is operator/model text" },
  "/api/grill": { tier: "redact", why: "chat-style operator content" },
  "/api/grill/stream": { tier: "redact", why: "chat-style operator content, streamed" },
  "/api/integrations": { tier: "redact", why: "integration config may carry operator-chosen identifiers" },
  "/api/integrations/:param/config": { tier: "redact", why: "integration config may carry operator-chosen identifiers" },
  "/api/integrations/:param/issues": { tier: "redact", why: "issue tracker content is operator text" },
  "/api/integrations/:param/issues/:param": { tier: "redact", why: "issue tracker content is operator text" },
  "/api/integrations/:param/setup": { tier: "redact", why: "setup payloads may carry operator-chosen identifiers" },
  "/api/integrations/:param/test": { tier: "redact", why: "test payloads may echo integration config" },
  "/api/integrations/setup": { tier: "redact", why: "setup payloads may carry operator-chosen identifiers" },
  "/api/models": { tier: "redact", why: "model listing may carry operator-configured endpoints/names" },
  "/api/config/models": { tier: "redact", why: "model config is operator-authored" },
  "/api/config/workers": { tier: "redact", why: "worker config is operator-authored" },
  "/api/config/coder-backend": { tier: "redact", why: "coder-backend config is operator-authored" },
  "/api/coder-backend": { tier: "redact", why: "coder-backend config is operator-authored" },
  "/api/metrics": { tier: "redact", why: "metrics payload may carry repo/task labels" },
  "/api/metrics/window": { tier: "redact", why: "metrics payload may carry repo/task labels" },

  // --- Tier 3: redacted. Credential-shaped ---
  "/api/auth/token": { tier: "redact", why: "credential-shaped" },
  "/api/auth/codex-key": { tier: "redact", why: "credential-shaped" },
  "/api/auth/codex-mode": { tier: "redact", why: "credential-shaped" },
  "/api/auth/status": { tier: "redact", why: "credential-shaped" },

  // --- Tier 3: redacted. Neither booleans-only nor otherwise verified safe ---
  "/api/telemetry/consent": { tier: "redact", why: "response body echoes the full telemetry config block (instance_id, posthog settings), not just the requested boolean toggle" },
};

const REDACTED = "[redacted: not on replay body allowlist]";

function stripQuery(value) {
  if (typeof value !== "string") return value;
  const i = value.indexOf("?");
  return i === -1 ? value : value.slice(0, i);
}

// Tier 3: shallow-clone `data`, keep the request line (method/name/url/
// status/timing) with any query string stripped from name/url, and replace
// body/header fields. Never mutates `data`.
function redact(data) {
  const out = { ...data };
  out.name = stripQuery(out.name);
  out.url = stripQuery(out.url);
  out.requestBody = REDACTED;
  out.responseBody = REDACTED;
  out.requestHeaders = {};
  out.responseHeaders = {};
  return out;
}

// Fails CLOSED: any error while inspecting/cloning `data` drops the request
// from replay rather than risk letting an unclassified or malformed request
// through unredacted because of a shape this function did not anticipate.
export function maskCapturedNetworkRequest(data) {
  if (data === undefined) return undefined;
  if (data === null) return null;
  try {
    const raw = String(data?.name ?? data?.url ?? "");
    let path = raw;
    let parsedOk = true;
    try {
      path = new URL(raw, "http://x").pathname;
    } catch {
      // Not a parseable URL. Deliberately do NOT fall through to the
      // allowlist below with a best-effort path — an endpoint we can't
      // reliably identify must never qualify for Tier 2's unchanged
      // pass-through. It can still hit Tier 1 (substring match, drop-only,
      // so over-matching is safe) and always lands in Tier 3 otherwise.
      parsedOk = false;
      path = raw.split("?")[0];
    }
    // Tier 1: drop entirely. Substring match is intentional here (over-broad
    // in the "drop more" direction is the safe direction for this tier).
    if (REPLAY_EXCLUDED_PATHS.some((p) => path.includes(p) || raw.includes(p))) {
      return null;
    }
    // Tier 2: exact-pathname allowlist, unchanged pass-through.
    if (parsedOk && REPLAY_BODY_ALLOWLIST.includes(path)) {
      return data;
    }
    // Tier 3 (default-deny): everything else.
    return redact(data);
  } catch {
    return null;
  }
}
