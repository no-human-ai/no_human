// The shell's THEME, tested at its call site.
//
// THE DEFECT THIS PINS: createWindow derived its pre-paint colour, and the
// Windows title-bar overlay colours, from `nativeTheme.shouldUseDarkColors` —
// the OS setting. The board does not follow the OS: web/src/App.jsx defaults
// "nh-theme" to "dark" regardless of it. So on a light-mode Mac or PC the shell
// painted a light window and light title-bar controls around a dark board.
// Nothing in the suite mentioned backgroundColor, titleBarOverlay or
// nativeTheme at all, so the mismatch was invisible to every test.
//
// WHY win32: the platform is forced to "win32" for this whole file because the
// win32 branch is a strict SUPERSET of the theme surface — backgroundColor and
// nativeTheme.themeSource are set outside any platform branch, and only win32
// adds colour options (titleBarOverlay). darwin's branch carries no colours at
// all (hiddenInset + a traffic-light position), so testing win32 covers both
// and additionally covers the min/max/close buttons, which are the thing a
// wrong overlay colour makes invisible.
//
// WHY FORCING IT IS SAFE. Not because the non-theme process.platform reads go
// undispatched — one of them does run here. stub.fireReady() below triggers
// app.whenReady, which calls buildAppMenu, whose `isMac` is a platform read. It
// is harmless because it only picks a MENU template and nothing in this file
// asserts on the menu: pinning that read to darwin's value while the platform
// stayed win32 left every assertion here passing. What actually holds the line
// is that a test FILE is its own process, so nothing outside it ever sees
// win32. main.mjs's other platform reads live in the window "close" and
// "window-all-closed" handlers, which this file never dispatches — but do not
// re-derive that from this comment. Re-derive it from main.mjs: an assertion
// added here that depends on the menu, or on either handler, makes the forcing
// load-bearing, and this paragraph will not have noticed.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { listenOnFreePort } from "./testing/ports.mjs";

// BEFORE main.mjs is imported — createWindow reads process.platform at call time
// but the module is loaded and the window created in one go below.
Object.defineProperty(process, "platform", { value: "win32", configurable: true });

register("./testing/electronLoader.mjs", import.meta.url);

// A private HOME, so nothing in this test can read or write the operator's real
// ~/.no_human.
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-theme-home-"));
fs.mkdirSync(path.join(home, ".no_human"));
fs.writeFileSync(path.join(home, ".no_human", ".env"),
  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-theme\n");
process.env.HOME = home;

// A live server so main.mjs ATTACHES instead of spawning a real nh.
const { server, origin: ORIGIN } = await listenOnFreePort((_q, s) => { s.end("[]"); });
process.env.NH_ORIGIN = ORIGIN;

const stub = await import("./testing/electronStub.mjs");
// A userData dir of THIS test's own. The stub's default is a fixed path under
// os.tmpdir() shared by every desktop test file and every past run — reading a
// persisted theme from there would make these assertions depend on run order.
const userData = fs.mkdtempSync(path.join(os.tmpdir(), "nh-theme-data-"));
stub.app.getPath = () => userData;

await import("./main.mjs");
stub.fireReady();
await new Promise((r) => setTimeout(r, 800));

test.after(() => {
  server.close();
  fs.rmSync(home, { recursive: true, force: true });
  fs.rmSync(userData, { recursive: true, force: true });
});

const themeFile = path.join(userData, "theme.json");
/** Contrast ratio (WCAG 2.x relative luminance), so "visible" is computed. */
const lin = (c) => (c /= 255, c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const lum = (hex) => {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
};
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};

test("the window pre-paints DARK on a light-mode OS, matching the board", () => {
  // Guard the guard: if the stub ever starts reporting a dark OS, the assertion
  // below passes for the wrong reason and pins nothing.
  assert.equal(stub.nativeTheme.shouldUseDarkColors, false,
    "this test is only meaningful while the stubbed OS reads as LIGHT");
  assert.equal(stub.BrowserWindow.last.opts.backgroundColor, "#0F1117",
    "the first frame is painted in backgroundColor before any content exists; "
    + "a light one flashes white around a board that is dark by default");
});

test("Electron's own chrome follows the APP's theme, not the OS's", () => {
  // themeSource is what colours the traffic lights, the native menus, and the
  // `prefers-color-scheme` that the shell's own token.html and error.html read.
  // Left at "system", those three stay light on a light-mode OS.
  assert.equal(stub.nativeTheme.themeSource, "dark",
    "main.mjs must set nativeTheme.themeSource, or only the pre-paint colour "
    + "is dark and every native surface around it is still the OS's");
});

test("the Windows min/max/close buttons stay visible on the dark overlay", () => {
  // On win32 `hiddenInset` degrades to a frameless window with no controls, so
  // the overlay IS the buttons. Wrong colours there leave a dead-looking strip.
  const { titleBarOverlay } = stub.BrowserWindow.last.opts;
  assert.ok(titleBarOverlay, "win32 must get a titleBarOverlay or it has no controls");
  assert.equal(titleBarOverlay.color, "#0F1117",
    "the overlay must match the window it sits on");
  assert.equal(titleBarOverlay.height, 40,
    "the overlay height changed. It is the win32 caption-button strip, carried "
    + "over unchanged from before this branch; nothing in the board's CSS pins "
    + "it, so re-check it against a real Windows window, not styles.css");
  const r = ratio(titleBarOverlay.symbolColor, titleBarOverlay.color);
  assert.ok(r >= 3, `the control glyphs are ${r.toFixed(2)}:1 on the overlay, `
    + "below the 3:1 of WCAG 1.4.11 — minimise/maximise/close read as a blank strip");
});

test("nh:set-theme ignores anything that is not one of the two themes", async () => {
  const handler = stub.calls.ipc.get("nh:set-theme");
  assert.ok(handler, "main.mjs must register nh:set-theme");
  for (const junk of ["purple", "", null, undefined, 0, { theme: "light" }, "../../etc"]) {
    const res = await handler({}, junk);
    assert.equal(res.ok, false, `accepted ${JSON.stringify(junk)} as a theme`);
  }
  assert.equal(fs.existsSync(themeFile), false,
    "a rejected value still created the state file");
  assert.equal(stub.nativeTheme.themeSource, "dark",
    "a rejected value still moved the app's theme");
});

test("choosing LIGHT on the board persists it and re-colours the live chrome",
  async () => {
    const handler = stub.calls.ipc.get("nh:set-theme");
    const win = stub.BrowserWindow.last;
    const res = await handler({}, "light");
    assert.equal(res.ok, true);
    assert.equal(res.saved, true, "accepted the choice but did not persist it");

    assert.equal(JSON.parse(fs.readFileSync(themeFile, "utf8")).theme, "light",
      "the choice must reach a file the MAIN process can read at next startup — "
      + "localStorage is unreadable before a window exists");
    assert.equal(stub.nativeTheme.themeSource, "light",
      "the native chrome kept the old theme while the board switched");
    assert.equal(win.overlay?.color, "#F4F5F7",
      "the win32 overlay is fixed at creation; without setTitleBarOverlay the "
      + "title bar keeps the old theme until the app is restarted");
    const r = ratio(win.overlay.symbolColor, win.overlay.color);
    assert.ok(r >= 3, `the control glyphs are ${r.toFixed(2)}:1 after the switch`);
  });

test("the NEXT launch pre-paints the stored choice, so light users get no flash",
  async () => {
    // This is the whole point of persisting: a naive always-dark pre-paint gives
    // the light-mode user a dark flash on every launch, which is the same defect
    // this branch fixes, merely aimed at a different user.
    const before = stub.BrowserWindow.last;
    before.isDestroyed = () => true;               // as after a real window close
    await stub.calls.handlers.get("activate")();
    await new Promise((r) => setTimeout(r, 300));

    const win = stub.BrowserWindow.last;
    assert.notEqual(win, before, "no new window was created — this test is vacuous");
    assert.equal(win.opts.backgroundColor, "#F4F5F7",
      "the stored LIGHT choice did not survive to the next launch's first frame");
    assert.equal(win.opts.titleBarOverlay.color, "#F4F5F7");
    assert.equal(win.opts.titleBarOverlay.symbolColor, "#1A1D23");
    assert.equal(stub.nativeTheme.themeSource, "light");
  });

test("the board actually CALLS the bridge — the chain has three links", () => {
  // Everything above drives nh:set-theme directly, so deleting the one line in
  // App.jsx that calls it, or the preload line that exposes it, leaves this
  // whole file green while nothing is ever persisted in the real app and the
  // light-mode user gets the flash back on every launch. There is no DOM here
  // (this repo unit-tests extracted logic and covers React through e2e), so the
  // two renderer links are pinned by source — anchored to the effect that owns
  // the theme, not matched anywhere in the file.
  const app = fs.readFileSync(new URL("../web/src/App.jsx", import.meta.url), "utf8");
  const at = app.indexOf('localStorage.setItem("nh-theme"');
  assert.ok(at > 0, "App.jsx no longer persists the theme where this expects — "
    + "the anchor is gone and the assertion below would search the whole file");
  const effect = app.slice(at, app.indexOf("}, [theme]);", at));
  assert.ok(effect.length > 0 && effect.length < 800,
    "the theme effect could not be delimited; this guard has gone blind");
  assert.match(effect, /nhDesktop\??\.\s*setTheme\??\.?\(\s*theme\s*\)/,
    "the theme effect does not tell the shell about the choice, so the main "
    + "process can never pre-paint anything but its default");

  const preload = fs.readFileSync(new URL("./preload.cjs", import.meta.url), "utf8");
  const channel = preload.match(/setTheme:[^\n]*invoke\(\s*"([^"]+)"/)?.[1];
  assert.ok(channel, "preload.cjs exposes no setTheme, so window.nhDesktop has "
    + "nothing for App.jsx to call");
  // Cross-check against what main ACTUALLY registered, rather than re-typing the
  // string here: a typo on either side is a silently dead bridge.
  assert.ok(stub.calls.ipc.has(channel),
    `preload.cjs invokes "${channel}", which main.mjs does not handle`);
});

test("switching back to dark persists dark — the default is not a one-way door",
  async () => {
    await stub.calls.ipc.get("nh:set-theme")({}, "dark");
    assert.equal(JSON.parse(fs.readFileSync(themeFile, "utf8")).theme, "dark");

    const before = stub.BrowserWindow.last;
    before.isDestroyed = () => true;
    await stub.calls.handlers.get("activate")();
    await new Promise((r) => setTimeout(r, 300));

    const win = stub.BrowserWindow.last;
    assert.notEqual(win, before, "no new window was created — this test is vacuous");
    assert.equal(win.opts.backgroundColor, "#0F1117");
  });
