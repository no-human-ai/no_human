// When may the app interrupt the operator about an update?
//
// The operator's requirement, verbatim: users "will be informed when there's an
// update and they could download it then or when they want". The second half is
// the hard part. A notification that reappears every launch is not a choice, it
// is nagging — so "later" has to PERSIST, and persist against the right key.
//
// The key is the VERSION, not a timestamp. Deferring 0.2.0 must stay deferred
// forever, but 0.3.0 must still get through — a time-based snooze gets this
// backwards, re-nagging about the version they rejected while a genuinely new
// one waits behind the same timer.
//
// Pure and dependency-free so the decision is testable without electron,
// electron-updater, a network, or a clock.

/**
 * Compare dotted numeric versions. Returns -1 | 0 | 1.
 *
 * Deliberately NOT semver-complete: electron-builder versions are `x.y.z`, and
 * a half-correct prerelease implementation is worse than none. A version
 * carrying a prerelease/build suffix (`1.2.3-beta.1`) compares by its numeric
 * core, and `isNewer` refuses to act on anything it cannot parse — an
 * unparseable version must never be announced as an upgrade.
 */
export function compareVersions(a, b) {
  const parse = (v) => String(v ?? "").trim().replace(/^v/, "")
    .split(/[-+]/)[0].split(".").map((n) => Number.parseInt(n, 10));
  const pa = parse(a);
  const pb = parse(b);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i += 1) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return 0;
    if (x > y) return 1;
    if (x < y) return -1;
  }
  return 0;
}

/** True only when `latest` is a parseable version strictly above `current`. */
export function isNewer(latest, current) {
  if (!/^v?\d+(\.\d+)*/.test(String(latest ?? "").trim())) return false;
  if (!/^v?\d+(\.\d+)*/.test(String(current ?? "").trim())) return false;
  return compareVersions(latest, current) > 0;
}

/**
 * Should the app raise an unprompted update notification?
 *
 * `manual` is the "Check for Updates…" menu path: an explicit request must
 * always get an answer, including "you are up to date" and including a version
 * the user previously deferred — otherwise the menu item looks broken.
 */
export function shouldNotify({ latest, current, deferredVersion, manual = false } = {}) {
  if (!isNewer(latest, current)) {
    return { notify: false, reason: manual ? "up-to-date" : "no-update" };
  }
  if (!manual && deferredVersion && compareVersions(deferredVersion, latest) === 0) {
    // The whole point of "later". Not a timer — this version is settled.
    return { notify: false, reason: "deferred" };
  }
  return { notify: true, reason: manual ? "manual" : "update-available" };
}

/**
 * The next persisted state after the user picks "Later".
 *
 * Returns a NEW object rather than mutating: the caller writes it to disk, and
 * a partially-mutated state that failed to persist would silence a version the
 * user was never actually asked about.
 */
export function deferVersion(state, version, now = Date.now()) {
  return { ...(state || {}), deferredVersion: version, deferredAt: now };
}

/**
 * Whether enough time has passed to check again. Checking on every launch is
 * free for us and rude to the network; once a day matches the CLI half.
 */
export function dueForCheck(lastCheckAt, now = Date.now(), intervalMs = 86_400_000) {
  if (!lastCheckAt || !Number.isFinite(Number(lastCheckAt))) return true;
  return now - Number(lastCheckAt) >= intervalMs;
}

/**
 * The user-visible sentence for an update state. Kept here, with the policy, so
 * the wording is tested rather than buried in a template literal in main.mjs.
 */
export function updateMessage({ mode, latest, current, canAutoUpdate }) {
  if (mode === "unavailable") {
    return `no_human ${latest} is available (you have ${current}), but this`
      + " build is not code-signed, so it cannot update itself. Download the"
      + " new version manually.";
  }
  if (mode === "up-to-date") return `no_human ${current} is up to date.`;
  if (mode === "available") {
    return `no_human ${latest} is available — you have ${current}.`
      + (canAutoUpdate ? "" : " This build cannot install it automatically.");
  }
  return "";
}

/**
 * Modes that state a FACT about versions, and so are worth handing to a
 * renderer that mounts after the event already fired (a late Settings open,
 * or the board's own late-mount pull below).
 */
export const RETAINED_UPDATE_MODES = new Set(["available", "unavailable", "up-to-date"]);

/**
 * What a late-mounting subscriber should be told: the last version FACT, or
 * null if none has landed yet. `updater.mjs` registers an unconditional
 * `autoUpdater.on("error")` that emits `{mode:"failed"}` for the AUTOMATIC
 * startup check too (its own `check()` only emits FAILED when `manual`) — so
 * retaining a failure here would resurrect a red "Could not check for
 * updates" card on every later mount, for a check the user never asked for.
 * A failure is therefore never retained: it leaves whatever fact (or absence
 * of one) was already on record. Live delivery of the event is untouched —
 * this only gates what gets REMEMBERED for someone who was not listening.
 *
 * A persisted "Later" (`{mode:"skipped", reason:"deferred"}`) clears the
 * record outright: the operator dismissed the notice, so a late mount must
 * not resurrect it either.
 */
export function retainedUpdate(prev, event) {
  if (event?.mode === "skipped" && event?.reason === "deferred") return null;
  return RETAINED_UPDATE_MODES.has(event?.mode) ? event : (prev ?? null);
}

// electron-updater's HttpError.message embeds the response headers and a
// node/electron stack trace verbatim — Cannot find latest.yml in the latest
// release artifacts (...): HttpError: 404, followed by cache-control,
// content-security-policy, x-github-request-id, and a trace through
// httpExecutor.js / node:electron/js2c/browser_init. None of that is
// actionable by a user, so the raw text is classified here, in ONE place,
// into a short sentence for the failure CLASS — never interpolated into it.
//
// Each sentence is exactly ONE claim: it names the failure class and nothing
// the app has not established. "no-metadata" in particular must not promise a
// new version (check() fails before isNewer() ever runs, so none is known)
// or point at an action the failed card does not offer (its only action is
// "check" — see web/src/updateNotice.js's `failed` branch).
export const UPDATE_ERROR_MESSAGES = {
  "no-metadata": "Release update information is unavailable for this platform right now.",
  offline: "Check your internet connection and try again.",
  server: "The update check failed; try again later.",
};

/**
 * Classify a raw electron-updater error into a failure category and its
 * short user-facing sentence. `raw` may be an Error, a string, or anything
 * else — it is always coerced to text before matching, and an unrecognised
 * shape falls back to the conservative "server" category rather than risk
 * mis-classifying it as transient/harmless.
 */
export function classifyUpdateError(raw) {
  const text = String(raw?.message ?? raw ?? "");
  const statusCode = raw?.statusCode;

  if (
    /cannot find .*\.yml/i.test(text)
    || /latest(-mac|-linux)?\.yml/i.test(text)
    || /HttpError:\s*404\b/.test(text)
    || statusCode === 404
  ) {
    return { category: "no-metadata", message: UPDATE_ERROR_MESSAGES["no-metadata"] };
  }
  // Node's http/dns codes cover a check run under plain Node (tests, and any
  // non-packaged path); the packaged app's electron-updater 6.8.9 uses
  // ElectronHttpExecutor, which goes through electron/net and surfaces
  // Chromium's `net::ERR_*` family instead — no `.code` at all, just this
  // string. Missing this family means a genuinely offline user reads "the
  // server is down" instead of "check your connection".
  if (
    /\b(ENOTFOUND|ECONNREFUSED|ENETUNREACH|EAI_AGAIN|ENETDOWN|getaddrinfo)\b/.test(text)
    || /net::ERR_(NAME_NOT_RESOLVED|INTERNET_DISCONNECTED|CONNECTION_REFUSED|CONNECTION_RESET|CONNECTION_TIMED_OUT|NETWORK_CHANGED|ADDRESS_UNREACHABLE)\b/.test(text)
  ) {
    return { category: "offline", message: UPDATE_ERROR_MESSAGES.offline };
  }
  return { category: "server", message: UPDATE_ERROR_MESSAGES.server };
}

/** The short, actionable sentence for a raw electron-updater error. */
export function updateErrorMessage(raw) {
  return classifyUpdateError(raw).message;
}
