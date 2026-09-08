// Unit tests for the shell's server-discovery helpers (node --test, no
// electron needed — these run in the standing web gate's node tier).
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import test from "node:test";

import {
  classifyBackendFailure, claudeAuthStatus, configuredPort, isAppOrigin,
  macClaudeKeychainExists, makeOutputCapture, probe, spawnOptionsFor,
  tailDetail, waitForServer,
} from "./server.mjs";
import * as serverModule from "./server.mjs";
import { MAC_KEYCHAIN_SERVICE } from "./setupGate.mjs";
import { freePort } from "./testing/ports.mjs";

function serve(handler) {
  return new Promise((resolve) => {
    const srv = http.createServer(handler);
    srv.listen(0, "127.0.0.1", () =>
      resolve({ srv, origin: `http://127.0.0.1:${srv.address().port}` }));
  });
}

test("probe: up only when /api/tasks answers 2xx", async () => {
  let seen = null;
  const { srv, origin } = await serve((req, res) => {
    seen = req.url;
    if (req.url.split("?")[0] === "/api/tasks") { res.end("[]"); return; }
    res.statusCode = 404; res.end();
  });
  try {
    assert.equal(await probe(origin), "up");
    assert.equal(seen, "/api/tasks?limit=1");
  } finally { srv.close(); }
});

test("probe: down on refused connection and on 5xx", async () => {
  assert.equal(await probe("http://127.0.0.1:1"), "down");
  let count = 0;
  const { srv, origin } = await serve((_req, res) => {
    count += 1;
    res.statusCode = 500; res.end();
  });
  try {
    assert.equal(await probe(origin), "down");
    assert.equal(count, 1);
  } finally { srv.close(); }
});

test("probe: a first-request timeout is retried once and the second answer wins", async () => {
  let count = 0;
  const held = [];
  const { srv, origin } = await serve((req, res) => {
    count += 1;
    if (count === 1) { held.push(req.socket); return; }
    res.end("[]");
  });
  try {
    assert.equal(await probe(origin, 200, 20), "up");
    assert.equal(count, 2);
  } finally {
    for (const sock of held) sock.destroy();
    srv.close();
  }
});

test("probe: a server that never answers is 'down', bounded", async () => {
  let count = 0;
  const held = [];
  const { srv, origin } = await serve((req) => {
    count += 1;
    held.push(req.socket);
  });
  try {
    const t0 = Date.now();
    const result = await probe(origin);
    const elapsed = Date.now() - t0;
    assert.equal(result, "down");
    assert.ok(elapsed < 4000, `expected < 4000ms, got ${elapsed}ms`);
    assert.equal(count, 2);
  } finally {
    for (const sock of held) sock.destroy();
    srv.close();
  }
});

test("probe: never throws", async () => {
  const result1 = await probe("http://127.0.0.1:1");
  assert.equal(typeof result1, "string");
  assert.ok(["up", "down"].includes(result1));

  const result2 = await probe("not-a-url");
  assert.equal(typeof result2, "string");
  assert.ok(["up", "down"].includes(result2));

  const { srv, origin } = await serve((req) => {
    req.socket.destroy();
  });
  try {
    const result3 = await probe(origin, 200, 20);
    assert.equal(typeof result3, "string");
    assert.ok(["up", "down"].includes(result3));
  } finally {
    srv.close();
  }
});

test("waitForServer: resolves true once the server comes up", async () => {
  let ready = false;
  const { srv, origin } = await serve((_req, res) => {
    if (!ready) { res.statusCode = 500; res.end(); return; }
    res.end("[]");
  });
  setTimeout(() => { ready = true; }, 300);
  try {
    assert.equal(await waitForServer(origin, 5000, 100), true);
  } finally { srv.close(); }
});

test("waitForServer: false past the deadline", async () => {
  assert.equal(await waitForServer("http://127.0.0.1:1", 400, 100), false);
});

test("waitForServer: an aborted signal stops the poll loop, not just the return value", async () => {
  let count = 0;
  // A plain 500 (not connection-refused/timeout) is "down" in exactly one
  // probe() call — probe() only retries on "unreachable" — so `count` tracks
  // probe attempts 1:1, making the post-abort assertion below exact.
  const { srv, origin } = await serve((_req, res) => {
    count += 1;
    res.statusCode = 500; res.end();
  });
  try {
    const ctrl = new AbortController();
    const t0 = Date.now();
    const p = waitForServer(origin, 30000, 100, { signal: ctrl.signal });
    while (count < 1) await new Promise((r) => setTimeout(r, 5));
    ctrl.abort();
    assert.equal(await p, false);
    assert.ok(Date.now() - t0 < 300,
      `abort must resolve within ~1 interval, took ${Date.now() - t0}ms`);
    const after = count;
    await new Promise((r) => setTimeout(r, 3 * 100));
    // The mechanism assertion: the loop itself is gone, not merely the
    // already-settled return value. On the pre-fix code waitForServer ignores
    // the 4th argument entirely, so it keeps probing every 100ms regardless.
    assert.equal(count, after,
      `probe count grew from ${after} to ${count} after abort — loop was not cancelled`);
  } finally { srv.close(); }
});

test("isAppOrigin: same-origin stays in-window, everything else leaves", () => {
  const o = "http://127.0.0.1:8420";
  assert.equal(isAppOrigin("http://127.0.0.1:8420/stats", o), true);
  assert.equal(isAppOrigin("https://code.example.com/x/pull/1", o), false);
  assert.equal(isAppOrigin("http://127.0.0.1:9999/", o), false);
  assert.equal(isAppOrigin("not a url", o), false);
});

test("server.mjs exports no setup-token runner", () => {
  // runClaudeSetupToken (the only spawn site for `claude setup-token`) is
  // deleted, not merely unused — this asserts the removal directly against
  // the module's own export surface, so a future re-add would have to
  // knowingly edit this test rather than silently resurrect a dead spawn.
  const names = Object.keys(serverModule);
  assert.ok(!names.includes("runClaudeSetupToken"),
    `server.mjs still exports runClaudeSetupToken: ${names.join(",")}`);
  for (const name of names) {
    assert.doesNotMatch(name.toLowerCase(), /setuptoken/,
      `unexpected setup-token-shaped export: ${name}`);
  }
});

test("claudeAuthStatus / macClaudeKeychainExists spawn only read-only argv, never setup-token", async () => {
  const calls = [];
  const fakeExec = (bin, args, opts, cb) => {
    calls.push({ bin, args });
    cb(null, "", "");
  };
  await claudeAuthStatus("/usr/local/bin/claude", fakeExec);
  await macClaudeKeychainExists(fakeExec, "darwin");
  assert.equal(calls.length, 2, `expected exactly 2 spawns, got ${JSON.stringify(calls)}`);
  for (const call of calls) {
    assert.doesNotMatch(call.args.join(" "), /setup-token/,
      `a read-only status probe must never invoke setup-token: ${JSON.stringify(call)}`);
  }
  // The two argv shapes are exactly what each function's doc comment claims:
  // `auth status` (no flags that could mutate anything) and a Keychain
  // existence-only lookup (no `-w`, which is what would print the secret).
  assert.deepEqual(calls[0].args, ["auth", "status"]);
  assert.deepEqual(calls[1].args,
    ["find-generic-password", "-s", MAC_KEYCHAIN_SERVICE]);
});

// ------------------------------ E2 tests ---------------------------------- //

import { CLI_HINT_DIRS, NH_EXE_NAME, POSIX_CLI_HINT_DIRS, WINDOWS_CLI_HINT_DIRS,
         bundledNhPath, ensureServer, mergePath, resolveClaudeCli, resolveNhBin,
         resolveNodeBin, stopServer, widenPath,
         taskkillArgs, windowsPathLookup } from "./server.mjs";
import { mkdirSync } from "node:fs";
import { mkdtempSync, writeFileSync, chmodSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";

const IS_WIN = process.platform === "win32";

/**
 * Write a fake `nh` that is launchable on BOTH platforms, and return how to
 * launch it.
 *
 * These fixtures were POSIX shebang scripts (`#!/bin/sh`, `#!${process.execPath}`)
 * made runnable with `chmod 0o755`. Windows has none of that machinery: no
 * shebang, no execute bit, and Node 22 refuses to spawn a .cmd/.bat shim
 * without `shell: true`. So the SAME JavaScript body is delivered differently
 * per platform — POSIX keeps the shebang script and launches it directly;
 * Windows writes a plain .js and launches `node` with it as an argument.
 *
 * Nothing in the code under test changes: `ensureServer` already accepts
 * `nhArgs`, so the difference is entirely in how the fixture is invoked. The
 * assertions each test makes are untouched, which is the point — this restores
 * these tests on Windows instead of exempting them.
 */
function fakeNh(dir, jsBody) {
  if (IS_WIN) {
    const js = join(dir, "nh.js");
    writeFileSync(js, jsBody);
    return { bin: process.execPath, args: [js] };
  }
  const sh = join(dir, "nh");
  // Absolute shebang: several callers narrow PATH deliberately, and resolving
  // the interpreter must not be what those tests accidentally measure.
  writeFileSync(sh, `#!${process.execPath}\n${jsBody}`);
  chmodSync(sh, 0o755);
  return { bin: sh, args: [] };
}

test("resolveNhBin: NH_BIN wins when it exists; missing → shell/known-path fallbacks", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nhbin-"));
  const fake = join(dir, "nh");
  writeFileSync(fake, "#!/bin/sh\nexit 0\n");
  chmodSync(fake, 0o755);
  assert.equal(await resolveNhBin({ NH_BIN: fake, SHELL: "/bin/sh" }), fake);
  // A bogus NH_BIN must not be returned.
  const got = await resolveNhBin({ NH_BIN: join(dir, "missing"),
                                   SHELL: "/usr/bin/false" });
  assert.notEqual(got, join(dir, "missing"));
});

test("bundledNhPath: finds Resources/nh-server/nh, empty when absent or unpackaged", () => {
  const res = mkdtempSync(join(tmpdir(), "nhres-"));
  assert.equal(bundledNhPath(res), "", "no bundle yet → empty");
  mkdirSync(join(res, "nh-server"));
  // NH_EXE_NAME, not a literal "nh": PyInstaller emits nh.exe on Windows, so a
  // hardcoded fixture asserted the wrong filename there. Same assertion, on the
  // name the platform actually produces.
  writeFileSync(join(res, "nh-server", NH_EXE_NAME), "#!/bin/sh\nexit 0\n");
  assert.equal(bundledNhPath(res), join(res, "nh-server", NH_EXE_NAME));
  // Outside Electron process.resourcesPath is undefined — must not throw.
  assert.equal(bundledNhPath(undefined), "");
});

test("NH_EXE_NAME is the binary PyInstaller actually emits for this platform", () => {
  assert.equal(NH_EXE_NAME, process.platform === "win32" ? "nh.exe" : "nh");
  // The exeName is injectable so BOTH platforms' resolution is verifiable from
  // either host — otherwise the Windows path is unreachable on the Mac that
  // builds the DMG, and vice versa.
  const res = mkdtempSync(join(tmpdir(), "nhres-"));
  mkdirSync(join(res, "nh-server"));
  writeFileSync(join(res, "nh-server", "nh.exe"), "");
  assert.equal(bundledNhPath(res, "nh.exe"), join(res, "nh-server", "nh.exe"));
  assert.equal(bundledNhPath(res, "nh"), "", "must not match a different name");
});

test("resolveNhBin: bundled nh wins over PATH, but NH_BIN still wins over bundled", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nhbin-"));
  const bundled = join(dir, "bundled-nh");
  writeFileSync(bundled, "#!/bin/sh\nexit 0\n");
  chmodSync(bundled, 0o755);
  // No NH_BIN → the bundle is used instead of the login shell's nh.
  assert.equal(await resolveNhBin({ SHELL: "/usr/bin/false" }, [], bundled),
               bundled);
  // Explicit NH_BIN outranks the bundle (documented escape hatch).
  const override = join(dir, "override-nh");
  writeFileSync(override, "#!/bin/sh\nexit 0\n");
  chmodSync(override, 0o755);
  assert.equal(await resolveNhBin({ NH_BIN: override, SHELL: "/usr/bin/false" },
                                  [], bundled), override);
});

test("resolveClaudeCli: shell hit wins over hint dirs", { skip: IS_WIN
  ? "the Windows branch reads env.PATH via windowsPathLookup instead of "
    + "shelling out, so this exec-stub assertion is POSIX-only; the Windows "
    + "lookup itself is covered by the windowsPathLookup custom-names test below"
  : false }, async () => {
  const execHit = (sh, args, opts, cb) => cb(null, "/opt/homebrew/bin/claude\n", "");
  assert.equal(await resolveClaudeCli({ SHELL: "/bin/zsh" }, execHit, () => true),
               "/opt/homebrew/bin/claude");
});

test("resolveClaudeCli: shell miss falls to CLI_HINT_DIRS, then empty", async () => {
  const execMiss = (sh, args, opts, cb) => cb(new Error("not found"), "", "");
  // The hint-dir scan runs on BOTH platforms (only the winNames/posix name
  // differs), so this half is not gated on IS_WIN.
  const hitDir = CLI_HINT_DIRS[0];
  const wanted = IS_WIN ? join(hitDir, "claude.cmd") : join(hitDir, "claude");
  assert.equal(
    await resolveClaudeCli({ SHELL: "/bin/zsh" }, execMiss, (p) => p === wanted),
    wanted, "a claude sitting in the first hint dir must be found");
  // Nothing anywhere → "", never a throw.
  assert.equal(await resolveClaudeCli({ SHELL: "/bin/zsh" }, execMiss, () => false), "");
});

test("resolveNodeBin: same shape as resolveClaudeCli, for `node`", { skip: IS_WIN
  ? "exec-stub path is POSIX-only, see resolveClaudeCli's equivalent skip" : false },
  async () => {
  const execHit = (sh, args, opts, cb) => cb(null, "/opt/homebrew/bin/node\n", "");
  assert.equal(await resolveNodeBin({ SHELL: "/bin/zsh" }, execHit, () => true),
               "/opt/homebrew/bin/node");
  const execMiss = (sh, args, opts, cb) => cb(new Error("not found"), "", "");
  assert.equal(await resolveNodeBin({ SHELL: "/bin/zsh" }, execMiss, () => false), "");
});

test("windowsPathLookup: a custom name list finds claude.cmd/claude.exe, not nh's names", () => {
  const has = (set) => (p) => set.has(p);
  // Default names are still nh's — an unrelated caller must not silently start
  // matching claude just because the parameter now exists.
  assert.equal(
    windowsPathLookup({ PATH: "C:\\a" }, has(new Set(["C:\\a\\claude.exe"])), ";"),
    "", "the default name list must still be nh's, not claude's");
  assert.equal(
    windowsPathLookup({ PATH: "C:\\a" }, has(new Set(["C:\\a\\claude.cmd"])), ";",
                       ["claude.cmd", "claude.exe"]),
    "C:\\a\\claude.cmd");
  // Order within the custom list is honoured, same as nh's .exe-before-.cmd rule.
  assert.equal(
    windowsPathLookup({ PATH: "C:\\a" },
      has(new Set(["C:\\a\\claude.cmd", "C:\\a\\claude.exe"])), ";",
      ["claude.cmd", "claude.exe"]),
    "C:\\a\\claude.cmd", "claude.cmd is listed first for this cmd, unlike nh's .exe-first order");
});

test("ensureServer: attaches without spawning when the server is up", async () => {
  const { srv, origin } = await serve((req, res) => { res.end("[]"); });
  try {
    const state = await ensureServer({ origin, env: { NH_BIN: "/nope" } });
    assert.equal(state.status, "attached");
    assert.equal(state.child, undefined);
  } finally { srv.close(); }
});

test("ensureServer: spawns a fake nh and waits until the port answers", async () => {
  // The fake `nh` starts a real HTTP server on a fixed port via node.
  const dir = mkdtempSync(join(tmpdir(), "nhbin-"));
  const { port } = await freePort();
  const { bin, args } = fakeNh(dir, `
const http = require("node:http");
setTimeout(() => {
  http.createServer((req, res) => res.end("[]")).listen(${port}, "127.0.0.1");
}, 300);
setInterval(() => {}, 1000);
`);
  const origin = "http://127.0.0.1:" + port;
  const state = await ensureServer({
    // Deadline, not a fixed wait: ensureServer returns as soon as the port
    // answers (~400ms idle). A roomier deadline only affects a genuinely slow
    // boot under load, which is the flake this widens out (gate finding #2).
    origin, spawnTimeoutMs: 20000, env: { NH_BIN: bin }, nhArgs: args });
  try {
    assert.equal(state.status, "spawned");
    assert.ok(state.child.pid > 0);
  } finally {
    stopServer(state);
  }
});

test("ensureServer: onSpawn fires immediately, not when the wait finishes", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nhbin-"));
  const { bin, args } = fakeNh(dir, "setTimeout(() => {}, 30000);\n");
  const seen = [];
  const t0 = Date.now();
  const state = await ensureServer({
    origin: "http://127.0.0.1:1", spawnTimeoutMs: 900, env: { NH_BIN: bin },
    nhArgs: args, onSpawn: (c) => seen.push({ pid: c.pid, at: Date.now() - t0 }) });
  try {
    assert.equal(seen.length, 1, "the caller must be able to track it at once");
    assert.ok(seen[0].pid > 0);
    assert.ok(seen[0].at < 500,
      `onSpawn must fire at spawn time, fired at ${seen[0].at}ms`);
  } finally { stopServer(state); }
});

test("ensureServer: failed resolution reports nh-not-found without spawning", async () => {
  const state = await ensureServer({
    origin: "http://127.0.0.1:1",
    env: { NH_BIN: "/definitely/missing", SHELL: "/usr/bin/false" },
    fallbackPaths: [],   // this machine has a real nh — keep it out
    spawnTimeoutMs: 500 });
  assert.equal(state.status, "failed");
  assert.equal(state.reason, "nh-not-found");
});

test("ensureServer: a spawn error stops the start-probe loop instead of polling out the window",
  { skip: IS_WIN ? "POSIX exec permission bits" : false }, async () => {
  // Mirrors mainSaveFailure.test.mjs's recipe: a binary that EXISTS (so
  // resolveNhBin returns it) but has no +x bit, so spawn() emits EACCES.
  const dir = mkdtempSync(join(tmpdir(), "nheacces-"));
  const notExecutable = join(dir, "nh");
  writeFileSync(notExecutable, "#!/bin/sh\nexit 0\n");
  chmodSync(notExecutable, 0o644);
  let count = 0;
  // Answers "down" so the post-race confirmation probe (server.mjs:619)
  // still finds it not up and the reason resolves through spawn-error.
  const { srv, origin } = await serve((_req, res) => {
    count += 1;
    res.statusCode = 500; res.end();
  });
  try {
    const state = await ensureServer({
      origin, env: { NH_BIN: notExecutable }, nhArgs: [], spawnTimeoutMs: 30000 });
    assert.equal(state.status, "failed");
    assert.equal(state.reason, "spawn-error");
    // A short grace period, well under one 500ms interval: ensureServer's own
    // pre-spawn "already up?" probe (server.mjs:563) and a waitForServer
    // probe that was already in flight when the race was decided may still
    // be settling on the microtask queue the instant ensureServer resolves.
    // That is normal completion of work already started, not a new loop
    // iteration — waiting here lets it land before the snapshot below.
    await new Promise((r) => setTimeout(r, 150));
    const after = count;
    // Three real 500ms intervals: on the pre-fix code the orphaned
    // waitForServer loop keeps probing every 500ms for the remainder of the
    // 30s spawnTimeoutMs, so `count` would keep climbing here.
    await new Promise((r) => setTimeout(r, 3 * 500));
    assert.equal(count, after,
      `probe count grew from ${after} to ${count} after ensureServer resolved — ` +
      `the losing waitForServer was not cancelled`);
  } finally { srv.close(); }
});

test("stopServer: ONLY kills a spawned child — attached/failed states are never killed", () => {
  let killed = false;
  const child = { kill: () => { killed = true; } };
  assert.equal(stopServer({ status: "attached" }), false);
  // The gate itself must protect attached — even with a child present
  // (review: without this, only production convention protects attached).
  assert.equal(stopServer({ status: "attached", child }), false);
  // A "failed" state that still carries a child IS ours: ensureServer leaves a
  // slow-booting nh running and it may yet bind the port. Refusing to stop it
  // orphaned the process (reparented to init, still holding the port).
  assert.equal(stopServer({ status: "failed", reason: "nh-not-found" }), false,
    "no child -> nothing to stop");
  assert.equal(killed, false, "attached must never kill");
  assert.equal(stopServer({ status: "spawned", child }), true);
  assert.equal(killed, true);

  // An already-exited child is not "stopped" — reporting true inflated the
  // drain count and preceded a group kill on a possibly-reused PID.
  assert.equal(stopServer({ status: "spawned",
    child: { pid: 999999, exitCode: 0, signalCode: null, kill: () => {} } }), false);

  let killed2 = false;
  const child2 = { kill: () => { killed2 = true; } };
  assert.equal(stopServer({ status: "failed", reason: "spawn-timeout", child: child2 }),
               true, "a spawn-timeout child is ours to stop, not an orphan");
  assert.equal(killed2, true);
});

// --- the spawned server must be able to find `claude` ------------------------
// The Agent SDK resolves its CLI off PATH. A macOS GUI app inherits launchd's
// PATH, so without this the packaged build serves a healthy board on which
// EVERY task dies with CLINotFoundError.

// The delimiter is passed EXPLICITLY in the POSIX cases below. It used to be a
// hardcoded ":" inside mergePath; it is now a parameter defaulting to
// path.delimiter, so naming it here keeps these assertions testing POSIX
// semantics on every host instead of quietly becoming Windows assertions when
// run on Windows. The Windows semantics get their own cases further down.
test("mergePath: appends missing dirs, never reorders or duplicates", () => {
  const exists = () => true;
  assert.equal(mergePath("/usr/bin:/bin", ["/opt/homebrew/bin"], exists, ":"),
    "/usr/bin:/bin:/opt/homebrew/bin", "hint dirs go AFTER the inherited PATH");
  assert.equal(mergePath("/usr/bin:/opt/homebrew/bin", ["/opt/homebrew/bin"], exists, ":"),
    "/usr/bin:/opt/homebrew/bin", "an entry already present is not duplicated");
  assert.equal(mergePath("", ["/opt/homebrew/bin"], exists, ":"), "/opt/homebrew/bin");
});

test("mergePath: never appends a directory that does not exist", () => {
  assert.equal(mergePath("/usr/bin", ["/nope/nowhere"], () => false, ":"), "/usr/bin");
});

test("mergePath: makes a Homebrew claude reachable from launchd's PATH", () => {
  // The real failure: /opt/homebrew/bin is in NEITHER launchd's default PATH
  // nor the SDK's hardcoded fallback list, so a Homebrew install is invisible.
  // POSIX_CLI_HINT_DIRS by name, not the platform-selected CLI_HINT_DIRS: this
  // is a statement about the macOS list, and it must keep holding when the
  // suite runs on Windows.
  const launchd = "/usr/bin:/bin:/usr/sbin:/sbin";
  const onlyBrew = (d) => d === "/opt/homebrew/bin";
  assert.ok(mergePath(launchd, POSIX_CLI_HINT_DIRS, onlyBrew, ":").split(":")
    .includes("/opt/homebrew/bin"),
    "a Homebrew-installed claude must be reachable by the spawned server");
});

// ------------------------- Windows path semantics ------------------------- //
// These run on every host: the logic is pure and the delimiter is injected, so
// the Windows behaviour is verifiable from the Mac that builds the DMG.

test("mergePath: a Windows PATH is split on ';' and never shredded on ':'", () => {
  // THE DEFECT this guards. Splitting a Windows PATH on ":" does not merely
  // fail to find entries, it destroys them: every entry contains a drive colon,
  // so "C:\\Windows;C:\\Users\\x" would become ["C", "\\Windows;C", "\\Users\\x"]
  // and the PATH handed to the spawned server would be garbage.
  const exists = () => true;
  const base = "C:\\Windows;C:\\Users\\x\\bin";
  const got = mergePath(base, ["C:\\hints"], exists, ";");
  assert.equal(got, "C:\\Windows;C:\\Users\\x\\bin;C:\\hints");
  assert.ok(got.split(";").includes("C:\\Windows"),
    "a drive-qualified entry must survive intact");
  assert.equal(mergePath(base, [], exists, ";"), base,
    "with nothing to add the PATH must come back byte-identical");
});

// THE DEFECT this guards (2026-08-17, packaged Windows app). `process.env` is a
// case-insensitive proxy, but the SPREAD COPY handed to spawn is a plain
// object keyed by the OS spelling — `Path` on Windows. `copy.PATH` was
// undefined, so the child got a PATH of hint dirs only and its inherited
// `Path` was dropped: no System32, no git, every task crashed on the first
// `git` spawn (WinError 2) and `icacls` was "not found". Only Git Bash, whose
// env says `PATH`, ever launched a working server.
test("widenPath: widens the inherited `Path` spelling in place, never a bare PATH beside it", () => {
  const merge = (v) => `${v ?? "<undefined>"};C:\hints`;
  const env = widenPath({ Path: "C:\Windows\System32;C:\Program Files\Git\cmd", HOME: "x" }, merge);
  assert.deepEqual(Object.keys(env).filter((k) => k.toUpperCase() === "PATH"), ["Path"],
    "exactly ONE path variable, in the spelling the OS gave us");
  assert.equal(env.Path, "C:\Windows\System32;C:\Program Files\Git\cmd;C:\hints",
    "the inherited entries survive and the hints are appended");
  assert.equal(env.HOME, "x", "unrelated variables are untouched");
});

test("widenPath: an exact PATH override still outranks the inherited spelling, and the duplicate is collapsed", () => {
  const merge = (v) => `${v};H`;
  const env = widenPath({ Path: "inherited", PATH: "override" }, merge);
  assert.deepEqual(Object.keys(env), ["PATH"], "the losing spelling is removed so the child cannot receive two");
  assert.equal(env.PATH, "override;H");
});

test("widenPath: POSIX shape (PATH) and a missing variable behave as before", () => {
  const merge = (v) => `${v ?? ""}:/opt/hint`.replace(/^:/, "");
  assert.deepEqual(widenPath({ PATH: "/usr/bin" }, merge), { PATH: "/usr/bin:/opt/hint" });
  assert.deepEqual(widenPath({ HOME: "/h" }, merge), { HOME: "/h", PATH: "/opt/hint" },
    "no variable at all still yields a PATH of the hints, as mergePath('') did");
});

test("mergePath: Windows entries are de-duplicated case-insensitively", () => {
  // Windows paths are case-insensitive, so these name ONE directory. Comparing
  // case-sensitively would append a duplicate and break the documented
  // never-duplicates contract.
  const got = mergePath("C:\\Program Files\\Git", ["c:\\program files\\git"],
                        () => true, ";");
  assert.equal(got, "C:\\Program Files\\Git", "a case-variant duplicate is not appended");
  // The KEY is folded, never the value: original casing must be preserved.
  assert.equal(mergePath("C:\\Windows", ["C:\\Hints"], () => true, ";"),
    "C:\\Windows;C:\\Hints", "appended entries keep their own casing");
});

test("mergePath: POSIX stays case-SENSITIVE (folding must not leak across)", () => {
  assert.equal(mergePath("/usr/Bin", ["/usr/bin"], () => true, ":"),
    "/usr/Bin:/usr/bin", "two distinct POSIX dirs must both survive");
});

test("WINDOWS_CLI_HINT_DIRS: real locations, and mergePath appends them", () => {
  assert.ok(WINDOWS_CLI_HINT_DIRS.length > 0);
  for (const d of WINDOWS_CLI_HINT_DIRS) {
    assert.ok(!d.includes("/"), `a Windows hint dir must be backslashed: ${d}`);
  }
  // A claude installed by `uv tool` under ~\.local\bin must become reachable.
  const uvBin = WINDOWS_CLI_HINT_DIRS[0];
  const only = (d) => d === uvBin;
  assert.ok(mergePath("C:\\Windows;C:\\Windows\\System32",
                      WINDOWS_CLI_HINT_DIRS, only, ";").split(";").includes(uvBin));
});

test("windowsPathLookup: finds nh.exe on PATH, prefers .exe, honours PATH order", () => {
  const has = (set) => (p) => set.has(p);
  // Reads env.PATH rather than shelling out to where.exe, so it is deterministic.
  assert.equal(
    windowsPathLookup({ PATH: "C:\\a;C:\\b" }, has(new Set(["C:\\b\\nh.exe"])), ";"),
    "C:\\b\\nh.exe");
  // Earlier PATH entries win, matching how Windows itself resolves.
  assert.equal(
    windowsPathLookup({ PATH: "C:\\a;C:\\b" },
      has(new Set(["C:\\a\\nh.exe", "C:\\b\\nh.exe"])), ";"),
    "C:\\a\\nh.exe");
  // .cmd and .bat shims are found too — a console entry point is installed as
  // any of the three.
  assert.equal(
    windowsPathLookup({ PATH: "C:\\a" }, has(new Set(["C:\\a\\nh.cmd"])), ";"),
    "C:\\a\\nh.cmd");
  // Within one directory .exe outranks .cmd.
  assert.equal(
    windowsPathLookup({ PATH: "C:\\a" },
      has(new Set(["C:\\a\\nh.cmd", "C:\\a\\nh.exe"])), ";"),
    "C:\\a\\nh.exe");
});

test("windowsPathLookup: accepts either PATH spelling and fails closed", () => {
  const has = (p) => p === "C:\\a\\nh.exe";
  // process.env normalises PATH/Path, a plain object handed in by a caller does
  // not — finding nothing because the key was spelled "Path" would be a silent
  // resolution failure.
  assert.equal(windowsPathLookup({ Path: "C:\\a" }, has, ";"), "C:\\a\\nh.exe");
  assert.equal(windowsPathLookup({ PATH: "C:\\a" }, has, ";"), "C:\\a\\nh.exe");
  // Nothing on PATH, no PATH at all, empty entries — all "" and never a throw.
  assert.equal(windowsPathLookup({ PATH: "C:\\zzz" }, has, ";"), "");
  assert.equal(windowsPathLookup({}, has, ";"), "");
  assert.equal(windowsPathLookup({ PATH: ";;" }, has, ";"), "");
});

test("taskkillArgs: /T always, /F only for SIGKILL", () => {
  // /T is load-bearing: without it only the direct child dies and nh's workers
  // are orphaned still holding the port. Mirrors _windows_try_kill(force=...)
  // in src/no_human/cli/commands.py.
  assert.deepEqual(taskkillArgs(1234, "SIGTERM"), ["/T", "/PID", "1234"]);
  assert.deepEqual(taskkillArgs(1234, "SIGKILL"), ["/F", "/T", "/PID", "1234"]);
  assert.ok(taskkillArgs(1, "SIGTERM").includes("/T"),
    "the graceful form must still take the TREE, or workers outlive the app");
  // The pid is stringified — execFile rejects a number argument outright.
  assert.equal(typeof taskkillArgs(1234, "SIGKILL").at(-1), "string");
});

test("win32 spawn is NOT detached: DETACHED_PROCESS makes Windows ignore CREATE_NO_WINDOW, so every console grandchild (claude.exe, git.exe) would get its own visible empty console", () => {
  const win = spawnOptionsFor("win32");
  assert.equal(win.detached, false);
  assert.equal(win.windowsHide, true);
  const posix = spawnOptionsFor("darwin");
  assert.equal(posix.detached, true, "POSIX keeps the process group — stopServer kills the group");
});

test("ensureServer: the spawned server inherits the widened PATH",
  // On a fresh Windows runner NONE of WINDOWS_CLI_HINT_DIRS exist (no uv tool,
  // no global npm, no ~/.claude), so `CLI_HINT_DIRS.filter(existsSync)` is empty
  // and the test's own guard ("no hint dir exists here; the test proves nothing")
  // fires. The Windows widening LOGIC is covered portably by the pure
  // "WINDOWS_CLI_HINT_DIRS: real locations, and mergePath appends them" test,
  // which runs on every host; only this end-to-end observation depends on a hint
  // dir actually existing, which happens to hold on a POSIX dev/CI box.
  { skip: IS_WIN ? "no WINDOWS_CLI_HINT_DIRS exist on a fresh runner, so the "
    + "spawned-child observation is vacuous; the widening logic is covered by the "
    + "pure WINDOWS_CLI_HINT_DIRS mergePath test" : false },
  async () => {
  const dir = mkdtempSync(join(tmpdir(), "nhpath-"));
  const { port } = await freePort();
  const out = join(dir, "path.txt");
  const { bin, args } = fakeNh(dir, `
require("node:fs").writeFileSync(${JSON.stringify(out)}, process.env.PATH || "");
const http = require("node:http");
http.createServer((req, res) => res.end("[]")).listen(${port}, "127.0.0.1");
setInterval(() => {}, 1000);
`);
  // LAUNCHD'S PATH. Running under a normal shell the hint dirs are already
  // inherited, so the widening is invisible and the test proves nothing —
  // which is exactly the reason this defect shipped. The Windows equivalent is
  // a bare system PATH: an app launched from Explorer or an elevated context
  // need not carry the user's own bin dirs either.
  const narrowPath = IS_WIN
    ? "C:\\Windows\\system32;C:\\Windows"
    : "/usr/bin:/bin:/usr/sbin:/sbin";
  const state = await ensureServer({
    origin: "http://127.0.0.1:" + port, spawnTimeoutMs: 8000,
    env: { NH_BIN: bin, PATH: narrowPath }, nhArgs: args });
  try {
    assert.equal(state.status, "spawned");
    const childPath = readFileSync(out, "utf8").split(delimiter);
    const expected = CLI_HINT_DIRS.filter((d) => existsSync(d));
    assert.ok(expected.length > 0, "no hint dir exists here; the test proves nothing");
    for (const d of expected) {
      assert.ok(childPath.includes(d),
        `the spawned server cannot see ${d}, so the Agent SDK cannot find claude`);
    }
  } finally {
    stopServer(state);
  }
});

// --- configuredPort: a hand-rolled YAML block parser with no tests at all ----
// docs/INSTALLER.md's verification recipe depends on it, and the shell must
// find the same port `nh start` binds or a configured install is stranded.

test("configuredPort: reads server.port, and only inside the server block", () => {
  const dir = mkdtempSync(join(tmpdir(), "nhcfg-"));
  const write = (body) => {
    const f = join(dir, `${Math.random().toString(36).slice(2)}.yaml`);
    writeFileSync(f, body); return f;
  };
  assert.equal(configuredPort(write("server:\n  port: 18994\n")), 18994);
  assert.equal(configuredPort(write("server:\n  host: 127.0.0.1\n  port: 9\n")), 9);
  // A port under a DIFFERENT top-level key must not be picked up.
  assert.equal(configuredPort(write("llm:\n  port: 1234\n")), 8420);
  // The block ends at the next top-level key.
  assert.equal(configuredPort(write("server:\n  host: x\nllm:\n  port: 1234\n")), 8420);
});

test("configuredPort: falls back to 8420 rather than throwing", () => {
  const dir = mkdtempSync(join(tmpdir(), "nhcfg-"));
  assert.equal(configuredPort(join(dir, "absent.yaml")), 8420, "no config file");
  const f = join(dir, "junk.yaml");
  writeFileSync(f, "not: [valid\n  yaml\n");
  assert.equal(configuredPort(f), 8420, "unparseable config must not crash the shell");
});

test("stopServer: kills the process GROUP, not just the direct child", async () => {
  // This is the entire reason for detached:true. Signalling only the direct
  // child left nh's workers alive at PPID 1, still holding the port.
  const dir = mkdtempSync(join(tmpdir(), "nhgrp-"));
  //
  // The MECHANISM differs by platform but the guarantee does not, so this test
  // is deliberately written against the guarantee. POSIX kills the process
  // group that detached:true created. Windows has no killable process group —
  // it walks the tree with `taskkill /T` instead, exactly as
  // src/no_human/testing/runner.py::_kill_process_tree does. If that dispatch
  // regressed to a plain child.kill(), the grandchild below would survive on
  // Windows and this assertion would catch it.
  const marker = join(dir, "grandchild.pid");
  const { bin, args } = fakeNh(dir, `
const { spawn } = require("node:child_process");
const kid = spawn(${JSON.stringify(process.execPath)}, ["-e", "setInterval(()=>{},1000)"],
                  { windowsHide: true });
require("node:fs").writeFileSync(${JSON.stringify(marker)}, String(kid.pid));
setInterval(() => {}, 1000);
`);
  const state = await ensureServer({
    origin: "http://127.0.0.1:1", spawnTimeoutMs: 700,
    env: { NH_BIN: bin }, nhArgs: args });
  const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
  // Poll for the worker's BIRTH as well as its death: a bare read here went
  // ENOENT under parallel load and produced a random red build.
  for (let i = 0; i < 60 && !existsSync(marker); i++) {
    await new Promise((r) => setTimeout(r, 50));
  }
  assert.ok(existsSync(marker), "the fake server never started its worker");
  const grandchild = Number(readFileSync(marker, "utf8"));
  assert.ok(alive(grandchild), "the worker should be running before we stop");

  assert.equal(stopServer(state), true);
  for (let i = 0; i < 40 && (alive(state.child.pid) || alive(grandchild)); i++) {
    await new Promise((r) => setTimeout(r, 50));
  }
  assert.equal(alive(grandchild), false,
    "the worker outlived the server it belonged to and still holds the port");
  assert.equal(alive(state.child.pid), false, "the server itself survived");
});

// ── SCRUM-11: launch-failure capture/classification (review finding: the
//    feature's server half shipped untested; these pin the contract) ──────────
test("classifyBackendFailure: cli-missing on the _assert_backend_usable text", () => {
  const out = "coding backend unavailable: the `claude` CLI was not found.\nFix: install";
  assert.equal(classifyBackendFailure(out), "cli-missing");
});

test("classifyBackendFailure: not-logged-in on the two missing-token AuthErrors only", () => {
  assert.equal(classifyBackendFailure(
    "auth error: No subscription token found. Expected CLAUDE_CODE_OAUTH_TOKEN in ~/.no_human/.env"),
    "not-logged-in");
  assert.equal(classifyBackendFailure(
    "auth error: auth profile 'work' has no token. Expected CLAUDE_CODE_OAUTH_TOKEN_WORK in ~/.no_human/.env"),
    "not-logged-in");
  // Other AuthErrors print a "claude setup-token" Fix block too, but
  // setup-token is the WRONG remediation — they must stay unclassified.
  assert.equal(classifyBackendFailure(
    "auth error: ANTHROPIC_API_KEY is set — subscription mode runs on CLAUDE_CODE_OAUTH_TOKEN only.\n" +
    "Fix: run nh init, or:\n  1. claude setup-token  (creates a subscription token)"),
    null);
});

test("classifyBackendFailure: cli-missing wins when both banners appear; unrelated output is null", () => {
  assert.equal(classifyBackendFailure(
    "coding backend unavailable: the `claude` CLI was not found.\nNo subscription token found."),
    "cli-missing");
  assert.equal(classifyBackendFailure("Address already in use: 8420"), null);
  assert.equal(classifyBackendFailure(""), null);
});

test("tailDetail: keeps the last lines, caps chars, empty-safe", () => {
  assert.equal(tailDetail(""), "");
  assert.equal(tailDetail("one\ntwo"), "one\ntwo");
  const many = Array.from({ length: 30 }, (_, i) => `line${i}`).join("\n");
  const tail = tailDetail(many);
  assert.ok(tail.startsWith("line20"), tail);
  assert.ok(!tail.includes("line19"));
  const long = "x".repeat(2000);
  const capped = tailDetail(long);
  assert.ok(capped.length <= 500 + "[truncated…] ".length);
  assert.ok(capped.startsWith("[truncated…] "));
});

test("makeOutputCapture: bounded — keeps only the tail past the cap", () => {
  const cap = makeOutputCapture(10);
  cap.add("aaaaa"); cap.add("bbbbb"); cap.add("ccccc");
  assert.equal(cap.text(), "bbbbbccccc");
});

// ── SCRUM-49: destroy spawn-capture pipes once the server is confirmed up ──
test("makeOutputCapture: stop() releases the buffer and ignores further chunks", () => {
  const cap = makeOutputCapture(100);
  cap.add("hello");
  assert.equal(cap.text(), "hello");
  assert.equal(cap.capturing, true);
  cap.stop();
  assert.equal(cap.capturing, false);
  assert.equal(cap.text(), "", "the buffer must be released once capture stops");
  cap.add("more, after stop");
  assert.equal(cap.text(), "", "no bytes retained after stop()");
});

test("ensureServer: failure-window capture still classifies real spawn output (diagnostics survive)", async () => {
  // Proves the diagnosis path stays intact for the window this ticket does
  // NOT touch: a launch that never comes up must still surface why.
  const dir = mkdtempSync(join(tmpdir(), "nhfail-"));
  const { bin, args } = fakeNh(dir,
    "console.log('coding backend unavailable: cli missing');\nprocess.exit(2);\n");
  const state = await ensureServer({
    origin: "http://127.0.0.1:1", env: { NH_BIN: bin }, nhArgs: args,
    spawnTimeoutMs: 2000 });
  assert.equal(state.status, "failed");
  assert.equal(state.reason, "backend-cli-missing");
  assert.ok(state.detail.includes("coding backend unavailable"),
    `expected the captured diagnosis in detail, got: ${state.detail}`);
});

test("ensureServer: a silent fast non-zero exit is labeled backend-exited, not spawn-timeout", async () => {
  // No output at all, so classifyBackendFailure(text) returns null and the
  // reason falls through to the raw race outcome. Uses a LARGE
  // spawnTimeoutMs: if the race still keyed off the old fallback (or 'close'
  // never fired), this assertion would fail only after a ~20s hang — so a
  // fast pass here proves the race resolves on 'close' within milliseconds.
  const dir = mkdtempSync(join(tmpdir(), "nhexit-"));
  const { bin, args } = fakeNh(dir, "process.exit(3);\n");
  const state = await ensureServer({
    origin: "http://127.0.0.1:1", env: { NH_BIN: bin }, nhArgs: args,
    spawnTimeoutMs: 20000 });
  assert.equal(state.status, "failed");
  assert.equal(state.reason, "backend-exited");
  assert.equal(state.detail, "", "silent exit must not fabricate a diagnosis");
});

// POSIX-ONLY, and deliberately NOT ported to Windows rather than faked.
//
// This test's guarantee rests on SIGPIPE, and its fixture must be a SHELL
// script for the reason given below: a shell dies on the next write to a broken
// pipe, while Node silently swallows EPIPE and would not catch a regression to
// destroy(). Windows has neither SIGPIPE nor a shell-script mechanism, so the
// only Windows fixture available is a node one — precisely the fixture this
// test already rejects as unable to detect the defect. A "ported" version would
// therefore be a test that cannot fail, which is worse than an honest skip.
//
// What still covers it: capture.stop() and the stream handling it guards are
// platform-independent — there is no Windows branch in that code — so the macOS
// run governs the behaviour on both. Recorded in docs/WINDOWS.md.
test("ensureServer: stops capturing once confirmed up, but keeps draining so the running server never EPIPEs",
     { skip: IS_WIN ? "requires SIGPIPE and a shell fixture; neither exists on Windows" : false },
     async () => {
  // A shell script, not node: on a broken pipe the shell's default SIGPIPE
  // action TERMINATES it on the very next write — Node silently swallows
  // EPIPE on stdout/stderr and would not catch a regression to destroy().
  const dir = mkdtempSync(join(tmpdir(), "nhdrain-"));
  const { port } = await freePort();
  const fake = join(dir, "nh");
  writeFileSync(fake, `#!/bin/sh
node -e "require('node:http').createServer(function(q,r){r.end('[]')}).listen(${port},'127.0.0.1')" &
i=0
while [ $i -lt 400 ]; do
  echo "log line $i"
  i=$((i+1))
  sleep 0.02
done
`);
  chmodSync(fake, 0o755);
  const origin = "http://127.0.0.1:" + port;
  const state = await ensureServer({
    origin, spawnTimeoutMs: 8000, env: { NH_BIN: fake }, nhArgs: [] });
  try {
    assert.equal(state.status, "spawned");
    const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
    assert.ok(alive(state.child.pid), "server just confirmed up must still be running");
    // Several more write cycles PAST confirmation — exactly the post-startup
    // window this ticket is about (the server keeps logging long after boot).
    await new Promise((r) => setTimeout(r, 500));
    assert.ok(alive(state.child.pid),
      "the still-running server must not die from EPIPE after its pipes are released");
  } finally {
    stopServer(state);
  }
});

test("reason strings match error.html's remediation blocks (cross-file contract)", () => {
  const html = fs.readFileSync(new URL("./error.html", import.meta.url), "utf8");
  // server.mjs emits backend-cli-missing / backend-not-logged-in; error.html
  // must route each to a dedicated steps block. A typo on either side breaks
  // this pin, not just the live behavior.
  assert.ok(html.includes('"backend-cli-missing"'));
  assert.ok(html.includes('"backend-not-logged-in"'));
  assert.ok(html.includes('id="steps-cli-missing"'));
  assert.ok(html.includes('id="steps-not-logged-in"'));
});
