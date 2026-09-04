// Incident 2026-09-04: the operator walked the onboarding wizard while the
// backend process died mid-flow. The SPA kept rendering, and every data step
// (repo path completion, docs chips, the integrations list) degraded to its
// own raw `fetch` rejection — a red "Failed to fetch" string with no
// explanation and no way back. Every one of those endpoints verifies 200
// against a healthy server; the defect is purely the wizard's behaviour when
// `fetch` rejects at the network level.
//
// This module is the fix's whole logic surface, kept dependency- and
// timer-global-free (like `wsReconnect.js`) so `node --test` can drive the
// reconnect loop with a fake clock. `Onboarding.jsx` wires it to the real
// `probeServer` from `api.js`.

export const PROBE_INTERVAL_MS = 3000;

const NETWORK_ERROR_RE = /failed to fetch|load failed|networkerror when attempting to fetch/i;

// True only for a network-level failure — the browser could not even reach
// the server (process dead, port closed, DNS/TLS refused). `e?.name` is
// checked rather than `instanceof TypeError` because a `fetch` rejection can
// cross a realm (iframe/worker) and still be "a TypeError" in every way that
// matters here. Anything `api.js` throws for an HTTP status is a plain
// `Error` with no such name/message, so it never matches. A user-initiated
// abort surfaces as `DOMException`/`AbortError`, not this — navigating away
// mid-fetch is not an outage.
export function isNetworkError(e) {
  if (!e || e.name !== "TypeError") return false;
  return NETWORK_ERROR_RE.test(String(e.message || ""));
}

// Pure view-model for the wizard-level reconnect banner. `state` is
// `{ offline, probing }`. `role: "status"`, never `alert(` — this is
// persistent-but-calm, the same idiom as `connectionBanner.js`.
export function offlineBanner(state) {
  if (!state || !state.offline) return null;
  return {
    text: "The no_human server is not responding",
    hint: state.probing ? "Checking…" : "Retrying every 3 seconds…",
    retryLabel: "Retry",
    className: "ob-offline-banner",
    role: "status",
  };
}

/**
 * Reconnect controller. `deps.probe()` resolves when the server answers
 * (any HTTP response, even a 4xx/5xx, counts as "alive") and rejects only
 * when `fetch` itself rejects. `deps.onReconnect()` fires exactly once, the
 * moment the probe first succeeds, so the caller can re-run the current
 * step's fetches. `deps.onStatus("probing" | "waiting")` fires on every
 * transition so the banner can say "Checking…" vs "Retrying…".
 *
 * Per the resolved incident triage: a fixed 3s interval (no escalation —
 * the endpoint is cheap and a flat cadence is simplest to reason about), and
 * retries never give up on their own — only `stop()` (unmount / reconnected)
 * ends them.
 */
export function createServerProbe({
  probe,
  onReconnect,
  onStatus,
  setTimeout: scheduleTimeout = globalThis.setTimeout,
  clearTimeout: cancelTimeout = globalThis.clearTimeout,
  intervalMs = PROBE_INTERVAL_MS,
}) {
  let timer = null;
  let inFlight = false;
  let generation = 0;
  let stopped = true;

  function setStatus(s) {
    if (onStatus) onStatus(s);
  }

  function schedule() {
    if (timer) cancelTimeout(timer);
    timer = scheduleTimeout(() => runProbe(generation), intervalMs);
  }

  function runProbe(gen) {
    if (stopped || gen !== generation || inFlight) return;
    timer = null;
    inFlight = true;
    setStatus("probing");
    probe().then(
      () => {
        inFlight = false;
        if (stopped || gen !== generation) return; // dropped by stop()/a later generation
        setStatus("online");
        if (onReconnect) onReconnect();
      },
      () => {
        inFlight = false;
        if (stopped || gen !== generation) return;
        setStatus("waiting");
        schedule();
      },
    );
  }

  return {
    // The server just died — an instant probe is noise, so the first attempt
    // waits one full interval like every retry after it.
    start() {
      if (!stopped) return;
      stopped = false;
      generation += 1;
      setStatus("waiting");
      schedule();
    },
    // Single-flight: a probe already in the air makes this a no-op instead
    // of stacking a second request behind it.
    retryNow() {
      if (stopped || inFlight) return;
      if (timer) {
        cancelTimeout(timer);
        timer = null;
      }
      runProbe(generation);
    },
    stop() {
      if (stopped) return;
      stopped = true;
      generation += 1; // drops a late-resolving in-flight probe
      if (timer) {
        cancelTimeout(timer);
        timer = null;
      }
    },
  };
}
