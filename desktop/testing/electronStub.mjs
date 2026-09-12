// Minimal electron stand-in. Records the handlers main.mjs registers so a test
// can invoke them and observe real behaviour (preventDefault, quit, IPC).
import os from "node:os";
import path from "node:path";
export const calls = { quit: 0, exit: 0, handlers: new Map(), ipc: new Map(),
                       opened: [], openedPaths: [], nav: new Map(), badge: [],
                       sent: [], images: [], protocolClients: [] };
export function reset() {
  calls.quit = 0; calls.exit = 0; calls.handlers.clear(); calls.ipc.clear();
  calls.opened.length = 0; calls.openedPaths.length = 0; calls.nav.clear();
  calls.badge.length = 0; calls.sent.length = 0; calls.images.length = 0;
  calls.protocolClients.length = 0;
}
let readyResolve;
export const readyGate = new Promise((r) => { readyResolve = r; });
export function fireReady() { readyResolve(); }

export const app = {
  isPackaged: false,
  requestSingleInstanceLock: () => true,
  whenReady: () => readyGate,
  on: (event, fn) => { calls.handlers.set(event, fn); },
  quit: () => { calls.quit += 1; },
  exit: () => { calls.exit += 1; },
  getAppPath: () => process.cwd(),
  setBadgeCount: (n) => { calls.badge.push(n); },
  // The updater needs both: a version to compare against the feed, and a
  // directory to persist "Later" into.
  getVersion: () => "0.1.0",
  getPath: (name) => path.join(os.tmpdir(), `nh-stub-${name}`),
  // RECORDS the scheme AND the Windows exec/args form. A no-op double would
  // make the `nohuman://` registration untestable in exactly the way the
  // openExternal comment below warns about — deleting the call would ship an
  // app no deep link can reach, with the suite green.
  setAsDefaultProtocolClient: (scheme, execPath, args) => {
    calls.protocolClients.push({ scheme, execPath, args });
    return true;
  },
};
export const ipcMain = { handle: (ch, fn) => { calls.ipc.set(ch, fn); } };
// RECORDS what is handed to the OS: a no-op double made the scheme guard
// untestable, so deleting it survived every test.
export const shell = {
  openExternal: (u) => { calls.opened.push(u); },
  // RECORDS instead of touching the real filesystem/OS — a no-op double would
  // make the nh:open-path allow-list guard untestable in the same way the
  // comment above warns about for openExternal. Returns "" (Electron's
  // success sentinel: an empty string means no error).
  openPath: (p) => { calls.openedPaths.push(p); return Promise.resolve(""); },
};
export const nativeTheme = { shouldUseDarkColors: false };
// RECORDS the bytes and the template flag. A double that discarded both made
// trayIcon()'s platform routing untestable: deleting its win32 branch shipped
// the macOS template MASK on Windows — where setTemplateImage is a no-op, so
// the mask paints as its own black RGB, 1.29:1 on the dark taskbar — and every
// one of the 281 tests stayed green. Recording is what lets a test tell the two
// bitmaps apart.
export const nativeImage = {
  createFromBuffer(buffer, opts) {
    const img = {
      buffer,
      opts,
      templated: false,
      setTemplateImage(on = true) { img.templated = on; },
    };
    calls.images.push(img);
    return img;
  },
};
export const Menu = { buildFromTemplate: (t) => t, setApplicationMenu: () => {} };
export function Tray() {
  return { setToolTip() {}, setContextMenu() {}, on() {} };
}
export function BrowserWindow(opts) {
  const win = {
    loaded: [],
    loadGen: 0,
    webContents: {
      on(evt, fn) { calls.nav.set(evt, fn); },
      // RECORDS: a no-op send made every main→renderer message untestable, so
      // deleting the update notification would have kept the suite green.
      send(channel, payload) { calls.sent.push({ channel, payload }); },
      setWindowOpenHandler(fn) { calls.nav.set("window-open", fn); },
    },
    async loadURL(u) {
      // A test can hold this open so a LATER navigation supersedes it while it
      // is still in flight — the only way to exercise the supersede guards.
      if (BrowserWindow.slowLoadUrlMs) {
        const mine = ++win.loadGen;
        await new Promise((r) => setTimeout(r, BrowserWindow.slowLoadUrlMs));
        // Electron aborts an in-flight load when a newer one starts; the old
        // promise rejects with ERR_ABORTED. A double that resolves both would
        // report a collision that cannot happen — and hide one that can.
        if (mine !== win.loadGen) throw new Error(`ERR_ABORTED (-3) loading '${u}'`);
      }
      // Electron rejects loadURL when the server dies between probe and load.
      if (BrowserWindow.failLoadURL) {
        win.loaded.push(`url-failed:${u}`);
        throw new Error("ERR_CONNECTION_REFUSED (-102) loading '" + u + "'");
      }
      win.loaded.push(`url:${u}`);
    },
    async loadFile(f, opts) {
      win.loadGen += 1;                     // supersedes any in-flight loadURL
      // May be `true` (fail every file) or a filename substring, so a test can
      // fail token.html while letting the error page through — otherwise the
      // recovery is indistinguishable from the failure.
      const want = BrowserWindow.failLoadFile;
      if (want === true || (typeof want === "string" && f.includes(want))) {
        // path.basename, not split("/"): loadFile receives host-native paths
        // (main.mjs builds them with path.join), and on Windows the "/" split
        // passed the WHOLE backslashed path through — every basename assertion
        // in main*.test.mjs failed on the platform the app was being ported to.
        win.loaded.push(`file-failed:${path.basename(f)}`);
        throw new Error("ERR_FILE_NOT_FOUND (-6) loading '" + f + "'");
      }
      const q = opts?.query ?? {};
      const tag = q.reason ? `?${q.reason}` : (q.canReturn ? "?canReturn" : "");
      win.loaded.push(`file:${path.basename(f)}${tag}`);
    },
    // once() RECORDS: with a no-op double, deleting the ready-to-show reveal
    // was invisible — and that ships an app whose window never appears.
    once(evt, fn) { win.onceHandlers[evt] = fn; },
    onceHandlers: {},
    shown: 0,
    on() {}, show() { win.shown += 1; }, focus() {}, hide() {},
    // RECORDS: a no-op double would let the live theme re-colour be deleted
    // with the suite green, and the win32 title-bar controls would silently
    // keep the previous theme's colours until the app restarted.
    setTitleBarOverlay(o) { win.overlay = o; },
    isDestroyed: () => false, isMinimized: () => false, restore() {},
  };
  win.opts = opts || {};
  BrowserWindow.last = win;
  return win;
}
