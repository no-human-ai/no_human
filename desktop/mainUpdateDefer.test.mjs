// The DEFER push, at the wiring layer: nh:update-defer must run the fake
// updater's defer() first, and only then — and only on a persisted
// ("skipped"/"deferred") result — push the clear through sendUpdateEvent().
//
// mainUpdateLast.test.mjs already proves this handler's early-return path
// (electronLoader.mjs's getUpdater() always resolves null there — not
// because electron-updater is uninstalled or "cannot initialise unpackaged",
// but because electron-updater's own CJS `require("electron")` bypasses that
// loader's ESM resolve hook and throws reading a property, e.g.
// "Cannot read properties of undefined (reading 'getVersion')", off the real
// "electron" package instead); that leaves the ACTUAL push
// — main.mjs ~:876-887 — completely unexercised. Deleting the
// `sendUpdateEvent({mode:"skipped", reason:"deferred", ...})` call, or moving
// it above `u.defer(version)`, still left that other file's desktop suite
// green (27/27). This file drives a fake updater (updaterLoader.mjs +
// updaterStub.mjs) whose defer() result is scriptable, so both mutations turn
// this file red instead.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";

register("./testing/updaterLoader.mjs", import.meta.url);

const PORT = 19900 + (process.pid % 90);
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-upddefer-"));
fs.mkdirSync(path.join(home, ".no_human"));
fs.writeFileSync(path.join(home, ".no_human", ".env"),
  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-test\n");
process.env.HOME = home;
process.env.USERPROFILE = home; // os.homedir() reads USERPROFILE on Windows
process.env.NH_ORIGIN = `http://127.0.0.1:${PORT}`;

const server = http.createServer((q, s) => s.end("[]"));
await new Promise((r) => server.listen(PORT, "127.0.0.1", r));

const stub = await import("./testing/electronStub.mjs");
const updaterStub = await import("./testing/updaterStub.mjs");
const main = await import("./main.mjs");
stub.fireReady();
await new Promise((r) => setTimeout(r, 800));

test.after(() => { server.close(); fs.rmSync(home, { recursive: true, force: true }); });

const FRAME = { senderFrame: { url: "http://127.0.0.1/" } };

test("a persisted defer pushes nh:update, and the push happens after defer() ran", async () => {
  updaterStub.resetUpdaterStub();
  // Seed a retained fact first, so clearing it is observable.
  main.sendUpdateEvent({ mode: "available", latest: "9.9.1", current: "0.1.0" });
  const lastHandler = stub.calls.ipc.get("nh:update-last");
  assert.equal((await lastHandler()).mode, "available", "setup: the seeded fact must be retained before the defer runs");

  updaterStub.state.deferResult = { mode: "skipped", reason: "deferred", latest: "9.9.1" };
  const beforeSent = stub.calls.sent.length;

  const handler = stub.calls.ipc.get("nh:update-defer");
  const result = await handler(FRAME, "9.9.1");

  assert.deepEqual(result, { mode: "skipped", reason: "deferred", latest: "9.9.1" },
    "nh:update-defer must return the updater's result unchanged — mutation: reshaping the return value");

  assert.equal(stub.calls.sent.length, beforeSent + 1,
    "mutation: removing the sendUpdateEvent push after a persisted defer");
  const delivered = stub.calls.sent.at(-1);
  assert.equal(delivered.channel, "nh:update");
  assert.equal(delivered.payload.mode, "skipped");
  assert.equal(delivered.payload.reason, "deferred");
  assert.equal(delivered.payload.latest, "9.9.1");

  assert.equal(await lastHandler(), null,
    "a persisted defer must clear the retained fact — mutation: dropping retainedUpdate()'s skipped/deferred clear");

  assert.equal(updaterStub.state.deferCalls.length, 1, "defer() must run exactly once per click");
  assert.equal(updaterStub.state.deferCalls.at(-1).sentAt, beforeSent,
    "mutation: moving the sendUpdateEvent push BEFORE u.defer(version) — " +
    "defer() must observe the pre-push sent count, not a count that already includes the clear");
});

test("no push fires when defer() returns failed", async () => {
  updaterStub.resetUpdaterStub();
  main.sendUpdateEvent({ mode: "available", latest: "9.9.2", current: "0.1.0" });
  updaterStub.state.deferResult = { mode: "failed", error: "nothing to defer" };
  const beforeSent = stub.calls.sent.length;

  const handler = stub.calls.ipc.get("nh:update-defer");
  const result = await handler(FRAME, "9.9.2");

  assert.deepEqual(result, { mode: "failed", error: "nothing to defer" },
    "nh:update-defer must return the updater's failed result verbatim");
  assert.equal(stub.calls.sent.length, beforeSent,
    "mutation: pushing sendUpdateEvent unconditionally, even when defer() did not persist anything");

  const lastHandler = stub.calls.ipc.get("nh:update-last");
  assert.equal((await lastHandler()).mode, "available",
    "a failed defer must leave the retained fact untouched");
});
