// PostHog session replay runs with `recordBody: true` / `recordHeaders: true`
// (telemetry.js — an explicit, documented operator choice for every OTHER
// request the app makes). `.ph-no-capture` (used throughout Onboarding.jsx)
// only ever protects the DOM: it stops rrweb from recording the *rendered
// elements* it's applied to, but it has no effect on the network-capture
// plugin, which records the request/response bodies of every `fetch`/XHR the
// page makes regardless of what DOM class wraps the element that triggered
// it. The onboarding email step's `POST /api/onboarding/email` body IS the
// address the user just typed, and `GET /api/onboarding/status`'s response
// echoes the same address back (defence-in-depth is applied server-side too,
// in app.py, but that is a second, independent layer — not a reason to skip
// this one). Both must be kept out of replay body capture by name.
//
// `maskCapturedNetworkRequestFn` is posthog-js's own sanctioned seam for
// this: returning `null` from it excludes that ONE request from replay
// capture entirely (distinct from the deprecated `maskNetworkRequestFn`,
// which only redacted fields within a captured request). Wired into
// telemetry.js's `client.init(...)` call exactly like
// `deadClickFilter.js`'s `DEAD_CLICK_IGNORE_SELECTORS`/`deadClickBeforeSend`
// are wired into `capture_dead_clicks`/`before_send` — a pure, exported,
// unit-testable predicate handed to posthog-js as an init option.
//
// Which layer is load-bearing differs per path. `/api/onboarding/email`'s
// REQUEST carries the address the user just typed — the server has no way to
// avoid receiving it verbatim, so THIS list is the only thing standing
// between that request body and a recording; there is no server-side
// redaction possible for an outgoing request. `/api/onboarding/status`,
// `/api/onboarding/complete`, `/api/onboarding/reset` and `/api/config`, by
// contrast, are covered first by `app.py`'s `_onboarding_public()` — the
// server never puts `email`/`email_at`/`welcome_status` in those response
// bodies at all, for any caller, replay or not — so their entries here are
// defence-in-depth, not the load-bearing layer.

export const REPLAY_EXCLUDED_PATHS = [
  "/api/onboarding/email", // request body carries the address (load-bearing: server can't redact an inbound request)
  "/api/onboarding/status", // response body: defence-in-depth, server already redacts via _onboarding_public
  "/api/onboarding/reset", // response body: defence-in-depth, server already redacts via _onboarding_public
];

// Fails CLOSED: any error while inspecting `data` drops the request from
// replay rather than risk letting an address-carrying request through
// unexcluded because of a shape this function did not anticipate.
export function maskCapturedNetworkRequest(data) {
  try {
    const raw = String(data?.name ?? data?.url ?? "");
    if (!raw) return data;
    let path = raw;
    try {
      path = new URL(raw, "http://x").pathname;
    } catch {
      // Not a parseable URL (e.g. an already-relative path with a query
      // string posthog didn't normalise) — fall through to a substring
      // match against the raw string below.
    }
    if (REPLAY_EXCLUDED_PATHS.some((p) => path.includes(p) || raw.includes(p))) {
      return null;
    }
    return data;
  } catch {
    return null;
  }
}
