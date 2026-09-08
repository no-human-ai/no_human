// Server discovery for the desktop shell (Phase E1: attach-only; E2 adds
// ensure/spawn). The HTTP probe is the source of truth — the pidfile is
// advisory and NEVER a kill target (an attached server belongs to the
// operator).

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { MAC_KEYCHAIN_SERVICE } from "./setupGate.mjs";

export const DEFAULT_PORT = 8420;

/**
 * The operator can move the server port via ~/.no_human/config.yaml
 * (`server.port`) — the shell must find the same server `nh start` binds
 * (review finding: hardcoding 8420 silently strands configured installs).
 * Minimal block-scoped parse; NH_ORIGIN still wins as the explicit override.
 */
export function configuredPort(
  configPath = path.join(os.homedir(), ".no_human", "config.yaml"),
) {
  try {
    const text = fs.readFileSync(configPath, "utf8");
    let inServer = false;
    for (const line of text.split("\n")) {
      if (/^server\s*:/.test(line)) { inServer = true; continue; }
      if (inServer) {
        if (/^\S/.test(line)) break;               // left the server: block
        const m = line.match(/^\s+port\s*:\s*(\d+)/);
        if (m) return Number(m[1]);
      }
    }
  } catch { /* no config → default */ }
  return DEFAULT_PORT;
}

export const DEFAULT_ORIGIN = `http://127.0.0.1:${configuredPort()}`;

/**
 * Single probe attempt. Resolves "up" (2xx), "down" (a real non-2xx HTTP
 * response — server state, not transient, so callers must not retry it), or
 * "unreachable" (abort/timeout, ECONNREFUSED, DNS failure, socket reset —
 * transient, safe to retry).
 */
async function probeOnce(origin, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${origin}/api/tasks?limit=1`, { signal: ctrl.signal });
    return res.ok ? "up" : "down";
  } catch {
    return "unreachable";
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Probe the nh server. Resolves "up" | "down". Never throws.
 * Uses /api/tasks?limit=1 (P5 opt-in pagination, landed 0.1.9 in
 * src/no_human/api/app.py — `limit: int | None = Query(default=None, ge=1,
 * le=1000)`) rather than the full task list, which on a large fleet can be
 * >1MB just to answer "is a server up". Backward-compatible with an 0.1.8
 * server: unknown query params are ignored there and the full list still
 * comes back as a valid 200.
 * Retries once after retryDelayMs on a transient ("unreachable") failure —
 * a single 1500ms timeout during a DB write burst previously read as "down"
 * mid-launch and sent the shell down the spawn path, producing the
 * pid-lock/"another instance is already running" error screen against a
 * perfectly healthy server. A real non-2xx HTTP response is not retried:
 * it reflects server state, not a transient timing issue.
 */
export async function probe(origin = DEFAULT_ORIGIN, timeoutMs = 1500, retryDelayMs = 300) {
  const first = await probeOnce(origin, timeoutMs);
  if (first !== "unreachable") return first;
  await new Promise((r) => setTimeout(r, retryDelayMs));
  const second = await probeOnce(origin, timeoutMs);
  return second === "up" ? "up" : "down";
}

/**
 * Poll until the server is up or the deadline passes. Resolves true when up.
 *
 * `signal` lets a caller racing this against something else (ensureServer
 * races it against the spawned child's 'error'/'close' events) cancel the
 * loop the instant the race is decided, instead of it probing the dead
 * origin every intervalMs until deadlineMs elapses for nothing.
 */
export async function waitForServer(origin = DEFAULT_ORIGIN, deadlineMs = 20000,
                                    intervalMs = 500, { signal } = {}) {
  const t0 = Date.now();
  while (!signal?.aborted && Date.now() - t0 < deadlineMs) {
    if ((await probe(origin)) === "up") return true;
    if (signal?.aborted) return false;
    await new Promise((resolve) => {
      if (signal?.aborted) { resolve(); return; }
      const t = setTimeout(done, intervalMs);
      function done() {
        clearTimeout(t);
        signal?.removeEventListener?.("abort", done);
        resolve();
      }
      signal?.addEventListener?.("abort", done, { once: true });
    });
  }
  return false;
}

/** True when a URL belongs to the local no_human server (kept in-window). */
export function isAppOrigin(url, origin = DEFAULT_ORIGIN) {
  try {
    return new URL(url).origin === origin;
  } catch {
    return false;
  }
}

// ---------------------------- E2: ensure/spawn ---------------------------- //

import { execFile, spawn } from "node:child_process";
import { canSignal, hasExited, ownsChild } from "./serverOwnership.mjs";

const IS_WINDOWS = process.platform === "win32";

/**
 * The frozen server's filename. PyInstaller's `EXE(name="nh")` emits `nh` on
 * POSIX and `nh.exe` on Windows — the spec needs no platform branch for this
 * (verified against a real Windows freeze; see docs/WINDOWS.md).
 */
export const NH_EXE_NAME = IS_WINDOWS ? "nh.exe" : "nh";

// BOTH lists are exported, and the platform only chooses between them. Gating a
// single list on process.platform would make the other one unreachable from the
// host that builds the other artefact — the Windows list would be untestable on
// the Mac that ships the DMG, and vice versa. The tests exercise each list by
// name, so neither can rot unnoticed.
export const POSIX_NH_PATHS = [
  path.join(os.homedir(), ".local", "bin", "nh"),
  "/opt/homebrew/bin/nh",
  "/usr/local/bin/nh",
];

// Deliberately short. On macOS this list is load-bearing because a GUI app
// inherits launchd's PATH and cannot see the operator's own bin dirs; on
// Windows a GUI process inherits the user's PATH, so the PATH scan in
// resolveNhBin already covers the ordinary installs and this is only a
// backstop for the one location a `uv tool install` uses.
export const WINDOWS_NH_PATHS = [
  path.join(os.homedir(), ".local", "bin", "nh.exe"),
];

export const DEFAULT_NH_PATHS = IS_WINDOWS ? WINDOWS_NH_PATHS : POSIX_NH_PATHS;

/**
 * The `nh` frozen by packaging/build-installer.sh (or build-installer.ps1 on
 * Windows) and shipped in the app's Resources (electron-builder
 * extraResources). Present only in a packaged build: unpackaged,
 * `process.resourcesPath` points inside node_modules/electron and has no
 * nh-server dir, and under plain `node --test` it is undefined — both cases
 * return "" so resolution falls through to a developer's own nh.
 */
export function bundledNhPath(resourcesPath = process.resourcesPath,
                              exeName = NH_EXE_NAME) {
  if (!resourcesPath) return "";
  const p = path.join(resourcesPath, "nh-server", exeName);
  return fs.existsSync(p) ? p : "";
}

/**
 * Find the `nh` executable the way the operator's shell would. GUI apps on
 * macOS don't inherit the login shell's PATH, so: $NH_BIN → the bundled server
 * → login-shell `command -v nh` → known install locations. Returns "" when not
 * found.
 *
 * NH_BIN stays ahead of the bundle: it is the documented escape hatch for
 * pointing a packaged app at a working tree, and demoting it would silently
 * pin such installs to the frozen copy.
 */
/**
 * Find `nh` on a Windows PATH, by READING the PATH rather than shelling out.
 *
 * This is deliberately NOT the Windows spelling of the login-shell trick above.
 * That trick exists on macOS for one reason: a GUI app inherits launchd's PATH
 * and genuinely cannot see the operator's shell PATH, so the only way to learn
 * it is to start a login shell and ask. A Windows GUI process DOES inherit the
 * user's PATH, so there is nothing to recover — spawning `where.exe` would add
 * a subprocess, a 5s timeout and a dependency on machine state to answer a
 * question already in `env`.
 *
 * Reading `env` also makes this deterministic and unit-testable from any host,
 * which `where.exe` (which consults the live process PATH, ignoring `env`)
 * could not be. That claim is only true because the join below is
 * `path.win32.join` and not the host-native `path.join`: on a POSIX host the
 * native join produces `C:\a/nh.exe`, so the function returned a path Windows
 * would never see and the tests for it failed everywhere except Windows —
 * including on the ubuntu CI runner that is supposed to be checking it.
 * PATHEXT order is fixed rather than read from the environment: these are the
 * three forms a console entry point is ever installed as, and hardcoding them
 * keeps a machine with an exotic PATHEXT from changing which binary the app
 * picks.
 */
export function windowsPathLookup(env = process.env, exists = fs.existsSync,
                                  delimiter = path.delimiter,
                                  names = ["nh.exe", "nh.cmd", "nh.bat"]) {
  // `PATH` and `Path` are the same variable on Windows and Node normalises the
  // real process.env, but a plain object handed in by a caller (or a test) is
  // not normalised — accept either spelling rather than silently finding nothing.
  const raw = env.PATH ?? env.Path ?? "";
  for (const dir of String(raw).split(delimiter).filter(Boolean)) {
    for (const name of names) {
      const p = path.win32.join(dir, name);
      if (exists(p)) return p;
    }
  }
  return "";
}

export async function resolveNhBin(env = process.env,
                                   fallbackPaths = DEFAULT_NH_PATHS,
                                   bundled = bundledNhPath()) {
  if (env.NH_BIN && fs.existsSync(env.NH_BIN)) return env.NH_BIN;
  if (bundled) return bundled;
  const viaShell = IS_WINDOWS
    ? windowsPathLookup(env)
    : await new Promise((resolve) => {
      execFile(env.SHELL || "/bin/zsh", ["-lc", "command -v nh"],
               { timeout: 5000 }, (err, stdout) =>
        resolve(err ? "" : stdout.trim()));
    });
  if (viaShell && fs.existsSync(viaShell)) return viaShell;
  for (const p of fallbackPaths) {
    if (fs.existsSync(p)) return p;
  }
  return "";
}

/**
 * Find an executable the way the operator's shell would, shared by
 * resolveClaudeCli and resolveNodeBin below: login-shell `command -v <cmd>`
 * on POSIX (a GUI app does not inherit the login shell's PATH — the same
 * reason resolveNhBin shells out), a PATH read on Windows (a GUI process
 * DOES inherit the user's PATH there, so windowsPathLookup's env read is
 * enough), then CLI_HINT_DIRS as a backstop for an install that lives
 * somewhere neither the launchd PATH nor a bare Windows PATH covers.
 * `winNames` is the set windowsPathLookup checks in order (e.g.
 * ["claude.cmd", "claude.exe"]) — on POSIX only the bare `cmd` name is ever
 * a real filename, so the hint-dir scan there ignores it.
 */
async function resolveOnPath(cmd, winNames, env, execFileFn, exists) {
  const viaShell = IS_WINDOWS
    ? windowsPathLookup(env, exists, path.delimiter, winNames)
    : await new Promise((resolve) => {
      execFileFn(env.SHELL || "/bin/zsh", ["-lc", `command -v ${cmd}`],
                 { timeout: 5000 }, (err, stdout) =>
        resolve(err ? "" : stdout.trim()));
    });
  if (viaShell && exists(viaShell)) return viaShell;
  const names = IS_WINDOWS ? winNames : [cmd];
  for (const dir of CLI_HINT_DIRS) {
    for (const name of names) {
      const candidate = IS_WINDOWS ? path.win32.join(dir, name) : path.join(dir, name);
      if (exists(candidate)) return candidate;
    }
  }
  return "";
}

/**
 * Find the `claude` CLI the Agent SDK shells out to for every task
 * (agent/backend_check.py). Same defect as an unfindable `nh`: a GUI app's
 * narrower PATH can hide a Homebrew/npm-global install that a login shell
 * sees fine, and the SDK never receives cli_path — so a missing `claude`
 * used to fail silently on the first task while the setup screen, which only
 * asked for a token, looked done. Returns "" when not found.
 */
export async function resolveClaudeCli(env = process.env, execFileFn = execFile,
                                       exists = fs.existsSync) {
  return resolveOnPath("claude", ["claude.cmd", "claude.exe"], env, execFileFn, exists);
}

/**
 * Find `node` the same way — the npm-install line the setup screen shows
 * only helps if node itself already runs, and "can the user's shell run
 * node" is the same login-shell/PATH question resolveClaudeCli answers, not
 * a question about the Node this Electron app is built on
 * (process.execPath is Electron's own bundled runtime, not necessarily on
 * the user's PATH, and answers a different question than the one this check
 * needs). Returns "" when not found.
 */
export async function resolveNodeBin(env = process.env, execFileFn = execFile,
                                     exists = fs.existsSync) {
  return resolveOnPath("node", ["node.exe"], env, execFileFn, exists);
}

/**
 * Probe whether `claude` reports an active sign-in. Read-only/idempotent —
 * never mutates any state. Returns the numeric exit code, or `null` when the
 * command could not be run at all (spawn failure or timeout) — callers must
 * treat `null` the same as "unknown", not as "signed out". Injectable
 * `execFileFn` so tests never need a real `claude` binary. Never throws,
 * never logs stdout/stderr (nothing about a sign-in's status is a secret).
 *
 * This module deliberately has NO runner for `claude setup-token`.
 * MEASURED (macOS, `claude` 2.1.263, `auth status` reporting
 * loggedIn:true): `setup-token` is unconditionally browser OAuth — it opens
 * a URL and waits on a loopback `/callback` — regardless of whether the CLI
 * is already signed in; `setup-token --help` exposes no non-interactive flag
 * or env var to skip that. Spawned the way this module spawns everything
 * else (`stdin:"ignore"`, `windowsHide:true`, piped stdio, non-TTY) it
 * writes NOTHING to stdout/stderr even once the loopback server is up, so no
 * token could ever be read back from its output; a bounded child only ever
 * ends in `{killed:true, code:143}` at the timeout with both streams empty.
 * A Windows 11 field report (no_human build bd70645a): 3 of 3 clicks opened
 * a `localhost:<port>/callback` tab and a 10-second probe found nothing
 * listening on that port (ERR_CONNECTION_REFUSED). The only supported way to
 * mint a token is the user running
 * `claude setup-token` themselves in their own terminal and pasting the
 * result — see setupUi.mjs's `existingSignInCopy()`.
 */
export async function claudeAuthStatus(cliPath, execFileFn = execFile) {
  return new Promise((resolve) => {
    try {
      execFileFn(cliPath, ["auth", "status"],
                 { timeout: 20000, windowsHide: true },
                 (err) => resolve(err ? (typeof err.code === "number" ? err.code : null) : 0));
    } catch {
      resolve(null);
    }
  });
}

/**
 * macOS only: does the login Keychain hold a Claude Code sign-in item?
 * `claude auth status` (above) is the primary signal; this is the fallback
 * for "the binary exists but isn't the one that's signed in" / "auth
 * status itself is unavailable" cases. Confirmed empirically (2026-09
 * against a real signed-in `claude` 2.1.252 install) that macOS Claude
 * Code CLI credentials live here, NOT in a `~/.claude/.credentials.json`
 * file (that only exists on linux) — see setupGate.mjs's
 * claudeCredentialPaths for the full note.
 *
 * `security find-generic-password -s "<service>"` WITHOUT `-w` never prints
 * the secret — only item metadata (service/account/dates) goes to stdout,
 * which this function doesn't even read — so this keeps the same
 * existence-check-only discipline as the fs-path checks on other
 * platforms. Off-platform, or on any spawn/exec error, fails closed to
 * false. Injectable execFileFn/platform so tests never touch a real
 * Keychain unless they explicitly fake `security` on PATH.
 */
export async function macClaudeKeychainExists(execFileFn = execFile,
                                              platform = process.platform) {
  if (platform !== "darwin") return false;
  return new Promise((resolve) => {
    try {
      execFileFn("security", ["find-generic-password", "-s", MAC_KEYCHAIN_SERVICE],
                 { timeout: 5000, windowsHide: true },
                 (err) => resolve(!err));
    } catch {
      resolve(false);
    }
  });
}

/**
 * Where Claude Code actually installs. The Agent SDK resolves its CLI with
 * `shutil.which("claude")` plus a hardcoded list, and no_human never passes
 * cli_path — so if `claude` is not on the spawned server's PATH, EVERY task
 * dies with CLINotFoundError while the board itself looks perfectly healthy.
 *
 * That is not hypothetical on macOS: a GUI app inherits launchd's PATH
 * (/usr/bin:/bin:/usr/sbin:/sbin), not the login shell's, so the operator's own
 * ~/.local/bin/claude is invisible to a packaged build. /opt/homebrew/bin is
 * worse still — it is in neither launchd's PATH nor the SDK's fallback list.
 */
export const POSIX_CLI_HINT_DIRS = [
  "/opt/homebrew/bin",                                   // Homebrew, Apple Silicon
  "/usr/local/bin",                                      // Homebrew, Intel
  path.join(os.homedir(), ".local", "bin"),
  path.join(os.homedir(), ".npm-global", "bin"),
  path.join(os.homedir(), ".claude", "local"),
  path.join(os.homedir(), ".yarn", "bin"),
];

/**
 * The same job on Windows, for the same reason — the Agent SDK still resolves
 * `claude` off PATH, and a task that cannot find it dies while the board looks
 * healthy. The pressure is lower here (a GUI process inherits the user PATH, so
 * an ordinary install is already visible) but not absent: an npm-global or
 * `uv tool` install can sit outside the PATH a service or elevated launch
 * inherits.
 *
 * `%APPDATA%`/`%LOCALAPPDATA%` are read from the environment rather than
 * derived from the home directory: on a roaming or redirected profile they do
 * not sit under it, and a derived path would silently point at nothing.
 * mergePath drops every directory that does not exist, so listing a location
 * that is absent on a given machine costs nothing.
 *
 * `path.win32.join`, not `path.join`. These are Windows paths by construction —
 * they are only ever consumed under IS_WINDOWS — so the separator must be the
 * WINDOWS one regardless of the host that evaluated this module. With the
 * native join they came out `/Users/x/.local/bin`-shaped on a Mac or a Linux
 * runner, which is not a path Windows resolves and is not something a test on
 * either host could assert about.
 */
export const WINDOWS_CLI_HINT_DIRS = [
  path.win32.join(os.homedir(), ".local", "bin"),        // uv tool install
  path.win32.join(process.env.APPDATA
    || path.win32.join(os.homedir(), "AppData", "Roaming"), "npm"),   // npm -g
  path.win32.join(os.homedir(), ".claude", "local"),
  path.win32.join(process.env.LOCALAPPDATA
    || path.win32.join(os.homedir(), "AppData", "Local"), "Programs", "claude"),
];

export const CLI_HINT_DIRS = IS_WINDOWS
  ? WINDOWS_CLI_HINT_DIRS : POSIX_CLI_HINT_DIRS;

/**
 * Append missing directories to a PATH. APPEND, never prepend: nh shells out to
 * git/gh/pytest, and putting user-writable dirs first would let a stray binary
 * shadow the system one. Non-existent dirs are skipped so PATH stays honest.
 */
export function mergePath(basePath, extraDirs = CLI_HINT_DIRS,
                          exists = fs.existsSync, delimiter = path.delimiter) {
  // The separator is a PARAMETER defaulting to path.delimiter, not a literal
  // ":". On Windows the separator is ";" and every entry contains a colon
  // ("C:\\Windows"), so splitting on ":" does not merely fail to find entries —
  // it SHREDS them: "C:\\Windows;C:\\Users\\x" becomes
  // ["C", "\\Windows;C", "\\Users\\x"], and the PATH handed to the spawned
  // server is then garbage. This was the one POSIX assumption in this file that
  // was actively destructive rather than inert.
  //
  // Injectable so each platform's behaviour is verifiable from either host —
  // path.delimiter alone would make the Windows semantics untestable on macOS.
  const parts = String(basePath || "").split(delimiter).filter(Boolean);
  // Windows paths are case-insensitive, so "C:\\Foo" and "c:\\foo" name one
  // directory; comparing them case-sensitively would append a duplicate and
  // break this function's stated contract. The KEY is folded, never the value —
  // the emitted PATH keeps each entry's original casing.
  const fold = (d) => (delimiter === ";" ? d.toLowerCase() : d);
  const seen = new Set(parts.map(fold));
  for (const dir of extraDirs) {
    if (!seen.has(fold(dir)) && exists(dir)) { parts.push(dir); seen.add(fold(dir)); }
  }
  return parts.join(delimiter);
}

/**
 * Widen the PATH entry of a spawn env IN PLACE and return it — whatever the
 * variable's spelling.
 *
 * `process.env` is a case-insensitive proxy on Windows, so `process.env.PATH`
 * reads the variable however the OS spelled it. A SPREAD COPY of it is a plain
 * object and keeps the OS spelling — on Windows that is `Path` (the registry
 * name), so `copy.PATH` is undefined and `copy.PATH = mergePath(undefined)`
 * used to hand the child a PATH made ONLY of the hint dirs, with the original
 * `Path` dropped by the spawn (measured 2026-08-17: the packaged server ran
 * with `~/.local/bin;%APPDATA%
pm` and nothing else — no System32, no git,
 * so every task died on its first `git` spawn with WinError 2, and `icacls`
 * was "not found" for the credential ACL). Only a Git Bash launch, whose env
 * says `PATH`, ever worked. Every same-name key is collapsed into ONE so the
 * child can never receive two spellings; an exact `PATH` (a caller override)
 * still outranks the inherited spelling, preserving the old precedence.
 */
export function widenPath(spawnEnv, merge = mergePath) {
  const keys = Object.keys(spawnEnv).filter((k) => k.toUpperCase() === "PATH");
  const key = keys.includes("PATH") ? "PATH" : (keys[0] ?? "PATH");
  const merged = merge(spawnEnv[key]);
  for (const k of keys) delete spawnEnv[k];
  spawnEnv[key] = merged;
  return spawnEnv;
}

/**
 * Bounded accumulator for a child's stdout+stderr. Capped so a chatty/looping
 * process cannot grow this without bound over a long-lived spawn — only the
 * TAIL matters for diagnosing a launch failure.
 *
 * `stop()` ends the capture once startup is confirmed (SCRUM-49): the buffer
 * is only useful for diagnosing the failure window, so it is dropped and
 * further chunks are discarded. It deliberately does NOT touch the
 * underlying stream — see the `stop`-caller in ensureServer for why closing
 * the pipe's read end here would break the still-running server instead.
 */
export function makeOutputCapture(maxChars = 8000) {
  let buf = "";
  let capturing = true;
  return {
    add(chunk) {
      if (!capturing) return;
      buf += String(chunk);
      if (buf.length > maxChars) buf = buf.slice(buf.length - maxChars);
    },
    text() { return buf; },
    stop() { capturing = false; buf = ""; },
    get capturing() { return capturing; },
  };
}

/**
 * Reduce captured output to something small enough for a URL query parameter
 * (~2KB practical max) while keeping the part most likely to explain a
 * failure: the last few lines. Mirrors nh start's own console output, which
 * prints its diagnosis last, right before exiting.
 */
export function tailDetail(text, maxLines = 10, maxChars = 500) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return "";
  const tail = trimmed.split("\n").slice(-maxLines).join("\n");
  if (tail.length <= maxChars) return tail;
  return "[truncated…] " + tail.slice(tail.length - maxChars);
}

/**
 * Classify captured nh-start output against the two backend-check failures
 * `agent/backend_check.py` (via `_assert_backend_usable`/`_bootstrap` in
 * cli/commands.py) diagnoses: a missing `claude` CLI, or no OAuth token on
 * file. Matched on the exact phrases those code paths print, so a genuine
 * match is never confused with an unrelated startup failure. Anything else
 * (network timeouts, version mismatches, ...) returns null and falls through
 * to the generic retry text — deliberately narrow scope.
 */
export function classifyBackendFailure(text) {
  const t = String(text || "");
  if (/claude` CLI was not found|coding backend unavailable/i.test(t)) {
    return "cli-missing";
  }
  // Matched on the two genuinely-missing-token AuthError texts only
  // (config.py: "No subscription token found", "auth profile '<p>' has no
  // token"). Other AuthErrors (e.g. a stray ANTHROPIC_API_KEY) print a
  // "claude setup-token" Fix block too, but setup-token is the WRONG
  // remediation for them — they fall through to generic + verbatim detail.
  if (/No subscription token found|has no token\. Expected/i.test(t)) {
    return "not-logged-in";
  }
  return null;
}

/**
 * Attach to a running server, or spawn `nh start --no-open` and wait for it.
 * Returns {status:"attached"} | {status:"spawned", child} | {status:"failed",
 * reason, detail?}. The caller must retain `child`: it is the ONLY legitimate
 * kill target — the pidfile belongs to the operator and is never consulted
 * for killing (design rule from the plan; an attached server is not ours).
 */
// Windows: `detached` maps to DETACHED_PROCESS, and Windows IGNORES
// CREATE_NO_WINDOW (what windowsHide sets) when DETACHED_PROCESS is present.
// nh.exe then has no console at all, and every console-subsystem grandchild —
// claude.exe, codex.exe, git.exe, npm — is allocated a fresh VISIBLE console.
// Real users saw "multiple empty claude terminals on top of other screens".
// Not detached + windowsHide gives nh one hidden console that all descendants
// inherit. stopServer never relied on a group on Windows (it walks the tree
// with taskkill /T — see stopServer/windowsStopTree), so nothing is lost.
export function spawnOptionsFor(platform) {
  return { detached: platform !== "win32", windowsHide: true, stdio: ["ignore", "pipe", "pipe"] };
}

export async function ensureServer({
  origin = DEFAULT_ORIGIN,
  // Widened from 20000: a cold start of the PyInstaller-frozen `nh` was measured
  // at ~17.4s (first /api/tasks 200 at 17,448ms), leaving only ~2.5s of headroom
  // under the old 20s window — and a post-install launch (AV scanning the 44MB
  // bundle, cold disk) routinely spends that headroom and times out on a server
  // that WAS about to come up. 30s restores real margin over the measured boot.
  // main.mjs's non-latching background re-probe is the backstop for the boot
  // that is slower still; this just stops the common case from ever seeing it.
  spawnTimeoutMs = 30000,
  env = process.env,
  nhArgs = ["start", "--no-open"],
  fallbackPaths = DEFAULT_NH_PATHS,
  bundled = bundledNhPath(),
  // Called the instant a child exists. The caller must register it HERE: for
  // the ~20s until this function returns, a server booted with the old token
  // was in no registry at all, so nothing could stop it and a token save
  // reported "Connected" over it.
  onSpawn = () => {},
} = {}) {
  if ((await probe(origin)) === "up") return { status: "attached" };
  const bin = await resolveNhBin(env, fallbackPaths, bundled);
  if (!bin) {
    return { status: "failed", reason: "nh-not-found" };
  }
  // Merge over the process env: nh (and a shebang'd test double) needs
  // PATH/HOME; the env param carries only resolution overrides.
  // detached: its own process GROUP, so stopServer can take the workers down
  // with it. A plain SIGTERM to the direct child left `nh`'s spawned workers
  // reparented to init, still holding the port.
  // PATH is widened AFTER the merge so a caller-supplied env cannot silently
  // drop it: without this the Agent SDK cannot find `claude` and every task
  // fails, even though the board serves normally.
  const spawnEnv = widenPath({ ...process.env, ...env });
  // stdout/stderr are PIPED (not ignored) so a launch failure's own diagnosis
  // — e.g. `_assert_backend_usable`'s "claude CLI was not found" — can reach
  // error.html instead of a generic lifecycle reason. Both streams are
  // unref'd immediately: this child is detached and may keep running long
  // after ensureServer returns (or outlive this process entirely under
  // `node --test`), and a referenced pipe would otherwise keep the event loop
  // alive waiting on it.
  const capture = makeOutputCapture();
  // `nh` is a CONSOLE-subsystem binary. Per-platform spawn options (detached +
  // windowsHide) come from spawnOptionsFor: POSIX keeps its own process group so
  // stopServer can take the workers down; win32 drops detached so windowsHide's
  // CREATE_NO_WINDOW is honoured and no console appears. windowsHide does not
  // affect capture — stdout/stderr are piped above, not written to any console.
  const child = spawn(bin, nhArgs, { env: spawnEnv, ...spawnOptionsFor(process.platform) });
  child.unref();
  for (const stream of [child.stdout, child.stderr]) {
    if (!stream) continue;
    stream.on("data", (chunk) => capture.add(chunk));
    // A stray stream-level error must not crash this process — an unhandled
    // 'error' event throws by default, and these streams now stay attached
    // for the server's whole lifetime (see capture.stop() below).
    stream.on("error", () => {});
    stream.unref?.();
  }
  // A throwing callback must not reject ensureServer with the child already
  // spawned and untracked.
  try { onSpawn(child); } catch { /* tracking must never break the spawn */ }
  const spawnErrored = new Promise((resolve) =>
    child.once("error", () => resolve("spawn-error")));
  // A backend-check refusal (`sys.exit(2)`) exits almost immediately — racing
  // its exit (not just the port) means Retry doesn't sit for the full
  // spawnTimeoutMs before showing the real reason. This races 'close', not
  // 'exit': 'exit' fires before the stdio pipes finish flushing, so a fast
  // non-zero exit could resolve the race before capture.text() has the
  // diagnosis. 'close' fires once both pipes have drained, guaranteeing the
  // captured output is complete before classifyBackendFailure runs below.
  const spawnExited = new Promise((resolve) =>
    child.once("close", (code, signal) => {
      if (code !== 0 || signal) resolve("spawn-exited");
    }));
  const waitAbort = new AbortController();
  const raced = await Promise.race(
    [waitForServer(origin, spawnTimeoutMs, 500, { signal: waitAbort.signal }),
      spawnErrored, spawnExited]);
  // The race is decided the instant this resolves — abort unconditionally so
  // a losing waitForServer stops probing the dead origin immediately instead
  // of polling every 500ms for the remainder of spawnTimeoutMs (30s default)
  // for nothing. A no-op when waitForServer itself won (it has already
  // returned true and isn't looking at the signal anymore).
  waitAbort.abort();
  if (raced === "spawn-error" || (await probe(origin)) !== "up") {
    // NOT killed here: a slow-booting nh may still be about to win the port,
    // and killing it from inside ensureServer would race a legitimately
    // starting server. The child is returned instead, so the CALLER owns the
    // decision — main.mjs tracks it and stops it before starting a
    // replacement, which is what keeps Retry from accumulating servers.
    const text = capture.text();
    const cause = classifyBackendFailure(text);
    const reason = cause === "cli-missing" ? "backend-cli-missing"
      : cause === "not-logged-in" ? "backend-not-logged-in"
      : raced === "spawn-error" ? "spawn-error"
      : raced === "spawn-exited" ? "backend-exited"
      : "spawn-timeout";
    return { status: "failed", reason, detail: tailDetail(text), child };
  }
  // Confirmed up (SCRUM-49): capture is only needed for the failure window
  // now behind us, so stop retaining it — a long-lived spawned server would
  // otherwise leak its entire console output into this buffer forever.
  //
  // This must NOT stream.destroy() stdout/stderr. Doing so closes OUR read
  // end, which is the pipe's ONLY reader — the still-running server's very
  // next stdout/stderr write would then raise EPIPE immediately, which is
  // exactly the breakage this ticket exists to prevent, just triggered by us
  // instead of by a later Electron crash. The 'data' listeners registered
  // above stay attached and keep draining the pipe (so the server's writes
  // keep succeeding for as long as it runs); capture.stop() only makes them
  // discard bytes instead of buffering them.
  capture.stop();
  return { status: "spawned", child };
}

/**
 * SIGKILL the group. Used only after SIGTERM has been given time to work: a
 * server that ignores SIGTERM otherwise survives quit and keeps the port.
 */
export function forceStopServer(state) {
  return stopServer(state, "SIGKILL");
}

/**
 * Stop the server ONLY when this shell spawned it. Attached servers belong
 * to the operator — never touched, never pidfile-killed.
 *
 * NOTE: a `true` return means the signal was DELIVERED, not that the process
 * died. Callers must confirm death with hasExited().
 */
/**
 * The `taskkill` argv for stopping a process TREE. Pure, so the escalation it
 * encodes is testable without spawning anything.
 *
 * `/T` is the load-bearing flag: it takes the process and its descendants.
 * Killing only the direct child would leave `nh`'s workers running and holding
 * the port — the exact orphaning the POSIX branch uses a process group to
 * avoid. `CREATE_NEW_PROCESS_GROUP` is NOT an alternative: a Windows process
 * group is not a kill target, which is why `src/no_human/testing/runner.py`
 * reaches for taskkill too rather than mirroring killpg.
 *
 * `/F` maps SIGKILL and its absence maps SIGTERM, mirroring
 * `cli/commands.py::_windows_try_kill(pid, force=...)` exactly. Windows has no
 * real graceful-terminate for a console process with no window, so the
 * un-forced form frequently does nothing — that is precisely why the caller's
 * SIGTERM→SIGKILL escalation is kept rather than collapsing straight to /F: the
 * escalation is what makes the difference observable instead of assumed.
 */
export function taskkillArgs(pid, signal) {
  return [...(signal === "SIGKILL" ? ["/F"] : []), "/T", "/PID", String(pid)];
}

/**
 * Stop a process tree on Windows. Returns true when the kill was DISPATCHED —
 * the same contract the POSIX branch has, where `process.kill` returning
 * without throwing says nothing about whether the process died. Callers confirm
 * death with hasExited(), and both lifecycle paths already poll for it.
 *
 * Deliberately ASYNC (execFile, not execFileSync). This runs from `before-quit`
 * on the main thread; a synchronous taskkill would freeze the UI for as long as
 * it takes, and the caller is polling for death anyway. The child's own 'exit'
 * event is what ultimately sets exitCode and satisfies hasExited().
 */
function windowsStopTree(child, signal) {
  const args = taskkillArgs(child.pid, signal);
  try {
    execFile("taskkill", args, (err) => {
      if (!err) return;
      // MEASURED on Windows 11, not assumed. taskkill exits 128 for BOTH
      // "the process ... not found" AND "This process can only be terminated
      // forcefully (with /F option)". Treating 128 as benign — which an earlier
      // draft of this function did — silently swallows the second case, and the
      // second case is the NORMAL one here: `nh` is a console process with no
      // window, so an un-forced taskkill CANNOT terminate it. Verified directly:
      //   taskkill /T /PID <console pid>  -> exit 128, process still alive
      //   taskkill /F /T /PID <same pid>  -> exit 0,   process gone
      if (signal === "SIGKILL") {
        console.error(`taskkill ${args.join(" ")} failed:`,
                      (err && err.message) || err);
        return;
      }
      // The graceful form failed. Either it is already gone (success) or
      // Windows refuses without /F. Distinguish by ASKING THE OS whether the
      // pid still exists rather than by matching taskkill's message — that
      // message is localised, and a German or Japanese Windows would defeat
      // any text match while this check is language-independent.
      let alive = true;
      try { process.kill(child.pid, 0); } catch { alive = false; }
      if (!alive) return;                     // raced its own shutdown: fine
      // Escalate NOW rather than leaving it to the caller's grace timer. There
      // is no grace period actually elapsing here — the un-forced kill did
      // nothing at all — so waiting the full 10s of lifecycle.shutdown() would
      // add a 10-second hang to EVERY quit on Windows and still end up sending
      // exactly this. If `nh` ever gains a console-ctrl handler that makes the
      // graceful form work, that path succeeds and this never runs.
      execFile("taskkill", taskkillArgs(child.pid, "SIGKILL"), (err2) => {
        if (err2) {
          console.error(`taskkill /F for pid ${child.pid} failed:`,
                        (err2 && err2.message) || err2);
        }
      });
    });
    return true;
  } catch {
    return false;
  }
}

export function stopServer(state, signal = "SIGTERM") {
  // ownsChild is the single definition of "ours". Gating on status==="spawned"
  // here contradicted it: ensureServer returns {status:"failed", child} for a
  // slow-booting nh that may still bind the port, so this refused to stop a
  // server we started and left it reparented to init, holding the port.
  if (!ownsChild(state)) return false;
  const child = state.child;
  // Already gone: nothing to signal, and saying otherwise inflates the
  // "stopped" count. Crucially it also stops us reaching the group kill with a
  // PID the OS may have reused.
  if (hasExited(child)) return false;
  // Windows first: `process.kill(-pid)` is POSIX-only. A negative pid is not a
  // process group there — Node rejects it outright (EINVAL), so this whole
  // branch used to throw and fall through to child.kill(), which terminates the
  // DIRECT CHILD ONLY and orphans `nh`'s workers still holding the port.
  // Guarded on canSignal for the same PID-reuse reason as the POSIX branch: a
  // reaped PID can be reused, and taskkill has no more identity check than
  // killpg does.
  if (IS_WINDOWS) {
    if (canSignal(child)) return windowsStopTree(child, signal);
    try {
      child.kill(signal);
      return true;
    } catch {
      return false;
    }
  }
  // Kill the process GROUP first: `nh` spawns workers, and signalling only the
  // direct child left them alive at PPID 1 holding the port.
  // Only signal a group while the child is demonstrably alive: after exit its
  // PID can be reused, and process.kill(-pid) would hit a stranger's group.
  if (canSignal(child)) {
    try {
      process.kill(-child.pid, signal);
      return true;
    } catch { /* no group (or already gone) — fall back to the child */ }
  }
  try {
    child.kill(signal);
    return true;
  } catch {
    return false;
  }
}
