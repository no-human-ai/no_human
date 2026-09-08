// The launch-probe race: a healthy server that is merely slow to answer the
// FIRST /api/tasks?limit=1 request (a DB write burst, per the measured
// incident) must not read as "down" and send the shell down the spawn path.
// probe()'s once-retry (server.mjs) exists so a single transient timeout at
// launch attaches to the live server instead of producing the pid-lock /
// "another instance is already running" error screen.
//
// Runs in its own process (node --test gives each FILE a fresh module graph)
// because main.mjs has top-level side effects — same harness shape as
// mainLateServer.test.mjs.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { listenOnFreePort } from "./testing/ports.mjs";

register("./testing/electronLoader.mjs", import.meta.url);

const IS_WIN = process.platform === "win32";

const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-probe-retry-"));
fs.mkdirSync(path.join(home, ".no_human"));
// A credential must exist, or main.mjs takes the first-run setup path instead
// of probing/spawning (see _loadBoardOrError's hasCredential branch).
fs.writeFileSync(path.join(home, ".no_human", ".env"),
  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-probe-retry\n");
process.env.HOME = home;
process.env.USERPROFILE = home; // os.homedir() reads USERPROFILE on Windows
delete process.env.NH_TEST_LOG;

// If probe() wrongly reports "down" on the first launch, main.mjs falls
// through to ensureServer, which spawns NH_BIN. Point it at a script that
// only writes a marker — if the marker never appears, nothing was spawned.
const spawnMarker = path.join(home, "spawned.marker");
const bin = path.join(home, "nh");
fs.writeFileSync(bin, `#!${process.execPath}
const fs = require("node:fs");
fs.writeFileSync(${JSON.stringify(spawnMarker)}, String(process.pid));
setInterval(() => {}, 1000);
`);
fs.chmodSync(bin, 0o755);
process.env.NH_BIN = bin;

// A REAL server, bound BEFORE launch, whose first /api/tasks request hangs
// (the response is never ended — reproducing a DB-write-burst stall) and
// whose second request answers immediately. probe()'s built-in retry (300ms
// after the first attempt's timeout) must pick up the second answer.
let requestCount = 0;
const heldSockets = [];
const { server, origin: ORIGIN } = await listenOnFreePort((req, res) => {
  requestCount += 1;
  if (requestCount === 1) { heldSockets.push(req.socket); return; }
  res.end("[]");
});
process.env.NH_ORIGIN = ORIGIN;

const rejections = [];
process.on("unhandledRejection", (e) => rejections.push(e));

const stub = await import("./testing/electronStub.mjs");
await import("./main.mjs");
stub.fireReady();
// probe()'s worst case is 1500 (first timeout) + 300 (retry delay) + fast
// second answer; give generous room for the retry, load, and CI jitter.
await new Promise((r) => setTimeout(r, 3500));

test.after(() => {
  for (const sock of heldSockets) sock.destroy();
  server.close();
  fs.rmSync(home, { recursive: true, force: true });
});

test("a first-request timeout during launch attaches to the live server instead of spawning",
  { skip: IS_WIN ? "NH_BIN alone cannot supply the argv override a Windows "
    + "fake nh needs; the POSIX run governs this platform-independent probe "
    + "path (no win32 branch in the retry logic itself)" : false },
  () => {
    const win = stub.BrowserWindow.last;
    assert.ok(win, "no window was created");
    assert.ok(win.loaded.includes(`url:${ORIGIN}`),
      `expected the board to load via the live server; got ${JSON.stringify(win.loaded)}`);
    assert.ok(!win.loaded.some((u) => u.startsWith("file:error.html")),
      `the error page must never show; got ${JSON.stringify(win.loaded)}`);
    assert.ok(!fs.existsSync(spawnMarker),
      "ensureServer spawned NH_BIN — the retry did not attach to the live server");
    assert.ok(requestCount >= 2,
      `expected the probe to retry at least once; saw ${requestCount} request(s)`);
  });

test("the retry-and-attach path leaves no unhandled rejection in the main process",
  { skip: IS_WIN ? "see the skip above" : false },
  () => {
    assert.deepEqual(rejections.map((e) => (e && e.message) || String(e)), [],
      "the probe retry must not leak an unhandled rejection");
  });
