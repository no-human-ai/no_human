// End-to-end wiring tests for main.mjs.
//
// WHY THIS EXISTS: every blocking defect in review rounds 8, 9, 10 and 11 lived
// in this wiring, and a reviewer proved all three round-10 fixes could be
// reverted with the suite still green:
//   - dropping event.preventDefault() from the keep-held branch
//   - reverting stop-failed to a state without the child
//   - reverting the quit grace from 10s to 2s
// The decisions are unit-tested elsewhere (quitPolicy, serverLifecycle); this
// file guards that main.mjs actually WIRES them together.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { probe } from "./server.mjs";
import { freePort } from "./testing/ports.mjs";
const HERE = fileURLToPath(new URL(".", import.meta.url));

register("./testing/electronLoader.mjs", import.meta.url);

const { port: PORT, origin: ORIGIN } = await freePort();
const MARK = `nhWiring${process.pid}`;

/** A temp HOME with a token, plus a fake `nh` that binds PORT and obeys SIGTERM. */
function bootstrapEnv() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-wiring-"));
  fs.mkdirSync(path.join(home, ".no_human"));
  fs.writeFileSync(path.join(home, ".no_human", ".env"),
    "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-wiring\n");
  const bin = path.join(home, "nh");
  fs.writeFileSync(bin, `#!/usr/bin/env node
process.title = ${JSON.stringify(MARK)};
const http = require("node:http");
http.createServer((q, s) => {
  if (q.url.split("?")[0] === "/api/tasks") { s.end("[]"); return; }
  s.setHeader("content-type", "text/html"); s.end("<h1>BOARD</h1>");
}).listen(${PORT}, "127.0.0.1");
setInterval(() => {}, 1000);
`);
  fs.chmodSync(bin, 0o755);
  process.env.HOME = home;                 // BEFORE importing main.mjs
  process.env.USERPROFILE = home; // os.homedir() reads USERPROFILE on Windows (see mainIpc.test.mjs)
  process.env.NH_ORIGIN = ORIGIN;
  process.env.NH_BIN = bin;
  return home;
}

const livePids = () => {
  const out = execSync(
    `/bin/ps -eo pid,command | grep ${MARK} | grep -v grep || true`).toString().trim();
  return out ? out.split("\n").map((l) => l.trim().split(/\s+/)[0]) : [];
};
const liveFakes = () => livePids().length;

// This whole file is a POSIX process-lifecycle scenario and cannot run on a real
// Windows host: bootstrapEnv writes a `#!/usr/bin/env node` shebang fake nh
// (unspawnable on Windows), livePids shells out to `/bin/ps ... | grep`, and the
// teardown uses /usr/bin/pkill — none exist on Windows, and the top-level poll
// below (liveFakes) would throw and crash the file outright before any test runs.
// A Windows form needs a .cmd/.bat shim spawned as `node <script>` plus
// tasklist/taskkill process detection (see the batch report — this is the T8
// piece that genuinely needs a Windows machine).
const ON_WINDOWS = process.platform === "win32";
if (ON_WINDOWS) {
  test("main.mjs wiring (POSIX process-lifecycle scenario)",
    { skip: "POSIX-only: shebang fake nh, /bin/ps and pkill do not exist on "
      + "Windows and the top-level process poll would crash the file" }, () => {});
} else {

// One module instance is shared: main.mjs has top-level side effects.
const home = bootstrapEnv();
const stub = await import("./testing/electronStub.mjs");
// Precondition, not an assumption: this test's whole premise is that nothing
// answers ORIGIN until the fake `nh` (spawned by main.mjs below) binds it. If
// some other process already holds this port, fail HERE with this message
// rather than in an inscrutable "server never attached" assertion later.
assert.notEqual(await probe(ORIGIN), "up",
  `${ORIGIN} answered a probe — this test requires NOTHING listening yet`);
const main = await import("./main.mjs");
stub.fireReady();
// Wait for whenReady -> createWindow -> loadBoardOrError -> ensureServer to
// settle: the fake server is live AND the board window has loaded. Poll to a
// generous deadline rather than sleep a fixed 4s — a blind wait flakes red when
// a loaded CI runner needs longer than 4s to boot the child (gate finding #2).
// This polls for the exact condition the first test asserts, so it cannot
// settle early on a broken boot; it just fails faster when idle.
{
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const w = stub.BrowserWindow.last;
    if (liveFakes() === 1 && w && w.loaded.some((x) => x.startsWith("url:"))) break;
    await new Promise((r) => setTimeout(r, 100));
  }
}

test.after(() => {
  try { execSync(`/usr/bin/pkill -9 -f ${MARK}`); } catch { /* already gone */ }
  fs.rmSync(home, { recursive: true, force: true });
});

test("the shell actually spawned and attached to a server", () => {
  assert.equal(liveFakes(), 1, "no server was started; later assertions are void");
  const win = stub.BrowserWindow.last;
  assert.ok(win.loaded.some((x) => x.startsWith("url:")),
    `expected the board to load, got ${JSON.stringify(win.loaded)}`);
});

test("a probe-up must not relabel a server WE spawned as operator-owned", async () => {
  // stateOnProbeUp exists because a probe-up overwrote a "spawned" state with
  // "attached": that discards the only legitimate kill target, so the child is
  // orphaned at quit and the tray reports it as the operator's. The function is
  // unit-tested; this pins that main.mjs still routes the probe-up through it.
  assert.equal(main.serverLabel(), "server: spawned by the app",
    "the shell should own the server it just started");

  const onNavigate = stub.calls.nav.get("will-navigate");
  await onNavigate({ preventDefault() {} }, "nh://retry");   // a nav that probes UP

  assert.equal(main.serverLabel(), "server: spawned by the app",
    "a probe-up relabelled our own child as operator-owned — it would be " +
    "orphaned at quit and the tray would lie about who owns it");
});

test("saving a token over OUR OWN live server restarts it and reports success",
  async () => {
    // The one state the other IPC fixture cannot reach: a live server we
    // spawned. Here the correct action is "restart" — stop ours, start it again
    // on the new credential. Treating it as somebody else's returns
    // needsRestart and strands the user on the credential screen forever.
    const save = stub.calls.ipc.get("nh:save-token");
    assert.ok(save, "main.mjs must register nh:save-token");
    assert.equal(liveFakes(), 1, "precondition: our server is running");
    // The PID of the server BEFORE the save. A genuine restart replaces the
    // process (old SIGTERM'd, a new one spawned on the new token), so this PID
    // must NOT survive. Without capturing it, every assertion below is satisfied
    // whether or not the server was actually cycled — the false guard that let a
    // mutation disabling the restart pass the whole suite green.
    const pidBefore = livePids()[0];

    const setupUrl = pathToFileURL(path.join(HERE, "token.html")).href;
    const res = await save({ senderFrame: { url: setupUrl } }, "sk-ant-oat-rotated");

    assert.equal(res.ok, true,
      `a token save over our own server should succeed; got ${JSON.stringify(res)}`);
    assert.match(fs.readFileSync(path.join(home, ".no_human", ".env"), "utf8"),
      /sk-ant-oat-rotated/, "the new token was not persisted");
    assert.equal(liveFakes(), 1,
      "the restart should leave exactly one server — not zero, and not a leak");
    // The one assertion the old test lacked: the process was actually replaced.
    // If the restart is skipped, the OLD server keeps the port on the OLD token
    // while the handler still returns ok — exactly the branch's signature defect.
    assert.notEqual(livePids()[0], pidBefore,
      "the server was NOT restarted: the same PID still holds the port, so it is "
      + "running on the OLD token even though the save reported success");
  });

test("a SECOND quit during the shutdown hold is HELD, not let through", async () => {
  const beforeQuit = stub.calls.handlers.get("before-quit");
  assert.ok(beforeQuit, "main.mjs must register a before-quit handler");

  let prevented = 0;
  const evt = () => ({ preventDefault: () => { prevented += 1; } });

  beforeQuit(evt());                       // first Cmd-Q -> delay
  assert.equal(prevented, 1, "the first quit must be held so shutdown can run");

  beforeQuit(evt());                       // second Cmd-Q while shutting down
  assert.equal(prevented, 2,
    "the re-entrant quit fell through, abandoning the SIGKILL escalation — " +
    "the server outlived the app and kept ~/.no_human/nh.pid");
});

test("the held shutdown stops the server we spawned", async () => {
  // shutdown() is already running from the test above. Wait for BOTH the
  // process to die and the quit to be released — asserting on the process
  // alone raced the async .finally(() => app.quit()).
  for (let i = 0; i < 80 && (liveFakes() > 0 || stub.calls.quit === 0); i++) {
    await new Promise((r) => setTimeout(r, 250));
  }
  assert.equal(liveFakes(), 0, "the spawned server survived the shutdown");
  assert.ok(stub.calls.quit >= 1, "the app must actually quit once shutdown finishes");
});

}  // end of the POSIX process-lifecycle scenario (see ON_WINDOWS guard above)
