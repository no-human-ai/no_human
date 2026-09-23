# no_human desktop (Electron shell)

The board as a native Mac app. The shell **attaches** to the local `nh` server
(or spawns `nh start --no-open` when none is running) and points a same-origin
window at it — the web app's fetch/WebSocket/SSE run untouched.

## Run from source
```bash
cd desktop && npm install && npm run desktop
```

## Package (unsigned dmg, v1)
```bash
cd desktop && npm run dist        # → desktop/dist/no_human-<v>-arm64-mac.zip + latest-mac.yml (no dmg — see below)
cd desktop && npm run dist:win    # → desktop/dist/no_human-<v>-UNSIGNED.exe (on Windows)
cd desktop && npm run dist:linux  # → desktop/dist/no_human-<v>-linux-amd64.deb + no_human-<v>-linux-x86_64.AppImage (on Linux; CI's `linux` job)
```
`npm run dist` alone does not produce a DMG: `mac.target` is `["dir",
"zip"]`, deliberately without `dmg` (electron-builder's own `dmg` target
signs but never notarizes/staples its output, so it isn't the DMG anyone
should ship — see `docs/INSTALLER.md`'s "Release assets" section and
`scripts/check_release_feeds.py`). The actual DMG comes from
`packaging/make-dmg.sh`, run as part of `npm run dist:bundled`
(`docs/INSTALLER.md`); it too is unsigned in a plain dev build: on first
launch either right-click → Open, or clear the quarantine bit:
`xattr -dr com.apple.quarantine /Applications/no_human.app`.
`nh` itself is NOT bundled — install it once with
`uv tool install --editable <repo>`; the shell finds it via `$NH_BIN`, the
login shell's PATH, or the usual install locations.

## Behavior contract (each item guarded by a test where possible)
- **Never touches an operator-owned server.** Quit stops the server only when
  the shell spawned it; the pidfile is never a kill target
  (`desktop/server.test.mjs` stop-gating).
- **External links** (PR/CI URLs) open in the OS browser via an https?-only
  allowlist; nothing opens in-app windows (`web/e2e/electron-smoke.mjs`).
- **Fresh bundle every launch:** the server sends `Cache-Control: no-cache` on
  index.html and the shell requests with no-cache — the stale-app-shell class
  is gated by the e2e smoke asserting the in-shell chrome actually renders.
- **Hide-on-close (macOS):** closing hides to the tray; the board keeps
  polling (`backgroundThrottling: false`) and notifications keep firing; quit
  is explicit (tray or Cmd-Q).
- **Server discovery:** port from `~/.no_human/config.yaml` `server.port`;
  `NH_ORIGIN` overrides.

## Regression checklist (manual, per release)
- WS/SSE recover after `nh` restarts underneath the open window.
- Theme choice persists across relaunch (Electron userData is a separate
  localStorage from the browser's — a one-time reset on first launch is
  expected).
- Theme choice persists **only while userData is writable.** The window frame
  and the Windows title-bar buttons are pre-painted from
  `<userData>/theme.json`, which the board rewrites on every toggle. If that
  write cannot land — read-only userData, full disk — the frame starts in the
  DEFAULT dark on *every* launch, not once, and nothing says so: `nh:set-theme`
  returns `{ ok: true, saved: false }` and no caller reads `saved`. Toggle the
  theme and confirm `theme.json` appeared; its absence is the only signal.
  (The board itself is always right either way — the cost is one wrong-coloured
  frame per launch, never wrong content.)
- Notifications fire while hidden to tray.
- Second launch focuses the existing window (single-instance lock).
- Board renders identically in Safari-free environments (the e2e gate's
  chromium == the shell's engine, so drift here means a packaging problem).
