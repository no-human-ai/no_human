// nh:save-token must NEVER report success when the server did not start.
//
// This is the branch's signature defect — the app lying about success — and the
// final failed-status check that prevents it was entirely unpinned. I declined
// to write this test claiming it needed a >=20s wall-clock path. That was wrong,
// and a reviewer showed the recipe: NH_BIN outranks every other resolution
// route, so a NON-EXECUTABLE NH_BIN makes spawn emit EACCES and ensureServer
// return failed/spawn-error in about 5ms, with nothing ever executed.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { probe } from "./server.mjs";
import { freePort } from "./testing/ports.mjs";

register("./testing/electronLoader.mjs", import.meta.url);

const HERE = fileURLToPath(new URL(".", import.meta.url));
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-savefail-"));
fs.mkdirSync(path.join(home, ".no_human"));
// A binary that EXISTS (so resolveNhBin returns it) but cannot be executed.
const fakeNh = path.join(home, "nh");
fs.writeFileSync(fakeNh, "#!/bin/sh\nexit 0\n");
fs.chmodSync(fakeNh, 0o644);                 // deliberately not +x -> EACCES
process.env.HOME = home;
process.env.USERPROFILE = home; // os.homedir() reads USERPROFILE on Windows (see mainIpc.test.mjs)
process.env.NH_BIN = fakeNh;
const { origin: ORIGIN } = await freePort();  // nothing listening
process.env.NH_ORIGIN = ORIGIN;

// Precondition, not an assumption: this test's whole premise is that the boot
// probe finds nothing. If some other process holds this port, fail HERE with
// this message rather than 20s later as "boot never reached the setup screen".
assert.notEqual(await probe(ORIGIN), "up",
  `${ORIGIN} answered a probe — this test requires NOTHING listening`);

const stub = await import("./testing/electronStub.mjs");
await import("./main.mjs");
stub.fireReady();
// whenReady -> createWindow -> _loadBoardOrError must SETTLE before the
// handler below runs: saveAction() on a boot still in flight sees
// lifecycle.state === null and answers "needs-restart"/"restart", masking
// the real failure this test exists to pin.
//
// This fresh $HOME has NO credential yet (the token is written by the
// handler under test, not before it), so the initial nav takes the
// !hasCredential() branch and shows token.html WITHOUT ever touching
// lifecycle.state (main.mjs:500-503) -- ensureServer only runs later,
// inside the handler itself. So serverLabel() stays "server: probing…"
// forever here; it is NOT the settled signal for this boot path. The
// observable settle point is the setup screen's loadFile: win.loaded
// records "file:token.html" once that nav lands, and BrowserWindow.last is
// main.mjs's actual `win` (main.mjs:784, testing/electronStub.mjs:87-103).
let settled = false;
{
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const w = stub.BrowserWindow.last;
    if (w && w.loaded.some((x) => x.startsWith("file:token.html"))) {
      settled = true;
      break;
    }
    await new Promise((r) => setTimeout(r, 50));
  }
}
assert.ok(settled, `boot never reached the setup screen; window loaded=` +
  `${JSON.stringify(stub.BrowserWindow.last?.loaded ?? [])}`);

test.after(() => fs.rmSync(home, { recursive: true, force: true }));

test("a token saved against a server that cannot start reports the failure", async () => {
  const handler = stub.calls.ipc.get("nh:save-token");
  assert.ok(handler, "main.mjs must register nh:save-token");
  const setupUrl = pathToFileURL(path.join(HERE, "token.html")).href;

  const started = Date.now();
  const res = await handler({ senderFrame: { url: setupUrl } }, "sk-ant-oat-cannot-start");
  const elapsed = Date.now() - started;

  assert.equal(res.ok, false,
    "reporting ok here paints 'Connected. Opening no_human…' over an error page");
  assert.match(res.error, /did not start/i, "the reason must say the server failed");
  // The token itself IS saved — only the server failed.
  assert.match(fs.readFileSync(path.join(home, ".no_human", ".env"), "utf8"),
    /sk-ant-oat-cannot-start/);
  assert.ok(elapsed < 8000,
    `this path must be fast (spawn fails immediately); took ${elapsed}ms`);
});
