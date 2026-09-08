// A rejected board load must NEVER leave a blank window.
//
// Measured in real Electron by a reviewer: when the server dies between the
// probe and the load, loadURL rejects, the window is shown BLANK, and the main
// process takes an unhandled rejection. main.mjs's own header calls a blank
// window "the one unacceptable failure mode", so this pins the recovery.
// Runs in its own process (node --test gives each FILE a fresh module graph),
// because main.mjs has top-level side effects.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { listenOnFreePort } from "./testing/ports.mjs";

register("./testing/electronLoader.mjs", import.meta.url);

const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-recovery-"));
fs.mkdirSync(path.join(home, ".no_human"));
fs.writeFileSync(path.join(home, ".no_human", ".env"),
  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-recovery\n");
process.env.HOME = home;
process.env.USERPROFILE = home; // os.homedir() reads USERPROFILE on Windows (see mainIpc.test.mjs)
// NH_TEST_LOG diverts openExternal to a file; with it set in the ambient
// environment the routing assertions below would pass vacuously.
delete process.env.NH_TEST_LOG;

// A server that answers the probe, so main.mjs takes the attach path — then the
// load itself fails. No spawning involved.
const { server, origin: ORIGIN } = await listenOnFreePort((_q, s) => s.end("[]"));
process.env.NH_ORIGIN = ORIGIN;

const rejections = [];
process.on("unhandledRejection", (e) => rejections.push(e));

const stub = await import("./testing/electronStub.mjs");
stub.BrowserWindow.failLoadURL = true;         // BEFORE the window is created
await import("./main.mjs");
stub.fireReady();
await new Promise((r) => setTimeout(r, 1500));

test.after(() => {
  server.close();
  fs.rmSync(home, { recursive: true, force: true });
});

test("a rejected board load falls back to error.html, not a blank window", () => {
  const win = stub.BrowserWindow.last;
  assert.ok(win, "no window was created");
  assert.ok(win.loaded.some((x) => x.startsWith("url-failed:")),
    `the load should have been attempted and rejected, got ${JSON.stringify(win.loaded)}`);
  // The REASON matters: the startup .catch also renders error.html, so
  // asserting the bare filename passes even with the fallback removed. Only
  // showBoard's own recovery reports "load-failed".
  assert.ok(win.loaded.includes("file:error.html?load-failed"),
    `the rejected load must be recovered by showBoard itself (reason=load-failed); `
    + `got ${JSON.stringify(win.loaded)}`);
});

test("a rejected board load produces no unhandled rejection", () => {
  assert.deepEqual(rejections.map((e) => (e && e.message) || String(e)), [],
    "startup must not leak an unhandled rejection into the main process");
});

// The shell must hand ONLY web schemes to the OS. The guard exists because a
// review found a bogus shell.openExternal on every Retry; with a no-op double
// it was untestable, so deleting it passed the whole suite.
test("only http(s) URLs are ever handed to the OS", () => {
  const onNavigate = stub.calls.nav.get("will-navigate");
  assert.ok(onNavigate, "main.mjs must register a will-navigate handler");
  const fire = (url) => onNavigate({ preventDefault() {} }, url);

  stub.calls.opened.length = 0;
  fire("file:///etc/passwd");
  fire("nh-evil://run?cmd=rm");
  fire("javascript:alert(1)");
  assert.deepEqual(stub.calls.opened, [],
    "a non-web scheme from board content was handed to the OS");

  fire("https://example.invalid/pr/1");
  assert.deepEqual(stub.calls.opened, ["https://example.invalid/pr/1"],
    "genuine external links must still open in the browser");
});

// --- call-site coverage for main.mjs wiring -------------------------------- //
// These invariants all have well-tested implementations; what was missing was
// any proof that main.mjs still WIRES them. Each one below was a surviving
// mutant, and several name defects that shipped in earlier rounds.

test("external links are routed to the OS, never opened in-window", () => {
  const openHandler = stub.calls.nav.get("window-open");
  assert.ok(openHandler, "main.mjs must set a window-open handler");
  stub.calls.opened.length = 0;

  const res = openHandler({ url: "https://example.invalid/pr/7" });
  assert.deepEqual(res, { action: "deny" }, "a child window must never be created");
  assert.deepEqual(stub.calls.opened, ["https://example.invalid/pr/7"],
    "an external target=_blank link stopped reaching the browser");

  // Same-origin stays in-window and must NOT be handed to the OS.
  stub.calls.opened.length = 0;
  openHandler({ url: `${process.env.NH_ORIGIN}/stats` });
  assert.deepEqual(stub.calls.opened, [], "an in-app URL was opened in the browser");
});

test("nh://token opens the credential screen", async () => {
  const onNavigate = stub.calls.nav.get("will-navigate");
  const win = stub.BrowserWindow.last;
  win.loaded.length = 0;
  let prevented = 0;

  await onNavigate({ preventDefault: () => { prevented += 1; } }, "nh://token");

  assert.equal(prevented, 1, "the pseudo-URL must not be navigated to");
  assert.ok(win.loaded.some((x) => x.startsWith("file:token.html")),
    `the error page's token link is dead; loaded=${JSON.stringify(win.loaded)}`);
});

test("the dock badge ignores a foreign title instead of clearing the count", () => {
  const onTitle = stub.calls.nav.get("page-title-updated");
  assert.ok(onTitle, "main.mjs must subscribe to page-title-updated");
  stub.calls.badge.length = 0;

  onTitle({}, "(3) no_human");
  assert.deepEqual(stub.calls.badge, [3], "the board's own count was not mirrored");

  // error.html has no count. Setting 0 here would wipe a truthful badge.
  onTitle({}, "no_human — can’t reach the server");
  assert.deepEqual(stub.calls.badge, [3],
    "a foreign title overwrote the last truthful badge");
});

test("navigation to an external or pseudo URL is always prevented", () => {
  // Without preventDefault the BOARD WINDOW navigates away — to the external
  // site, or to a nh:// URL it cannot load — and the app is gone.
  const onNavigate = stub.calls.nav.get("will-navigate");
  for (const url of ["https://example.invalid/pr/9", "nh://retry", "nh://token"]) {
    let prevented = 0;
    onNavigate({ preventDefault: () => { prevented += 1; } }, url);
    assert.equal(prevented, 1, `${url} was allowed to navigate the window away`);
  }
});

test("re-entering the token screen over a LIVE board can return to it", async () => {
  // openSetup decides canReturn from a probe. Hardcoding false makes Escape and
  // the secondary button QUIT THE APP over a working board — the N1 regression,
  // reached through the File-menu door after being fixed on the other one.
  const onNavigate = stub.calls.nav.get("will-navigate");
  const win = stub.BrowserWindow.last;
  win.loaded.length = 0;

  await onNavigate({ preventDefault() {} }, "nh://token");   // server IS up here

  assert.ok(win.loaded.includes("file:token.html?canReturn"),
    `the setup screen must know a board is behind it; got ${JSON.stringify(win.loaded)}`);
});
