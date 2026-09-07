// What the updater DID, observed through a fake autoUpdater.
//
// These deliberately do not re-derive expected values from updatePolicy — they
// assert the side effects a user would feel: whether 145 MB moved without being
// asked for, whether "Later" reached the disk, whether an unsigned build was
// told the truth at click time instead of at install time.
import assert from "node:assert/strict";
import test from "node:test";
import { AVAILABLE, FAILED, SKIPPED, UNAVAILABLE, UP_TO_DATE, createUpdater }
  from "./updater.mjs";
import { SIGNED, UNSIGNED } from "./signing.cjs";

/** Records every interaction, so a missing call is visible as an absence. */
function fakeAutoUpdater({ version = "0.2.0", throws = null } = {}) {
  const au = {
    autoDownload: true,              // electron-updater's real defaults, so a
    autoInstallOnAppQuit: true,      // failure to override them is detectable
    disableDifferentialDownload: false, // default off; configure() must turn it ON
    listeners: new Map(),
    downloadCalls: 0,
    quitAndInstallCalls: 0,
    checkCalls: 0,
    on(evt, fn) { au.listeners.set(evt, fn); },
    async checkForUpdates() {
      au.checkCalls += 1;
      if (throws) throw new Error(throws);
      return { updateInfo: { version, releaseNotes: "notes" } };
    },
    async downloadUpdate() { au.downloadCalls += 1; },
    quitAndInstall() { au.quitAndInstallCalls += 1; },
  };
  return au;
}

const T0 = 1_000_000_000;
const DAY = 86_400_000;

function harness({ plan, version = "0.2.0", state = {}, throws = null,
                   currentVersion = "0.1.0", isPackaged = true, now = T0 } = {}) {
  const disk = { ...state };
  const events = [];
  const au = fakeAutoUpdater({ version, throws });
  const up = createUpdater({
    autoUpdater: au, plan, currentVersion, isPackaged,
    readState: () => ({ ...disk }),
    writeState: (s) => { Object.assign(disk, s); },
    onEvent: (e) => events.push(e),
    now: () => now,
  });
  return { up, au, disk, events };
}

const SIGNED_PLAN = { mode: SIGNED, canAutoUpdate: true };
const UNSIGNED_PLAN = { mode: UNSIGNED, canAutoUpdate: false };

test("configure turns OFF both of electron-updater's automatic behaviours", () => {
  // autoDownload true would move 145 MB unasked; autoInstallOnAppQuit true
  // would install on quit the very update the user chose to defer. Both
  // default to the wrong value, so this asserts the override actually lands.
  const { up, au } = harness({ plan: SIGNED_PLAN });
  up.configure();
  assert.equal(au.autoDownload, false, "a check must never download by itself");
  assert.equal(au.autoInstallOnAppQuit, false,
    "a deferred update must not install itself on quit");
  // Differential (blockmap-delta) download stalls at 0% on macOS; configure()
  // must force a full download so an update actually completes.
  assert.equal(au.disableDifferentialDownload, true,
    "the macOS blockmap-delta path must be disabled so downloads don't stall at 0%");
});

test("a check with an update available notifies but downloads NOTHING", () => {
  return (async () => {
    const { up, au, events } = harness({ plan: SIGNED_PLAN });
    up.configure();
    const r = await up.check();
    assert.equal(r.mode, AVAILABLE);
    assert.equal(r.latest, "0.2.0");
    assert.equal(au.downloadCalls, 0,
      "finding an update must not start a download — the user chooses");
    assert.equal(events.length, 1, "the user must be informed exactly once");
    assert.equal(events[0].mode, AVAILABLE);
  })();
});

test("download only moves bytes after an explicit call", async () => {
  const { up, au } = harness({ plan: SIGNED_PLAN });
  up.configure();
  await up.check();
  assert.equal(au.downloadCalls, 0);
  await up.download();
  assert.equal(au.downloadCalls, 1, "the explicit choice must reach electron-updater");
});

test("an UNSIGNED build still reports the update, but refuses to install it", async () => {
  // The brief's requirement: fail loudly and legibly rather than silently doing
  // nothing. Checking is a plain HTTPS fetch and works unsigned, so the user is
  // still told — the refusal happens at the click, naming the cause.
  const { up, au, events } = harness({ plan: UNSIGNED_PLAN });
  up.configure();
  const r = await up.check();
  assert.equal(r.mode, UNAVAILABLE, "an unsigned build must not claim it can update");
  assert.equal(r.latest, "0.2.0", "it must still say a new version exists");
  assert.equal(r.canAutoUpdate, false);

  const d = await up.download();
  assert.equal(au.downloadCalls, 0,
    "an unsigned build must never hand a download to Squirrel.Mac");
  assert.match(d.error, /code-signed/i, "the refusal must name the cause");
  assert.match(d.error, /manually/i, "and must offer the remaining route");
  assert.ok(events.some((e) => e.mode === UNAVAILABLE),
    "the refusal must be surfaced, not swallowed");
});

test("Later is persisted, and the next launch is silent about that version", async () => {
  const { up, disk, events } = harness({ plan: SIGNED_PLAN });
  up.configure();
  await up.check();
  up.defer();
  assert.equal(disk.deferredVersion, "0.2.0",
    "the deferral must reach the store, or it dies with the process");

  // A fresh updater over the SAME persisted state — i.e. the next launch, a
  // full day later so the daily throttle is NOT what produces the silence.
  // Without advancing the clock this test passes even with deferral deleted.
  const next = harness({ plan: SIGNED_PLAN, state: disk, now: T0 + DAY });
  next.up.configure();
  const r = await next.up.check();
  assert.equal(r.mode, SKIPPED);
  assert.equal(r.reason, "deferred");
  assert.equal(next.events.length, 0,
    "a user who said Later must not be prompted again on relaunch");
  assert.ok(events.length >= 1);
});

test("a newer release breaks through an earlier deferral", async () => {
  const { up } = harness({ plan: SIGNED_PLAN, version: "0.3.0",
                           state: { deferredVersion: "0.2.0" } });
  up.configure();
  const r = await up.check();
  assert.equal(r.mode, AVAILABLE,
    "deferring one version must not mute the product forever");
  assert.equal(r.latest, "0.3.0");
});

test("an explicit check bypasses both the throttle and a deferral", async () => {
  const { up, events } = harness({
    plan: SIGNED_PLAN,
    state: { deferredVersion: "0.2.0", lastCheckAt: 1_000_000_000 },
  });
  up.configure();
  const auto = await up.check();
  assert.equal(auto.mode, SKIPPED, "an automatic check is throttled");

  const manual = await up.check({ manual: true });
  assert.equal(manual.mode, AVAILABLE, "the menu item must always answer");
  assert.ok(events.some((e) => e.mode === AVAILABLE));
});

test("being up to date is silent automatically and spoken when asked", async () => {
  const { up, events } = harness({ plan: SIGNED_PLAN, version: "0.1.0" });
  up.configure();
  const auto = await up.check();
  assert.equal(auto.mode, UP_TO_DATE);
  assert.equal(events.length, 0, "do not interrupt to say nothing changed");

  const manual = await up.check({ manual: true });
  assert.equal(manual.mode, UP_TO_DATE);
  assert.equal(events.length, 1, "an explicit check must confirm, not stay mute");
});

test("the once-a-day throttle is recorded even when nothing is new", async () => {
  // Without this the app re-checks on every launch forever.
  const { up, au, disk } = harness({ plan: SIGNED_PLAN, version: "0.1.0" });
  up.configure();
  await up.check();
  assert.equal(disk.lastCheckAt, 1_000_000_000, "the attempt must be recorded");
  const second = await up.check();
  assert.equal(second.mode, SKIPPED);
  assert.equal(second.reason, "not-due");
  assert.equal(au.checkCalls, 1, "the throttle must prevent a second network call");
});

test("a network failure is reported, never thrown, and never blocks", async () => {
  const { up, events } = harness({ plan: SIGNED_PLAN, throws: "getaddrinfo ENOTFOUND" });
  up.configure();
  const auto = await up.check();
  assert.equal(auto.mode, FAILED, "a dead network must not crash the app");
  assert.equal(events.length, 0, "an automatic check must not nag about being offline");

  const manual = await up.check({ manual: true });
  assert.equal(manual.mode, FAILED);
  assert.match(manual.error, /Check your internet connection/,
    "the user gets a short, actionable sentence, not the raw error");
  assert.match(manual.rawError, /ENOTFOUND/,
    "the raw text is kept, but only in rawError for a collapsed Details view");
  assert.equal(events.length, 1, "an explicit check must say it could not reach the feed");
});

test("a 404 latest.yml failure emits a short sentence and keeps the dump in rawError", async () => {
  const raw = "Cannot find latest.yml in the latest release artifacts "
    + "(https://github.com/no-human-ai/no_human/releases/download/v0.2.2/latest.yml): "
    + "HttpError: 404\n"
    + 'Headers: {"x-github-request-id":"ABCD:1234:56789:ABCDEF:0123456"}\n'
    + "    at createHttpError (.../electron-updater/out/util/httpExecutor.js:52:12)\n"
    + "    at node:electron/js2c/browser_init:2:12345";
  const { up, events } = harness({ plan: SIGNED_PLAN, throws: raw });
  up.configure();
  const manual = await up.check({ manual: true });
  assert.equal(manual.mode, FAILED);
  assert.doesNotMatch(manual.error, /x-github-request-id/);
  assert.doesNotMatch(manual.error, /browser_init/);
  assert.match(manual.rawError, /x-github-request-id/,
    "the dump must still be available for the collapsed Details section");
  assert.match(manual.rawError, /browser_init/);
  assert.equal(events.length, 1);
  assert.doesNotMatch(events[0].error, /x-github-request-id/);
});

test("an unpackaged dev run is skipped rather than reported as broken", async () => {
  const { up, au } = harness({ plan: SIGNED_PLAN, isPackaged: false });
  up.configure();
  const r = await up.check();
  assert.equal(r.mode, SKIPPED);
  assert.equal(r.reason, "not-packaged");
  assert.equal(au.checkCalls, 0, "there is no app-update.yml to read in dev");
});

test("install refuses until the bytes are actually on disk", async () => {
  const { up, au } = harness({ plan: SIGNED_PLAN });
  up.configure();
  await up.check();
  const early = up.install();
  assert.equal(early.mode, FAILED, "restarting before the download completes bricks it");
  assert.equal(au.quitAndInstallCalls, 0);

  au.listeners.get("update-downloaded")({ version: "0.2.0" });
  const ok = up.install();
  assert.equal(ok.mode, "installing");
  assert.equal(au.quitAndInstallCalls, 1);
});

test("download progress and completion reach the listener", async () => {
  const { up, au, events } = harness({ plan: SIGNED_PLAN });
  up.configure();
  await up.check();
  au.listeners.get("download-progress")({ percent: 42.7 });
  au.listeners.get("update-downloaded")({ version: "0.2.0" });
  const progress = events.find((e) => e.mode === "downloading");
  assert.equal(progress.percent, 43, "progress must be reported to the UI");
  assert.ok(events.some((e) => e.mode === "downloaded"));
});

test("a listener that throws cannot take the updater down", async () => {
  const au = fakeAutoUpdater();
  const up = createUpdater({
    autoUpdater: au, plan: SIGNED_PLAN, currentVersion: "0.1.0",
    onEvent: () => { throw new Error("renderer went away"); },
  });
  up.configure();
  const r = await up.check();
  assert.equal(r.mode, AVAILABLE, "a dead renderer must not break the check");
});
