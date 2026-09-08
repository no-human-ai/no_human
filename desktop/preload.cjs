// Minimal preload: contextIsolation is ON and nodeIntegration OFF, so the
// web app runs exactly as it does in a browser. The only bridge is a marker
// letting the UI know it's inside the desktop shell (used by e2e smoke and,
// later, notification dedupe in E3).
const { contextBridge, ipcRenderer } = require("electron");

// First-run credential screen (token.html) only. Deliberately separate from
// nhDesktop: the board never needs these, and the credential never crosses
// back — saveToken returns {ok} or {error}, never the value. `mode` selects
// the Claude billing path: "subscription" (default) or "api_key" (BYO Anthropic
// key). `openaiKey` is the OPTIONAL codex api_key credential — "" / omitted when
// the codex section is skipped or set to subscription (which stores nothing).
contextBridge.exposeInMainWorld("nhSetup", {
  saveToken: (value, mode, openaiKey) =>
    ipcRenderer.invoke("nh:save-token", value, mode, openaiKey),
  dismiss: () => ipcRenderer.invoke("nh:dismiss"),
  quit: () => ipcRenderer.invoke("nh:quit"),
  // First-run requirements check (claude/node on PATH) — see nh:requirements
  // in main.mjs for what it returns and why the setup screen needs it.
  requirements: () => ipcRenderer.invoke("nh:requirements"),
  // "Use my existing Claude Code sign-in" row: a read-only status probe,
  // safe to call on load, that only decides whether the row renders — see
  // nh:claude-signin-status in main.mjs. There is deliberately no matching
  // "import" call: minting a token is `claude setup-token`, an unconditional
  // browser OAuth flow (see main.mjs's comment above the deleted
  // nh:claude-import-token handler for the measured evidence), so it never
  // mints or transports a credential through this bridge — the click handler
  // in token.html is local, IPC-free, and only reveals the manual-paste
  // instructions.
  claudeSignInStatus: () => ipcRenderer.invoke("nh:claude-signin-status"),
});

// `npm_package_version` is set by `npm run`, and NOTHING sets it in a packaged
// app — this read was always the literal string "dev" in every shipped DMG.
// electron-builder writes the real version into the packaged package.json, so
// the next lines TRIED to read that and keep the env var as a dev fallback.
// (The update CHECK is unaffected either way — main.mjs runs it on
// app.getVersion(); this value is what the board DISPLAYS.)
// …EXCEPT that this preload runs SANDBOXED (Electron's default for a preload
// since 20), and a sandboxed preload's `require` cannot load "./package.json"
// — the read below threw, silently, in every packaged app, and the fallback
// "dev" is what the Settings > Updates card showed on a real Linux desktop
// (2026-08-18: "no_human dev"). The value therefore travels the documented
// way for sandboxed preloads: main.mjs passes `--nh-app-version=<app.getVersion()>`
// through webPreferences.additionalArguments and it is read off process.argv.
// The require stays only as a fallback (dead in every sandboxed run, dev
// included — main always passes the argument), then npm_package_version, then
// "dev" — and
// desktop/uiPages.test.mjs measures the argument path in a real sandboxed
// renderer, with a no-argument negative control.
let appVersion = process.env.npm_package_version || "dev";
try {
  appVersion = require("./package.json").version || appVersion;
} catch {
  /* sandboxed preload: expected — the argument below is the real source */
}
const versionArg = (process.argv || []).find((a) => a.startsWith("--nh-app-version="));
if (versionArg) {
  const v = versionArg.slice("--nh-app-version=".length).trim();
  if (v) appVersion = v;
}

contextBridge.exposeInMainWorld("nhDesktop", {
  shell: true,
  // The board's shell accommodations are SIDED: macOS window controls sit
  // top-left (traffic-light clearance), Windows' titleBarOverlay sits
  // top-right — without knowing which, the board's own top-right controls
  // (+ New Task) render UNDER the overlay, visible as a clipped sliver and
  // unclickable. Found by a user on the first Windows walkthrough.
  platform: process.platform,
  version: appVersion,
  // The application menu (main process) drives the board's own navigation:
  // main sends "nh:menu" with a page id ("board"/"stats"/"settings") or
  // "new-task"; App.jsx subscribes and updates its existing state. Returns an
  // unsubscribe so React effects can clean up.
  onMenu: (callback) => {
    const listener = (_event, action) => callback(action);
    ipcRenderer.on("nh:menu", listener);
    return () => ipcRenderer.removeListener("nh:menu", listener);
  },
  // Updates. The shell finds them; the board decides what to say and when the
  // user acts. `download` and `install` are separate on purpose — the operator
  // asked that users be informed and then choose, so nothing moves bytes or
  // restarts the app without a distinct click.
  onUpdate: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("nh:update", listener);
    return () => ipcRenderer.removeListener("nh:update", listener);
  },
  checkForUpdates: () => ipcRenderer.invoke("nh:update-check"),
  downloadUpdate: () => ipcRenderer.invoke("nh:update-download"),
  installUpdate: () => ipcRenderer.invoke("nh:update-install"),
  deferUpdate: (version) => ipcRenderer.invoke("nh:update-defer", version),
  // The push (onUpdate) misses every event fired before a renderer mounts —
  // the board's own notice needs this matching pull to catch up.
  getLastUpdate: () => ipcRenderer.invoke("nh:update-last"),
  // The board's light/dark choice, mirrored to the main process. It is the
  // renderer that owns the theme (localStorage), but only the main process can
  // colour the window frame and the Windows title-bar controls — and it has to
  // do that before any renderer exists, so it needs its own copy. Nothing flows
  // back but a receipt: the main side accepts "dark" or "light" and returns
  // `{ ok, saved }` — `ok` is "that was one of the two themes", `saved` is "it
  // reached disk". They differ: a read-only userData gives `ok:true` with
  // `saved:false`, and no caller reads `saved` today. See the residual list in
  // main.mjs's createWindow before treating this as fire-and-forget.
  setTheme: (theme) => ipcRenderer.invoke("nh:set-theme", theme),
  // "Open repo" (SlideOver.jsx) — hands a repo folder to the OS default
  // handler. main.mjs re-checks the path against /api/repos itself before
  // calling shell.openPath, so this bridge cannot be used to open an
  // arbitrary path even if the renderer were compromised.
  openPath: (p) => ipcRenderer.invoke("nh:open-path", p),
});
