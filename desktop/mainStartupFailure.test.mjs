// Startup must not end in an unhandled rejection with a blank window.
//
// Reachable in a real packaged app: electron-builder's `files` is a literal
// allowlist, and a missing entry is silently absent from app.asar — that
// shipped an app that could not launch at all (review round 1). If token.html
// is the missing file, showSetup's loadFile rejects and the rejection escapes
// createWindow -> whenReady, leaving a blank window and no error page.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { probe } from "./server.mjs";
import { freePort } from "./testing/ports.mjs";

register("./testing/electronLoader.mjs", import.meta.url);

// No token and nothing listening: main.mjs takes the first-run setup path.
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-startfail-"));
fs.mkdirSync(path.join(home, ".no_human"));
process.env.HOME = home;
process.env.USERPROFILE = home; // os.homedir() reads USERPROFILE on Windows (see mainIpc.test.mjs)
const { origin: ORIGIN } = await freePort();
process.env.NH_ORIGIN = ORIGIN;

// Precondition, not an assumption: this test's whole premise is that the boot
// probe finds nothing. If some other process holds this port, fail HERE with
// this message rather than minutes later as inexplicable board-loaded assertions.
assert.notEqual(await probe(ORIGIN), "up",
  `${ORIGIN} answered a probe — this test requires NOTHING listening`);

const rejections = [];
process.on("unhandledRejection", (e) => rejections.push(e));

const stub = await import("./testing/electronStub.mjs");
stub.BrowserWindow.failLoadFile = "token.html";  // only token.html is missing
await import("./main.mjs");
stub.fireReady();
await new Promise((r) => setTimeout(r, 2500));

test.after(() => fs.rmSync(home, { recursive: true, force: true }));

test("a setup screen that cannot load does not leak an unhandled rejection", () => {
  const win = stub.BrowserWindow.last;
  assert.ok(win.loaded.some((x) => x.startsWith("file-failed:")),
    `the setup load should have been attempted; got ${JSON.stringify(win.loaded)}`);
  assert.deepEqual(rejections.map((e) => (e && e.message) || String(e)), [],
    "startup left an unhandled rejection — the window stays blank with no error page");
  // Absence of a rejection is NOT the invariant: a silently swallowed error also
  // leaves a blank window. The recovery must actually render something.
  assert.ok(win.loaded.includes("file:error.html?startup-failed"),
    `startup must fall back to the error page; got ${JSON.stringify(win.loaded)}`);
});

test("a credential screen that cannot load falls back to the error page, not a blank window",
  async () => {
    // The gap the ERR_ABORTED change opened: showBoard returns quietly on an
    // abort, and openSetup used to RETHROW anything else while both callers only
    // logged — so if token.html is missing, nothing paints at all.
    const onNavigate = stub.calls.nav.get("will-navigate");
    assert.ok(onNavigate, "main.mjs must register a will-navigate handler");
    const win = stub.BrowserWindow.last;
    win.loaded.length = 0;

    await onNavigate({ preventDefault() {} }, "nh://token");   // failLoadFile is on

    assert.ok(win.loaded.some((x) => x.startsWith("file-failed:token.html")),
      `the setup load should have been attempted; got ${JSON.stringify(win.loaded)}`);
    assert.ok(win.loaded.includes("file:error.html?setup-failed"),
      `nothing painted — this is the blank window; got ${JSON.stringify(win.loaded)}`);
  });
