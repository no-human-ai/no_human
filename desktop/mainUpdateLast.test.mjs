// The retention half of the update-visibility fix.
//
// The automatic startup check fires its ONE "nh:update" push before Settings'
// UpdatesPanel — the only subscriber — ever mounts, so the event was lost and
// a 0.2.0 user was never told 0.2.1 exists unless they clicked "Check for
// updates" manually. main.mjs now retains the last payload it sent (even with
// no window/subscriber alive) behind a new "nh:update-last" pull, so a
// renderer that mounts late can still ask "what was the last result?"
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";

register("./testing/electronLoader.mjs", import.meta.url);

const PORT = 19990 + (process.pid % 90);
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-updlast-"));
fs.mkdirSync(path.join(home, ".no_human"));
fs.writeFileSync(path.join(home, ".no_human", ".env"),
  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-test\n");
process.env.HOME = home;
process.env.USERPROFILE = home; // os.homedir() reads USERPROFILE on Windows (see mainIpc.test.mjs)
process.env.NH_ORIGIN = `http://127.0.0.1:${PORT}`;

const server = http.createServer((q, s) => s.end("[]"));
await new Promise((r) => server.listen(PORT, "127.0.0.1", r));

const stub = await import("./testing/electronStub.mjs");
const main = await import("./main.mjs");
stub.fireReady();
await new Promise((r) => setTimeout(r, 800));

test.after(() => { server.close(); fs.rmSync(home, { recursive: true, force: true }); });

test("nh:update-last is registered", () => {
  assert.ok(stub.calls.ipc.has("nh:update-last"),
    "main.mjs never registered \"nh:update-last\" — a late-mounting renderer has no way to ask");
});

test("nothing reported yet reads as no news", async () => {
  // Fresh electronStub state per test file (module-level `lastUpdate` starts
  // null); no sendUpdateEvent has run in this file yet at this point.
  const handler = stub.calls.ipc.get("nh:update-last");
  const result = await handler();
  assert.equal(result, null,
    "before any update event, nh:update-last must read as no news, not a stale mode");
});

test("an update event with no subscriber is still retained", async () => {
  // This is exactly the startup case: sendUpdateEvent fires before Settings'
  // UpdatesPanel (its only subscriber) has mounted, so nobody is listening on
  // "nh:update" — yet the result must still be readable afterward.
  main.sendUpdateEvent({ mode: "unavailable", latest: "0.2.1", current: "0.2.0", canAutoUpdate: false });

  const handler = stub.calls.ipc.get("nh:update-last");
  const result = await handler();
  assert.ok(result, "an update event with no subscriber must still be retained");
  assert.equal(result.latest, "0.2.1");
  assert.equal(result.mode, "unavailable");
  assert.equal(typeof result.message, "string");
  assert.ok(result.message.length > 0,
    "the retained payload must carry the same enrichment (message) the push path gets");
});

test("a FAILED result from the automatic check is never retained", async () => {
  // updater.mjs registers an UNCONDITIONAL autoUpdater.on("error") listener
  // (updater.mjs:78-81) that emits {mode:"failed"} even when the check that
  // triggered it was the automatic startup one, not a manual click. Retaining
  // that would turn Settings > Updates on mount into a red "Could not check
  // for updates" card in place of whatever the last REAL (version) result was.
  main.sendUpdateEvent({ mode: "unavailable", latest: "0.2.1", current: "0.2.0", canAutoUpdate: false });
  main.sendUpdateEvent({ mode: "failed", error: "HttpError: 404 latest.yml" });

  const last = stub.calls.ipc.get("nh:update-last");
  const afterFailed = await last();
  assert.ok(afterFailed, "a startup FAILED must not blank out a prior retained result");
  assert.equal(afterFailed.mode, "unavailable",
    "nh:update-last must still read the last VERSION fact, not the failure that followed it");
  assert.equal(afterFailed.latest, "0.2.1");

  // The live push is unaffected: only retention is gated, not delivery. A
  // manual check's FAILED (or, as here, the unconditional error listener
  // firing on the automatic one) must still reach a mounted panel.
  const sentFailed = stub.calls.sent.filter((c) => c.channel === "nh:update").at(-1);
  assert.equal(sentFailed.payload.mode, "failed",
    "the failure must still reach the live \"nh:update\" push — only retention is gated, not delivery");

  // And from a different retained mode: FAILED must not displace it either.
  main.sendUpdateEvent({ mode: "up-to-date", current: "0.2.1" });
  main.sendUpdateEvent({ mode: "failed", error: "HttpError: 404 latest.yml" });
  const afterSecondFailed = await last();
  assert.equal(afterSecondFailed.mode, "up-to-date",
    "a FAILED must never displace ANY previously retained mode, not just \"unavailable\"");
});

test("a defer that did not persist leaves the retained result alone", async () => {
  // getUpdater() returns null in this test env: real electron-updater reaches
  // into `require("electron")` from its own CommonJS internals (e.g.
  // ElectronAppAdapter), which resolves to the real npm "electron" launcher
  // package (its main export is a path STRING outside an actual Electron
  // process) rather than ./testing/electronStub.mjs — electronLoader.mjs's
  // resolve hook only intercepts the ESM-level "electron" specifier, not a
  // nested CJS require() inside a dependency. That is exactly the "unpackaged
  // run" state this suite is testing FROM, not a gap to paper over.
  //
  // The OLD nh:update-defer cleared `lastUpdate` unconditionally before ever
  // checking any of that, so Settings would go blank on "Later" even though
  // nothing was written to disk. It must now leave the retained result (and
  // the live channel) alone whenever the defer did not actually persist.
  main.sendUpdateEvent({ mode: "unavailable", latest: "0.2.1", current: "0.2.0", canAutoUpdate: false });
  const sentBefore = stub.calls.sent.length;

  const defer = stub.calls.ipc.get("nh:update-defer");
  const result = await defer({ senderFrame: { url: `http://127.0.0.1:${PORT}/` } }, "0.2.1");
  assert.equal(result.mode, "failed",
    "with no updater available, defer must report failure rather than silently pretending to persist");

  const last = stub.calls.ipc.get("nh:update-last");
  const retained = await last();
  assert.ok(retained, "a defer that did not persist must not clear a retained result");
  assert.equal(retained.mode, "unavailable");
  assert.equal(retained.latest, "0.2.1");

  assert.equal(stub.calls.sent.length, sentBefore,
    "nothing persisted, so there must be no deferral push on \"nh:update\"");
});

// "Later clears the retained result and pushes the deferral" — the PERSISTED
// half of nh:update-defer — lives in ./mainUpdateDeferSuccess.test.mjs, not
// here. getUpdater()'s `updater` is memoized permanently on its first success
// (main.mjs ~194-223) and electron-updater's own `autoUpdater` export is a
// getter that re-runs on every read, so faking a working updater requires
// pre-seeding Node's CJS require cache for "electron-updater" before its
// VERY FIRST import anywhere in the process — including main.mjs's own
// automatic startup checkForUpdates() call a few hundred ms after
// stub.fireReady(). Doing that in THIS file would make every test above
// (which relies on the real, always-unavailable-in-this-harness
// electron-updater) observe a working updater instead, from the very first
// test onward — the two scenarios cannot coexist in one process. The split
// file gets a virgin module registry so the fake is the only "electron-updater"
// it, or main.mjs's boot sequence, ever sees.

test("the preload and main agree on the channel", () => {
  const preload = fs.readFileSync(new URL("./preload.cjs", import.meta.url), "utf8");
  assert.ok(preload.includes('"nh:update-last"'),
    "preload.cjs does not invoke \"nh:update-last\", but main.mjs handles it");
  assert.ok(preload.includes("getLastUpdate"),
    "preload.cjs must expose getLastUpdate so the renderer can pull the last result");
});
