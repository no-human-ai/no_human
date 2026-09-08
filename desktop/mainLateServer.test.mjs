// The first-launch race: a bundled server that comes up AFTER the spawn window
// must not latch the error page — the shell has to re-probe in the background
// and swap in the board on its own.
//
// Measured root cause: a cold start of the PyInstaller-frozen `nh` is ~17.4s
// while the spawn window was 20s, so a post-install launch (AV scan of the 44MB
// bundle, cold disk) times out on a server that WAS about to bind the port. The
// child is deliberately left alive by ensureServer; before this fix nothing
// re-probed it, so the error page stayed up until a manual Retry.
//
// This is the faithful end-to-end proof: a REAL spawned `nh` (a fake one that
// binds the port LATE, past a deliberately short spawn window) drives the REAL
// ensureServer -> spawn-timeout -> error.html path, then the background re-probe
// must load the board with no further user action. Runs in its own process
// (node --test gives each FILE a fresh module graph) because main.mjs has
// top-level side effects.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { probe } from "./server.mjs";
import { freePort } from "./testing/ports.mjs";

register("./testing/electronLoader.mjs", import.meta.url);

const IS_WIN = process.platform === "win32";
const { port: PORT, origin: ORIGIN } = await freePort();

const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-late-"));
fs.mkdirSync(path.join(home, ".no_human"));
// A credential must exist, or main.mjs takes the first-run setup path instead of
// spawning a server (see _loadBoardOrError's hasCredential branch).
fs.writeFileSync(path.join(home, ".no_human", ".env"),
  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-late\n");
process.env.HOME = home;
process.env.USERPROFILE = home; // os.homedir() reads USERPROFILE on Windows
process.env.NH_ORIGIN = ORIGIN;
delete process.env.NH_TEST_LOG;

// A fake `nh`, launched by the REAL ensureServer through NH_BIN, that binds the
// port ~2s AFTER launch and then stays alive — so the spawn window (below,
// 1000ms) elapses first and ensureServer reports spawn-timeout with the child
// still running, exactly the real scenario. It ignores argv, so main.mjs's fixed
// ["start","--no-open"] is harmless. POSIX only: a shebang script needs no argv
// override, whereas the Windows fake would be `node nh.js` and NH_BIN cannot
// supply that extra arg (nhArgs is fixed inside main.mjs) — server.test.mjs's
// fakeNh covers the Windows launch, and pollForLateServer has no win32 branch.
const bindDelayMs = 2000;
const marker = path.join(home, "nh.pid");
const sh = path.join(home, "nh");
fs.writeFileSync(sh, `#!${process.execPath}
const http = require("node:http");
const fs = require("node:fs");
fs.writeFileSync(${JSON.stringify(marker)}, String(process.pid));
setTimeout(() => {
  http.createServer((q, r) => r.end("[]")).listen(${PORT}, "127.0.0.1");
}, ${bindDelayMs});
setInterval(() => {}, 1000);
`);
fs.chmodSync(sh, 0o755);
process.env.NH_BIN = sh;

// Reproduce a spawn-timeout in ~1s instead of the real 30s (test seam), and keep
// the background re-probe window generous — the server binds at ~2s, well inside
// it, so the board loads within a couple of seconds.
process.env.NH_SPAWN_TIMEOUT_MS = "1000";

// Precondition, not an assumption: this test's whole premise is that nothing
// answers the port until the fake `nh` binds it ~2s in. If some other process
// already holds it, fail HERE with this message rather than in an inscrutable
// error-page/board-load assertion later.
assert.notEqual(await probe(ORIGIN), "up",
  `${ORIGIN} answered a probe — this test requires NOTHING listening yet`);

const rejections = [];
process.on("unhandledRejection", (e) => rejections.push(e));

const stub = await import("./testing/electronStub.mjs");
await import("./main.mjs");
stub.fireReady();
// Spawn window (1s) + late bind (2s) + a probe interval + board load, with room.
await new Promise((r) => setTimeout(r, 5000));

test.after(() => {
  try {
    const pid = Number(fs.readFileSync(marker, "utf8"));
    if (pid > 0) { try { process.kill(-pid, "SIGKILL"); } catch { /* group gone */ }
                   try { process.kill(pid, "SIGKILL"); } catch { /* gone */ } }
  } catch { /* never started */ }
  fs.rmSync(home, { recursive: true, force: true });
});

test("a late-binding bundled server auto-loads the board instead of latching the error page",
  { skip: IS_WIN ? "the fake-nh launch needs argv override, which NH_BIN alone "
    + "cannot supply on Windows; the POSIX run governs this platform-independent "
    + "poll (no win32 branch in pollForLateServer)" : false },
  () => {
    const win = stub.BrowserWindow.last;
    assert.ok(win, "no window was created");
    const idxError = win.loaded.indexOf("file:error.html?spawn-timeout");
    const idxBoard = win.loaded.indexOf(`url:${ORIGIN}`);
    // The error page IS shown first — a truly-dead server must still surface it,
    // so the fix renders it and re-probes rather than suppressing it.
    assert.ok(idxError >= 0,
      `the spawn-timeout error page must be shown first; got ${JSON.stringify(win.loaded)}`);
    // …and then the background re-probe loads the board with NO user action.
    // Without the poll this index is -1 and the user sits on the error page.
    assert.ok(idxBoard > idxError,
      `the board must auto-load AFTER the error page via the background re-probe; `
      + `got ${JSON.stringify(win.loaded)}`);
  });

test("the auto-recovery leaves no unhandled rejection in the main process",
  { skip: IS_WIN ? "see the skip above" : false },
  () => {
    assert.deepEqual(rejections.map((e) => (e && e.message) || String(e)), [],
      "the background re-probe must not leak an unhandled rejection");
  });
