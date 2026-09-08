// Which renderer is allowed to drive the credential IPC.
//
// The setup screen shares ONE BrowserWindow (and one preload) with the board,
// so `window.nhSetup` is reachable from every page the server renders. Only the
// local setup file may save a token or dismiss the screen — otherwise injected
// board content could overwrite the operator's credential.
//
// Pure and electron-free so it is unit-testable; main.mjs supplies the URL.
import { fileURLToPath } from "node:url";

/** True only for the exact local setup file. Fails closed on anything odd. */
export function isSetupUrl(url, setupFile) {
  if (typeof url !== "string" || !url.startsWith("file://")) return false;
  try {
    return fileURLToPath(url) === setupFile;
  } catch {
    return false;
  }
}

/**
 * Where a Claude Code sign-in would live on disk, per platform. These paths
 * are existence-check targets ONLY — passed to `fs.existsSync` and nowhere
 * else. Never read, never parsed: this module has no reader for them, by
 * design (a credential file's *contents* are out of scope for this feature).
 *
 * darwin deliberately returns `[]`: verified empirically (2026-09, a real
 * signed-in `claude` 2.1.252 install) that Claude Code CLI does NOT write
 * `~/.claude/.credentials.json` on macOS — that file only shows up on
 * linux — and `~/Library/Application Support/Claude` belongs to a
 * DIFFERENT product (the Claude Desktop chat app), so treating its
 * existence as a Code sign-in signal is a false-positive trap, not a
 * fallback. The real macOS store is the login Keychain, which is not a
 * filesystem path — see MAC_KEYCHAIN_SERVICE and server.mjs's
 * `macClaudeKeychainExists`, which does the existence check there via
 * `security find-generic-password` (still existence-only: never reads the
 * secret). linux/win32 are returned so the same fs-based existence-check
 * machinery degrades safely to today's behaviour if a path is ever wrong.
 */
export function claudeCredentialPaths(platform, env, home) {
  const h = typeof home === "string" && home ? home : "";
  if (platform === "linux") {
    return [`${h}/.claude/.credentials.json`, `${h}/.config/claude`];
  }
  if (platform === "win32") {
    const appData = (env && env.APPDATA) || "";
    return appData ? [`${appData}\\Claude`] : [];
  }
  return [];
}

/**
 * macOS: the login Keychain service name Claude Code CLI stores its OAuth
 * credential under. Confirmed empirically (2026-09) against a real
 * signed-in install: `security find-generic-password -s
 * "Claude Code-credentials"` finds an item and exits 0; a fresh/never-
 * signed-in HOME exits non-zero. Exported so server.mjs's
 * macClaudeKeychainExists and this file's own tests share one source of
 * truth instead of a string duplicated in two places.
 */
export const MAC_KEYCHAIN_SERVICE = "Claude Code-credentials";

/**
 * Fallback chain for "does this machine already have a Claude Code
 * sign-in": no CLI at all fails closed immediately; a clean `claude auth
 * status` exit is the strongest signal; anything else (non-zero, or `null`
 * for a missing/erroring/timed-out command) falls through to the existence
 * of a credential store on disk. Garbage input (undefined/null/non-object)
 * fails closed to "not detected" rather than throwing.
 */
export function detectSignIn(input) {
  const { cliPath, authStatusCode, storeExists } = input || {};
  if (!cliPath) return { detected: false, via: "no-cli" };
  if (authStatusCode === 0) return { detected: true, via: "cli" };
  if (storeExists) return { detected: true, via: "store" };
  return { detected: false };
}

const TOKEN_RE = /sk-ant-oat[A-Za-z0-9_-]+/;
const INTERACTIVE_RE = /https?:\/\/|\bbrowser\b|\bvisit\b/i;
const CLI_FALLBACK_MESSAGE =
  "This feature needs a recent Claude CLI — run `claude setup-token` " +
  "manually and paste the token below.";

/**
 * Classify what `claude setup-token` did, from its exit code and captured
 * output — never from a timeout race. A token is only ever trusted on a
 * clean exit (code 0); browser/URL markers or a clean-but-empty exit both
 * mean "needs interactive auth", which is the safe fallback to manual paste.
 * A non-zero exit with no recognizable content returns a FIXED message —
 * never the captured bytes — so nothing the CLI printed can leak into the
 * UI or a log via this path.
 *
 * No production path calls this today: `setup-token` is unconditional
 * browser OAuth with no non-interactive flag, and under the piped/non-TTY
 * stdio this app would have to use it writes nothing to either stream even
 * once its loopback listener is up (MEASURED — see server.mjs's
 * claudeAuthStatus doc comment) — so `runClaudeSetupToken` was removed
 * rather than kept unreachable. This function, and the fixed-message /
 * never-echo-captured-bytes discipline it encodes, stays exported as the
 * pinned contract ANY future token-handling path must satisfy.
 */
export function classifySetupTokenOutput(input) {
  const { code, stdout, stderr } = input || {};
  const out = typeof stdout === "string" ? stdout : "";
  const err = typeof stderr === "string" ? stderr : "";
  const tokenMatch = out.match(TOKEN_RE) || err.match(TOKEN_RE);
  if (tokenMatch && code === 0) return { kind: "token", token: tokenMatch[0] };
  if (INTERACTIVE_RE.test(out) || INTERACTIVE_RE.test(err)) {
    return { kind: "interactive" };
  }
  // A numeric non-zero exit is a real CLI failure ⇒ fixed error message.
  // `code === null` (spawn/timeout — see runClaudeSetupToken) is NOT treated
  // as a failure here: we don't know the CLI actually rejected anything, so
  // it falls through to the safe default below (manual paste), matching
  // server.mjs's documented timeout shape `{code:null, stderr:"timeout"}`.
  if (typeof code === "number" && code !== 0) {
    return { kind: "error", message: CLI_FALLBACK_MESSAGE };
  }
  return { kind: "interactive" };
}

/** Replaces every occurrence of `secret` in `text` with `***`. Defensive —
 * used everywhere main.mjs might otherwise surface a raw error string that
 * happened to echo back a pasted credential; `nh:save-token`'s catch is the
 * live caller now that the `setup-token` import path is gone. */
export function redact(text, secret) {
  const t = typeof text === "string" ? text : "";
  if (!secret) return t;
  return t.split(secret).join("***");
}
