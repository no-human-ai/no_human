// Reading and writing the coding credential in ~/.no_human/.env.
//
// Two modes, mirroring the backend's llm.auth_mode (config.py): a Claude
// subscription OAuth token (the default), or the operator's own Anthropic API
// key — the sanctioned opt-in for installs that pay Anthropic directly. The
// MODE lives in config.yaml; the credential itself lives only in .env, 0600,
// and never crosses the IPC bridge back to a renderer.

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN";
export const API_KEY_VAR = "ANTHROPIC_API_KEY";
// The OPTIONAL codex-backend credential, in `llm.codex_auth_mode: api_key` only.
// codex SUBSCRIPTION mode stores NOTHING (constraint #6b): no_human holds no
// OpenAI credential, the user runs `codex login` themselves, and nothing here is
// ever called on that path — writeOpenAiKey is the sole writer and exists for
// the api_key mode alone.
export const OPENAI_KEY_VAR = "OPENAI_API_KEY";

/**
 * Read the platform at CALL time, not at module load.
 *
 * This was `const IS_WINDOWS = process.platform === "win32"`, captured once
 * when the module was first imported and therefore impossible to steer from a
 * test. The consequence was not cosmetic: `windowsRestrictToOwner` — the icacls
 * invocation, the ACL readback and all four of its throw sites — had never
 * executed on any machine, in any suite, because the only dispatch to it was
 * frozen to `false` on every developer and CI box we own, and there is no
 * Windows CI. A security-relevant branch that nothing can reach is a branch
 * nothing can regress-test.
 *
 * Reading it per call is what `main.mjs`'s `trayIcon()` already does, and it is
 * behaviour-identical in production: `process.platform` does not change during
 * a real run. It grants an attacker nothing either — code able to redefine
 * `process.platform` in-process can already replace `fs.chmodSync` (which
 * tokenStore.test.mjs does), `fs.writeFileSync`, or this module outright.
 */
function isWindows() {
  return process.platform === "win32";
}

/**
 * Raised when a credential file cannot be restricted to its owner.
 *
 * FAIL-CLOSED, deliberately: the alternative is writing a credential any
 * account on the machine can read and reporting success. Mirrors
 * `config.CredentialPermissionError`, which the Python half already raises for
 * the SAME file.
 */
export class CredentialPermissionError extends Error {}

/**
 * WHY THIS EXISTS. `fs.chmodSync(p, 0o600)` is a SILENT NO-OP on Windows: it
 * only toggles FILE_ATTRIBUTE_READONLY, and the `mode` option to writeFileSync
 * is ignored except for that same bit. Measured on this machine, a freshly
 * written `.env` carried the INHERITED ACL:
 *     NT AUTHORITY\SYSTEM:(I)(F)
 *     BUILTIN\Administrators:(I)(F)
 *     <user>:(I)(F)
 * so the OAuth token was readable by every administrator on the box while this
 * module reported it had written a 0600 file.
 *
 * `src/no_human/config.py` already fail-closes on exactly this for the Python
 * side. This is the Electron shell's mirror of that decision — the two halves
 * write the SAME file and must not disagree about whether it is protected. It
 * is a mirror, not a fork: the command shapes, the readback verification and
 * the error semantics are taken from `config.windows_restrict_to_owner`.
 */
export function windowsOwnerPrincipal(env = process.env) {
  const user = (env.USERNAME || "").trim();
  if (!user) {
    throw new CredentialPermissionError(
      "cannot secure the credential file: USERNAME is not set, so there is no "
      + "account to restrict it to.");
  }
  const domain = (env.USERDOMAIN || "").trim();
  return domain ? `${domain}\\${user}` : user;
}

/**
 * Parse `icacls <path>` output into the set of granted principals.
 *
 * icacls prints `<path> <PRINCIPAL>:(perms)` on the first line and
 * `<PRINCIPAL>:(perms)` indented on each subsequent one, then a summary.
 * Deliberately permissive about WHAT the permissions are: any principal
 * appearing at all is access we did not intend to grant.
 */
export function icaclsGrantees(output, targetPath) {
  const grantees = new Set();
  for (const raw of String(output || "").split("\n")) {
    let line = raw.trim();
    if (!line
        || line.startsWith("Successfully processed")
        || line.startsWith("Failed processing")) continue;
    if (targetPath && line.startsWith(targetPath)) {
      line = line.slice(targetPath.length).trim();
    }
    if (!line.includes(":(")) continue;
    grantees.add(line.split(":(")[0].trim());
  }
  return grantees;
}

function runIcacls(args) {
  try {
    const out = execFileSync("icacls", args,
      { encoding: "utf8", timeout: 30000, windowsHide: true });
    return { code: 0, out };
  } catch (err) {
    if (err && err.code === "ENOENT") {
      throw new CredentialPermissionError(
        "cannot secure the credential file: `icacls` was not found on PATH, so "
        + "its permissions cannot be restricted to your account. Refusing to "
        + "write a credential that any account on this machine could read.");
    }
    return {
      code: typeof err?.status === "number" ? err.status : 1,
      out: `${err?.stdout || ""}\n${err?.stderr || ""}`,
    };
  }
}

/**
 * The Windows Trusted Computing Base: SYSTEM and the local Administrators
 * group. These are the platform's root-equivalent — the exact analog of POSIX
 * `root`, which this module's own POSIX branch (`fs.chmodSync(p, 0o600)`)
 * leaves with full access and cannot exclude. Any local administrator can
 * already read this file, take ownership of it, or run as SYSTEM to reach it
 * regardless of its ACL, so treating them as forbidden is BOTH impossible on an
 * admin-owned file — the common case, since the primary account on most
 * personal Windows installs is a local admin, and files it creates carry
 * EXPLICIT Administrators/SYSTEM ACEs that `/inheritance:r` does not strip —
 * AND stricter than the POSIX contract this module mirrors. Accepting them
 * realigns Windows with that contract. The readback STILL flags every OTHER
 * principal (Users, Everyone, a specific non-owner account); that is the real
 * protection for a non-admin user and it is unchanged.
 *
 * Matched by the localized names an en-US readback emits AND by the well-known
 * SIDs, since icacls prints a raw SID when it cannot resolve a name. A
 * non-English Windows emits localized display names not listed here; those fall
 * through to the throw, which names the surviving principal, so an incomplete
 * allowlist is self-reporting on the next run rather than a silent weakening.
 */
const WINDOWS_TCB_NAMES = new Set([
  "nt authority\\system",
  "builtin\\administrators",
]);
const WINDOWS_TCB_SIDS = new Set(["s-1-5-18", "s-1-5-32-544"]);

export function isWindowsTcbPrincipal(grantee) {
  const g = String(grantee || "").replace(/^\*/, "").trim().toLowerCase();
  return WINDOWS_TCB_NAMES.has(g) || WINDOWS_TCB_SIDS.has(g);
}

/**
 * The grantees on a readback that are neither the owner nor the Windows TCB —
 * i.e. some OTHER account can reach the credential. A non-empty result is the
 * fail-closed signal. Case-insensitive: Windows account names are, and a case
 * difference would otherwise fail closed on a correctly secured file.
 */
export function nonOwnerGrantees(grantees, principal) {
  const owner = String(principal || "").toLowerCase();
  return [...grantees].filter(
    (g) => g.toLowerCase() !== owner && !isWindowsTcbPrincipal(g));
}

/** Replace *p*'s ACL with an owner-only one, then VERIFY the result. */
export function windowsRestrictToOwner(p, { directory = false } = {}) {
  const principal = windowsOwnerPrincipal();
  // (OI)(CI) makes a directory's ACE inheritable by its future contents; on a
  // file those flags are meaningless and icacls rejects them.
  const rights = directory ? "(OI)(CI)(F)" : "(R,W)";
  const applied = runIcacls([p, "/inheritance:r", "/grant:r", `${principal}:${rights}`]);
  if (applied.code !== 0) {
    throw new CredentialPermissionError(
      `cannot secure ${p}: icacls exited ${applied.code}. ${applied.out.trim()}`);
  }
  // The step that actually matters. A command that "succeeded" having changed
  // nothing is the exact defect this replaces, so the ACL is read back.
  const read = runIcacls([p]);
  if (read.code !== 0) {
    throw new CredentialPermissionError(
      `cannot verify permissions on ${p}: icacls exited ${read.code}. `
      + read.out.trim());
  }
  const grantees = icaclsGrantees(read.out, p);
  if (grantees.size === 0) {
    throw new CredentialPermissionError(
      `cannot verify permissions on ${p}: icacls listed no grantees, so the `
      + "restriction cannot be confirmed to have taken effect.");
  }
  // SYSTEM and the local Administrators group are the platform TCB and are
  // accepted (see nonOwnerGrantees / WINDOWS_TCB_*); any OTHER non-owner
  // grantee is the fail-closed signal.
  const extra = nonOwnerGrantees(grantees, principal);
  if (extra.length) {
    throw new CredentialPermissionError(
      `refusing to write a credential to ${p}: it is still readable by `
      + `${extra.sort().join(", ")}. Fix the ACL with: `
      + `icacls "${p}" /inheritance:r /grant:r "${principal}:(R,W)"`);
  }
}

/** Restrict a credential file to its owner, whatever the platform calls that. */
export function restrictToOwner(p, opts = {}) {
  if (isWindows()) return windowsRestrictToOwner(p, opts);
  fs.chmodSync(p, opts.directory ? 0o700 : 0o600);
  return undefined;
}

export function envPath(home = os.homedir()) {
  return path.join(home, ".no_human", ".env");
}

/** Parse KEY=VALUE lines. Later wins, matching dotenv; comments preserved by
 *  writeToken, which edits lines rather than re-serialising this map.
 *
 * Splits on `/\r?\n/`, NOT `"\n"`: on a CRLF file, `"\n"`-splitting leaves a
 * trailing `\r` on every line. JS `.` excludes ALL line terminators
 * (including `\r`), and the regex below is un-anchored by `m`, so `$` demands
 * end-of-string — the `\r` makes the WHOLE LINE fail to match and it is
 * silently dropped, not merely mis-trimmed. There is no value to `.trim()`;
 * the split is the fix. */
export function parseEnv(text) {
  const out = {};
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (m) out[m[1]] = m[2].trim();
  }
  return out;
}

/**
 * The .env variable holding a profile's token, mirroring
 * config.profile_token_var: the default profile uses the bare key, a named one
 * gets an upper-cased suffix. Without this, an operator on `nh auth use
 * personal` is nagged for a credential they already have.
 */
export function tokenVarFor(profile) {
  const p = (profile || "default").trim().toLowerCase();
  return p === "default" ? TOKEN_KEY : `${TOKEN_KEY}_${p.toUpperCase()}`;
}

/** The configured auth profile from ~/.no_human/config.yaml (`llm.auth_profile`). */
export function configuredProfile(home = os.homedir()) {
  try {
    const text = fs.readFileSync(
      path.join(home, ".no_human", "config.yaml"), "utf8");
    let inLlm = false;
    for (const line of text.split(/\r?\n/)) {
      if (/^llm\s*:/.test(line)) { inLlm = true; continue; }
      if (inLlm) {
        if (/^\S/.test(line)) break;
        const m = line.match(/^\s+auth_profile\s*:\s*["']?([A-Za-z0-9_-]+)/);
        if (m) return m[1];
      }
    }
  } catch { /* no config → default profile */ }
  return "default";
}

/**
 * True when a usable token exists for the ACTIVE profile. The .env file wins
 * over the process environment, mirroring config.py:167 ("`.env` wins over an
 * inherited token: it is the curated source").
 */
export function hasToken(env = process.env, home = os.homedir()) {
  const key = tokenVarFor(configuredProfile(home));
  try {
    if (parseEnv(fs.readFileSync(envPath(home), "utf8"))[key]) return true;
  } catch { /* no .env yet */ }
  return Boolean(env[key] && env[key].trim());
}

/** Reject values that cannot be a token before touching disk — an empty or
 *  whitespace-only write would produce a file that looks configured and fails
 *  at boot. A pasted API key gets a pointer to the mode that takes it. */
export function validateToken(value) {
  const v = (value ?? "").trim();
  if (!v) return "Paste the token to continue.";
  // Only the API-key shape is redirected. A broader `sk-ant-` test also
  // matched the VALID subscription token and false-accused a correct paste.
  if (/^sk-ant-api/i.test(v)) {
    return "That looks like an API key. This field takes the subscription " +
           "token from `claude setup-token` — to bill your own Anthropic " +
           "account instead, choose the API-key option.";
  }
  if (/\s/.test(v)) return "That value contains a space — check the paste.";
  return "";
}

/** Mode-aware validation: "subscription" keeps validateToken's rules;
 *  "api_key" accepts exactly the Anthropic API-key shape. */
export function validateCredential(value, mode) {
  if (mode !== "api_key") return validateToken(value);
  const v = (value ?? "").trim();
  if (!v) return "Paste the key to continue.";
  if (/\s/.test(v)) return "That value contains a space — check the paste.";
  if (/^sk-ant-oat/i.test(v)) {
    return "That looks like a subscription token. This field takes an " +
           "Anthropic API key (sk-ant-api…) — to use your Claude " +
           "subscription, choose the subscription option.";
  }
  if (!/^sk-ant-api/i.test(v)) {
    return "An Anthropic API key starts with sk-ant-api… — check the paste.";
  }
  return "";
}

/**
 * Write the token, PRESERVING every other line. Integration secrets (Jira,
 * CircleCI…) live in this same file, so a clobbering write would silently
 * disconnect them. Drops every existing line for this key and appends the new
 * one last. Every write goes through a temp file that is restricted to its
 * owner while still EMPTY and then renamed into place, so the result is 0600
 * (owner-only ACL on Windows) from its first credential byte.
 */
export function writeToken(value, home = os.homedir()) {
  const err = validateToken(value);
  if (err) throw new Error(err);
  // Write the key the ACTIVE profile actually reads — writing the bare key
  // while `llm.auth_profile` names another one would report success and still
  // fail to boot.
  return writeEnvVar(tokenVarFor(configuredProfile(home)), value.trim(), home);
}

/** Mode-aware storage: "api_key" writes ANTHROPIC_API_KEY; "subscription"
 *  writes the active profile's token variable. Validation runs first in both
 *  modes — a wrong-shaped credential never touches disk. */
export function writeCredential(value, mode, home = os.homedir()) {
  if (mode !== "api_key") return writeToken(value, home);
  const err = validateCredential(value, "api_key");
  if (err) throw new Error(err);
  return writeEnvVar(API_KEY_VAR, value.trim(), home);
}

/**
 * Validate an OpenAI API key before it can touch disk — the codex backend's
 * OPTIONAL api_key credential. Empty and whitespace-bearing values are rejected
 * exactly as the Claude paths reject them; an Anthropic key (sk-ant-…) is
 * redirected to the Claude field above, since that is the likely mispaste;
 * anything else must have the OpenAI shape, which is `sk-` (covering both `sk-`
 * and `sk-proj-`).
 */
export function validateOpenAiKey(value) {
  const v = (value ?? "").trim();
  if (!v) return "Paste the key to continue.";
  if (/\s/.test(v)) return "That value contains a space — check the paste.";
  if (/^sk-ant-/i.test(v)) {
    return "That's an Anthropic key — use the Claude credential above; this " +
           "field takes an OpenAI key (sk-…).";
  }
  if (!/^sk-/i.test(v)) {
    return "An OpenAI API key starts with sk-… — check the paste.";
  }
  return "";
}

/**
 * Write OPENAI_API_KEY, PRESERVING every other .env line — modelled exactly on
 * writeCredential's api_key branch (validate first, then the same owner-only
 * writeEnvVar). An invalid key never touches disk. This is the SOLE writer of an
 * OpenAI credential; it is reached only for codex's api_key mode. In codex
 * SUBSCRIPTION mode no_human stores no OpenAI credential (constraint #6b), so
 * this function is simply never called on that path.
 */
export function writeOpenAiKey(value, home = os.homedir()) {
  const err = validateOpenAiKey(value);
  if (err) throw new Error(err);
  return writeEnvVar(OPENAI_KEY_VAR, value.trim(), home);
}

function writeEnvVar(key, value, home) {
  const p = envPath(home);
  fs.mkdirSync(path.dirname(p), { recursive: true });

  let lines = [];
  try {
    lines = fs.readFileSync(p, "utf8").split(/\r?\n/);
  } catch { /* first run — no file yet */ }

  // Remove EVERY existing occurrence, not just the first: dotenv (and parseEnv)
  // are later-wins, so replacing only the first would leave a stale duplicate
  // that still beats the new value while the UI reports success.
  const re = new RegExp(`^\\s*${key}\\s*=`);
  const kept = lines.filter((l) => !re.test(l));
  while (kept.length && kept[kept.length - 1].trim() === "") kept.pop();
  kept.push(`${key}=${value}`, "");   // always end with a newline

  // ORDERING IS THE WHOLE PROPERTY. Write to a sibling temp, secure it while it
  // is still EMPTY, then rename it into place — exactly what
  // `config.atomic_write_0600` does for this same file, and the half of that
  // function this module previously failed to mirror.
  //
  // The bug that ordering fixes: writing `p` first and calling restrictToOwner
  // afterwards put the credential on disk under the INHERITED ACL and LEFT IT
  // THERE. `mode: 0o600` is a silent no-op on Windows (see this module's
  // header), so the token was readable by SYSTEM and every administrator for
  // the whole window; every throw site in windowsRestrictToOwner fired after
  // those bytes had landed, nothing unlinked them, and nh:save-token merely
  // returned {ok:false}. The docstring on CredentialPermissionError says the
  // alternative is "writing a credential any account on the machine can read
  // and reporting success" — it wrote one, it only declined to report success.
  //
  // EXISTING FILES ARE NOT REGRESSED. The old code hardened an existing 0644
  // .env by chmodding it in place; this reaches the same end state without a
  // permissionless window, because renameSync carries the temp's owner-only
  // mode/ACL onto `p` and replaces whatever the old file had. A leftover temp
  // from a crashed run is safe for the same reason the real file was not: a
  // `mode` argument applies only on CREATE, so the temp is hardened by
  // restrictToOwner whether it was just created or reused — always before a
  // credential byte exists in it.
  //
  // POSIX: chmod 0600, exactly as before. Windows: replace the inherited ACL
  // and VERIFY it, because chmod there does nothing. Throws
  // CredentialPermissionError rather than returning a file the whole machine
  // can read — the caller (main.mjs's nh:save-token) already surfaces a thrown
  // message to the setup screen, so the operator is told instead of being
  // silently exposed.
  //
  // The DIRECTORY is deliberately not restricted here: `config.ensure_private_dir`
  // owns that and is called by the server on every `nh start` (via
  // _acquire_pid_lock). Duplicating it would be a second copy of a
  // security-relevant policy, which is what this repo's own signing.cjs header
  // argues against.
  const tmp = `${p}.tmp`;
  try {
    fs.closeSync(fs.openSync(tmp, "w", 0o600));
    restrictToOwner(tmp);
    fs.writeFileSync(tmp, kept.join("\n"));
    fs.renameSync(tmp, p);
  } finally {
    // Suppress broadly, like the Python mirror's `contextlib.suppress(OSError)`:
    // after a successful rename there is nothing to remove, and on Windows an
    // unremovable leftover must not mask the CredentialPermissionError that is
    // the reason we are unwinding.
    try { fs.unlinkSync(tmp); } catch { /* renamed away, or not removable */ }
  }
  return p;
}

/** The configured billing mode from ~/.no_human/config.yaml (`llm.auth_mode`).
 *  Missing file, missing block or missing key all mean the default. */
export function configuredAuthMode(home = os.homedir()) {
  try {
    const text = fs.readFileSync(
      path.join(home, ".no_human", "config.yaml"), "utf8");
    let inLlm = false;
    for (const line of text.split(/\r?\n/)) {
      if (/^llm\s*:/.test(line)) { inLlm = true; continue; }
      if (inLlm) {
        if (/^\S/.test(line)) break;
        const m = line.match(/^\s+auth_mode\s*:\s*["']?([A-Za-z0-9_-]+)/);
        if (m) return m[1];
      }
    }
  } catch { /* no config → default */ }
  return "subscription";
}

/**
 * Upsert `llm.auth_mode` in config.yaml, preserving everything else. Only the
 * MODE goes in config — the credential stays in .env (config.py's
 * _reject_api_key_in_config enforces the same split server-side). Line-based
 * on purpose: the file is generated by yaml.safe_dump (2-space indent), and a
 * yaml library is not available in the shell.
 */
export function setAuthMode(mode, home = os.homedir()) {
  if (mode !== "subscription" && mode !== "api_key") {
    throw new Error(`unknown auth mode: ${mode}`);
  }
  const p = path.join(home, ".no_human", "config.yaml");
  let lines, eol = "\n";
  try {
    const text = fs.readFileSync(p, "utf8");
    eol = text.includes("\r\n") ? "\r\n" : "\n";
    lines = text.split(/\r?\n/);
  } catch {
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, `llm:\n  auth_mode: ${mode}\n`);
    return p;
  }
  const llmIdx = lines.findIndex((l) => /^llm\s*:/.test(l));
  if (llmIdx === -1) {
    while (lines.length && lines[lines.length - 1].trim() === "") lines.pop();
    lines.push("llm:", `  auth_mode: ${mode}`, "");
    fs.writeFileSync(p, lines.join(eol));
    return p;
  }
  let end = lines.length;
  for (let i = llmIdx + 1; i < lines.length; i++) {
    if (/^\S/.test(lines[i]) && lines[i].trim() !== "") { end = i; break; }
  }
  let replaced = false;
  for (let i = llmIdx + 1; i < end; i++) {
    const m = lines[i].match(/^(\s+)auth_mode\s*:/);
    if (m) { lines[i] = `${m[1]}auth_mode: ${mode}`; replaced = true; break; }
  }
  if (!replaced) lines.splice(llmIdx + 1, 0, `  auth_mode: ${mode}`);
  fs.writeFileSync(p, lines.join(eol));
  return p;
}

/** True when the CONFIGURED mode's credential exists — the first-run gate.
 *  api_key mode looks for ANTHROPIC_API_KEY (.env wins, then process env);
 *  subscription mode keeps hasToken's profile-aware logic. */
export function hasCredential(env = process.env, home = os.homedir()) {
  if (configuredAuthMode(home) !== "api_key") return hasToken(env, home);
  try {
    if (parseEnv(fs.readFileSync(envPath(home), "utf8"))[API_KEY_VAR]) return true;
  } catch { /* no .env yet */ }
  return Boolean(env[API_KEY_VAR] && env[API_KEY_VAR].trim());
}
