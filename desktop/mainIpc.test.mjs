// The credential IPC gate, tested at its CALL SITE.
//
// preload.cjs exposes window.nhSetup to EVERY page the server renders, so the
// board shares it with the setup screen. main.mjs gates each handler on
// fromSetupScreen(). setupGate.test.mjs pins isSetupUrl() itself beautifully —
// but nothing pinned that main.mjs still CALLS it, so all three ways of deleting
// the gate (open the predicate, or drop either guard line) kept the suite green.
// A one-line deletion would let board content rewrite ~/.no_human/.env.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

register("./testing/electronLoader.mjs", import.meta.url);

const PORT = 19500 + (process.pid % 150);
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-ipc-"));
fs.mkdirSync(path.join(home, ".no_human"));
fs.writeFileSync(path.join(home, ".no_human", ".env"),
  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-original\n");
process.env.HOME = home;
// os.homedir() reads USERPROFILE on Windows, not HOME — without this the run
// WRITES THROUGH to the operator's real ~/.no_human (observed: a fixture token
// landed in the real .env and flipped the real config.yaml to api_key mode).
process.env.USERPROFILE = home;
process.env.NH_ORIGIN = `http://127.0.0.1:${PORT}`;
delete process.env.NH_TEST_LOG;      // must not divert the routing under test

// A live server so main.mjs attaches instead of spawning anything.
let probes = 0;
// nh:open-path's tests below swap this in to make one path "known" — every
// other test never touches /api/repos, so its default of no known repos is
// the one they run against.
let knownRepos = "[]";
const server = http.createServer((q, s) => {
  if (q.url.split("?")[0] === "/api/tasks") probes += 1;   // one per navigation run
  if (q.url === "/api/repos") { s.end(knownRepos); return; }
  s.end("[]");
});
await new Promise((r) => server.listen(PORT, "127.0.0.1", r));

const stub = await import("./testing/electronStub.mjs");
await import("./main.mjs");
stub.fireReady();
await new Promise((r) => setTimeout(r, 800));

test.after(() => { server.close(); fs.rmSync(home, { recursive: true, force: true }); });

// import.meta.dirname does not exist on node 20, which is what runs here.
const HERE = fileURLToPath(new URL(".", import.meta.url));
const SETUP_URL = pathToFileURL(path.join(HERE, "token.html")).href;
const from = (url) => ({ senderFrame: { url } });
const envText = () => fs.readFileSync(path.join(home, ".no_human", ".env"), "utf8");

const BOARD_PAGES = [
  `http://127.0.0.1:${PORT}/`,                 // the board itself
  `http://127.0.0.1:${PORT}/token.html`,       // a board page named alike
  pathToFileURL(path.join(HERE, "error.html")).href,
  `${SETUP_URL}.evil`,                          // strict-prefix lookalike
];

test("nh:save-token refuses any sender that is not the setup screen", async () => {
  const handler = stub.calls.ipc.get("nh:save-token");
  assert.ok(handler, "main.mjs must register nh:save-token");
  for (const url of BOARD_PAGES) {
    const res = await handler(from(url), "sk-ant-oat-injected");
    assert.equal(res.ok, false, `accepted a token from ${url}`);
    assert.match(res.error, /not permitted/i);
  }
  assert.match(envText(), /sk-ant-oat-original/,
    "the operator's credential was overwritten from board content");
  assert.doesNotMatch(envText(), /injected/, "an injected token reached disk");
});

test("nh:quit and nh:dismiss refuse a non-setup sender, and obey a real one", async () => {
  const quit = stub.calls.ipc.get("nh:quit");
  const dismiss = stub.calls.ipc.get("nh:dismiss");
  assert.ok(quit && dismiss, "both handlers must be registered");

  const before = stub.calls.quit;
  for (const url of BOARD_PAGES) {
    assert.equal(await quit(from(url)), false, `board content quit the app via ${url}`);
    assert.equal(await dismiss(from(url)), false, `board content drove dismiss via ${url}`);
  }
  assert.equal(stub.calls.quit, before, "the app was quit by a page that is not the setup screen");

  // The gate must not be closed to everyone either, or the real screen is dead.
  assert.equal(await quit(from(SETUP_URL)), true, "the real setup screen cannot quit");
  assert.ok(stub.calls.quit > before, "a genuine quit did not reach app.quit()");
});

test("a missing or malformed senderFrame fails CLOSED", async () => {
  const quit = stub.calls.ipc.get("nh:quit");
  for (const evt of [{}, { senderFrame: null }, { senderFrame: { url: "" } }]) {
    assert.equal(await quit(evt), false, `fell open on ${JSON.stringify(evt)}`);
  }
});

test("the window is revealed on first paint — it must never stay hidden", () => {
  // createWindow uses show:false to kill the pre-paint flash and relies on
  // ready-to-show to reveal. Lose that and the app runs with an invisible
  // window: no board, no error page, nothing to click.
  const win = stub.BrowserWindow.last;
  assert.ok(win.onceHandlers["ready-to-show"],
    "main.mjs must subscribe to ready-to-show, or the window never appears");

  const before = win.shown;
  win.onceHandlers["ready-to-show"]();
  assert.ok(win.shown > before, "ready-to-show fired but the window was not shown");
});

test("Retry is rate-limited: a second click while one is in flight is declined", async () => {
  // Retry is a plain link that can be clicked repeatedly, and each accepted
  // click is a full ensureServer AND another spawned nh. Dropping the idle gate
  // made every extra click leak a live server.
  const onNavigate = stub.calls.nav.get("will-navigate");
  const win = stub.BrowserWindow.last;
  win.loaded.length = 0;

  // Count PROBES, not loads: with a plain schedule() the second nav supersedes
  // the first, so exactly one page is loaded either way — but two navigations
  // RAN, and on a real first run each of those is another ensureServer spawn.
  const before = probes;
  // Fire both WITHOUT awaiting, so the second lands while the first is pending.
  const a = onNavigate({ preventDefault() {} }, "nh://retry");
  const b = onNavigate({ preventDefault() {} }, "nh://retry");
  await Promise.all([a, b]);

  assert.equal(probes - before, 1,
    `a concurrent Retry was accepted (${probes - before} navigations ran); on a `
    + "real first run each one spawns another nh that nothing reclaims");
});

// --- the save contract, not just the gate --------------------------------- //
// mainIpc previously tested only WHO may call nh:save-token. Every behaviour of
// what it RETURNS was unpinned, including the round-14 fix — so "the app lies
// about success", this branch's signature defect, could be reintroduced silently
// for a third time. These run against a live foreign server with nothing owned.

test("a token that cannot be written is reported as a failure, never as success", async () => {
  const handler = stub.calls.ipc.get("nh:save-token");
  const before = envText();

  const res = await handler(from(SETUP_URL), "sk-ant-api-this-is-a-metered-key");

  assert.equal(res.ok, false,
    "reporting ok here paints 'Connected. Opening no_human…' while NOTHING was written");
  assert.match(res.error, /api key/i, "the reason must name the actual problem");
  assert.equal(envText(), before, "a rejected value still reached disk");
});

test("a blank token is rejected without touching disk", async () => {
  const handler = stub.calls.ipc.get("nh:save-token");
  const before = envText();
  const res = await handler(from(SETUP_URL), "   ");
  assert.equal(res.ok, false);
  assert.equal(envText(), before);
});

test("saving against someone else's live server says so instead of claiming success", async () => {
  // Nothing of ours is running and the port answers: that server is not ours to
  // restart, and it still holds the OLD token. Reporting ok would send the user
  // back to a board where every task keeps failing on the old credential.
  const handler = stub.calls.ipc.get("nh:save-token");

  const res = await handler(from(SETUP_URL), "sk-ant-oat-brand-new-value");

  assert.equal(res.ok, false, "claimed success over a server holding the old token");
  assert.equal(res.needsRestart, true, "the setup screen needs this to keep the field alive");
  assert.match(res.error, /already running|restart/i);
  assert.match(envText(), /sk-ant-oat-brand-new-value/,
    "the token should still be SAVED — only the running server is stale");
});

test("a superseded navigation must not paint over the screen the user moved to",
  async () => {
    // Every `if (!current())` guard in _loadBoardOrError exists because a
    // discarded navigation went on to publish state and paint. The worst case is
    // the credential screen: the user opens it, an older nav completes, and the
    // board is slammed back over the top of it.
    const onNavigate = stub.calls.nav.get("will-navigate");
    const win = stub.BrowserWindow.last;
    win.loaded.length = 0;
    stub.BrowserWindow.slowLoadUrlMs = 400;      // hold the board load open

    const board = onNavigate({ preventDefault() {} }, "nh://retry");
    await new Promise((r) => setTimeout(r, 30));  // let it get past the probe
    await onNavigate({ preventDefault() {} }, "nh://token");   // supersedes it
    await board;
    stub.BrowserWindow.slowLoadUrlMs = 0;

    const last = win.loaded[win.loaded.length - 1];
    assert.ok(String(last).startsWith("file:token.html"),
      `the credential screen was painted over by a discarded navigation; `
      + `sequence was ${JSON.stringify(win.loaded)}`);
  });

test("the renderer stays sandboxed from node", () => {
  // This shell is shipped in a DMG and renders text it did not author (task
  // titles, PR bodies, CI output). Both switches flipped with the whole suite
  // green and no test mentioned webPreferences at all.
  const { opts } = stub.BrowserWindow.last;
  assert.ok(opts.webPreferences, "the window was created without webPreferences");
  assert.equal(opts.webPreferences.contextIsolation, true,
    "contextIsolation off lets page script reach the preload's scope");
  assert.equal(opts.webPreferences.nodeIntegration, false,
    "nodeIntegration on gives page script require() — full node in the renderer");
  assert.ok(String(opts.webPreferences.preload || "").endsWith("preload.cjs"),
    "the bridge must come from the preload, not the page");
});

test("nh:save-token in api_key mode writes the key and flips config.yaml", async () => {
  const handler = stub.calls.ipc.get("nh:save-token");
  // Disk effects are the contract here; the returned action (ok / needsRestart)
  // depends on server state the other tests already pin.
  await handler(from(SETUP_URL), "sk-ant-api03-e2test", "api_key");
  assert.match(envText(), /ANTHROPIC_API_KEY=sk-ant-api03-e2test/,
    "the key must land in .env under ANTHROPIC_API_KEY");
  const cfg = fs.readFileSync(path.join(home, ".no_human", "config.yaml"), "utf8");
  assert.match(cfg, /auth_mode: api_key/, "the MODE must land in config.yaml");

  // A subscription token pasted while api_key is selected is refused pre-disk.
  const bad = await handler(from(SETUP_URL), "sk-ant-oat-wrong", "api_key");
  assert.equal(bad.ok, false);
  assert.doesNotMatch(envText(), /ANTHROPIC_API_KEY=sk-ant-oat-wrong/);

  // An omitted mode (stale renderer) keeps today's subscription behaviour.
  await handler(from(SETUP_URL), "sk-ant-oat-back-to-sub");
  assert.match(envText(), /CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-back-to-sub/);
  assert.match(fs.readFileSync(path.join(home, ".no_human", "config.yaml"), "utf8"),
    /auth_mode: subscription/, "saving a token must configure subscription mode");
});

test("nh:save-token writes the OPTIONAL OpenAI key only when one is supplied (#6b)", async () => {
  const handler = stub.calls.ipc.get("nh:save-token");

  // A subscription-codex / skipped save omits the 4th arg → NOTHING for OpenAI.
  await handler(from(SETUP_URL), "sk-ant-oat-nocodex");
  assert.doesNotMatch(envText(), /OPENAI_API_KEY/,
    "codex subscription/skip must write no OpenAI credential (constraint #6b)");

  // An empty codex field (api_key chosen but nothing typed) also writes nothing.
  await handler(from(SETUP_URL), "sk-ant-oat-emptycodex", "subscription", "   ");
  assert.doesNotMatch(envText(), /OPENAI_API_KEY/, "an empty OpenAI field writes nothing");

  // api_key codex WITH a value writes OPENAI_API_KEY alongside the Claude token.
  await handler(from(SETUP_URL), "sk-ant-oat-withcodex", "subscription", "sk-proj-openaireal");
  assert.match(envText(), /OPENAI_API_KEY=sk-proj-openaireal/,
    "a supplied OpenAI key must land in .env under OPENAI_API_KEY");
  assert.match(envText(), /CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-withcodex/,
    "the required Claude token is written and untouched by the OpenAI write");

  // An Anthropic key in the OpenAI field is refused BEFORE anything is written.
  const before = envText();
  const bad = await handler(from(SETUP_URL), "sk-ant-oat-x", "subscription", "sk-ant-api03-wrong");
  assert.equal(bad.ok, false, "an Anthropic key in the OpenAI field must be rejected");
  assert.match(bad.error, /Anthropic key/);
  assert.equal(envText(), before,
    "a rejected optional key must leave both credentials on disk untouched");
});

// --- nh:requirements (Task 5: first-run requirements check) --------------- //

test("nh:requirements refuses any sender that is not the setup screen", async () => {
  const handler = stub.calls.ipc.get("nh:requirements");
  assert.ok(handler, "main.mjs must register nh:requirements");
  for (const url of BOARD_PAGES) {
    const res = await handler(from(url));
    assert.deepEqual(res, { ok: false, error: "not permitted" },
      `accepted a requirements probe from ${url}`);
  }
});

test("nh:requirements reports resolved claude/node the same shape the setup screen expects", async () => {
  const handler = stub.calls.ipc.get("nh:requirements");
  const res = await handler(from(SETUP_URL));
  // No "ok"/"error" envelope on the success path — the setup screen renders
  // res.claude/res.node directly, and wrapping it would silently break that.
  assert.equal(res.ok, undefined);
  assert.equal(typeof res.claude.ok, "boolean");
  assert.equal(typeof res.claude.path, "string");
  assert.equal(typeof res.claude.version, "string");
  assert.equal(typeof res.node.ok, "boolean");
  assert.equal(typeof res.node.path, "string");
  // node's shape carries no version field — the setup screen's row for node
  // must not expect one.
  assert.equal("version" in res.node, false);
  if (!res.claude.ok) assert.equal(res.claude.version, "",
    "a missing claude must not report a version");
});

// --- Claude Code existing sign-in (nh:claude-signin-status; nh:claude-import-token removed) --- //
// resolveClaudeCli() (server.mjs) is called with NO arguments at every call site in
// main.mjs, so it reads process.env / execFile / fs.existsSync FRESH on every
// invocation — there is no memoized resolution to defeat. That lets each test below
// fake the `claude` CLI by pointing PATH (and the login shell resolveOnPath shells out
// to) at a throwaway script for the duration of one call, then restoring both, instead
// of needing a process-wide stub in place before main.mjs's single top-level import.
const fakeBinDir = fs.mkdtempSync(path.join(os.tmpdir(), "nh-fake-claude-"));
const fakeShellPath = path.join(fakeBinDir, "fake-shell.sh");
const fakeClaudePath = path.join(fakeBinDir, "claude");
// A minimal non-login shell: resolveOnPath always invokes `$SHELL -lc "command -v
// claude"`; this drops the "-l" (login) semantics entirely and just runs the given
// command string, so it never depends on the host's real login shell or rc files.
fs.writeFileSync(fakeShellPath, "#!/bin/sh\nexec /bin/sh -c \"$2\"\n", { mode: 0o755 });
// The fake `claude` binary: behaviour is entirely driven by env vars so one script
// serves every test below without being rewritten per case.
fs.writeFileSync(fakeClaudePath, [
  "#!/bin/sh",
  // Every invocation is logged (argv only, never stdin/env) when a caller
  // sets NH_FAKE_SPAWN_LOG — used to prove no surviving handler ever runs
  // `setup-token`, not just to drive its (now-unreachable) behaviour below.
  "if [ -n \"$NH_FAKE_SPAWN_LOG\" ]; then echo \"$@\" >> \"$NH_FAKE_SPAWN_LOG\"; fi",
  "if [ \"$1\" = \"auth\" ] && [ \"$2\" = \"status\" ]; then",
  "  exit \"${NH_FAKE_AUTH_STATUS:-1}\"",
  "fi",
  "if [ \"$1\" = \"setup-token\" ]; then",
  "  if [ -n \"$NH_FAKE_SETUP_STDOUT\" ]; then printf '%s' \"$NH_FAKE_SETUP_STDOUT\"; fi",
  "  if [ -n \"$NH_FAKE_SETUP_STDERR\" ]; then printf '%s' \"$NH_FAKE_SETUP_STDERR\" >&2; fi",
  "  exit \"${NH_FAKE_SETUP_CODE:-0}\"",
  "fi",
  "exit 1",
].join("\n") + "\n", { mode: 0o755 });
// Fake `security` (macOS Keychain CLI): shadows the real /usr/bin/security
// only for the duration of a withFakeClaude() call, same as the fake
// `claude` above. macClaudeKeychainExists (server.mjs) shells out to
// `security find-generic-password -s "<service>"` WITHOUT `-w`, so this
// fixture never needs to simulate returning an actual secret — only the
// exit code, matching the real binary's existence-check-only contract.
const fakeSecurityPath = path.join(fakeBinDir, "security");
fs.writeFileSync(fakeSecurityPath, [
  "#!/bin/sh",
  "if [ \"$1\" = \"find-generic-password\" ]; then",
  "  if [ \"$NH_FAKE_KEYCHAIN_HAS_ITEM\" = \"1\" ]; then",
  "    echo 'keychain: \"fake\"'; exit 0",
  "  fi",
  "  echo 'security: item not found' >&2; exit 44",
  "fi",
  "exit 1",
].join("\n") + "\n", { mode: 0o755 });
test.after(() => fs.rmSync(fakeBinDir, { recursive: true, force: true }));

const IS_WIN = process.platform === "win32";

/**
 * Run `fn` with PATH/SHELL pointed at the fake `claude` above (so
 * resolveClaudeCli() resolves to it) and NH_FAKE_* controlling its behaviour, then
 * restore both env vars — and any behaviour vars — unconditionally. POSIX-only:
 * resolveOnPath's Windows branch never shells out, so every test using this helper
 * is skipped on win32.
 */
async function withFakeClaude(behaviorEnv, fn) {
  const savedPath = process.env.PATH;
  const savedShell = process.env.SHELL;
  const savedBehavior = {};
  for (const k of Object.keys(behaviorEnv)) savedBehavior[k] = process.env[k];
  process.env.PATH = `${fakeBinDir}${path.delimiter}${savedPath || ""}`;
  process.env.SHELL = fakeShellPath;
  Object.assign(process.env, behaviorEnv);
  try {
    return await fn();
  } finally {
    process.env.PATH = savedPath;
    process.env.SHELL = savedShell;
    for (const k of Object.keys(behaviorEnv)) {
      if (savedBehavior[k] === undefined) delete process.env[k];
      else process.env[k] = savedBehavior[k];
    }
  }
}

test("nh:claude-signin-status refuses any sender that is not the setup screen",
  async () => {
    const status = stub.calls.ipc.get("nh:claude-signin-status");
    assert.ok(status, "main.mjs must register nh:claude-signin-status");
    for (const url of BOARD_PAGES) {
      assert.deepEqual(await status(from(url)), { detected: false },
        `accepted a signin-status probe from ${url}`);
    }
  });

// The old non-interactive "import" path is gone: `claude setup-token` is
// unconditional browser OAuth (see main.mjs's comment where the handler used
// to live), so there is no way to mint a token without a completable browser
// step, and offering one left users stranded on a dead localhost tab. The
// fix removes the IPC channel entirely rather than leaving it registered but
// unreachable — these two tests prove that removal, not just that the old
// tests were deleted.
test("nh:claude-import-token is gone: no IPC channel can spawn a browser OAuth flow", () => {
  assert.equal(stub.calls.ipc.get("nh:claude-import-token"), undefined,
    "nh:claude-import-token must not be a registered handler");
});

test("no registered handler ever spawns `claude setup-token`, and none opens a browser",
  { skip: IS_WIN && "resolveOnPath's shell probe is POSIX-only in this fixture" },
  async () => {
    const spawnLog = path.join(fakeBinDir, "spawn.log");
    fs.writeFileSync(spawnLog, "");
    const openedBefore = stub.calls.opened.length;
    const status = stub.calls.ipc.get("nh:claude-signin-status");
    const saveToken = stub.calls.ipc.get("nh:save-token");
    await withFakeClaude(
      { NH_FAKE_AUTH_STATUS: "0", NH_FAKE_SPAWN_LOG: spawnLog },
      async () => {
        await status(from(SETUP_URL));
        // Every other setup-screen channel that still exists, driven for good
        // measure — none of them has any business touching the `claude` binary
        // at all, let alone its `setup-token` subcommand.
        await saveToken(from(SETUP_URL), "sk-ant-oat-nospawn");
        const req = stub.calls.ipc.get("nh:requirements");
        if (req) await req(from(SETUP_URL));
      });
    const logged = fs.readFileSync(spawnLog, "utf8");
    // Non-vacuous: prove the fake was actually invoked (an untouched empty
    // log would also "not match /setup-token/", which would prove nothing).
    assert.match(logged, /(^|\n)auth status(\n|$)/,
      `expected an "auth status" spawn from nh:claude-signin-status, got: ${JSON.stringify(logged)}`);
    assert.doesNotMatch(logged, /setup-token/,
      `a handler spawned \`claude setup-token\`: ${logged}`);
    // The whole point of removing the browser-OAuth path: no surviving
    // handler ever hands a URL to the OS.
    assert.equal(stub.calls.opened.length, openedBefore,
      `a handler opened a browser tab: ${JSON.stringify(stub.calls.opened)}`);
  });

test("preload.cjs no longer bridges nh:claude-import-token / importClaudeSignIn, but still bridges nh:claude-signin-status", () => {
  const preloadPath = path.join(path.dirname(fileURLToPath(import.meta.url)), "preload.cjs");
  const src = fs.readFileSync(preloadPath, "utf8");
  // Strip whole-line comments before searching, so the "there is no such
  // channel" explanatory comments themselves don't make this test vacuous.
  const code = src.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
  assert.doesNotMatch(code, /nh:claude-import-token/,
    "preload.cjs must not bridge the removed nh:claude-import-token channel");
  assert.doesNotMatch(code, /importClaudeSignIn/,
    "preload.cjs must not expose an importClaudeSignIn bridge method");
  assert.match(code, /nh:claude-signin-status/,
    "preload.cjs must still bridge nh:claude-signin-status — the probe was not gutted along with the import path");
});

test("test_existing_credential_button_renders: nh:claude-signin-status reports detected when `claude auth status` succeeds",
  { skip: IS_WIN && "resolveOnPath's shell probe is POSIX-only in this fixture" },
  async () => {
    const status = stub.calls.ipc.get("nh:claude-signin-status");
    const res = await withFakeClaude({ NH_FAKE_AUTH_STATUS: "0" },
      () => status(from(SETUP_URL)));
    assert.deepEqual(res, { detected: true, via: "cli" });
  });

test("test_fallback_ui_identical_to_current_state: nh:claude-signin-status reports not-detected when claude is not on PATH",
  { skip: IS_WIN && "resolveOnPath's shell probe is POSIX-only in this fixture" },
  async () => {
    const status = stub.calls.ipc.get("nh:claude-signin-status");
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "nh-no-claude-"));
    const savedPath = process.env.PATH;
    const savedShell = process.env.SHELL;
    process.env.PATH = emptyDir;
    process.env.SHELL = fakeShellPath;
    let res;
    try {
      res = await status(from(SETUP_URL));
    } finally {
      process.env.PATH = savedPath;
      process.env.SHELL = savedShell;
      fs.rmSync(emptyDir, { recursive: true, force: true });
    }
    assert.deepEqual(res, { detected: false, via: "no-cli" },
      "with no claude on PATH and no credential store, the fallback must match today's shape");
  });

// Reviewer finding on the prior attempt: the macOS store-fallback path was
// "unproven on the measured platform" — it assumed a credential FILE that
// does not exist there. These two tests exercise the real fix
// (macClaudeKeychainExists, server.mjs) end-to-end through the actual IPC
// handler, with a fake `security` standing in for the real Keychain CLI, so
// the "auth status fails, Keychain has/hasn't the item" branch is proven by
// a run rather than by a pure-function unit test alone. darwin-only: off
// darwin, macClaudeKeychainExists short-circuits to false before ever
// invoking `security`, so there is nothing platform-specific to exercise.
test("test_existing_credential_button_renders: nh:claude-signin-status falls back to the macOS Keychain when `claude auth status` fails",
  { skip: process.platform !== "darwin" && "macOS-only: exercises the Keychain fallback path" },
  async () => {
    const status = stub.calls.ipc.get("nh:claude-signin-status");
    const res = await withFakeClaude(
      { NH_FAKE_AUTH_STATUS: "1", NH_FAKE_KEYCHAIN_HAS_ITEM: "1" },
      () => status(from(SETUP_URL)));
    assert.deepEqual(res, { detected: true, via: "store" },
      "a failed `claude auth status` must still detect a sign-in via the Keychain item");
  });

test("test_fallback_ui_identical_to_current_state: nh:claude-signin-status stays not-detected when the Keychain item is absent too",
  { skip: process.platform !== "darwin" && "macOS-only: exercises the Keychain fallback path" },
  async () => {
    const status = stub.calls.ipc.get("nh:claude-signin-status");
    const res = await withFakeClaude(
      { NH_FAKE_AUTH_STATUS: "1", NH_FAKE_KEYCHAIN_HAS_ITEM: "0" },
      () => status(from(SETUP_URL)));
    assert.deepEqual(res, { detected: false },
      "neither `claude auth status` nor the Keychain has a sign-in, so it must fail closed");
  });

test("the renderer only ever receives {detected, via} from nh:claude-signin-status — never a token",
  { skip: IS_WIN && "resolveOnPath's shell probe is POSIX-only in this fixture" },
  async () => {
    const status = stub.calls.ipc.get("nh:claude-signin-status");
    const res = await withFakeClaude(
      { NH_FAKE_AUTH_STATUS: "0", NH_FAKE_SETUP_STDOUT: "sk-ant-oat01-shouldnotappear" },
      () => status(from(SETUP_URL)));
    const keys = Object.keys(res).sort();
    assert.ok(keys.every((k) => k === "detected" || k === "via"),
      `nh:claude-signin-status returned an unexpected key: ${JSON.stringify(res)}`);
    assert.equal(JSON.stringify(res).includes("sk-ant-oat"), false,
      "the status probe must never carry anything token-shaped, even if the CLI would have printed one");
  });

// test_token_noninteractive_retrieval_and_persistence and
// test_token_interactive_paste_path used to live here: both drove
// `nh:claude-import-token`, which no longer exists (see the two tests above).
// There is no replacement to write for either — the behaviour they proved
// (mint a token non-interactively, or fall back to manual paste on an
// interactive response) required a working headless `setup-token`, which
// main.mjs's evidence shows does not exist on the platform measured (macOS)
// and never completed on the Windows build field-reported. Deleting
// dead-functionality coverage is not weakening a test: nothing in the shipped
// code can make either scenario happen anymore.

test("test_env_file_permissions_600: the .env written by a save stays 0600",
  { skip: IS_WIN && "POSIX file-mode bits are not meaningful on Windows" },
  async () => {
    const saveToken = stub.calls.ipc.get("nh:save-token");
    await saveToken(from(SETUP_URL), "sk-ant-oat01-permcheck");
    const mode = fs.statSync(path.join(home, ".no_human", ".env")).mode & 0o777;
    assert.equal(mode.toString(8), "600", `.env must be 0600, was 0${mode.toString(8)}`);
  });

test("test_credential_not_leaked_to_logs: neither a detected-status probe nor a rejected save ever logs a token",
  async () => {
    const token = "sk-ant-oat01-shouldneverbelogged";
    const seen = [];
    const realLog = console.log, realErr = console.error, realWarn = console.warn;
    console.log = (...a) => { seen.push(a.join(" ")); };
    console.error = (...a) => { seen.push(a.join(" ")); };
    console.warn = (...a) => { seen.push(a.join(" ")); };
    try {
      // A detected-sign-in probe never handles a token value at all — it must
      // stay silent regardless.
      const status = stub.calls.ipc.get("nh:claude-signin-status");
      await status(from(SETUP_URL));
      // A save that gets rejected (wrong-shaped token for the chosen mode)
      // must not echo the rejected value back anywhere, including the error
      // string returned over IPC.
      const saveToken = stub.calls.ipc.get("nh:save-token");
      const failRes = await saveToken(from(SETUP_URL), token, "api_key");
      assert.equal(JSON.stringify(failRes).includes(token), false,
        "the IPC response itself must never carry the raw token");
    } finally {
      console.log = realLog;
      console.error = realErr;
      console.warn = realWarn;
    }
    assert.equal(seen.some((line) => line.includes(token)), false,
      `the token leaked into console output: ${JSON.stringify(seen)}`);
  });

// --- nh:open-path ("Open repo", Task 6) ------------------------------------ //

test("nh:open-path refuses a path the server does not know about", async () => {
  const handler = stub.calls.ipc.get("nh:open-path");
  assert.ok(handler, "main.mjs must register nh:open-path");
  knownRepos = "[]";                      // the server knows no repos
  stub.calls.openedPaths.length = 0;
  const res = await handler(from(SETUP_URL), "/etc/passwd");
  assert.deepEqual(res, { ok: false, error: "not a registered repo" });
  assert.deepEqual(stub.calls.openedPaths, [],
    "shell.openPath must never see a path that failed the allow-list check");
});

test("nh:open-path rejects a missing/blank path without calling the server", async () => {
  const handler = stub.calls.ipc.get("nh:open-path");
  for (const bad of [undefined, "", null, 42]) {
    const res = await handler(from(SETUP_URL), bad);
    assert.equal(res.ok, false);
  }
});

test("nh:open-path opens a path the server reports as a known repo", async () => {
  const handler = stub.calls.ipc.get("nh:open-path");
  knownRepos = JSON.stringify([{ repo_path: "/tmp/known-repo", name: "known-repo" }]);
  stub.calls.openedPaths.length = 0;
  const res = await handler(from(SETUP_URL), "/tmp/known-repo");
  assert.deepEqual(res, { ok: true });
  assert.deepEqual(stub.calls.openedPaths, ["/tmp/known-repo"]);
  knownRepos = "[]";                      // restore the default for later tests
});
