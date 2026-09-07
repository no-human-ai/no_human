// The update flow, with every dependency injected.
//
// electron-updater cannot be imported in a unit test (it reaches for
// `app.getVersion()`, `app-update.yml`, and the network at import time), so
// this module receives `autoUpdater` rather than importing it. That is also why
// the interesting behaviour is testable at all: the tests drive a fake
// autoUpdater and assert what this module DID — which handlers it registered,
// whether it called `downloadUpdate`, what it persisted — rather than
// re-deriving the answer from the same code.
//
// Two rules the shape encodes:
//
//  1. autoDownload is OFF. The operator asked that users "could download it
//     then or when they want", so a check must never move 145 MB on its own.
//  2. An unsigned build must FAIL LOUDLY. Squirrel.Mac refuses to install into
//     an unsigned bundle, and electron-updater surfaces that as a late, opaque
//     "Could not get code signature for running application". Checking still
//     works unsigned (it is a plain HTTPS fetch of latest-mac.yml), so this
//     module still tells the user an update exists — it just refuses the
//     install path up front, with a sentence that says why.

import {
  deferVersion, dueForCheck, isNewer, shouldNotify, updateErrorMessage,
} from "./updatePolicy.mjs";

/** Result modes returned by check(). */
export const AVAILABLE = "available";
export const UP_TO_DATE = "up-to-date";
export const UNAVAILABLE = "unavailable"; // an update exists, we cannot install it
export const SKIPPED = "skipped";         // deferred, not due, or not packaged
export const FAILED = "failed";

/**
 * @param autoUpdater  electron-updater's autoUpdater (or a double)
 * @param plan         signingPlan() result — decides canAutoUpdate
 * @param currentVersion  app.getVersion()
 * @param readState/writeState  persistence for {deferredVersion, lastCheckAt}
 * @param onEvent      called with every user-visible state change
 * @param isPackaged   electron-updater is inert in an unpackaged app
 */
export function createUpdater({
  autoUpdater,
  plan,
  currentVersion,
  readState = () => ({}),
  writeState = () => {},
  onEvent = () => {},
  log = () => {},
  now = () => Date.now(),
  isPackaged = true,
} = {}) {
  let pending = null;      // the UpdateInfo we last told the user about
  let downloaded = false;

  function emit(payload) {
    try {
      onEvent(payload);
    } catch (err) {
      // A broken listener must never take down the updater.
      log(`update listener failed: ${err?.message ?? err}`);
    }
  }

  function configure() {
    // Both OFF. autoInstallOnAppQuit defaults TRUE in electron-updater, which
    // would install on quit an update the user explicitly deferred — the exact
    // "downloads without being asked" behaviour the operator ruled out.
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = false;

    // Force a FULL download, never a blockmap delta. On macOS the differential
    // downloader fetches the new build's .blockmap and diffs it against the
    // running app; when that computation stalls, electron-updater sits at
    // "downloading 0%" and never fires download-progress or falls back — the
    // exact "stuck at 0%, never installs" the operator hit updating to 0.1.7.
    // The app zip is ~140 MB; a full download is a few seconds and strictly
    // more reliable than the delta, so we opt out of differential entirely.
    autoUpdater.disableDifferentialDownload = true;

    autoUpdater.on?.("error", (err) => {
      const raw = String(err?.message ?? err);
      log(`updater error: ${raw}`);
      emit({ mode: FAILED, error: updateErrorMessage(err), rawError: raw });
    });
    autoUpdater.on?.("download-progress", (p) => {
      emit({ mode: "downloading", percent: Math.round(p?.percent ?? 0) });
    });
    autoUpdater.on?.("update-downloaded", (info) => {
      downloaded = true;
      emit({ mode: "downloaded", latest: info?.version ?? pending?.version });
    });
    return autoUpdater;
  }

  /**
   * @param manual  true when the user clicked "Check for Updates…" — an
   *                explicit request bypasses both the once-a-day throttle and
   *                a previous "Later", and always reports an answer.
   */
  async function check({ manual = false } = {}) {
    if (!isPackaged) {
      // In `npm run desktop` there is no app-update.yml; electron-updater
      // throws. Say so plainly instead of surfacing that as a real error.
      const r = { mode: SKIPPED, reason: "not-packaged", current: currentVersion };
      if (manual) emit(r);
      return r;
    }

    const state = readState() || {};
    if (!manual && !dueForCheck(state.lastCheckAt, now())) {
      return { mode: SKIPPED, reason: "not-due", current: currentVersion };
    }

    let info;
    try {
      const res = await autoUpdater.checkForUpdates();
      info = res?.updateInfo ?? null;
    } catch (err) {
      const raw = String(err?.message ?? err);
      log(`update check failed: ${raw}`);
      const r = { mode: FAILED, error: updateErrorMessage(err), rawError: raw,
                  current: currentVersion };
      if (manual) emit(r);
      return r;   // never blocks, never throws — a check is not a feature gate
    }

    // Record the attempt even when nothing is new, or a machine that is always
    // up to date re-checks on every single launch.
    writeState({ ...state, lastCheckAt: now() });

    const latest = info?.version ?? null;
    if (!isNewer(latest, currentVersion)) {
      const r = { mode: UP_TO_DATE, current: currentVersion, latest };
      if (manual) emit(r);
      return r;
    }

    pending = info;
    const decision = shouldNotify({
      latest, current: currentVersion,
      deferredVersion: state.deferredVersion, manual,
    });
    if (!decision.notify) {
      return { mode: SKIPPED, reason: decision.reason,
               current: currentVersion, latest };
    }

    // An update exists. Whether we may INSTALL it is a separate question, and
    // the answer is visible to the user rather than discovered at click time.
    const r = {
      mode: plan?.canAutoUpdate ? AVAILABLE : UNAVAILABLE,
      current: currentVersion,
      latest,
      canAutoUpdate: Boolean(plan?.canAutoUpdate),
      releaseNotes: info?.releaseNotes ?? null,
      signingMode: plan?.mode ?? "unknown",
    };
    emit(r);
    return r;
  }

  /** The user chose "Download now". */
  async function download() {
    if (!plan?.canAutoUpdate) {
      // Loud and legible, at the moment of the click, naming the cause.
      const error = `This build is ${plan?.mode ?? "unsigned"} and cannot`
        + " install updates: macOS (Squirrel.Mac) requires a code-signed"
        + " application. Download the new version manually.";
      log(error);
      const r = { mode: UNAVAILABLE, error, canAutoUpdate: false };
      emit(r);
      return r;
    }
    if (!pending) return { mode: FAILED, error: "no update has been found yet" };
    try {
      await autoUpdater.downloadUpdate();
      return { mode: "downloading", latest: pending.version };
    } catch (err) {
      const raw = String(err?.message ?? err);
      const r = { mode: FAILED, error: updateErrorMessage(err), rawError: raw };
      emit(r);
      return r;
    }
  }

  /** Restart into the downloaded update. Only legal once it is on disk. */
  function install() {
    if (!downloaded) return { mode: FAILED, error: "no update has been downloaded" };
    autoUpdater.quitAndInstall();
    return { mode: "installing" };
  }

  /** The user chose "Later" — persist it so this version stops asking. */
  function defer(version = pending?.version) {
    if (!version) return { mode: FAILED, error: "nothing to defer" };
    writeState(deferVersion(readState() || {}, version, now()));
    return { mode: SKIPPED, reason: "deferred", latest: version };
  }

  return { configure, check, download, install, defer,
           get pending() { return pending; },
           get downloaded() { return downloaded; } };
}
