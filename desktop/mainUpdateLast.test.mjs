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

test("Later stops the retained result from being served again", async () => {
  main.sendUpdateEvent({ mode: "unavailable", latest: "0.2.1", current: "0.2.0", canAutoUpdate: false });

  const defer = stub.calls.ipc.get("nh:update-defer");
  await defer({ senderFrame: { url: `http://127.0.0.1:${PORT}/` } }, "0.2.1");

  const last = stub.calls.ipc.get("nh:update-last");
  const result = await last();
  assert.equal(result, null,
    "a deferred version must not be re-served by nh:update-last — that would re-notify a panel that mounts later");
});

test("the preload and main agree on the channel", () => {
  const preload = fs.readFileSync(new URL("./preload.cjs", import.meta.url), "utf8");
  assert.ok(preload.includes('"nh:update-last"'),
    "preload.cjs does not invoke \"nh:update-last\", but main.mjs handles it");
  assert.ok(preload.includes("getLastUpdate"),
    "preload.cjs must expose getLastUpdate so the renderer can pull the last result");
});
