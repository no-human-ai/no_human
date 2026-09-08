// The update IPC, tested at its REGISTRATION site.
//
// updater.test.mjs proves the update logic is correct in isolation. That is not
// the same as proving main.mjs ever connects it to anything — and an unwired
// handler is a button in the board that silently does nothing, which is exactly
// the class of defect the packaging guard was written for.
//
// So this asserts what the preload can actually reach: the four channels
// preload.cjs invokes must exist as handlers, and each must return a value
// rather than throw when the updater component is unavailable (which is the
// state in every unpackaged run, including this one).
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { listenOnFreePort } from "./testing/ports.mjs";

register("./testing/electronLoader.mjs", import.meta.url);

const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-upd-"));
fs.mkdirSync(path.join(home, ".no_human"));
fs.writeFileSync(path.join(home, ".no_human", ".env"),
  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-test\n");
process.env.HOME = home;
process.env.USERPROFILE = home; // os.homedir() reads USERPROFILE on Windows (see mainIpc.test.mjs)

const { server, port: PORT, origin: ORIGIN } = await listenOnFreePort((q, s) => s.end("[]"));
process.env.NH_ORIGIN = ORIGIN;

const stub = await import("./testing/electronStub.mjs");
const main = await import("./main.mjs");
stub.fireReady();
await new Promise((r) => setTimeout(r, 800));

test.after(() => { server.close(); fs.rmSync(home, { recursive: true, force: true }); });

// Exactly the channels preload.cjs invokes. Kept as a literal list rather than
// derived from preload.cjs, so a rename has to be made in both places
// deliberately instead of silently agreeing with itself.
const CHANNELS = ["nh:update-check", "nh:update-download", "nh:update-install",
                  "nh:update-defer"];

test("every update channel the preload calls is registered in main", () => {
  for (const ch of CHANNELS) {
    assert.ok(stub.calls.ipc.has(ch),
      `main.mjs never registered "${ch}" — the board would invoke it and hang`);
  }
});

test("the preload and the main process agree on the channel names", () => {
  // The two halves are wired by string. A typo on either side is invisible
  // until a user clicks the button.
  const preload = fs.readFileSync(new URL("./preload.cjs", import.meta.url), "utf8");
  for (const ch of CHANNELS) {
    assert.ok(preload.includes(`"${ch}"`),
      `preload.cjs does not invoke "${ch}", but main.mjs handles it`);
  }
  // And the notification channel, which flows the other way.
  assert.ok(preload.includes('"nh:update"'),
    "preload.cjs must subscribe to the nh:update notification channel");
});

test("no update handler throws when the updater is unavailable", async () => {
  // electron-updater cannot initialise in an unpackaged test run. Every handler
  // must degrade to a value — an exception here surfaces in the renderer as an
  // unhandled rejection and takes out the board's click handler.
  for (const ch of CHANNELS) {
    const handler = stub.calls.ipc.get(ch);
    const result = await handler({ senderFrame: { url: "http://127.0.0.1/" } });
    assert.ok(result && typeof result === "object",
      `"${ch}" must return a result object, got ${JSON.stringify(result)}`);
    assert.equal(typeof result.mode, "string",
      `"${ch}" must report a mode the board can render`);
  }
});

test("the signing verdict fails CLOSED when the stamp is absent", () => {
  // packagedSigning reads extraMetadata that electron-builder injects at
  // package time. This checkout's desktop/package.json carries no such field —
  // exactly the shape of a build where the stamp was lost — and the honest
  // answer to "may this app auto-update?" is then NO.
  //
  // The mutation this exists to catch is a one-character one: `=== true`
  // becoming `!== false`, which turns a missing field into permission and
  // silently offers an install macOS will refuse.
  const plan = main.packagedSigning();
  assert.equal(plan.canAutoUpdate, false,
    "an unstamped build must not claim it can install its own updates");
  assert.equal(plan.mode, "unsigned",
    "an unstamped build must report itself unsigned, not unknown-but-fine");
});

test("the update handlers are NOT restricted to the setup screen", () => {
  // nh:save-token and friends are gated on token.html. These are driven by the
  // board, so reusing that gate would reject every real call — a mistake worth
  // pinning, because the gate is the obvious thing to copy.
  return (async () => {
    const handler = stub.calls.ipc.get("nh:update-check");
    const fromBoard = await handler({ senderFrame: { url: `http://127.0.0.1:${PORT}/` } });
    assert.notEqual(fromBoard?.error, "not permitted",
      "the board must be allowed to check for updates");
  })();
});
