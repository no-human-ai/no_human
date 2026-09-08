// no_human desktop shell — Phase E1 (attach-only).
//
// Points a native window at the LOCAL nh server (same-origin, so the web
// app's relative fetch / location.host WebSocket / SSE work untouched).
// External links (PR/CI URLs) open in the OS browser, never in-window.
// If the server isn't reachable, a friendly error page renders with retry —
// a blank window is the one unacceptable failure mode.

import { app, BrowserWindow, ipcMain, Menu, nativeImage, nativeTheme, shell, Tray } from "electron";
import { hasCredential, setAuthMode, validateOpenAiKey, writeCredential, writeOpenAiKey } from "./tokenStore.mjs";
import {
  isSetupUrl, claudeCredentialPaths, detectSignIn, redact,
} from "./setupGate.mjs";
import { restartFailedMessage } from "./setupUi.mjs";
import {
  saveAction, stateOnProbeUp,
} from "./serverOwnership.mjs";
import { createNavScheduler } from "./navScheduler.mjs";
import { createServerLifecycle } from "./serverLifecycle.mjs";
import { quitAction } from "./quitPolicy.mjs";
import { parseBadgeCount, overlayBadgeBitmap } from "./badge.mjs";
import { buildMenuTemplate } from "./menu.mjs";
import { docPage } from "./docRender.mjs";
import { createUpdater } from "./updater.mjs";
import { updateMessage } from "./updatePolicy.mjs";
import { readUpdateState, writeUpdateState } from "./updateState.mjs";
import { normalizeTheme, readTheme, themeColors, writeTheme } from "./themeState.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  DEFAULT_ORIGIN,
  claudeAuthStatus,
  ensureServer,
  forceStopServer,
  isAppOrigin,
  macClaudeKeychainExists,
  probe,
  resolveClaudeCli,
  resolveNodeBin,
  stopServer,
} from "./server.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ORIGIN = process.env.NH_ORIGIN || DEFAULT_ORIGIN;

// The spawn window ensureServer waits for the port before giving up (defaults to
// server.mjs's own 30s when unset). NH_SPAWN_TIMEOUT_MS is a TEST seam only — it
// lets a test reproduce a spawn-timeout in ~1s instead of the real 30, so the
// non-latching re-probe below can be exercised without a 30s hang. Unset in
// production, so the shipped app uses server.mjs's measured default unchanged.
const SPAWN_TIMEOUT_MS = process.env.NH_SPAWN_TIMEOUT_MS
  ? Number(process.env.NH_SPAWN_TIMEOUT_MS) : undefined;

// The non-latching re-probe's window and cadence (main.mjs part of the
// first-launch race fix). After a spawn-timeout error page is shown, keep
// probing for this long: a slow post-install boot can run well past the 30s
// spawn window (AV scan of the 44MB bundle + cold disk), and this is the extra
// tail that still flips it to the board on its own. NH_LATE_POLL_MS overrides
// the window for tests; the cadence matches waitForServer's own 500ms.
const LATE_POLL_MS = process.env.NH_LATE_POLL_MS
  ? Number(process.env.NH_LATE_POLL_MS) : 40000;
const LATE_POLL_INTERVAL_MS = 500;

let win = null;
// The ensure result lives in `lifecycle` (below) — ONE owner, because keeping a
// second copy here is how a spawned child got overwritten and orphaned.
let tray = null;
let quitting = false;

// 16x16 template circle, GENERATED + verified by scripts (review finding:
// the previous hand-typed base64 was corrupt — invisible tray, dead feature).
// desktop/trayIcon.test.mjs decodes + inflates it so corruption can't return.
//
// THIS ONE IS A macOS TEMPLATE MASK: three colours only (transparent, black,
// antialiased black). macOS reads the ALPHA and paints the glyph itself, so a
// black mask is correct there and only there — see trayIcon().
export const TRAY_ICON_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAARUlEQVR42mNgoCHoQcNkafwP" +
  "xUQbhK4RHeM1hJBmgoYQoxnZELJsx+kKUjRjdQVVDKDICxQHIlWikeKERJWkTJXMRBIAAFkt" +
  "fBGd64lBAAAAAElFTkSuQmCC";

// The WINDOWS tray glyph. Same 16x16 silhouette as the mask above, but with
// real colours, because `setTemplateImage` is macOS-ONLY and Windows renders
// the PNG's own RGB. Shipping the mask on Windows put a black-on-near-black
// circle in the notification area: measured 1.29:1 against the Windows dark
// taskbar (#202020), with ZERO opaque pixels reaching even 3:1. That is a
// blocker on this branch and not a cosmetic one — close now hides to the tray
// on Windows, and docs/WINDOWS.md §7 records that the application menu bar is
// unreachable there, so the tray menu is the only reachable Quit. A dark-mode
// user closed the window and was hunting an invisible icon.
//
// TWO-TONE ON PURPOSE, rather than simply inverting to a light glyph. The
// taskbar is not always dark: a plain white circle would have scored ~1.1:1 on
// the LIGHT taskbar (#F3F3F3) and merely moved the same defect to the other
// theme. A white core carries dark mode (16.29:1) and a near-black rim carries
// light mode (15.68:1), so one static bitmap is legible on both. Deliberately
// NOT switched on nativeTheme.shouldUseDarkColors: on Windows that follows the
// APPS theme (AppsUseLightTheme) while the taskbar follows the SYSTEM theme
// (SystemUsesLightTheme), and a "Custom" theme decouples them — the switch
// would pick confidently and wrongly. trayIcon.test.mjs pins both ratios.
export const TRAY_ICON_WIN_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAhUlEQVR42rVTwQkAIQxzjkzg" +
  "0yX8O4ILOYPbuMaN0qNFoYiciL1AQaqJTa3O/QUAAUDuEU6ICcADgKbgXNqR6yDEGKmUIsFr" +
  "JVS/bibvPbXWaAbneK+LpJWAlL0ia5FhZ9UwKXUHZSdoAe60+N2Bz3SBbCpwZ+G6iSbPeD1I" +
  "JqNs8plO8QIbHVhQosLM3wAAAABJRU5ErkJggg==";

// Exported ONLY so trayIconRouting.test.mjs can call it: the asset is pinned by
// trayIcon.test.mjs, but nothing exercised the ROUTING, so deleting the win32
// branch below put the mask back on Windows with all 281 tests green.
export function trayIcon() {
  // Windows AND Linux: hand them the real-colour glyph and do NOT call
  // setTemplateImage — it is a no-op off macOS, so the mask would render as
  // its own black RGB. Only darwin wants the template (the menu bar recolours
  // it for light/dark/highlight). Linux was left on the mask until 2026-08-18
  // ("unverified there"); it is now routed by !== "darwin" so it cannot inherit
  // the wrong bitmap by omission, and docs/LINUX.md records what the tray
  // actually looked like on GNOME/KDE once walked.
  if (process.platform !== "darwin") {
    return nativeImage.createFromBuffer(Buffer.from(TRAY_ICON_WIN_B64, "base64"));
  }
  const img = nativeImage.createFromBuffer(Buffer.from(TRAY_ICON_B64, "base64"));
  img.setTemplateImage(true);
  return img;
}

export function serverLabel() {
  if (!lifecycle.state) return "server: probing…";
  return {
    attached: "server: attached (operator-owned)",
    spawned: "server: spawned by the app",
    failed: `server: unreachable (${lifecycle.state.reason})`,
  }[lifecycle.state.status] ?? "server: unknown";
}

function buildTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip("no_human");
  const rebuild = () => tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Open no_human", click: showWindow },
    { label: serverLabel(), enabled: false },
    { type: "separator" },
    { label: "Quit no_human", click: () => { quitting = true; app.quit(); } },
  ]));
  rebuild();
  tray.on("click", showWindow);
  // Keep the server line fresh when the menu is about to show (macOS builds
  // the menu lazily, so rebuilding on click keeps it truthful).
  tray.on("mouse-enter", rebuild);
}

function showWindow() {
  if (win && !win.isDestroyed()) {
    win.show();
    win.focus();
  } else {
    createWindow();
  }
}

// A native application menu — the operator-facing keyboard surface (view
// navigation + New Task + reload/zoom + dev-only devtools). Nav items post to
// the renderer over IPC ("nh:menu"); the preload forwards them to the board's
// own page state (App.jsx), so the menu drives the SAME UI, not a second one.
function sendToRenderer(action) {
  showWindow(); // a menu action on a tray-hidden window should surface it first
  if (win && !win.isDestroyed()) win.webContents.send("nh:menu", action);
}

// ------------------------------- updates -------------------------------- //
//
// Whether this build may install its own updates is decided at BUILD time, not
// here: `process.env.CSC_LINK` is meaningless inside a shipped app, so
// electron-builder.config.cjs stamps the answer into the packaged
// package.json. Reading the live environment instead would let any user turn
// on an update path macOS will refuse.
export function packagedSigning() {
  try {
    const pkg = JSON.parse(
      fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));
    return {
      mode: pkg.nhSigning || "unsigned",
      // Strict true, so a missing or malformed field fails CLOSED.
      canAutoUpdate: pkg.nhCanAutoUpdate === true,
    };
  } catch {
    return { mode: "unsigned", canAutoUpdate: false };
  }
}

let updater = null;

// electron-updater is imported LAZILY and never at module scope: it touches
// app.getVersion() and app-update.yml on import, which throws in an unpackaged
// run and makes main.mjs unimportable under the test loader.
async function getUpdater() {
  if (updater) return updater;
  let autoUpdater;
  try {
    const mod = await import("electron-updater");
    autoUpdater = (mod.default ?? mod).autoUpdater;
  } catch (err) {
    console.error("electron-updater unavailable:", (err && err.message) || err);
    return null;
  }
  if (!autoUpdater) return null;
  const dir = app.getPath("userData");
  updater = createUpdater({
    autoUpdater,
    plan: packagedSigning(),
    currentVersion: app.getVersion(),
    isPackaged: app.isPackaged,
    readState: () => readUpdateState(dir),
    writeState: (s) => writeUpdateState(dir, s),
    onEvent: (payload) => sendUpdateEvent(payload),
    log: (m) => console.log(`[update] ${m}`),
  });
  updater.configure();
  return updater;
}

// The board renders the notice; the shell never puts a modal in the way. An
// update is information, not an interruption.
function sendUpdateEvent(payload) {
  if (win && !win.isDestroyed()) {
    win.webContents.send("nh:update", {
      ...payload,
      message: updateMessage({
        mode: payload.mode, latest: payload.latest,
        current: payload.current ?? app.getVersion(),
        canAutoUpdate: payload.canAutoUpdate,
      }),
    });
  }
}

async function checkForUpdates({ manual = false } = {}) {
  const u = await getUpdater();
  if (!u) {
    if (manual) {
      sendUpdateEvent({ mode: "failed", rawError: null,
        error: "The updater component is not available in this build." });
    }
    return null;
  }
  // Never allowed to reject: a failed update check must not surface as a
  // startup error or an unhandled rejection.
  return u.check({ manual }).catch((err) => {
    console.error("update check failed:", (err && err.message) || err);
    return null;
  });
}

// The SECOND route to documentation, not the only one: the bundle ships its
// own docs as `extraResources` (`../docs/quickstart.md` and
// `../docs/configuration.md` → `Contents/Resources/docs/`, declared in
// electron-builder.config.cjs), and `bundledDoc()` below resolves them under
// `process.resourcesPath` with a repo-relative fallback for dev runs. Help
// offers the bundled copy first and this URL second — see menu.mjs.
//
// CANONICAL, not `/docs.html` — that form only reaches the page through a 307,
// and the site's own markup links `/docs` in all five places. A redirect is a
// thing someone eventually retires.
const DOCS_URL = "https://getnohuman.com/docs";

/** Absolute path to a doc shipped inside the bundle, or null when unpackaged.
 *
 * `extraResources` land in `Contents/Resources`, which is `process.resourcesPath`
 * in a packaged app. In a dev run there is no bundle, so the repo's own `docs/`
 * is used — otherwise the menu item would be dead for every developer and the
 * one person able to notice it broken would never see it.
 */
// The native macOS About panel (⌘-triggered via the app menu's "About
// no_human", and shown by app.showAboutPanel()). Content is truthful and
// matches the site: what it is, that it never merges, and the site. A contact
// email is deliberately absent until the operator signs one off — the term
// gate refuses the personal address in shipped content, and it is not this
// code's call to override that.
function setupAboutPanel() {
  try {
    app.setAboutPanelOptions({
      applicationName: "no_human",
      applicationVersion: app.getVersion(),
      credits:
        "A team of AI agents that works your tickets to reviewed pull requests.\n" +
        "It opens PRs — it never merges. You approve.\n\n" +
        "getnohuman.com",
      copyright: "© no_human",
    });
  } catch (err) {
    console.error("about panel failed:", err);
  }
}

function bundledDoc(name) {
  const base = app.isPackaged
    ? path.join(process.resourcesPath, "docs")
    : path.join(__dirname, "..", "docs");
  const p = path.join(base, `${name}.md`);
  return fs.existsSync(p) ? p : null;
}

function buildAppMenu() {
  const template = buildMenuTemplate({
    isMac: process.platform === "darwin",
    isDev: !app.isPackaged,
    onNavigate: (page) => sendToRenderer(page),
    onNewTask: () => sendToRenderer("new-task"),
    onCheckForUpdates: () => {
      showWindow();
      checkForUpdates({ manual: true });
    },
    onReenterToken: async () => {
      showWindow();
      if (win && !win.isDestroyed()) {
        await openSetup(win).catch((err) =>
          console.error("setup screen failed:", err));
      }
    },
    // The only route back into the first-run wizard. The flag lives in the
    // server's config, not here, so this is a POST and then a RELOAD: the board
    // reads onboarding status once, at load (web/src/App.jsx), so a reset
    // without the reload changes nothing the user can see. Reload happens even
    // if the POST failed — loadBoardOrError probes first and renders the error
    // page when the server is down, which is the honest thing to show.
    onRerunSetup: async () => {
      showWindow();
      try {
        const res = await fetch(`${ORIGIN}/api/onboarding/reset`, { method: "POST" });
        if (!res.ok) console.error("setup reset failed:", res.status);
      } catch (err) {
        console.error("setup reset failed:", String((err && err.message) || err));
      }
      if (win && !win.isDestroyed()) loadBoardOrError(win);
    },
    // Routed through openExternallyIfWeb, NOT shell.openExternal directly. That
    // helper is the one place this process hands a URL to the OS, and it
    // refuses anything that is not http(s); going around it for a URL that
    // "is obviously fine" is how that guard stops being the one place.
    //
    // It points at the site rather than the GitHub repository on purpose: the
    // repository is private until the operator makes it public, and a link that
    // 404s for every user is worse than no link at all. Revisit when it opens.
    onOpenDocs: (which) => {
      if (which === "site") return openExternallyIfWeb(DOCS_URL);
      // Falls back to the site when the bundled copy is missing rather than
      // doing nothing: a Help item that silently no-ops is worse than one that
      // opens the wrong-ish page, because the user cannot tell it tried.
      const p = bundledDoc("quickstart");
      // RENDER the bundled markdown in-app rather than shell.openPath(p): the
      // OS opens a `.md` in whatever owns that extension (TextEdit → raw
      // markdown), which is what the user saw. openDocWindow keeps it offline
      // and readable. Falls back to the site if the bundled copy is missing.
      return p ? openDocWindow(p, "no_human Quickstart") : openExternallyIfWeb(DOCS_URL);
    },
    onShowAbout: () => (app.showAboutPanel ? app.showAboutPanel() : null),
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// Render a bundled markdown doc into a self-contained, offline HTML page and
// show it in its own frameless-menu BrowserWindow. Falls back to the site if
// the file cannot be read. In test mode it records what it would render
// (rendered=<bytes>) instead of opening a window, so a test can prove the
// quickstart is RENDERED and never handed to the OS as raw `.md`.
function openDocWindow(mdPath, title) {
  let md;
  try {
    md = fs.readFileSync(mdPath, "utf8");
  } catch {
    return openExternallyIfWeb(DOCS_URL);
  }
  const html = docPage(title, md);
  if (process.env.NH_TEST_LOG) {
    fs.appendFileSync(
      process.env.NH_TEST_LOG,
      `openDoc ${path.basename(mdPath)} rendered=${html.length}\n`,
    );
    return;
  }
  const win = new BrowserWindow({
    width: 860,
    height: 780,
    title,
    backgroundColor: "#0e1320",
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true },
  });
  if (win.removeMenu) win.removeMenu();
  // In-doc anchors stay in the window; any http(s) link goes to the OS browser
  // through the one guarded helper, never straight to the shell.
  win.webContents.setWindowOpenHandler(({ url }) => {
    openExternallyIfWeb(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    if (/^https?:\/\//i.test(url)) {
      event.preventDefault();
      openExternallyIfWeb(url);
    }
  });
  win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
}

// Defense-in-depth (review finding): only ever hand web schemes to the OS —
// the board sanitizes its links today, but the shell must not TRUST that.
function openExternallyIfWeb(url) {
  if (!/^https?:\/\//i.test(url)) return;
  if (process.env.NH_TEST_LOG) {
    // Test mode observes routing without side effects (the smoke must not
    // actually open browser tabs on the operator's machine).
    import("node:fs").then((fs) =>
      fs.appendFileSync(process.env.NH_TEST_LOG, `openExternal ${url}\n`));
    return;
  }
  shell.openExternal(url);
}

function routeExternally(contents) {
  // target="_blank" (Board/SlideOver PR links) → OS browser, no child window.
  contents.setWindowOpenHandler(({ url }) => {
    if (!isAppOrigin(url, ORIGIN)) openExternallyIfWeb(url);
    return { action: "deny" };
  });
  // ONE will-navigate handler owns everything (review finding: a second
  // global handler double-fired on nh://retry and leaked a bogus
  // shell.openExternal to the OS on every Retry click).
  contents.on("will-navigate", async (event, url) => {
    if (url.startsWith("nh://retry")) {
      event.preventDefault();
      // Retry is a plain link and can be clicked repeatedly; each accepted
      // click is a full ensureServer AND another spawned nh, so it runs only
      // when idle (navScheduler owns that rule, and tests it).
      // Through loadBoardOrError so lifecycle.state is updated: loading the URL
      // directly left a stale "failed" state and the tray read
      // "server: unreachable" while the board was live.
      if (win && !win.isDestroyed()) {
        // May reject (nav timeout); an async listener must not leak that.
        await (navs.scheduleIfIdle(win) ?? Promise.resolve())
          .catch((err) => console.error("retry failed:", err));
      }
      return;
    }
    if (url.startsWith("nh://token")) {
      event.preventDefault();
      if (win && !win.isDestroyed()) {
        await openSetup(win).catch((err) =>
          console.error("setup screen failed:", err));
      }
      return;
    }
    if (!isAppOrigin(url, ORIGIN)) {
      event.preventDefault();
      openExternallyIfWeb(url);
    }
  });
}

// Server ownership + restart live in serverLifecycle.mjs so they can be driven
// under `node --test` without electron — both of the last two blocking defects
// were in this code while it was inline and untested.
const lifecycle = createServerLifecycle({
  probe: () => probe(ORIGIN),
  stopServer,
  forceStopServer,
  sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
});
const rememberChild = (child) => lifecycle.track(child);
const stopAllOwned = () => lifecycle.stopAll();

// Navigations are SERIALIZED and supersedable — see navScheduler.mjs, which
// owns both behaviours and is unit-tested (this logic went wrong twice inline).
const navs = createNavScheduler((w, isCurrent) => _loadBoardOrError(w, isCurrent));
const loadBoardOrError = (w) => navs.schedule(w);
const supersedeNav = () => navs.supersede();

const showError = (w, reason, detail) => w.loadFile(path.join(__dirname, "error.html"), {
  query: {
    origin: ORIGIN, reason, packaged: app.isPackaged ? "1" : "0",
    ...(detail ? { detail } : {}),
  },
});

/**
 * Load the board, and never leave a BLANK window if that fails. A probe can
 * answer moments before the server dies (see restartOwnServer's note), and a
 * rejected loadURL both blanks the window and throws an unhandled rejection in
 * the main process — "a blank window is the one unacceptable failure mode".
 */
async function showBoard(w) {
  try {
    await w.loadURL(ORIGIN, { extraHeaders: "Cache-Control: no-cache" });
    return true;
  } catch (err) {
    const msg = String((err && err.message) || err);
    // ERR_ABORTED means a NEWER load replaced this one — opening the credential
    // screen over an in-flight board load does exactly that. It is not a
    // failure, and rendering the error page here would paint over the screen the
    // user just moved to.
    if (/ERR_ABORTED/.test(msg)) return false;
    console.error("board load failed:", msg);
    // The fallback can fail too (window torn down mid-load); swallowing that is
    // correct — there is nothing left to render onto.
    try { await showError(w, "load-failed"); } catch { /* window is gone */ }
    return false;
  }
}

async function _loadBoardOrError(w, current) {
  // Probe BEFORE anything else: a server the operator already started is
  // authoritative, and it may hold its token in its own environment rather than
  // in ~/.no_human/.env. Prompting there would nag about a credential while a
  // healthy board sits one probe away.
  // heldStopFailed(): we are holding a server we could not stop, so a probe-up
  // is THAT server, still on the OLD token. Falling through to the restart path
  // means a Retry genuinely re-attempts the stop instead of quietly attaching.
  if ((await probe(ORIGIN)) === "up" && !lifecycle.heldStopFailed()) {
    if (!current()) return;           // don't publish state for a dead nav
    // Keeps a spawned state (and its child) intact — see serverOwnership.
    lifecycle.state = stateOnProbeUp(lifecycle.state);
    await showBoard(w);
    return;
  }
  // A first launch has no credential, and `nh start` exits rather than boot
  // without one — so the board (and its web onboarding) is unreachable until a
  // token exists. Ask for it natively: pointing the operator at a terminal, as
  // error.html does, is a dead end in a packaged app.
  if (!hasCredential()) {
    if (!current()) return;
    await showSetup(w);
    return;
  }
  // E2: spawn `nh start --no-open` and wait. Failure renders error.html —
  // never blank.
  // Nothing below is free: ensureServer boots a real server (SQLite, workers,
  // a port bind). A nav that is already superseded must not do it — the
  // discarded process went on to win the port with the OLD token and dead-ended
  // the credential screen.
  if (!current()) return;
  // At most ONE owned server. A previous attempt's child is still running (and
  // still failing), so starting another just adds a process racing for the same
  // port — repeated Retry accumulated live servers. Stop it and wait for the
  // port before replacing it.
  if (lifecycle.ownsAny() && !(await restartOwnServer())) {
    // We could not stop our own server. Falling through to ensureServer here
    // attached to it, so the board ran on the OLD token, the tray called it
    // "operator-owned", and the handle was gone — unstoppable forever.
    // Keeps the child — see serverLifecycle.failedStopState().
    lifecycle.state = lifecycle.failedStopState();
    if (!current()) return;
    await showError(w, lifecycle.state.reason);
    return;
  }
  if (!current()) return;             // the stop above can take up to 20s
  // Register at SPAWN, not on return: for the ~20s until ensureServer resolves
  // the child was in no registry, so nothing could stop it and a token save
  // reported success over a server holding the old credential.
  const result = await ensureServer({
    origin: ORIGIN,
    spawnTimeoutMs: SPAWN_TIMEOUT_MS,   // undefined → server.mjs's measured default
    onSpawn: (child) => rememberChild(child),
  });
  if (!current()) return;             // superseded while we waited
  lifecycle.state = result;
  if (lifecycle.state.status !== "failed") {
    await showBoard(w);
    return;
  }
  await showError(w, lifecycle.state.reason, lifecycle.state.detail);
  // A plain spawn-timeout is the ONE failure that self-heals: ensureServer
  // deliberately leaves the slow-booting child alive ("may still be about to win
  // the port"), so keep probing in the background and swap the board in the
  // moment it answers — instead of latching this error page until the user
  // clicks Retry. Every OTHER reason is a real, non-self-healing failure
  // (spawn-error / backend-cli-missing / backend-not-logged-in / backend-exited /
  // nh-not-found / stop-failed), so those are NOT polled: the error page is the
  // final word. Not awaited — the error page is already painted and the nav must
  // complete; the poll carries `current()` so it stands down the instant the
  // user navigates away (credential screen, Retry).
  if (lifecycle.state.reason === "spawn-timeout") {
    pollForLateServer(w, current).catch((err) =>
      console.error("late-server re-probe failed:", (err && err.message) || err));
  }
}

/**
 * After a spawn-timeout error page, re-probe the SAME origin on a bounded window
 * and load the board the first time it answers. This is the non-latching half of
 * the first-launch race fix: the child ensureServer left running may be a slow
 * boot (measured ~17.4s cold; a post-install AV scan pushes it past the 30s
 * spawn window), and without this the user sits on an error page for a server
 * that is seconds from ready.
 *
 * It obeys the SAME supersession contract as _loadBoardOrError: `current()`
 * going false (Retry, or the credential screen opening over the board) or the
 * window being torn down stops the poll WITHOUT painting over whatever replaced
 * it — the guard is re-checked after every await, because both can happen while
 * a probe or a load is in flight. Deps are injectable so the flap/teardown/
 * timeout paths are unit-testable without a real server (see
 * mainLatePoll.test.mjs); production uses the module defaults.
 */
export async function pollForLateServer(w, current, {
  probeOrigin = () => probe(ORIGIN),
  loadBoard = showBoard,
  windowMs = LATE_POLL_MS,
  intervalMs = LATE_POLL_INTERVAL_MS,
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
} = {}) {
  const deadline = Date.now() + windowMs;
  while (Date.now() < deadline) {
    await sleep(intervalMs);
    // Superseded or torn down while we slept: stand down. Painting now would
    // clobber the screen the user moved to (e.g. the credential screen).
    if (!current() || w.isDestroyed()) return;
    if ((await probeOrigin()) !== "up") continue;
    // Re-check across the probe await — the user may have navigated away in it.
    if (!current() || w.isDestroyed()) return;
    // Keep the spawned child (ownership is what makes it stoppable at quit);
    // correct only the stale "failed (spawn-timeout)" status. See stateOnProbeUp.
    lifecycle.state = stateOnProbeUp(lifecycle.state);
    // A load can still fail if the port flapped down between probe and load —
    // showBoard renders load-failed rather than blanking, and we keep probing so
    // a genuine late boot is not abandoned on a single flap. A supersession
    // during the load returns false too, so re-check the guard before looping.
    if (await loadBoard(w)) return;
    if (!current() || w.isDestroyed()) return;
  }
}

const SETUP_FILE = path.join(__dirname, "token.html");

/**
 * Stop the server this shell started and WAIT for the port to actually free.
 * Killing and probing immediately raced a graceful shutdown: the probe still
 * answered, so the shell reported "Connected" and attached to a server that
 * died moments later. Returns false if it never goes down.
 */
const restartOwnServer = (timeoutMs = 20000) => lifecycle.restart(timeoutMs);

/**
 * Open the credential screen, deciding for itself whether a board is reachable
 * behind it. BOTH entry points (the File menu and nh://token) go through here:
 * when nh://token hardcoded canReturn:false, Escape quit the app even though a
 * board was live — N1's failure reached through the other door.
 */
async function openSetup(w) {
  const up = (await probe(ORIGIN)) === "up";
  supersedeNav();      // an in-flight nav must not paint over this screen
  // Deliberately OFF the nav queue: queueing this behind a 20s ensureServer
  // would make the menu item feel dead. The cost is that it can cancel an
  // in-flight loadURL, which rejects with ERR_ABORTED — expected, not an error.
  try {
    await showSetup(w, { canReturn: up });
  } catch (err) {
    const msg = String((err && err.message) || err);
    if (/ERR_ABORTED/.test(msg)) return;     // superseded — something newer paints
    // The credential screen itself failed to load. token.html HAS been missing
    // from app.asar before, and both callers only log, so rethrowing here left
    // a BLANK window — the one unacceptable failure mode. Give this door the
    // same net the startup path has. (Retry on that page re-opens this screen,
    // which is a legitimate retry rather than a dead end.)
    console.error("setup screen failed:", msg);
    await showError(w, "setup-failed").catch(() => {});
  }
}

/**
 * Show the credential screen. `canReturn` tells it whether a board exists
 * behind it: on genuine first run there is nothing to dismiss to and the
 * secondary action quits, but when reached from File > Re-enter Claude Token
 * over a working board, quitting would tear down the app (and stop a
 * shell-spawned server) on one unconfirmed keystroke.
 */
async function showSetup(w, { canReturn = false } = {}) {
  await w.loadFile(SETUP_FILE, canReturn ? { query: { canReturn: "1" } } : {});
}

/**
 * The setup screen shares ONE BrowserWindow (and therefore one preload) with
 * the board, so `window.nhSetup` is reachable from every page the server
 * renders. Gate on the sender actually BEING the local setup file: without
 * this, anything injected into the board could overwrite the operator's token
 * or quit the app. Same standard as openExternallyIfWeb — the shell does not
 * trust the board's content.
 */
function fromSetupScreen(event) {
  return isSetupUrl(event.senderFrame?.url ?? "", SETUP_FILE);
}

// Shared "a credential just changed" tail for nh:save-token, the only
// entry point that ever writes a credential (see the removed
// nh:claude-import-token note below): decide whether the already-running
// server needs a restart (it read its token once at bootstrap, so writing a
// new one is otherwise inert until it restarts), restart it if so, then
// navigate back to the board.
// ownsAnything covers a boot still in flight: saveAction on lifecycle.state
// alone saw "nothing running" mid-boot and returned "proceed", so the user
// was told "Connected" over a server started with the old token. ONE
// predicate drives both the branch and the message below — deriving them
// from different sources is what produced the last two blockers, in mirror
// image.
async function finishSave() {
  const alive = lifecycle.ownsAnyAlive();
  const action = saveAction(lifecycle.state, (await probe(ORIGIN)) === "up", alive);
  if (action === "needs-restart") {
    return { ok: false, needsRestart: true, error:
      "Saved. The no_human server was already running and still holds the old " +
      "token — restart it (quit `nh start` and run it again) to use the new one." };
  }
  if (action === "restart" && !(await restartOwnServer())) {
    // Same `alive` that chose the branch — see ownsAnyAlive().
    return { ok: false, error: restartFailedMessage(ORIGIN, alive) };
  }
  try {
    if (win && !win.isDestroyed()) await loadBoardOrError(win);
  } catch (err) {
    // Never leave the setup screen wedged on "Saving…": report and let the
    // user retry rather than stranding a disabled button.
    return { ok: false, error: `Saved, but the board did not open: ${err.message}` };
  }
  // The nav ran, but "ran" is not "started". Reporting ok here painted
  // "Connected. Opening no_human…" over an error page.
  if (lifecycle.state?.status === "failed") {
    return { ok: false, error:
      `Saved, but the server did not start (${lifecycle.state.reason}).` };
  }
  return { ok: true };
}

// token.html -> main. Returns {ok} or {error}; the value is never echoed back
// and never logged. `mode` is the credential type the operator selected —
// anything but the explicit "api_key" opt-in is treated as the default, so a
// stale renderer that omits it keeps today's behaviour.
ipcMain.handle("nh:save-token", async (event, value, mode, openaiKey) => {
  if (!fromSetupScreen(event)) return { ok: false, error: "not permitted" };
  const m = mode === "api_key" ? "api_key" : "subscription";
  // The OPTIONAL codex api_key credential. "" (or omitted) means the codex
  // section was skipped or set to subscription — which stores NOTHING (#6b), so
  // writeOpenAiKey is never reached on that path.
  const oaiKey = typeof openaiKey === "string" ? openaiKey.trim() : "";
  try {
    // Validate the OpenAI key BEFORE writing anything: an invalid optional key
    // must not leave the required Claude credential half-saved. Nothing is
    // written for OpenAI unless a non-empty key was supplied.
    if (oaiKey) {
      const oaiErr = validateOpenAiKey(oaiKey);
      if (oaiErr) return { ok: false, error: oaiErr };
    }
    // Credential first: if it fails validation, the configured mode is
    // untouched and the screen still matches config.yaml.
    writeCredential(value, m);
    setAuthMode(m);
    // Claude is required and now saved; write the optional OpenAI key too. Its
    // preserving write leaves the Claude token and every other .env line alone.
    if (oaiKey) writeOpenAiKey(oaiKey);
  } catch (err) {
    // redact() so a pasted credential can never ride out in an error string.
    return { ok: false, error: redact(err.message, value) };
  }
  return finishSave();
});

// token.html -> main. AC1 (existing-credential detection): does this machine
// already have a Claude Code sign-in? Read-only/idempotent — this handler
// never writes anything and never runs `claude setup-token`; it only decides
// whether the "Use my existing Claude Code sign-in" button should render.
// Same sender gate as every other setup-only channel; fails closed (button
// stays hidden) on any refusal or error, which is exactly today's screen.
ipcMain.handle("nh:claude-signin-status", async (event) => {
  if (!fromSetupScreen(event)) return { detected: false };
  const cliPath = await resolveClaudeCli();
  const authStatusCode = cliPath ? await claudeAuthStatus(cliPath) : null;
  // macOS has no credential FILE to check (see setupGate.mjs's
  // claudeCredentialPaths note) — its store is the login Keychain, checked
  // via macClaudeKeychainExists instead of fs.existsSync.
  const storeExists = process.platform === "darwin"
    ? await macClaudeKeychainExists()
    : claudeCredentialPaths(process.platform, process.env, os.homedir())
      .some((p) => fs.existsSync(p));
  return detectSignIn({ cliPath, authStatusCode, storeExists });
});

// There is deliberately no "nh:claude-import-token" (or any other) IPC
// channel that runs `claude setup-token`. MEASURED (macOS, `claude` 2.1.263,
// `auth status` already loggedIn:true): `setup-token` is unconditionally
// browser OAuth with a loopback `/callback`, exposes no non-interactive flag,
// and under the piped/non-TTY stdio this app would have to use writes NOTHING
// to stdout/stderr while the listener is up, and a bounded child only ever
// ends `{killed:true, code:143}` with both streams empty — so no token could
// ever be read back.
// A Windows 11 field report (no_human build bd70645a): 3 of 3 clicks opened
// a `localhost:<port>/callback` tab and a 10-second probe found nothing
// listening on that port (ERR_CONNECTION_REFUSED). See server.mjs's
// claudeAuthStatus doc comment for the full evidence. The only supported
// path to a subscription token is the user
// running `claude setup-token` themselves in a terminal and pasting the
// result into nh:save-token above — see setupUi.mjs's existingSignInCopy().

// token.html -> main. First-run requirements check: the Agent SDK shells out
// to `claude` for EVERY task (agent/backend_check.py) and no_human never
// passes cli_path, so a first run that only asked for a token used to die on
// its first task with the setup screen looking done. Same gate as
// nh:save-token — board content must never be able to probe what's on this
// machine's PATH. Never throws and never logs the resolved paths' contents
// (they are filesystem paths, not credentials, but kept out of logs anyway —
// same discipline as the token this screen also collects).
ipcMain.handle("nh:requirements", async (event) => {
  if (!fromSetupScreen(event)) return { ok: false, error: "not permitted" };
  const claudePath = await resolveClaudeCli();
  const nodePath = await resolveNodeBin();
  const version = claudePath ? await new Promise((resolve) => {
    execFile(claudePath, ["--version"], { timeout: 5000 }, (err, stdout) =>
      resolve(err ? "" : stdout.trim()));
  }) : "";
  return {
    claude: { ok: !!claudePath, path: claudePath, version },
    node: { ok: !!nodePath, path: nodePath },
  };
});

// Dismiss back to the board — only meaningful when one is reachable.
ipcMain.handle("nh:dismiss", async (event) => {
  if (!fromSetupScreen(event)) return false;
  if (win && !win.isDestroyed()) await loadBoardOrError(win);
  return true;
});

ipcMain.handle("nh:quit", (event) => {
  if (!fromSetupScreen(event)) return false;
  quitting = true;
  app.quit();
  return true;
});

// Update IPC. Deliberately NOT gated by fromSetupScreen: that predicate admits
// only token.html, and these are driven by the BOARD. They are safe to expose
// there because none of them accepts a URL, a path, or a credential — the feed
// is fixed at build time and the only inputs are "yes", "later", and "restart".
ipcMain.handle("nh:update-check", async () => {
  const r = await checkForUpdates({ manual: true });
  return r ?? { mode: "failed", error: "the updater is unavailable" };
});

ipcMain.handle("nh:update-download", async () => {
  const u = await getUpdater();
  if (!u) return { mode: "failed", error: "the updater is unavailable" };
  return u.download();
});

ipcMain.handle("nh:update-install", async () => {
  const u = await getUpdater();
  if (!u) return { mode: "failed", error: "the updater is unavailable" };
  // Refuse before latching `quitting`: if nothing is downloaded the app keeps
  // running, and a latched flag would turn the next window-close into a real
  // quit instead of hide-to-tray.
  if (!u.downloaded) return { mode: "failed", error: "no update has been downloaded" };
  // Quitting for an install is a real quit, not a hide-to-tray. The flag must
  // be set BEFORE install() — quitAndInstall fires the quit path immediately.
  quitting = true;
  return u.install();
});

ipcMain.handle("nh:update-defer", async (_event, version) => {
  const u = await getUpdater();
  if (!u) return { mode: "failed", error: "the updater is unavailable" };
  return u.defer(version);
});

/**
 * The win32 title-bar overlay for a theme. On win32 `hiddenInset` degrades to a
 * frameless window with NO controls, so this overlay IS the minimise/maximise/
 * close buttons: its colour has to track the window's and its symbolColor has to
 * stay legible against it. The height is carried over unchanged from the literal
 * this function replaced and is NOT known to correspond to anything in the
 * board's CSS: the shell block there reserves 34px of traffic-light clearance on
 * `.nh-sidebar-brand` and makes `.nh-main-bar` draggable, and neither is 40.
 * Treat it as the caption-button strip's height — changing it wants a look at a
 * real Windows window, not at styles.css.
 */
function overlayFor(theme) {
  const { bg, symbol } = themeColors(theme);
  return { color: bg, symbolColor: symbol, height: 40 };
}

// The board's light/dark choice, mirrored where the MAIN process can read it —
// see createWindow for why a copy has to exist at all. Board-driven like the
// update handlers above, and safe there for the same reason, held to a stricter
// line: the only values that reach disk are the two literals normalizeTheme
// admits, so no path, URL or credential can pass through this channel whatever
// the page sends.
ipcMain.handle("nh:set-theme", (_event, value) => {
  const theme = normalizeTheme(value);
  if (!theme) return { ok: false };
  const saved = writeTheme(app.getPath("userData"), theme);
  // The live window, not only the next one. themeSource re-colours Electron's
  // own chrome immediately; the win32 overlay is fixed at creation, so without
  // this the title bar keeps the old theme until the app restarts.
  nativeTheme.themeSource = theme;
  if (process.platform === "win32" && win && !win.isDestroyed()) {
    // Throws if the window was not created with a titleBarOverlay. Losing the
    // recolour is cosmetic and self-corrects next launch; throwing out of an
    // IPC handler is not.
    try { win.setTitleBarOverlay(overlayFor(theme)); } catch { /* next launch */ }
  }
  return { ok: true, saved };
});

// "Open repo" (SlideOver.jsx, next to a task's repo path). Hands a folder to
// the OS default handler via shell.openPath — never the user's editor, which
// we do not know. The one thing this handler must never become is an
// arbitrary-path opener for whatever a compromised renderer sends it, so it
// re-checks against the SERVER's own idea of known repos (/api/repos) rather
// than trusting the caller: a path not in that list is refused before
// shell.openPath ever sees it.
ipcMain.handle("nh:open-path", async (_event, p) => {
  if (typeof p !== "string" || !p) return { ok: false, error: "no path given" };
  let repos;
  try {
    const res = await fetch(`${ORIGIN}/api/repos`);
    repos = await res.json();
  } catch {
    return { ok: false, error: "could not reach the server" };
  }
  const known = new Set((Array.isArray(repos) ? repos : []).map((r) => r.repo_path));
  if (!known.has(p)) return { ok: false, error: "not a registered repo" };
  const err = await shell.openPath(p);
  return err ? { ok: false, error: err } : { ok: true };
});

async function createWindow() {
  // THE SHELL FOLLOWS THE APP, NOT THE OS.
  //
  // This read used to be `nativeTheme.shouldUseDarkColors`, with a comment
  // saying the OS-following colour was there because a light-mode launch flashed
  // dark before first paint (shell audit, 2026-07-17). That was only ever true
  // while the CONTENT followed the OS too. It does not: web/src/App.jsx defaults
  // "nh-theme" to "dark" whatever the OS says. So on a light-mode Mac or PC the
  // OS-following colour produced the mismatch it was written to prevent — a
  // light window frame and light title-bar controls around a dark board.
  //
  // The honest trade-off, since a default cannot be right for everyone: a user
  // who has toggled the board to LIGHT holds that in renderer localStorage,
  // which nothing here can read before a window exists. Rather than hand that
  // user the dark flash we just took away from the majority, the renderer
  // mirrors every choice into theme.json (nh:set-theme, above) and startup reads
  // it back.
  //
  // RESIDUALS, stated so nobody rediscovers them as bugs. All three are one
  // wrong-coloured FRAME, never wrong content — the board itself is always
  // right, because localStorage is still the truth.
  //  1. UPGRADE. The first launch after upgrading, for a user who had already
  //     chosen light, has no file yet and pre-paints dark. Once — but only if
  //     the write that follows lands. See 2.
  //  2. THE WRITE CANNOT LAND (read-only userData, full disk). Then it is not
  //     once: it is EVERY launch, for good, and silently. writeTheme swallows
  //     the error and returns false, nh:set-theme reports that honestly as
  //     `{ ok: true, saved: false }`, and the renderer discards the return
  //     value. Driven on a `chmod 0555` userData: no theme.json appears and
  //     relaunch after relaunch pre-paints #0F1117 for a user who chose light.
  //     Nothing here can fix it — the shell cannot write — so it is written
  //     down instead, here and in docs/desktop.md's release checklist.
  //  3. THE INVERSE. theme.json says light while renderer localStorage has been
  //     reset — a changed `NH_ORIGIN` or `server.port` is a supported knob and
  //     moves the origin localStorage belongs to, and cleared site data does it
  //     too. Then the shell pre-paints LIGHT around a board that boots dark.
  //     This one self-corrects inside the launch: the effect in App.jsx runs on
  //     mount and rewrites theme.json, so it costs a frame and nothing more.
  const theme = readTheme(app.getPath("userData"));
  // Also drives Electron's OWN surfaces — traffic lights, native menus, and the
  // `prefers-color-scheme` that this shell's token.html and error.html read.
  // Left at "system" those stay light on a light-mode OS while the board is dark.
  nativeTheme.themeSource = theme;
  const { bg } = themeColors(theme);
  win = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 720,
    minHeight: 480,
    title: "no_human",
    // Present only once the first frame is ready (below) — a window shown
    // eagerly paints a bg-colored rectangle before content, the classic flash.
    show: false,
    backgroundColor: bg,
    // Native-Mac chrome (operator: "Claude-desktop-grade look"): content
    // extends under a hidden-inset title bar; traffic lights float over the
    // sidebar's brand zone, which the web app makes draggable when it detects
    // the shell. On Windows, `hiddenInset` degrades to a frameless window with
    // NO controls — use `hidden` + a themed titleBarOverlay so the min/max/
    // close buttons come back (untestable on this Mac; guarded by platform).
    ...(process.platform === "darwin"
      ? { titleBarStyle: "hiddenInset", trafficLightPosition: { x: 18, y: 18 } }
      : {}),
    ...(process.platform === "win32"
      ? { titleBarStyle: "hidden", titleBarOverlay: overlayFor(theme) }
      : {}),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.cjs"),
      // The preload is sandboxed and cannot require("./package.json"), so the
      // real app version is handed to it here — the documented channel for
      // sandboxed preloads. Without this the board DISPLAYED "dev" in every
      // packaged app (measured on Linux 2026-08-18); the update check itself
      // runs here in main on app.getVersion() and was never affected.
      additionalArguments: [`--nh-app-version=${app.getVersion()}`],
      // The board polls for fresh arrivals; a hidden window must keep ticking.
      backgroundThrottling: false,
    },
  });
  // Reveal on the first paint — with show:false above this kills the flash and
  // never leaves the window hidden: ready-to-show fires for the board AND for
  // error.html (loadBoardOrError always loads one of them).
  win.once("ready-to-show", () => { if (win && !win.isDestroyed()) win.show(); });
  win.on("closed", () => { win = null; });
  // E3 (darwin AND win32): closing HIDES to the tray — the board keeps polling
  // and notifications keep coming. Quit is explicit: tray menu, Cmd-Q, or
  // File→Quit. Windows joined 2026-08-05 for feature parity — the two apps are
  // one product, and "close stops the notifications" was a real divergence, not
  // a platform convention (the tray-resident close is the norm for apps of this
  // shape on Windows too). Linux keeps the plain close-quits behavior.
  win.on("close", (e) => {
    if ((process.platform === "darwin" || process.platform === "win32") && !quitting) {
      e.preventDefault();
      win.hide();
    }
  });
  // Dock/taskbar badge mirrors the web app's own "(N) no_human" title —
  // the count the board derives with isNeedsYou. No IPC, no second truth.
  // setBadgeCount only renders on macOS and Unity Linux; the Windows taskbar
  // equivalent is an overlay icon, drawn from the SAME parsed title.
  win.webContents.on("page-title-updated", (_e, title) => {
    const count = parseBadgeCount(title);
    if (count === null) return; // foreign title (error page) — keep the last truthful badge
    try { app.setBadgeCount(count); } catch { /* linux without libunity */ }
    if (process.platform === "win32" && win && !win.isDestroyed()) {
      try {
        if (count === 0) {
          win.setOverlayIcon(null, "");
        } else {
          const { width, height, data } = overlayBadgeBitmap(count);
          win.setOverlayIcon(
            nativeImage.createFromBuffer(Buffer.from(data.buffer), { width, height, scaleFactor: 2.0 }),
            `${count} task${count === 1 ? "" : "s"} need${count === 1 ? "s" : ""} you`);
        }
      } catch { /* a failed overlay must never take down title handling */ }
    }
  });
  routeExternally(win.webContents);
  await loadBoardOrError(win);
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    // Never relaunch a window into a teardown. `showWindow()` FALLS THROUGH to
    // `createWindow()` when `win` is null or destroyed, and on darwin `win` is
    // null in exactly one situation: the app is quitting. Quitting is
    // deliberately DELAYED here (see before-quit below) so a server that
    // ignores SIGTERM can be escalated to SIGKILL — and the single-instance
    // lock is still held for the whole of that delay, so a second launch during
    // it lands in this handler rather than starting a fresh app. Without this
    // line that second launch builds a new window and points it at a server
    // being torn down underneath it.
    if (quitting) return;
    // Guard the close→quit race (review finding: a destroyed BrowserWindow
    // reference throws in the main process).
    //
    // showWindow, not bare focus(): with close-to-tray on darwin AND win32,
    // "launch the app again" is how a user reopens a HIDDEN window — the
    // Windows walkthrough measured focus() alone leaving it invisible (the
    // process count moved, the screen did not). focus() without show() only
    // ever worked for a window that was still visible.
    if (win && !win.isDestroyed() && win.isMinimized()) win.restore();
    showWindow();
  });
  app.whenReady().then(async () => {
    // Tray failure must never abort startup (review: a bad image on some
    // platforms throws here, and this runs BEFORE the window exists).
    try { buildTray(); } catch (err) { console.error("tray failed:", err); }
    setupAboutPanel();
    buildAppMenu();
    await createWindow();
    // AFTER the window exists (it is the thing that renders the notice) and
    // deliberately NOT awaited: a slow or dead update feed must never delay the
    // board appearing. Throttled to once a day inside the updater.
    checkForUpdates().catch(() => {});
  }).catch((err) => {
    // Startup must never end in an unhandled rejection: that leaves a blank
    // window with no error page and no Retry.
    console.error("startup failed:", (err && err.message) || err);
    if (win && !win.isDestroyed()) showError(win, "startup-failed").catch(() => {});
  });
  app.on("activate", () => showWindow());
  // E3: on darwin the app lives in the tray after window close; only an
  // explicit quit tears down. Elsewhere, all-windows-closed still quits.
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  // Quitting is DELAYED so a server that ignores SIGTERM can be escalated to
  // SIGKILL instead of outliving the app and holding the port. quitPolicy owns
  // the decision (and is tested): a SECOND Cmd-Q during the delay must hold
  // again, not fall through and abandon the escalation.
  let shuttingDown = false;
  let shutdownDone = false;
  app.on("before-quit", (event) => {
    quitting = true;
    const action = quitAction({
      ownsAny: lifecycle.ownsAny(), shuttingDown, shutdownDone });
    if (action === "keep-held") { event.preventDefault(); return; }
    if (action === "delay") {
      shuttingDown = true;
      event.preventDefault();
      // Hard ceiling: a shutdown that never settles must not make the app
      // un-quittable.
      const hardExit = setTimeout(() => { shutdownDone = true; app.exit(0); }, 20000);
      lifecycle.shutdown()
        .catch((err) => console.error("shutdown failed:", err))
        .finally(() => {
          clearTimeout(hardExit);
          shutdownDone = true;
          app.quit();
        });
      return;
    }
    // E2 gating: stops ONLY a shell-spawned server; attached is untouched.
    // Fallback for the non-owning path (nothing of ours is running).
    stopAllOwned();
    stopServer(lifecycle.state);
    lifecycle.state = null;
  });
}
