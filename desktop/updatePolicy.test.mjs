// "Later" has to mean later, not "in ten seconds".
//
// The operator's requirement is that a user who declines an update is not
// re-prompted every launch, but IS told about the next one. That is a single
// predicate with two failure directions — nagging, and silently swallowing a
// newer release — and both are asserted here.
import assert from "node:assert/strict";
import test from "node:test";
import {
  compareVersions, deferVersion, dueForCheck, isNewer, shouldNotify, updateMessage,
  updateErrorMessage, retainedUpdate,
} from "./updatePolicy.mjs";

test("version comparison orders releases, including uneven segment counts", () => {
  assert.equal(compareVersions("0.2.0", "0.1.0"), 1);
  assert.equal(compareVersions("0.1.0", "0.2.0"), -1);
  assert.equal(compareVersions("0.1.0", "0.1.0"), 0);
  // "0.10.0" > "0.9.0" is the classic string-compare bug; assert the number.
  assert.equal(compareVersions("0.10.0", "0.9.0"), 1,
    "0.10.0 must outrank 0.9.0 — string comparison gets this backwards");
  assert.equal(compareVersions("1.0", "1.0.0"), 0, "missing segments are zero");
  assert.equal(compareVersions("v0.2.0", "0.1.0"), 1, "a leading v is tolerated");
});

test("isNewer refuses to act on anything it cannot parse", () => {
  // A feed that returns garbage (an HTML error page, an empty string) must not
  // be announced as an upgrade.
  assert.equal(isNewer(null, "0.1.0"), false);
  assert.equal(isNewer("", "0.1.0"), false);
  assert.equal(isNewer("latest", "0.1.0"), false);
  assert.equal(isNewer("<!doctype html>", "0.1.0"), false);
  assert.equal(isNewer("0.2.0", "0.1.0"), true);
  assert.equal(isNewer("0.1.0", "0.1.0"), false, "equal is not newer");
  assert.equal(isNewer("0.0.9", "0.1.0"), false, "a downgrade is not an update");
});

test("an available update notifies once and then stays quiet for that version", () => {
  const current = "0.1.0";
  const first = shouldNotify({ latest: "0.2.0", current, deferredVersion: null });
  assert.equal(first.notify, true, "the first sighting must inform the user");

  // The user clicks "Later". That is the ONLY thing that changes.
  const state = deferVersion({}, "0.2.0", 1000);
  assert.equal(state.deferredVersion, "0.2.0");

  const again = shouldNotify({ latest: "0.2.0", current,
                               deferredVersion: state.deferredVersion });
  assert.equal(again.notify, false, "a deferred version must not re-prompt");
  assert.equal(again.reason, "deferred");
});

test("deferring 0.2.0 does not silence 0.3.0", () => {
  // The failure this catches: keying the deferral on "an update exists" rather
  // than on WHICH update, which mutes the product permanently.
  const d = shouldNotify({ latest: "0.3.0", current: "0.1.0",
                           deferredVersion: "0.2.0" });
  assert.equal(d.notify, true, "a newer release must get through a deferral");
});

test("an explicit Check for Updates always answers, deferral or not", () => {
  const deferred = shouldNotify({ latest: "0.2.0", current: "0.1.0",
                                  deferredVersion: "0.2.0", manual: true });
  assert.equal(deferred.notify, true,
    "the menu item must not look broken because of an earlier Later");

  const uptodate = shouldNotify({ latest: "0.1.0", current: "0.1.0", manual: true });
  assert.equal(uptodate.notify, false);
  assert.equal(uptodate.reason, "up-to-date",
    "a manual check must report being current, not stay silent");
});

test("deferVersion does not mutate the state it was handed", () => {
  // The caller persists the RETURN value; mutating in place would silence a
  // version even when the disk write failed.
  const before = { lastCheckAt: 5 };
  const after = deferVersion(before, "0.2.0", 99);
  assert.equal(before.deferredVersion, undefined, "the input must be untouched");
  assert.equal(after.deferredVersion, "0.2.0");
  assert.equal(after.lastCheckAt, 5, "existing state must survive");
});

test("the daily throttle allows the first check and blocks a second same-day one", () => {
  const day = 86_400_000;
  assert.equal(dueForCheck(null, 1_000_000), true, "never checked = due");
  assert.equal(dueForCheck(undefined, 1_000_000), true);
  assert.equal(dueForCheck("nonsense", 1_000_000), true, "unreadable state = due");
  assert.equal(dueForCheck(1000, 1000 + day - 1), false, "under a day is not due");
  assert.equal(dueForCheck(1000, 1000 + day), true, "exactly a day is due");
});

test("the unsigned message states the cause instead of just failing", () => {
  const msg = updateMessage({ mode: "unavailable", latest: "0.2.0",
                              current: "0.1.0", canAutoUpdate: false });
  assert.match(msg, /0\.2\.0/);
  assert.match(msg, /not code-signed/i,
    "an unsigned build must say WHY it cannot update itself");
  assert.match(msg, /manually/i, "and must offer the remaining route");

  assert.match(updateMessage({ mode: "up-to-date", current: "0.1.0" }), /up to date/);
  assert.match(updateMessage({ mode: "available", latest: "0.2.0",
                               current: "0.1.0", canAutoUpdate: true }), /0\.2\.0/);
});

test("a raw electron-updater HttpError never reaches the user", () => {
  // A realistic dump of what electron-updater's HttpError.message actually
  // contains: the URL, the status, the full response headers, and a
  // node/electron stack trace — exactly the artefact this bug leaked to the
  // Settings card.
  const raw = "Cannot find latest.yml in the latest release artifacts "
    + "(https://github.com/no-human-ai/no_human/releases/download/v0.2.2/latest.yml): "
    + "HttpError: 404\n"
    + 'Headers: {"cache-control":"no-cache","content-security-policy":"default-src '
    + '\'none\'","x-github-request-id":"ABCD:1234:56789:ABCDEF:0123456"}\n'
    + "    at createHttpError (/Applications/no_human.app/Contents/Resources/app.asar"
    + "/node_modules/electron-updater/out/util/httpExecutor.js:52:12)\n"
    + "    at node:electron/js2c/browser_init:2:12345";

  const msg = updateErrorMessage(raw);
  assert.match(msg, /Release update information is unavailable/);

  for (const forbidden of [
    "x-github-request-id", "content-security-policy", "browser_init",
    "httpExecutor", "HttpError", "latest.yml",
  ]) {
    assert.doesNotMatch(msg, new RegExp(forbidden.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
      `the primary message must not contain "${forbidden}"`);
  }
});

test("network failures ask the user to check their connection", () => {
  for (const raw of [
    "ENOTFOUND", "ECONNREFUSED", "ENETUNREACH",
    "getaddrinfo ENOTFOUND github.com",
    new Error("connect ECONNREFUSED 127.0.0.1:443"),
  ]) {
    assert.match(updateErrorMessage(raw), /Check your internet connection/,
      `expected an offline sentence for ${raw}`);
  }
});

test("Electron's own net:: errors are offline, not a server problem", () => {
  // The packaged app's electron-updater (6.8.9) fetches through
  // ElectronHttpExecutor (electron/net), whose errors carry NO `.code` and
  // none of the node http/dns strings above — an unresolvable host, a
  // refused port, and no connectivity at all come back as these exact
  // Chromium strings. Measured against the bundled Electron binary; a
  // regression here tells an offline user that GitHub is down.
  for (const raw of [
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_INTERNET_DISCONNECTED",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_TIMED_OUT",
    "net::ERR_NETWORK_CHANGED",
    "net::ERR_ADDRESS_UNREACHABLE",
  ]) {
    assert.match(updateErrorMessage(raw), /Check your internet connection/,
      `expected an offline sentence for the Electron net error "${raw}"`);
  }
});

test("other failures fall back to the try-again-later sentence", () => {
  for (const raw of [
    "HttpError: 503", "ETIMEDOUT", "EACCES", "", null, undefined, {},
  ]) {
    assert.doesNotThrow(() => updateErrorMessage(raw));
    assert.match(updateErrorMessage(raw), /The update check failed; try again later/,
      `expected the conservative fallback for ${JSON.stringify(raw)}`);
  }
});

// retainedUpdate() — the retention half of "automatic failures are quiet".
// updater.mjs's unconditional autoUpdater.on("error") emits {mode:"failed"}
// for the AUTOMATIC startup check too, so this is the ONE place that decides
// what a late-mounting renderer inherits.
test("a failed check never displaces a retained version fact", () => {
  const available = { mode: "available", latest: "0.3.0" };
  const failed = { mode: "failed", error: "x", rawError: "y" };
  assert.deepEqual(retainedUpdate(available, failed), available,
    "an automatic failure must leave the previously-known fact alone");
});

test("a failed check from nothing retained stays nothing retained", () => {
  const failed = { mode: "failed", error: "x", rawError: "y" };
  assert.equal(retainedUpdate(null, failed), null,
    "a failed AUTOMATIC startup check must never become the board's first notice");
});

test("a failed check never displaces a retained up-to-date", () => {
  const uptodate = { mode: "up-to-date", current: "0.1.0", latest: "0.1.0" };
  const failed = { mode: "failed", error: "x" };
  assert.deepEqual(retainedUpdate(uptodate, failed), uptodate);
});

test("a persisted deferral clears the retained notice", () => {
  const available = { mode: "available", latest: "0.3.0" };
  const skipped = { mode: "skipped", reason: "deferred", latest: "0.3.0" };
  assert.equal(retainedUpdate(available, skipped), null,
    "Settings' Later must clear whatever version fact was retained");
});

test("version facts themselves ARE retained", () => {
  for (const mode of ["available", "unavailable", "up-to-date"]) {
    const event = { mode, latest: "0.3.0" };
    assert.deepEqual(retainedUpdate(null, event), event);
    assert.deepEqual(retainedUpdate({ mode: "up-to-date" }, event), event);
  }
});
