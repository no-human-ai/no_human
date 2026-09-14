// #330 follow-up: `nhCanAutoUpdate` was computed from the macOS signing plan
// ALONE and stamped into the single shared `extraMetadata` block used by
// every platform target. A credentialed Apple environment (an operator's own
// shell, or a future consolidated release job) that also emits a Windows or
// Linux target therefore stamped `nhCanAutoUpdate: true` into THOSE artifacts
// too, even though the Windows/Linux update path is unverified — see
// signing.cjs's header for why that is not merely "unverified" but actively
// unsafe (NsisUpdater skips Authenticode verification entirely when no
// publisherName is configured, which is exactly this build's shape).
//
// These tests drive the REAL config, out-of-process, with fake-but-shaped
// Apple credentials present, and assert what gets stamped for a Windows
// target and a Linux target — not what the code is expected to compute in
// prose. A revert of the fix (stamping `plan.canAutoUpdate` directly again)
// must turn AC1 and the mixed-invocation test red; the mutation test at the
// bottom demonstrates that directly rather than asserting it.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";

const requireCjs = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(here, "..");
const REAL_MASTER = path.join(ROOT, "web", "public", "nh-mark-512.png");

// electron-builder.config.cjs refuses to load (process.exit(1)) unless the
// derived icons are present and fresh — same prologue packagedFiles.test.mjs
// uses, run BEFORE any config import/spawn below.
{
  const r = spawnSync(process.execPath,
    [path.join(ROOT, "packaging", "derive-icons.mjs")], { stdio: "inherit" });
  if (r.status !== 0) {
    throw new Error("updateStamp.test.mjs: derive-icons.mjs failed to produce "
      + "fresh desktop icons; see its FAIL: output above");
  }
}

// Apple credentials, shaped correctly so signingPlan() reads them as
// signed+notarized, but pointing at nothing real — signingPlan only checks
// PRESENCE of these vars; no signing or network I/O happens by loading the
// config or calling beforePack in-process.
const CREDS = {
  CSC_LINK: "file:///tmp/not-a-real-cert.p12",
  APPLE_API_KEY: "fake-key",
  APPLE_API_KEY_ID: "fake-key-id",
  APPLE_API_ISSUER: "fake-issuer",
};

// Vars that must NOT leak in from this process's own environment into a
// "no credentials" test case, or a developer's real keychain-profile shell
// would make the "unsigned" branch silently untested.
const CREDENTIAL_VAR_NAMES = [
  "CSC_LINK", "CSC_NAME", "CSC_KEY_PASSWORD", "CSC_KEYCHAIN",
  "APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER",
  "APPLE_ID", "APPLE_APP_SPECIFIC_PASSWORD", "APPLE_TEAM_ID",
  "APPLE_KEYCHAIN", "APPLE_KEYCHAIN_PROFILE",
  "WIN_CSC_LINK", "WIN_CSC_KEY_PASSWORD",
  "NH_REQUIRE_SIGNED",
];

function baseEnv() {
  const env = { ...process.env };
  for (const name of CREDENTIAL_VAR_NAMES) delete env[name];
  return env;
}

/**
 * Loads electron-builder.config.cjs in a CHILD process (argv and platform
 * flags matter, so `require()` must happen after the flags are already in
 * `process.argv`) and reports back the `extraMetadata` it computed.
 *
 * @param {object} opts
 * @param {string[]} opts.argv - flags to append after `--` (e.g. ["--win"]).
 * @param {object} [opts.env] - extra env vars layered onto a clean base env.
 * @param {string} [opts.cwd] - working directory for the child (for the
 *   mutation test's tmp copy); defaults to this file's directory.
 */
function stampFor({ argv, env = {}, cwd = here }) {
  const script = "const c = require(process.env.NH_CFG); "
    + "process.stdout.write('NHSTAMP' + JSON.stringify(c.extraMetadata));";
  const r = spawnSync(process.execPath,
    ["-e", script, "--", ...argv],
    {
      cwd,
      encoding: "utf8",
      env: { ...baseEnv(), ...env, NH_CFG: "./electron-builder.config.cjs" },
    });
  const marker = r.stdout.indexOf("NHSTAMP");
  const meta = marker === -1 ? null : JSON.parse(r.stdout.slice(marker + "NHSTAMP".length));
  return { status: r.status, stdout: r.stdout, stderr: r.stderr, meta };
}

// ---------------------------------------------------------------------------
// AC1: a build carrying Apple credentials that emits a Windows or Linux
// target does not stamp nhCanAutoUpdate true into that artifact.
// ---------------------------------------------------------------------------

test("AC1: Windows target with Apple credentials present stamps false", () => {
  for (const argv of [["--win"], ["-w"], ["--win", "nsis"]]) {
    const { meta, status, stderr } = stampFor({ argv, env: CREDS });
    assert.equal(status, 0, `child failed for argv=${argv}: ${stderr}`);
    assert.ok(meta, `no stamp captured for argv=${argv}`);
    assert.equal(meta.nhCanAutoUpdate, false, `argv=${argv} stamped true`);
    assert.equal(meta.nhSigning, "signed",
      "the Apple credentials must still be read as signed — this is not a "
      + "test of signingPlan(), only of what gets STAMPED for a non-mac target");
  }
});

test("AC1: Linux target with Apple credentials present stamps false", () => {
  for (const argv of [["--linux"], ["-l"], ["--linux", "deb", "AppImage"]]) {
    const { meta, status, stderr } = stampFor({ argv, env: CREDS });
    assert.equal(status, 0, `child failed for argv=${argv}: ${stderr}`);
    assert.ok(meta, `no stamp captured for argv=${argv}`);
    assert.equal(meta.nhCanAutoUpdate, false, `argv=${argv} stamped true`);
    assert.equal(meta.nhSigning, "signed");
  }
});

// ---------------------------------------------------------------------------
// AC2: macOS behaviour is unchanged — signed+notarized stamps true, and
// without credentials it still stamps false, on every platform.
// ---------------------------------------------------------------------------

test("AC2: macOS target with Apple credentials present stamps true", () => {
  for (const argv of [["--mac"], ["--mac", "dir"], ["-m"]]) {
    const { meta, status, stderr } = stampFor({ argv, env: CREDS });
    assert.equal(status, 0, `child failed for argv=${argv}: ${stderr}`);
    assert.ok(meta, `no stamp captured for argv=${argv}`);
    assert.equal(meta.nhCanAutoUpdate, true, `argv=${argv} did not stamp true`);
    assert.equal(meta.nhSigning, "signed");
  }
});

test("AC2: without credentials, every platform stamps false", () => {
  for (const argv of [["--mac"], ["--win"], ["--linux"]]) {
    const { meta, status, stderr } = stampFor({ argv, env: {} });
    assert.equal(status, 0, `child failed for argv=${argv}: ${stderr}`);
    assert.ok(meta, `no stamp captured for argv=${argv}`);
    assert.equal(meta.nhCanAutoUpdate, false, `argv=${argv} stamped true unsigned`);
    assert.equal(meta.nhSigning, "unsigned");
  }
});

// ---------------------------------------------------------------------------
// Mixed invocation: one credentialed run that emits BOTH a mac and a non-mac
// target has no single correct stamp, so the config must refuse outright
// rather than guess — this is the exact shape #330 reports.
// ---------------------------------------------------------------------------

test("mixed --mac --win with Apple credentials: refuses rather than guess", () => {
  const { status, stderr } = stampFor({ argv: ["--mac", "--win"], env: CREDS });
  assert.equal(status, 1, "a credentialed mixed-platform invocation must exit non-zero");
  assert.match(stderr, /REFUSING/);
  assert.match(stderr, /mac/i);
});

test("mixed --mac --win with no credentials: succeeds, stamps false", () => {
  const { meta, status, stderr } = stampFor({ argv: ["--mac", "--win"], env: {} });
  assert.equal(status, 0, `unsigned mixed invocation should not be fatal: ${stderr}`);
  assert.ok(meta);
  assert.equal(meta.nhCanAutoUpdate, false);
});

// ---------------------------------------------------------------------------
// AC3 (text-anchored): the three sentences this ticket protects are still
// present verbatim, and the code — not the sentences — was changed to match
// them: the stamp must be read from `stamp.canAutoUpdate`, never bare
// `plan.canAutoUpdate`, in the extraMetadata block.
// ---------------------------------------------------------------------------

test("AC3: the protected Windows and Linux sentences are unchanged", () => {
  const config = fs.readFileSync(path.join(here, "electron-builder.config.cjs"), "utf8");
  assert.match(config,
    /rather than worked around, and `nhCanAutoUpdate` below stays false: NSIS/,
    "the Windows sentence this ticket protects must still read verbatim");
  assert.match(config,
    /nhCanAutoUpdate stays false on Linux\s*\n\/\/ exactly as on the shipped unsigned Windows app/,
    "the Linux sentence this ticket protects must still read verbatim");

  const windowsDoc = fs.readFileSync(path.join(ROOT, "docs", "WINDOWS.md"), "utf8");
  assert.match(windowsDoc,
    /`nhCanAutoUpdate` stays `false` for an unsigned build,\s+so the shipped app will not offer one\./,
    "docs/WINDOWS.md's sentence this ticket protects must still read verbatim");
});

test("AC3: the code, not the sentences, was fixed — extraMetadata reads the per-platform stamp", () => {
  const config = fs.readFileSync(path.join(here, "electron-builder.config.cjs"), "utf8");
  const metaBlock = config.slice(config.indexOf("extraMetadata: {"));
  const block = metaBlock.slice(0, metaBlock.indexOf("},"));
  assert.match(block, /nhCanAutoUpdate:\s*stamp\.canAutoUpdate/,
    "extraMetadata.nhCanAutoUpdate must be wired to the per-platform stamp");
  assert.doesNotMatch(block, /nhCanAutoUpdate:\s*plan\.canAutoUpdate/,
    "the bug this ticket reports is exactly this line — the macOS plan alone "
    + "must never be stamped directly again");
});

// ---------------------------------------------------------------------------
// AC5 (implicit in the single-extraMetadata design defended at the file's
// header): electron-builder's own schema forces extraMetadata to be a single
// root-level key, so a per-platform extraMetadata block was never available
// as an alternative fix — pinned here so a future electron-builder upgrade
// that changed this would be caught rather than silently invalidate the
// header's argument.
// ---------------------------------------------------------------------------

test("AC5: extraMetadata is root-level only in electron-builder's schema", () => {
  const schema = JSON.parse(fs.readFileSync(
    requireCjs.resolve("electron-builder/../app-builder-lib/scheme.json"), "utf8"));
  // The root Configuration's properties live at the schema's own top level in
  // this electron-builder version (26.15.3), not under definitions.Configuration
  // — verified directly against the file rather than assumed.
  assert.ok(schema.properties?.extraMetadata,
    "electron-builder's root Configuration must still declare extraMetadata");
  // extraMetadata does NOT appear anywhere else in the schema (grep -c on the
  // raw file is 1) — pin that fact too, since it's what makes "the header's
  // argument" literally true rather than merely plausible.
  const raw = fs.readFileSync(
    requireCjs.resolve("electron-builder/../app-builder-lib/scheme.json"), "utf8");
  assert.equal((raw.match(/"extraMetadata"/g) ?? []).length, 1,
    "extraMetadata must be declared exactly once in the schema — at the Configuration root");
  for (const key of ["MacConfiguration", "WindowsConfiguration", "LinuxConfiguration"]) {
    const def = schema.definitions?.[key];
    assert.ok(def, `schema no longer defines ${key}`);
    // Each per-platform config neither declares its own `extraMetadata`...
    assert.ok(!Object.hasOwn(def.properties ?? {}, "extraMetadata"),
      `${key} must not declare its own extraMetadata property`);
    // ...nor accepts one it doesn't declare: additionalProperties: false means
    // AJV rejects `{ mac: { extraMetadata: {...} } }` outright. Together these
    // two facts are what makes a per-platform extraMetadata block impossible,
    // not merely unconventional — the single shared block this file's header
    // defends is schema-enforced, not a style choice that could be traded away.
    assert.equal(def.additionalProperties, false,
      `${key} must still reject unknown properties, or extraMetadata could be `
      + "smuggled into a per-platform block despite not being declared there");
  }
});

// ---------------------------------------------------------------------------
// Guard wiring: assertStampMatchesPlatform is the fail-closed backstop for
// any invocation shape buildPlatforms cannot see from argv (the Node API, a
// future flag alias). Confirm it is actually wired into beforePack, ahead of
// the existing notices check, and that it does not fire under the normal
// (unsigned CI) case.
// ---------------------------------------------------------------------------

test("beforePack calls both assertStampMatchesPlatform and assertElectronNoticesPresent", async () => {
  const builderConfig = await import("./electron-builder.config.cjs").then((m) => m.default ?? m);
  const src = String(builderConfig.beforePack);
  assert.match(src, /assertStampMatchesPlatform/);
  assert.match(src, /assertElectronNoticesPresent/);
  // Under this suite's own (uncredentialed) environment the stamp is false
  // everywhere, so the guard must not block a normal win32 pack.
  await assert.doesNotReject(
    builderConfig.beforePack({ electronPlatformName: "win32" }),
  );
});

// ---------------------------------------------------------------------------
// Mutation test: revert the fix (stamp `plan.canAutoUpdate` directly, the
// pre-fix bug) in a throwaway copy of the tree and confirm AC1 goes RED.
// This demonstrates the fix by mutation rather than by asserting it in
// prose — the recipe (copy desktop/ + packaging/ + the brand master into a
// tmp tree) mirrors deriveIcons.test.mjs's "electron-builder config refuses
// when the derived ico is absent" test.
// ---------------------------------------------------------------------------

test("mutation: reverting stamp.canAutoUpdate to plan.canAutoUpdate makes AC1 fail", () => {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "nh-updatestamp-mutant-"));
  fs.mkdirSync(path.join(outDir, "web", "public"), { recursive: true });
  fs.copyFileSync(REAL_MASTER, path.join(outDir, "web", "public", "nh-mark-512.png"));
  fs.cpSync(ROOT_desktop(), path.join(outDir, "desktop"), { recursive: true });
  fs.cpSync(path.join(ROOT, "packaging"), path.join(outDir, "packaging"), { recursive: true });

  const configPath = path.join(outDir, "desktop", "electron-builder.config.cjs");
  const original = fs.readFileSync(configPath, "utf8");
  assert.match(original, /nhCanAutoUpdate:\s*stamp\.canAutoUpdate/,
    "sanity check: the copy must still contain the fixed line before mutating it");
  const mutated = original.replace(
    /nhCanAutoUpdate:\s*stamp\.canAutoUpdate/,
    "nhCanAutoUpdate: plan.canAutoUpdate",
  );
  assert.notEqual(mutated, original, "the mutation must actually change the file");
  fs.writeFileSync(configPath, mutated);

  const { meta, status, stderr } = stampFor({
    argv: ["--win"],
    env: CREDS,
    cwd: path.join(outDir, "desktop"),
  });
  assert.equal(status, 0, `mutant child crashed unexpectedly: ${stderr}`);
  assert.ok(meta, "mutant produced no stamp");
  // This is the failure this whole ticket is about: the mutated (pre-fix)
  // config stamps `true` for a Windows target when Apple credentials are
  // present. If this assertion itself fails, the fix has regressed to the
  // pre-fix behaviour.
  assert.equal(meta.nhCanAutoUpdate, true,
    "expected the REVERTED code to reproduce the original bug (stamping true "
    + "for Windows under Apple credentials) — if this is false, either the "
    + "mutation didn't take or something else now prevents the bug, and this "
    + "test needs to be revisited, not deleted");

  fs.rmSync(outDir, { recursive: true, force: true });
});

function ROOT_desktop() {
  return path.join(ROOT, "desktop");
}
