// Unit tests for credential storage. Pure logic over a real temp HOME — no
// mocks, and never the operator's real ~/.no_human/.env.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { execFileSync } from "node:child_process";

import {
  CredentialPermissionError,
  TOKEN_KEY, configuredProfile, envPath, hasToken, icaclsGrantees,
  isWindowsTcbPrincipal, nonOwnerGrantees, parseEnv,
  tokenVarFor, validateToken, windowsOwnerPrincipal, writeToken,
} from "./tokenStore.mjs";

/**
 * Run *fn* with the platform's "restrict this file to its owner" primitive
 * forced to fail, then restore it.
 *
 * Stubs `fs.chmodSync` rather than `restrictToOwner` itself, so the REAL
 * restrictToOwner runs and throws from its real POSIX branch — the same shape
 * `windowsRestrictToOwner` throws when icacls is missing, exits non-zero, or
 * reads back an ACL that still lists SYSTEM. `import fs from "node:fs"` yields
 * node:fs's own module.exports object in both this file and tokenStore.mjs, so
 * the assignment is visible to the code under test; the `assert.throws` in each
 * caller is what proves the stub actually took effect rather than passing
 * vacuously.
 */
function withFailingRestrict(fn) {
  const real = fs.chmodSync;
  fs.chmodSync = () => {
    throw new CredentialPermissionError("forced failure: cannot secure the file");
  };
  try {
    return fn();
  } finally {
    fs.chmodSync = real;
  }
}

const home = () => mkdtempSync(join(tmpdir(), "nhhome-"));

// withFailingRestrict forces the POSIX primitive (fs.chmodSync) to throw so the
// REAL restrictToOwner fails closed. On a real Windows host restrictToOwner
// dispatches to icacls instead, which has NO mockable seam from a test: it is a
// named `execFileSync` import (not the reassignable `fs` module object the chmod
// stub relies on) and the real binary lives in System32, so emptying PATH cannot
// hide it either. The forced-failure ORDERING guard is therefore only observable
// on POSIX, where it runs in full. The Windows END STATE — a correctly restricted
// .env — is covered by "writeToken: creates the file 0600", which reads the real
// ACL back. (Coverage gap on real Windows: the writeToken cleanup-on-restrict-
// failure path is unexercised; closing it needs a mockable icacls seam.)
const SKIP_FAILING_RESTRICT = process.platform === "win32"
  ? "forcing restrictToOwner to fail needs the POSIX chmod seam; on Windows it "
    + "uses icacls, which has no mockable seam (named import; real binary in "
    + "System32). POSIX runs this in full; the Windows end state is covered by "
    + "\"writeToken: creates the file 0600\"."
  : false;

/**
 * Assert the platform's "only the owner can read this" guarantee.
 *
 * The assertion used to be `statSync(p).mode & 0o777 === 0o600` everywhere.
 * That can NEVER hold on Windows — node reports 0o666 for any file without the
 * readonly attribute, whatever the ACL says — so on Windows it was testing a
 * number the OS does not model rather than the property anyone cares about.
 *
 * So the PROPERTY is asserted per platform, and the Windows form reads the real
 * ACL back. The owner must be present, and NO principal outside the platform
 * TCB (SYSTEM + the local Administrators group — POSIX root's analog, which the
 * POSIX branch's 0600 also cannot exclude) may appear. That still catches the
 * original defect — a .env inherited by *other* accounts — while accepting the
 * TCB, which is unavoidable on an admin-owned file (the CI runner's case).
 */
function assertOwnerOnly(p) {
  if (process.platform !== "win32") {
    assert.equal(fs.statSync(p).mode & 0o777, 0o600);
    return;
  }
  const out = execFileSync("icacls", [p], { encoding: "utf8", windowsHide: true });
  const grantees = icaclsGrantees(out, p);
  const owner = windowsOwnerPrincipal();
  const got = [...grantees].map((g) => g.toLowerCase()).sort();
  assert.ok(got.includes(owner.toLowerCase()),
    `${p} must grant its owner, got: ${got.join(", ")}`);
  const extra = nonOwnerGrantees(grantees, owner);
  assert.deepEqual(extra, [],
    `${p} must grant only its owner and the platform TCB, but also grants: `
    + extra.join(", "));
}

// The readback filter, tested directly (the real icacls path is Windows-only).
// SYSTEM and the local Administrators group are the platform TCB and accepted;
// any OTHER non-owner account is the fail-closed signal that protects a
// non-admin user's credential.
test("nonOwnerGrantees: accepts owner + TCB, flags any other account", () => {
  const readback =
    "C:\\Users\\alice\\.no_human\\.env CORP\\alice:(R,W)\n"
    + "        NT AUTHORITY\\SYSTEM:(F)\n"
    + "        BUILTIN\\Administrators:(F)\n"
    + "        BUILTIN\\Users:(RX)\n"
    + "\nSuccessfully processed 1 files; Failed processing 0 files\n";
  const grantees = icaclsGrantees(readback, "C:\\Users\\alice\\.no_human\\.env");
  assert.deepEqual([...grantees].sort(), [
    "BUILTIN\\Administrators", "BUILTIN\\Users",
    "CORP\\alice", "NT AUTHORITY\\SYSTEM",
  ]);
  // SYSTEM and Administrators are accepted (owner too); only Users survives.
  assert.deepEqual(nonOwnerGrantees(grantees, "CORP\\alice"), ["BUILTIN\\Users"]);
  // Owner + TCB alone ⇒ nothing flagged.
  assert.deepEqual(
    nonOwnerGrantees(
      new Set(["CORP\\alice", "NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators"]),
      "CORP\\alice"),
    []);
});

test("isWindowsTcbPrincipal: names and well-known SIDs, not other accounts", () => {
  for (const g of ["NT AUTHORITY\\SYSTEM", "builtin\\administrators",
                   "S-1-5-18", "*S-1-5-32-544"]) {
    assert.ok(isWindowsTcbPrincipal(g), `${g} is the platform TCB`);
  }
  for (const g of ["BUILTIN\\Users", "Everyone", "CORP\\bob", "S-1-5-11"]) {
    assert.ok(!isWindowsTcbPrincipal(g), `${g} must NOT be accepted`);
  }
});

test("hasToken: the .env file wins, then process env, else false", () => {
  const h = home();
  assert.equal(hasToken({}, h), false, "empty home has no token");
  assert.equal(hasToken({ [TOKEN_KEY]: "sk-ant-oat-x" }, h), true);
  assert.equal(hasToken({ [TOKEN_KEY]: "   " }, h), false, "blank is not a token");
  writeToken("sk-ant-oat-fromfile", h);
  assert.equal(hasToken({}, h), true, "reads the .env file");
});

test("writeToken: preserves other secrets, one token line after rewrite", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h),
    "# comment\nJIRA_API_TOKEN=keepme\nCIRCLECI_TOKEN=keeptoo\n");
  writeToken("sk-ant-oat-first", h);
  let text = fs.readFileSync(envPath(h), "utf8");
  assert.match(text, /JIRA_API_TOKEN=keepme/, "other secrets survive");
  assert.match(text, /CIRCLECI_TOKEN=keeptoo/);
  assert.match(text, /# comment/, "comments survive");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-first");

  // Rewriting must REPLACE, not append a second line.
  writeToken("sk-ant-oat-second", h);
  text = fs.readFileSync(envPath(h), "utf8");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-second");
  const occurrences = text.split("\n").filter((l) => l.startsWith(TOKEN_KEY));
  assert.equal(occurrences.length, 1, "exactly one token line");
  assert.match(text, /JIRA_API_TOKEN=keepme/, "still preserved on rewrite");
});

test("writeToken: creates the file 0600", () => {
  const h = home();
  const p = writeToken("sk-ant-oat-x", h);
  assertOwnerOnly(p);
});

test("writeToken: removes DUPLICATE keys — dotenv is later-wins", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h),
    `${TOKEN_KEY}=OLD1\nFOO=b\n${TOKEN_KEY}=OLD2\n`);
  writeToken("sk-ant-oat-new", h);
  const text = fs.readFileSync(envPath(h), "utf8");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-new",
    "the effective (last) value must be the new one");
  assert.equal(text.split("\n").filter((l) => l.startsWith(TOKEN_KEY)).length, 1);
  assert.match(text, /FOO=b/);
});

test("writeToken: hardens an existing 0644 .env to 0600", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h), "FOO=b\n", { mode: 0o644 });
  fs.chmodSync(envPath(h), 0o644);
  writeToken("sk-ant-oat-x", h);
  assertOwnerOnly(envPath(h));
});

// The ordering guard. Before the atomic-rename fix these two passed ONLY
// because nothing asserted them: the credential was written to .env first and
// restrictToOwner ran after, so a failure left a readable token on disk and
// nh:save-token reported {ok:false} over it.
test("writeToken: a failing restrictToOwner leaves NO .env behind",
  { skip: SKIP_FAILING_RESTRICT }, () => {
  const h = home();
  withFailingRestrict(() => {
    assert.throws(() => writeToken("sk-ant-oat-mustnotpersist", h),
      /forced failure: cannot secure the file/);
  });
  assert.equal(fs.existsSync(envPath(h)), false,
    ".env must not exist: the credential may not reach disk before its "
    + "permissions are proven");
  assert.equal(fs.existsSync(`${envPath(h)}.tmp`), false,
    "the temp file must be cleaned up, not left holding the credential");
});

test("writeToken: a failing restrictToOwner leaves an EXISTING .env untouched",
  { skip: SKIP_FAILING_RESTRICT }, () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  const before = "# comment\nJIRA_API_TOKEN=keepme\n";
  fs.writeFileSync(envPath(h), before);
  withFailingRestrict(() => {
    assert.throws(() => writeToken("sk-ant-oat-mustnotpersist", h));
  });
  const after = fs.readFileSync(envPath(h), "utf8");
  assert.equal(after, before, "an existing .env must be byte-identical");
  assert.ok(!after.includes("sk-ant-oat-mustnotpersist"),
    "no credential byte may reach disk on the failure path");
  assert.equal(fs.existsSync(`${envPath(h)}.tmp`), false,
    "the temp file must be cleaned up");
});

test("writeToken: always ends with a newline", () => {
  const h = home();
  const p = writeToken("sk-ant-oat-x", h);
  assert.ok(fs.readFileSync(p, "utf8").endsWith("\n"));
});

test("profile-aware: a named auth_profile uses its own key", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(join(h, ".no_human", "config.yaml"),
    "llm:\n  auth_profile: personal\n");
  assert.equal(configuredProfile(h), "personal");
  assert.equal(tokenVarFor("personal"), `${TOKEN_KEY}_PERSONAL`);
  writeToken("sk-ant-oat-profile", h);
  const env = parseEnv(fs.readFileSync(envPath(h), "utf8"));
  assert.equal(env[`${TOKEN_KEY}_PERSONAL`], "sk-ant-oat-profile");
  assert.equal(env[TOKEN_KEY], undefined, "must not write the bare key");
  assert.equal(hasToken({}, h), true);
});

test("hasToken: a bare token does NOT satisfy a named profile", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(join(h, ".no_human", "config.yaml"),
    "llm:\n  auth_profile: personal\n");
  fs.writeFileSync(envPath(h), `${TOKEN_KEY}=bare-only\n`);
  assert.equal(hasToken({}, h), false);
});

test("validateToken: rejects empty, whitespace and API keys", () => {
  assert.notEqual(validateToken(""), "");
  assert.notEqual(validateToken("   "), "");
  assert.match(validateToken("sk-ant-api03-abc"), /API key/);
  assert.match(validateToken("SK-ANT-API03-ABC"), /API key/, "case-insensitive");
  assert.notEqual(validateToken("has space"), "");
  assert.equal(validateToken("sk-ant-oat-valid"), "", "a real token passes");
});

test("writeToken: refuses to persist an invalid value", () => {
  const h = home();
  assert.throws(() => writeToken("", h));
  assert.throws(() => writeToken("sk-ant-api03-nope", h));
  assert.equal(fs.existsSync(envPath(h)), false, "nothing written on reject");
});

// ── E2: BYO API key — the sanctioned opt-in billing mode ────────────────────
// The desktop screen historically accepted ONLY a subscription token; the
// backend has supported llm.auth_mode: "api_key" since 2026-07-24. These pin
// the desktop half: mode-aware validation, storage, config upsert, and gate.

import {
  API_KEY_VAR, configuredAuthMode, hasCredential, setAuthMode,
  validateCredential, writeCredential,
  OPENAI_KEY_VAR, validateOpenAiKey, writeOpenAiKey,
} from "./tokenStore.mjs";

test("validateCredential: api_key mode requires the sk-ant-api shape", () => {
  assert.equal(validateCredential("sk-ant-api03-abc", "api_key"), "");
  assert.equal(validateCredential("SK-ANT-API03-ABC", "api_key"), "");
  assert.notEqual(validateCredential("", "api_key"), "");
  assert.notEqual(validateCredential("has space", "api_key"), "");
  assert.match(validateCredential("sk-ant-oat-tok", "api_key"), /subscription token/i,
    "an OAuth token pasted into the key field gets a pointer, not a shrug");
  assert.notEqual(validateCredential("hello", "api_key"), "");
});

test("validateCredential: subscription mode keeps today's rules exactly", () => {
  assert.equal(validateCredential("sk-ant-oat-valid", "subscription"), "");
  assert.match(validateCredential("sk-ant-api03-abc", "subscription"), /API key/);
  assert.notEqual(validateCredential("", "subscription"), "");
});

test("writeCredential: api_key mode writes ANTHROPIC_API_KEY, 0600, others preserved", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h), "JIRA_API_TOKEN=keepme\n");
  const p = writeCredential("sk-ant-api03-abc", "api_key", h);
  const text = fs.readFileSync(p, "utf8");
  assert.equal(parseEnv(text)[API_KEY_VAR], "sk-ant-api03-abc");
  assert.match(text, /JIRA_API_TOKEN=keepme/);
  assertOwnerOnly(p);
  assert.throws(() => writeCredential("sk-ant-oat-x", "api_key", h),
    /subscription token/i, "wrong shape never touches disk");
});

test("writeCredential: subscription mode behaves exactly like writeToken", () => {
  const h = home();
  writeCredential("sk-ant-oat-x", "subscription", h);
  assert.equal(parseEnv(fs.readFileSync(envPath(h), "utf8"))[TOKEN_KEY], "sk-ant-oat-x");
});

test("setAuthMode: creates config.yaml with an llm block when absent", () => {
  const h = home();
  setAuthMode("api_key", h);
  const text = fs.readFileSync(join(h, ".no_human", "config.yaml"), "utf8");
  assert.match(text, /^llm:$/m);
  assert.match(text, /^  auth_mode: api_key$/m);
  assert.equal(configuredAuthMode(h), "api_key");
});

test("setAuthMode: upserts inside an existing llm block, preserving neighbours", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(join(h, ".no_human", "config.yaml"),
    "server:\n  port: 8420\nllm:\n  auth_profile: personal\n  auth_mode: subscription\ngit:\n  x: y\n");
  setAuthMode("api_key", h);
  const text = fs.readFileSync(join(h, ".no_human", "config.yaml"), "utf8");
  assert.match(text, /^  auth_mode: api_key$/m);
  assert.doesNotMatch(text, /auth_mode: subscription/);
  assert.match(text, /auth_profile: personal/, "sibling keys survive");
  assert.match(text, /port: 8420/, "other blocks survive");
  assert.match(text, /^git:$/m);
  // Switching BACK must work too (nh init does this in both directions).
  setAuthMode("subscription", h);
  assert.equal(configuredAuthMode(h), "subscription");
});

test("setAuthMode: adds auth_mode to an llm block that lacks one", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(join(h, ".no_human", "config.yaml"),
    "llm:\n  auth_profile: personal\n");
  setAuthMode("api_key", h);
  assert.equal(configuredAuthMode(h), "api_key");
  assert.match(fs.readFileSync(join(h, ".no_human", "config.yaml"), "utf8"),
    /auth_profile: personal/);
});

test("setAuthMode: rejects an unknown mode without touching the file", () => {
  const h = home();
  assert.throws(() => setAuthMode("bedrock", h));
  assert.equal(fs.existsSync(join(h, ".no_human", "config.yaml")), false);
});

test("hasCredential: follows the configured mode", () => {
  const h = home();
  // Default (no config) = subscription: an API key alone does not satisfy it.
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h), `${API_KEY_VAR}=sk-ant-api03-abc\n`);
  assert.equal(hasCredential({}, h), false,
    "subscription mode must not be satisfied by an API key");
  // api_key mode: the key satisfies it, .env wins, process env is the fallback.
  setAuthMode("api_key", h);
  assert.equal(hasCredential({}, h), true);
  fs.writeFileSync(envPath(h), "");
  assert.equal(hasCredential({}, h), false);
  assert.equal(hasCredential({ [API_KEY_VAR]: "sk-ant-api03-env" }, h), true);
  // And a subscription token does not satisfy api_key mode.
  writeToken("sk-ant-oat-x", h);
  assert.equal(hasCredential({}, h), false,
    "api_key mode must not be satisfied by an OAuth token");
});

// --- OPTIONAL codex OpenAI credential (constraint #6b) --------------------- //

test("validateOpenAiKey: accepts the OpenAI shapes, rejects empty/space/Anthropic", () => {
  assert.equal(validateOpenAiKey("sk-abc123"), "", "sk- is a valid OpenAI key");
  assert.equal(validateOpenAiKey("sk-proj-abc123"), "", "sk-proj- is valid");
  assert.equal(validateOpenAiKey("SK-PROJ-ABC"), "", "case-insensitive prefix");
  assert.match(validateOpenAiKey(""), /Paste the key/);
  assert.match(validateOpenAiKey("   "), /Paste the key/, "whitespace-only is empty");
  assert.match(validateOpenAiKey("sk-abc def"), /space/, "an internal space is caught");
  assert.match(validateOpenAiKey("sk-ant-api03-abc"), /Anthropic key/,
    "an Anthropic key is redirected to the Claude field, never accepted");
  assert.match(validateOpenAiKey("nope"), /starts with sk-/,
    "a value without the sk- shape is rejected");
});

test("writeOpenAiKey: writes OPENAI_API_KEY and PRESERVES the Claude token + Jira line", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h),
    `${TOKEN_KEY}=sk-ant-oat-keepme\nJIRA_API_TOKEN=jirakeep\n`);
  const p = writeOpenAiKey("sk-proj-openaivalue", h);
  const text = fs.readFileSync(p, "utf8");
  assert.equal(parseEnv(text)[OPENAI_KEY_VAR], "sk-proj-openaivalue");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-keepme",
    "the required Claude token must survive an optional OpenAI write");
  assert.match(text, /JIRA_API_TOKEN=jirakeep/, "other secrets untouched");
  assertOwnerOnly(p);
});

test("writeOpenAiKey: an invalid key NEVER touches disk", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h), `${TOKEN_KEY}=sk-ant-oat-x\n`);
  const before = fs.readFileSync(envPath(h), "utf8");
  assert.throws(() => writeOpenAiKey("sk-ant-api03-anthropic", h), /Anthropic key/);
  assert.throws(() => writeOpenAiKey("", h), /Paste the key/);
  assert.throws(() => writeOpenAiKey("garbage", h), /starts with sk-/);
  assert.equal(fs.readFileSync(envPath(h), "utf8"), before,
    "a rejected OpenAI key must not reach the .env");
});

// The #6b invariant at the storage layer: nothing this module writes for the
// REQUIRED Claude credential ever emits an OPENAI_API_KEY line. codex
// subscription mode is exactly "the OpenAI writer is never called", so a
// Claude-only save must leave the .env free of any OpenAI credential.
test("codex subscription writes NOTHING: no OPENAI_API_KEY from a Claude-only save", () => {
  const h = home();
  writeCredential("sk-ant-oat-claudeonly", "subscription", h);   // the whole save
  const text = fs.readFileSync(envPath(h), "utf8");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-claudeonly");
  assert.equal(parseEnv(text)[OPENAI_KEY_VAR], undefined,
    "no OpenAI credential may appear when the codex section is subscription/skipped");
  assert.ok(!/OPENAI/.test(text), "not even an OPENAI-prefixed line is written");
});

test("setAuthMode preserves a CRLF file's newline convention (no mixed EOL)", () => {
  const dir = mkdtempSync(join(tmpdir(), "nh-eol-"));
  const cfgDir = join(dir, ".no_human"); fs.mkdirSync(cfgDir, { recursive: true });
  const p = join(cfgDir, "config.yaml");
  fs.writeFileSync(p, "llm:\r\n  auth_mode: subscription\r\n  auth_profile: default\r\n");
  setAuthMode("api_key", dir);
  const out = fs.readFileSync(p, "utf8");
  assert.match(out, /auth_mode: api_key/);
  assert.ok(out.split("\n").every((l, i, a) => i === a.length - 1 || l.endsWith("\r")), "every line keeps its CR (no bare LF in a CRLF file)");
});

// ── CRLF .env regression: a CRLF-terminated line used to be DROPPED, not ──
// merely mis-trimmed. `.` excludes ALL line terminators (including \r), and
// the KEY=VALUE regex is un-anchored by the `m` flag, so `$` demanded
// end-of-string and the whole line failed to match under text.split("\n").
// These pin the fix at every layer the bug actually broke: the low-level
// parse map (AC1), the real gate main.mjs calls (AC2), and mixed/absent
// terminators (AC4).

test("parseEnv: a CRLF .env parses byte-identically to the LF equivalent", () => {
  const body = `FOO=bar\n${TOKEN_KEY}=sk-ant-oat-crlfliteral0123\nBAZ=qux\n`;
  const lf = body;
  const crlf = body.replace(/\n/g, "\r\n");
  // deepStrictEqual alone would pass if BOTH sides dropped the key — the
  // literal assertion below is what makes this test discriminating.
  assert.deepStrictEqual(parseEnv(crlf), parseEnv(lf));
  assert.equal(parseEnv(crlf)[TOKEN_KEY], "sk-ant-oat-crlfliteral0123");
});

test("hasToken: a CRLF .env is seen even with an EMPTY process env", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  const bytes = Buffer.from(`${TOKEN_KEY}=sk-ant-oat-crlfbytes\r\n`, "utf8");
  assert.equal(bytes[bytes.length - 2], 0x0d, "fixture sanity: bytes end 0d 0a");
  assert.equal(bytes[bytes.length - 1], 0x0a);
  fs.writeFileSync(envPath(h), bytes);
  // An empty env object means the process.env fall-through cannot mask a
  // dropped line — if parseEnv silently loses the key, this is false.
  assert.equal(hasToken({}, h), true);
});

test("hasCredential: a CRLF .env and a CRLF config.yaml still gate correctly", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  // Subscription (default mode, no config.yaml at all): CRLF .env token.
  fs.writeFileSync(envPath(h), Buffer.from(`${TOKEN_KEY}=sk-ant-oat-crlfsub\r\n`));
  assert.equal(hasCredential({}, h), true,
    "subscription mode must see a CRLF-terminated token");
  // api_key mode via a CRLF config.yaml — exercises the second reader
  // (configuredAuthMode) and the second hasCredential branch.
  fs.writeFileSync(join(h, ".no_human", "config.yaml"),
    "llm:\r\n  auth_mode: api_key\r\n");
  fs.writeFileSync(envPath(h), Buffer.from(`${API_KEY_VAR}=sk-ant-api03-crlf\r\n`));
  assert.equal(hasCredential({}, h), true,
    "api_key mode must see a CRLF-terminated key behind a CRLF config.yaml");
});

test("hasToken/hasCredential guard-rail: no .env, empty env => false", () => {
  // Proves the positive assertions above are discriminating: with nothing on
  // disk and nothing in the environment, both must still report absent.
  const h = home();
  assert.equal(hasToken({}, h), false);
  assert.equal(hasCredential({}, h), false);
});

test("a CRLF .env still wins over process.env", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  const A = "sk-ant-oat-FILEVALUE-AAAA";
  const B = "sk-ant-oat-ENVVALUE-BBBB";
  fs.writeFileSync(envPath(h), Buffer.from(`${TOKEN_KEY}=${A}\r\n`));
  // The file's own value must survive CRLF parsing exactly, not merely
  // "something truthy" — matching config.py:167's documented precedence
  // (".env wins over an inherited token: it is the curated source").
  assert.equal(parseEnv(fs.readFileSync(envPath(h), "utf8"))[TOKEN_KEY], A);
  assert.equal(hasToken({ [TOKEN_KEY]: B }, h), true,
    "the .env file must still be consulted even though process.env also has a value");
});

test("parseEnv: mixed CRLF and LF line endings in one file", () => {
  const env = parseEnv("A=1\r\nB=2\nC=3\r\n");
  assert.equal(env.A, "1");
  assert.equal(env.B, "2");
  assert.equal(env.C, "3");
});

test("parseEnv: no trailing newline still parses the final key (LF-interior)", () => {
  const env = parseEnv("A=1\nB=2");
  assert.equal(env.A, "1");
  assert.equal(env.B, "2");
});

test("parseEnv: no trailing newline still parses the final key (CRLF-interior)", () => {
  const env = parseEnv("A=1\r\nB=2");
  assert.equal(env.A, "1");
  assert.equal(env.B, "2");
});

test("writeEnvVar over a CRLF .env leaves no mixed endings", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h),
    Buffer.from(`JIRA_TOKEN=keep\r\n${TOKEN_KEY}=OLDVALUE\r\n`, "utf8"));
  writeToken("sk-ant-oat-newvalue", h);
  const bytes = fs.readFileSync(envPath(h));
  assert.ok(!bytes.includes(0x0d), "no CR byte anywhere in the rewritten file");
  assert.equal(bytes[bytes.length - 1], 0x0a, "file must end with a newline");
  assert.notEqual(bytes[bytes.length - 2], 0x0a,
    "exactly one trailing newline, not a blank line after it");
  const text = bytes.toString("utf8");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-newvalue");
  assert.equal(parseEnv(text).JIRA_TOKEN, "keep",
    "another secret must survive the CRLF-to-LF cleanup");
  assertOwnerOnly(envPath(h));
});
