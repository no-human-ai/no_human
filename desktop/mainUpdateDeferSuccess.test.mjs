// The PERSISTED half of nh:update-defer (MAJOR-2): "Later" must clear the
// retained result and push {mode:"skipped", reason:"deferred"} on "nh:update"
// — but only AFTER the defer actually wrote to disk. mainUpdateLast.test.mjs
// proves the "did not persist" side (getUpdater() unavailable, as this
// harness always is by default); this file proves the other side actually
// works, which needs a real, successful getUpdater().
//
// That requires faking electron-updater itself, not just calling into
// main.mjs's handlers: `updater` (main.mjs's memo of getUpdater()'s result)
// locks in on its FIRST success and never re-checks, and electron-updater's
// own `autoUpdater` export is a lazy getter that re-runs
// doLoadAutoUpdater() — which reaches into `require("electron")`, resolving
// to the real npm "electron" launcher package (a path STRING outside an
// actual Electron process, so `.app.getVersion()` throws) — on EVERY read.
// So a fake has to be in place before the FIRST-ever import of
// "electron-updater" anywhere in the process, including main.mjs's own
// automatic startup checkForUpdates() call. Doing that in the same process as
// mainUpdateLast.test.mjs's "unavailable" tests would make every one of those
// see a working updater too, from their very first call onward — the two
// states cannot coexist in one module registry. `node --test` gives each
// test FILE its own process, so this lives in its own file for a virgin one.
//
// This does not touch updater.mjs or updatePolicy.mjs: `defer()`
// (updater.mjs ~189-193) never calls the autoUpdater object at all, so an
// empty fake autoUpdater is enough once getUpdater() stops short-circuiting
// to null — everything after that point is our own, real, unmodified code.
import { createRequire, Module } from "node:module";
import path from "node:path";
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";

const req = createRequire(import.meta.url);
const resolvedElectronUpdater = req.resolve("electron-updater");
const fakeModule = new Module(resolvedElectronUpdater, null);
fakeModule.filename = resolvedElectronUpdater;
fakeModule.loaded = true;
fakeModule.paths = Module._nodeModulePaths(path.dirname(resolvedElectronUpdater));
fakeModule.exports = { autoUpdater: {} };
Module._cache[resolvedElectronUpdater] = fakeModule;

register("./testing/electronLoader.mjs", import.meta.url);

const PORT = 19980 + (process.pid % 90);
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-upddefer-"));
fs.mkdirSync(path.join(home, ".no_human"));
fs.writeFileSync(path.join(home, ".no_human", ".env"),
  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-test\n");
process.env.HOME = home;
process.env.USERPROFILE = home;
process.env.NH_ORIGIN = `http://127.0.0.1:${PORT}`;

const server = http.createServer((q, s) => s.end("[]"));
await new Promise((r) => server.listen(PORT, "127.0.0.1", r));

const stub = await import("./testing/electronStub.mjs");
const main = await import("./main.mjs");
stub.fireReady();
await new Promise((r) => setTimeout(r, 800));

test.after(() => { server.close(); fs.rmSync(home, { recursive: true, force: true }); });

test("Later clears the retained result and pushes the deferral", async () => {
  main.sendUpdateEvent({ mode: "unavailable", latest: "0.2.1", current: "0.2.0", canAutoUpdate: false });

  const last = stub.calls.ipc.get("nh:update-last");
  const seeded = await last();
  assert.equal(seeded?.mode, "unavailable",
    "setup check: the seeded payload must actually be retained before deferring it");

  const defer = stub.calls.ipc.get("nh:update-defer");
  const result = await defer({ senderFrame: { url: `http://127.0.0.1:${PORT}/` } }, "0.2.1");
  assert.equal(result.mode, "skipped",
    "the fake updater's defer() must actually persist for this test to prove anything");

  const retained = await last();
  assert.equal(retained, null,
    "a persisted defer must clear the retained result so a later mount is not re-notified");

  const pushed = stub.calls.sent.at(-1);
  assert.equal(pushed.channel, "nh:update");
  assert.equal(pushed.payload.mode, "skipped");
  assert.equal(pushed.payload.reason, "deferred");
  assert.equal(pushed.payload.latest, "0.2.1");
});
