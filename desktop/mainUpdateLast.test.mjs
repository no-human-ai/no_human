// The PULL half of "the board renders the notice": nh:update-last, and the
// retention rule that makes an automatic startup failure quiet without ever
// gating the LIVE push (Settings must still see a manual failure it asked for).
//
// mainUpdateWiring.test.mjs already proves the four preload channels are
// registered; this file adds the fifth (nh:update-last) and the one behaviour
// that lives only at this wiring layer — sendUpdateEvent() feeding lastUpdate
// through retainedUpdate() — because updatePolicy.test.mjs proves the pure
// function correct in isolation, not that main.mjs actually calls it.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";

register("./testing/electronLoader.mjs", import.meta.url);

const PORT = 19700 + (process.pid % 90);
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-updlast-"));
fs.mkdirSync(path.join(home, ".no_human"));
fs.writeFileSync(path.join(home, ".no_human", ".env"),
  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-test\n");
process.env.HOME = home;
process.env.USERPROFILE = home; // os.homedir() reads USERPROFILE on Windows
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
    "main.mjs never registered nh:update-last — a late-mounting board would stay notice-free forever");
});

test("nothing reported yet reads null", async () => {
  const handler = stub.calls.ipc.get("nh:update-last");
  assert.equal(await handler(), null);
});

test("an unavailable event with no subscriber is still retained, with its message", async () => {
  main.sendUpdateEvent({ mode: "unavailable", latest: "9.9.1", current: "9.9.0" });
  const handler = stub.calls.ipc.get("nh:update-last");
  const last = await handler();
  assert.equal(last.mode, "unavailable");
  assert.equal(last.latest, "9.9.1");
  assert.match(last.message, /9\.9\.1/, "the enriched message must be retained too, not just the raw mode/latest");
});

test("a failed automatic check does not displace the retained fact, but IS delivered live", async () => {
  const beforeSentCount = stub.calls.sent.length;
  main.sendUpdateEvent({ mode: "failed", error: "x", rawError: "y" });

  const handler = stub.calls.ipc.get("nh:update-last");
  const last = await handler();
  assert.equal(last.mode, "unavailable", "the retained payload must still be the earlier version fact");
  assert.equal(last.latest, "9.9.1");

  assert.equal(stub.calls.sent.length, beforeSentCount + 1,
    "the failure must still reach the live nh:update push — delivery is ungated");
  const delivered = stub.calls.sent.at(-1);
  assert.equal(delivered.channel, "nh:update");
  assert.equal(delivered.payload.mode, "failed",
    "a live Settings panel must still see the failure a late mount must not inherit");
});

test("nh:update-defer with no updater available leaves retention and delivery untouched", async () => {
  // This harness's getUpdater() always resolves null — not because
  // electron-updater is uninstalled or "cannot initialise unpackaged" (it is
  // an installed dependency), but because electron-updater's own CJS
  // `require("electron")` bypasses electronLoader.mjs's ESM resolve hook and
  // throws reading a property (e.g. "Cannot read properties of undefined
  // (reading 'getVersion')") off the real "electron" package instead — so
  // nh:update-defer takes its early-return path —
  // proving the board's clear is gated on an ACTUAL persisted defer, not on
  // every click of "Later".
  const beforeSentCount = stub.calls.sent.length;
  const handler = stub.calls.ipc.get("nh:update-defer");
  const result = await handler({ senderFrame: { url: "http://127.0.0.1/" } }, "9.9.1");
  assert.equal(result.mode, "failed");

  const lastHandler = stub.calls.ipc.get("nh:update-last");
  const last = await lastHandler();
  assert.equal(last.mode, "unavailable", "retention must be untouched by a defer that never persisted");
  assert.equal(stub.calls.sent.length, beforeSentCount,
    "no push should fire for a defer that changed nothing");
});
