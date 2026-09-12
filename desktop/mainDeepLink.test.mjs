// The `nohuman://` deep link — the scheme an email button uses to raise the
// installed app. Driven the same way mainSecondInstance.test.mjs drives the
// single-instance handler: main.mjs is imported under the electron stub, the
// handlers it registers are pulled back out of the stub and invoked directly,
// so nothing here needs Electron, a display, or a real OS registration.
//
// THE DEFECT THIS FILE EXISTS FOR is the ordering one. On macOS a COLD launch
// from a link fires `open-url` BEFORE `app.whenReady()` resolves — the OS
// started the app because of the link — so the handler runs when `win` is still
// null and there is nothing to raise. An implementation that acts immediately
// drops that click on the floor: the app opens and the button appears dead.
// The first three tests below fire the link BEFORE `fireReady()` for exactly
// that reason, and assert the window is surfaced anyway once startup finishes.
//
// The second thing asserted is that the URL is inert. It is UNTRUSTED INPUT —
// any web page can navigate to `nohuman://…`, so its author is whoever wrote
// the link — and the only byte main.mjs is allowed to read off it is the
// scheme. `stub.shell` RECORDS openExternal/openPath and `win.loaded` RECORDS
// every navigation, so "nothing was opened or navigated to" is an observation
// here, not an assumption.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execSync } from "node:child_process";

register("./testing/electronLoader.mjs", import.meta.url);

const MARK = `nhDeepLink${process.pid}`;

// Same isolation as mainSecondInstance.test.mjs: no token in ~/.no_human so
// startup takes the setup path, NH_BIN points at a fake so a stray resolution
// can never reach the operator's real nh, and NH_ORIGIN is non-routable.
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-deeplink-"));
fs.mkdirSync(path.join(home, ".no_human"));
const fakeNh = path.join(home, "nh");
fs.writeFileSync(fakeNh, `#!/usr/bin/env node
process.title = ${JSON.stringify(MARK)};
setInterval(() => {}, 1000);
`);
fs.chmodSync(fakeNh, 0o755);
process.env.HOME = home;
process.env.USERPROFILE = home;   // os.homedir() reads USERPROFILE on Windows
process.env.NH_BIN = fakeNh;
process.env.NH_ORIGIN = `http://10.255.255.1:${19940 + (process.pid % 15)}`;

const stub = await import("./testing/electronStub.mjs");
const main = await import("./main.mjs");

// Captured BEFORE fireReady(): if open-url were registered inside whenReady it
// would not exist yet here, which is the bug this ordering reproduces.
const openUrlBeforeReady = stub.calls.handlers.get("open-url");
const clientsBeforeReady = stub.calls.protocolClients.length;

// The cold launch: the OS hands us the URL, then the app becomes ready.
const preventedBeforeReady = [];
openUrlBeforeReady?.(
  { preventDefault() { preventedBeforeReady.push("nohuman://open"); } },
  "nohuman://open");
const shownDuringBuffer = stub.BrowserWindow.last?.shown ?? null;

stub.fireReady();

// Wait for the READY PHASE to finish, not for a fixed number of seconds. The
// buffered link is acted on after `await createWindow()` returns, and that
// awaits a page load whose duration is a network probe (~3.5s here, and a
// 3000ms sleep — the figure mainSecondInstance.test.mjs uses, which only needs
// the window to EXIST — measured as too short). The condition polled is the
// window having loaded a page, which happens whether or not the buffered link
// survives, so this cannot mask the defect it is waiting to expose.
{
  const deadline = Date.now() + 30000;
  while ((stub.BrowserWindow.last?.loaded.length ?? 0) === 0) {
    if (Date.now() > deadline) {
      throw new Error("mainDeepLink.test.mjs: startup never loaded a page — the "
        + "harness is broken, and every assertion below would be meaningless");
    }
    await new Promise((r) => setTimeout(r, 50));
  }
  // One more turn so the promise chain's finally() — which runs immediately
  // after the awaited load resolves — has settled.
  await new Promise((r) => setTimeout(r, 250));
}

test.after(() => {
  try { execSync(`/usr/bin/pkill -9 -f ${MARK}`); } catch { /* already gone */ }
  fs.rmSync(home, { recursive: true, force: true });
});

test("an open-url handler exists BEFORE the app is ready", () => {
  assert.ok(openUrlBeforeReady,
    "main.mjs registers no open-url handler at module scope — on macOS the "
    + "cold-launch event fires before whenReady() resolves, so a handler added "
    + "later never sees the link that started the app");
  assert.ok(stub.BrowserWindow.last, "no window was ever created; the rest is void");
});

test("the app claims the nohuman scheme with the OS", () => {
  const clients = stub.calls.protocolClients;
  assert.ok(clients.length > 0,
    "app.setAsDefaultProtocolClient was never called — on Windows that is the "
    + "ONLY registration there is (the NSIS target writes no scheme keys), so "
    + "a nohuman:// link there resolves to nothing");
  assert.deepEqual(clients.map((c) => c.scheme), ["nohuman"],
    "the app must register exactly the nohuman scheme, once");
});

test("an UNPACKAGED run registers the executable and the script, not a bare exe", () => {
  // app.isPackaged is false in the stub, i.e. the `npm run desktop` case. On
  // Windows the registry command for an unpackaged run has to name Electron's
  // executable AND the script, or the OS launches a bare Electron with no app.
  assert.equal(stub.app.isPackaged, false,
    "sanity: this test only means anything while the stub reports unpackaged");
  const c = stub.calls.protocolClients[0];
  assert.equal(c.execPath, process.execPath,
    "the unpackaged registration must name the running executable");
  assert.ok(Array.isArray(c.args) && c.args.length === 1 && path.isAbsolute(c.args[0]),
    `the unpackaged registration must carry the script path as an absolute `
    + `argument; got ${JSON.stringify(c.args)}`);
});

test("registration does NOT happen before the app is ready", () => {
  // Not cosmetic: Electron's app APIs are only reliable after ready, and the
  // registration is deliberately inside whenReady while the HANDLER is not.
  assert.equal(clientsBeforeReady, 0,
    "setAsDefaultProtocolClient ran at module scope, before app ready");
});

test("a link that arrived BEFORE ready still raises the window", () => {
  assert.equal(preventedBeforeReady.length, 1,
    "the open-url handler must call preventDefault() to mark the URL handled");
  assert.equal(shownDuringBuffer, null,
    "sanity: there was no window at all when the link arrived — that is the "
    + "situation this test exists for");
  assert.equal(stub.BrowserWindow.last.shown, 1,
    "the cold-launch link was DROPPED: the window exists but was never shown, "
    + "which is what a user sees as 'I clicked the button and nothing happened'");
});

test("a link to the RUNNING app raises the window again", () => {
  const openUrl = stub.calls.handlers.get("open-url");
  const win = stub.BrowserWindow.last;
  const before = win.shown;

  openUrl({ preventDefault() {} }, "nohuman://open");

  assert.equal(stub.BrowserWindow.last, win, "an existing window must be reused");
  assert.equal(win.shown, before + 1,
    "show() was not called: with close-to-tray the window may be HIDDEN, and "
    + "focus() alone leaves it invisible (measured on Windows, see main.mjs)");
});

test("a MINIMIZED window is restored before it is shown", () => {
  const openUrl = stub.calls.handlers.get("open-url");
  const win = stub.BrowserWindow.last;
  let restored = 0;
  win.isMinimized = () => true;
  win.restore = () => { restored += 1; };
  const before = win.shown;

  openUrl({ preventDefault() {} }, "nohuman://open");

  assert.equal(restored, 1, "a minimized window must be restored, not just shown");
  assert.equal(win.shown, before + 1, "and then surfaced");
  win.isMinimized = () => false;
});

test("only the nohuman scheme is honoured — everything else is dropped", () => {
  // The whole validation surface. A prefix match, a foreign scheme, an
  // executable-looking one, a local file, and a non-string all reach the same
  // handler an attacker-authored link reaches.
  const openUrl = stub.calls.handlers.get("open-url");
  const win = stub.BrowserWindow.last;
  const foreign = [
    "nohumanx://open",          // prefix, not our scheme
    "xnohuman://open",          // suffix, not our scheme
    "https://evil.example/x",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "not a url at all",
    "",
    undefined,
    null,
    42,
    { toString: () => "nohuman://open" },   // not a string; must not be coerced
  ];
  for (const url of foreign) {
    const before = win.shown;
    openUrl({ preventDefault() {} }, url);
    assert.equal(win.shown, before,
      `${JSON.stringify(String(url))} was treated as ours and surfaced the window`);
  }
  assert.equal(stub.BrowserWindow.last, win, "no foreign URL may build a window");
});

test("the URL is never opened, navigated to, or handed anywhere", () => {
  // OBSERVED, not assumed: the stub RECORDS what would reach the OS and what
  // would reach the window. An attacker picks every byte after the scheme, so
  // "raise the window" has to be the entire effect.
  const openUrl = stub.calls.handlers.get("open-url");
  const win = stub.BrowserWindow.last;
  const openedBefore = stub.calls.opened.length;
  const pathsBefore = stub.calls.openedPaths.length;
  const loadedBefore = win.loaded.length;
  const sentBefore = stub.calls.sent.length;

  openUrl({ preventDefault() {} }, "nohuman://open/../../etc/passwd?x=1#y");
  openUrl({ preventDefault() {} }, "https://evil.example/x");

  assert.equal(stub.calls.opened.length, openedBefore,
    "a deep link reached shell.openExternal — the OS was asked to open a URL "
    + "an attacker chose");
  assert.equal(stub.calls.openedPaths.length, pathsBefore,
    "a deep link reached shell.openPath");
  assert.equal(win.loaded.length, loadedBefore,
    "a deep link caused a navigation; the window must only be raised");
  assert.equal(stub.calls.sent.length, sentBefore,
    "a deep link was forwarded to the renderer, where it becomes page input");
});

test("isAppDeepLink accepts our scheme in every legal spelling and nothing else", () => {
  // The guard itself, directly: URL scheme comparison is case-insensitive per
  // RFC 3986 and `new URL` lowercases it, so an upper-case link from an email
  // client is still ours.
  assert.equal(main.isAppDeepLink("nohuman://open"), true);
  assert.equal(main.isAppDeepLink("NOHUMAN://open"), true);
  assert.equal(main.isAppDeepLink("nohuman:open"), true, "opaque form is still ours");
  assert.equal(main.isAppDeepLink("nohumans://open"), false);
  assert.equal(main.isAppDeepLink("https://getnohuman.com"), false);
  // MEASURED, not assumed: the WHATWG parser STRIPS leading C0/space before it
  // reads the scheme (`new URL(" nohuman://open").protocol === "nohuman:"`), so
  // a padded link is still ours. Written down because the intuitive expectation
  // is the opposite, and it is harmless here only because the effect of a
  // recognised link is "raise the window" and nothing else.
  assert.equal(main.isAppDeepLink(" nohuman://open"), true);
  assert.equal(main.isAppDeepLink(undefined), false);
  assert.equal(main.isAppDeepLink(""), false);
});

test("Windows/Linux argv delivery needs nothing beyond the existing handler", () => {
  // Those platforms have no open-url: the OS starts a SECOND instance with the
  // URL appended to argv and the single-instance lock routes it here. Raising
  // the window is the whole behaviour, so the handler is already complete —
  // this pins that, and pins that the argv is NOT read (a handler that started
  // parsing attacker-authored argv would be new surface for no new effect).
  const secondInstance = stub.calls.handlers.get("second-instance");
  assert.ok(secondInstance, "main.mjs must keep its second-instance handler");
  const win = stub.BrowserWindow.last;
  const before = win.shown;
  const openedBefore = stub.calls.opened.length;
  const loadedBefore = win.loaded.length;

  secondInstance({}, [process.execPath, ".", "nohuman://open"], process.cwd());

  assert.equal(win.shown, before + 1,
    "a relaunch carrying a nohuman:// argv did not surface the window");
  assert.equal(stub.calls.opened.length, openedBefore,
    "the argv URL was handed to the OS");
  assert.equal(win.loaded.length, loadedBefore,
    "the argv URL caused a navigation");
});

// LAST: before-quit latches `quitting` for the rest of the process.
test("a link DURING the delayed quit builds nothing", () => {
  const openUrl = stub.calls.handlers.get("open-url");
  const beforeQuit = stub.calls.handlers.get("before-quit");
  assert.ok(beforeQuit, "main.mjs must register a before-quit handler");

  beforeQuit({ preventDefault() {} });
  const dying = stub.BrowserWindow.last;
  dying.isDestroyed = () => true;
  const before = dying.shown;

  openUrl({ preventDefault() {} }, "nohuman://open");

  assert.equal(stub.BrowserWindow.last, dying,
    "a link arriving during the SIGTERM->SIGKILL quit delay built a FRESH "
    + "window against a server being torn down — showWindow() falls through to "
    + "createWindow() whenever the window is gone, and the app is reachable by "
    + "URL for the whole of that delay");
  assert.equal(dying.shown, before,
    "the dying window was surfaced again mid-teardown");
});
