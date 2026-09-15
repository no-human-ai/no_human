// The build must never hand back something that LOOKS shippable and isn't.
//
// These assert the observable outputs a consumer acts on — the electron-builder
// `identity`/`notarize` values, the artifact filename tag, and the
// auto-update permission — not the internal shape of the branch that produced
// them. Breaking the wiring (returning `null` identity when a cert is present,
// dropping the "-UNSIGNED" tag, letting an unsigned build auto-update) turns
// these red while every helper stays individually correct.
import assert from "node:assert/strict";
import test from "node:test";
import {
  NOTARY_CREDENTIAL_SETS, SIGNED, SIGNED_NOT_NOTARIZED, UNSIGNED,
  WINDOWS_CERTIFICATE_VAR,
  notaryCredentialSet, notarizeCredentials, signingBanner, signingPlan,
  windowsSigningBanner, windowsSigningPlan,
  buildPlatforms, autoUpdateStamp, assertStampMatchesPlatform,
} from "./signing.cjs";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const NOTARY = { APPLE_API_KEY: "k", APPLE_API_KEY_ID: "id", APPLE_API_ISSUER: "iss" };
const CERT = { CSC_LINK: "file:///cert.p12" };

test("no credentials at all: unsigned, tagged, and barred from auto-update", () => {
  const p = signingPlan({});
  assert.equal(p.mode, UNSIGNED);
  // `null` is electron-builder's explicit "do not sign". `undefined` would mean
  // "auto-discover", which on a machine that happens to hold a cert in its
  // keychain would sign a build that must not be signed.
  assert.equal(p.identity, null, "unsigned builds must pin identity to null");
  assert.equal(p.notarize, false);
  assert.equal(p.artifactTag, "-UNSIGNED",
    "an unsigned artifact must be named so it cannot be shipped by accident");
  assert.equal(p.canAutoUpdate, false,
    "Squirrel.Mac cannot install into an unsigned bundle");
  assert.equal(p.fatal, false, "an unsigned build is still allowed to succeed");
});

test("cert but no notarization credentials: refuses the clean name", () => {
  const p = signingPlan({ ...CERT });
  assert.equal(p.mode, SIGNED_NOT_NOTARIZED);
  assert.equal(p.artifactTag, "-UNNOTARIZED");
  assert.equal(p.notarize, false, "cannot notarize without credentials");
  assert.equal(p.canAutoUpdate, false,
    "Gatekeeper rejects an un-notarized download, so an update offer would lie");
  assert.match(p.reason, /notarization credentials/i);
});

test("a bare APPLE_KEYCHAIN path with no profile is not a credential", () => {
  // A keychain PATH alone identifies nowhere to look up a password item —
  // notarytool needs the PROFILE. Must never be mistaken for set 3.
  const p = signingPlan({ ...CERT, APPLE_KEYCHAIN: "/k.keychain-db" });
  assert.equal(p.mode, SIGNED_NOT_NOTARIZED);
  assert.equal(p.notarize, false);
  assert.equal(p.artifactTag, "-UNNOTARIZED");
  assert.equal(notarizeCredentials({ APPLE_KEYCHAIN: "/k.keychain-db" }), null);
});

test("cert + notarization credentials: the only combination that ships clean", () => {
  const p = signingPlan({ ...CERT, ...NOTARY });
  assert.equal(p.mode, SIGNED);
  assert.equal(p.artifactTag, "",
    "only a signed+notarized build may carry the plain release filename");
  assert.equal(p.notarize, true);
  assert.equal(p.canAutoUpdate, true);
  assert.equal(p.identity, undefined,
    "identity must be auto-discovered, never a hardcoded certificate name");
});

test("CSC_NAME is accepted as an identity source as well as CSC_LINK", () => {
  const p = signingPlan({ CSC_NAME: "Developer ID Application: X (Y)", ...NOTARY });
  assert.equal(p.mode, SIGNED);
});

test("all three of Apple's credential sets are honoured", () => {
  // Guard the guard: if this list is silently trimmed, builds that could be
  // notarized would be tagged unshippable and nobody would know why.
  assert.equal(NOTARY_CREDENTIAL_SETS.length, 3);
  const sets = [
    { APPLE_API_KEY: "a", APPLE_API_KEY_ID: "b", APPLE_API_ISSUER: "c" },
    { APPLE_ID: "a", APPLE_APP_SPECIFIC_PASSWORD: "b", APPLE_TEAM_ID: "c" },
    { APPLE_KEYCHAIN_PROFILE: "b" },
  ];
  for (const env of sets) {
    assert.ok(notaryCredentialSet(env), `unrecognised set: ${Object.keys(env)}`);
    assert.equal(signingPlan({ ...CERT, ...env }).mode, SIGNED);
  }
});

test("a partially-filled credential set does not count as credentials", () => {
  // The failure this prevents: two of three vars set in CI, notarization
  // silently skipped, artifact still named as a release.
  const p = signingPlan({ ...CERT, APPLE_ID: "a", APPLE_TEAM_ID: "c" });
  assert.equal(p.mode, SIGNED_NOT_NOTARIZED);
  assert.equal(notaryCredentialSet({ APPLE_ID: "a", APPLE_TEAM_ID: "c" }), null);
  // Pins that only set 3 was relaxed — sets 1 and 2 still require every var.
  assert.equal(notaryCredentialSet({ APPLE_API_KEY: "a", APPLE_API_KEY_ID: "b" }), null);
});

test("APPLE_KEYCHAIN_PROFILE alone is a complete notarization credential", () => {
  // The regression: notarytool resolves a profile through its DEFAULT
  // keychain search. Requiring APPLE_KEYCHAIN alongside it broke that
  // resolution (`--keychain <path>` cannot see what the default search
  // finds), forcing a signed-but-unnotarized release. This must be SIGNED.
  const p = signingPlan({ ...CERT, APPLE_KEYCHAIN_PROFILE: "nh-notary" });
  assert.equal(p.mode, SIGNED);
  assert.equal(p.notarize, true);
  assert.equal(p.artifactTag, "");
  assert.equal(p.canAutoUpdate, true);
});

test("the keychain PATH is optional and forwarded only when set", () => {
  const profileOnly = notarizeCredentials({ APPLE_KEYCHAIN_PROFILE: "p" });
  assert.equal(profileOnly.keychainProfile, "p");
  // Absence of the KEY, not an undefined value — `--keychain undefined` is
  // the bug this guards against.
  assert.ok(!("keychain" in profileOnly));

  const both = notarizeCredentials({ APPLE_KEYCHAIN: "/k.keychain-db", APPLE_KEYCHAIN_PROFILE: "p" });
  assert.equal(both.keychain, "/k.keychain-db");
  assert.equal(both.keychainProfile, "p");

  const whitespaceKeychain = notarizeCredentials({ APPLE_KEYCHAIN: "   ", APPLE_KEYCHAIN_PROFILE: "p" });
  assert.ok(!("keychain" in whitespaceKeychain),
    "a whitespace-only APPLE_KEYCHAIN must count as absent, like everywhere else");
});

test("the banner names credential VARIABLES, never their values", () => {
  const sentinelSets = [
    { APPLE_API_KEY: "SEKRIT-KEY", APPLE_API_KEY_ID: "SEKRIT-KEYID", APPLE_API_ISSUER: "SEKRIT-ISSUER" },
    { APPLE_ID: "SEKRIT-ID", APPLE_APP_SPECIFIC_PASSWORD: "SEKRIT-PASSWORD", APPLE_TEAM_ID: "SEKRIT-TEAM" },
    { APPLE_KEYCHAIN_PROFILE: "SEKRIT-PROFILE-VALUE" },
  ];
  const secretValues = ["SEKRIT-KEY", "SEKRIT-KEYID", "SEKRIT-ISSUER", "SEKRIT-ID",
    "SEKRIT-PASSWORD", "SEKRIT-TEAM", "SEKRIT-PROFILE-VALUE", "SEKRIT-CERT"];
  for (const notary of sentinelSets) {
    const plan = signingPlan({ CSC_LINK: "SEKRIT-CERT", ...notary });
    const banner = signingBanner(plan);
    for (const secret of secretValues) {
      assert.ok(!banner.includes(secret), `banner leaked ${secret}`);
      assert.ok(!plan.reason.includes(secret), `reason leaked ${secret}`);
    }
  }
  assert.match(
    signingBanner(signingPlan({ CSC_LINK: "SEKRIT-CERT", APPLE_KEYCHAIN_PROFILE: "SEKRIT-PROFILE-VALUE" })),
    /APPLE_KEYCHAIN_PROFILE/,
  );
});

test("empty and whitespace-only vars are absent, not present", () => {
  // CI systems export unset secrets as the empty string. Treating "" as set is
  // how you get `codesign --sign ""`.
  assert.equal(signingPlan({ CSC_LINK: "" }).mode, UNSIGNED);
  assert.equal(signingPlan({ CSC_LINK: "   " }).mode, UNSIGNED);
  assert.equal(signingPlan({ ...CERT, APPLE_API_KEY: "", APPLE_API_KEY_ID: "b",
                             APPLE_API_ISSUER: "c" }).mode, SIGNED_NOT_NOTARIZED);
});

test("NH_REQUIRE_SIGNED turns an unshippable build into a hard failure", () => {
  assert.equal(signingPlan({ NH_REQUIRE_SIGNED: "1" }).fatal, true);
  assert.equal(signingPlan({ ...CERT, NH_REQUIRE_SIGNED: "1" }).fatal, true,
    "signed-but-not-notarized must also fail a release build");
  assert.equal(signingPlan({ ...CERT, ...NOTARY, NH_REQUIRE_SIGNED: "1" }).fatal,
    false, "a real release build must pass its own gate");
  assert.equal(signingPlan({ NH_REQUIRE_SIGNED: "0" }).fatal, false,
    "an explicit 0 must not enable the gate");
});

test("the banner names the mode and never renders empty", () => {
  // The operator's stated failure mode is a SILENT unsigned build. The banner
  // is the thing that makes it not silent, so its content is asserted.
  for (const env of [{}, CERT, { ...CERT, ...NOTARY }]) {
    const plan = signingPlan(env);
    const banner = signingBanner(plan);
    assert.ok(banner.includes(plan.reason), "the banner must carry the reason");
    assert.ok(banner.length > 40);
  }
  assert.match(signingBanner(signingPlan({})), /UNSIGNED — NOT SHIPPABLE/);
  assert.match(signingBanner(signingPlan({ ...CERT, ...NOTARY })),
    /release build \(signed \+ notarized\)/);
  assert.match(signingBanner(signingPlan({ NH_REQUIRE_SIGNED: "1" })),
    /refusing to continue/);
});


// ---------------------------------------------------------------------------
// The Windows half (#330): the .exe name must claim only what Windows inputs
// support. The macOS identity variables decided it before, so an exe could be
// named as a release while unsigned — no Windows certificate exists for
// CSC_NAME to point at.
// ---------------------------------------------------------------------------

test("no Windows certificate: the exe is tagged", () => {
  const p = windowsSigningPlan({});
  assert.equal(p.signed, false);
  assert.equal(p.artifactTag, "-UNSIGNED");
});

test("an Apple identity cannot make the exe name claim a signature", () => {
  // The exact shape the issue names: a shared org secret, or a matrix that
  // exports the Apple variables once for every platform.
  const p = windowsSigningPlan({
    CSC_LINK: "file:///apple.p12",
    CSC_NAME: "Developer ID Application: Someone (TEAMID)",
    CSC_KEY_PASSWORD: "pw",
    ...NOTARY,
  });
  assert.equal(p.signed, false, "an Apple .p12 cannot sign an exe");
  assert.equal(p.artifactTag, "-UNSIGNED");
});

test("a Windows certificate clears the tag", () => {
  const p = windowsSigningPlan({ [WINDOWS_CERTIFICATE_VAR]: "file:///win.pfx" });
  assert.equal(p.signed, true);
  assert.equal(p.artifactTag, "");
});

test("the two plans are independent in both directions", () => {
  // macOS signed, Windows not: the DMG loses its tag, the exe keeps one.
  const env = { ...CERT, ...NOTARY };
  assert.equal(signingPlan(env).artifactTag, "");
  assert.equal(windowsSigningPlan(env).artifactTag, "-UNSIGNED");
  // ...and the other way round.
  const winEnv = { [WINDOWS_CERTIFICATE_VAR]: "file:///win.pfx" };
  assert.equal(signingPlan(winEnv).artifactTag, "-UNSIGNED");
  assert.equal(windowsSigningPlan(winEnv).artifactTag, "");
});

test("the Windows banner sends the reader to the artifact, not the log", () => {
  // electron-builder prints "signing with signtool.exe path=..." for every exe
  // even when nothing is signed. A reader grepping the log for "signing" gets
  // the wrong answer, so the banner has to name the real check.
  const banner = windowsSigningBanner(windowsSigningPlan({}));
  assert.match(banner, /UNSIGNED/);
  assert.match(banner, /Get-AuthenticodeSignature/);
  assert.match(banner, /step name/);
});

test("the Windows artifact name is wired to the Windows plan", () => {
  // The one line that makes the fix live. Nothing else in this file can see
  // it: the plan can be perfect and the config can still interpolate the
  // macOS tag into the exe name, which is the state this issue reports.
  const here = dirname(fileURLToPath(import.meta.url));
  const config = readFileSync(join(here, "electron-builder.config.cjs"), "utf8");
  const win = config.slice(config.indexOf("const win = {"));
  const artifactName = win.slice(0, win.indexOf("};"));
  assert.match(artifactName, /winPlan\.artifactTag/);
  assert.doesNotMatch(artifactName, /[^n]plan\.artifactTag/);
});


// ---------------------------------------------------------------------------
// buildPlatforms: which platform(s) THIS invocation actually targets, parsed
// from argv the same way electron-builder's own yargs config does
// (node_modules/electron-builder/out/builder.js:189-210 — aliases m/o/macos,
// w/windows, l — transcribed, not guessed).
// ---------------------------------------------------------------------------

test("buildPlatforms: long flags", () => {
  assert.deepEqual(buildPlatforms(["--mac"], "linux"), new Set(["darwin"]));
  assert.deepEqual(buildPlatforms(["--macos"], "linux"), new Set(["darwin"]));
  assert.deepEqual(buildPlatforms(["--win"], "linux"), new Set(["win32"]));
  assert.deepEqual(buildPlatforms(["--windows"], "linux"), new Set(["win32"]));
  assert.deepEqual(buildPlatforms(["--linux"], "win32"), new Set(["linux"]));
});

test("buildPlatforms: short flags and clustered single-dash letters", () => {
  assert.deepEqual(buildPlatforms(["-m"], "linux"), new Set(["darwin"]));
  assert.deepEqual(buildPlatforms(["-o"], "linux"), new Set(["darwin"]));
  assert.deepEqual(buildPlatforms(["-w"], "linux"), new Set(["win32"]));
  assert.deepEqual(buildPlatforms(["-l"], "win32"), new Set(["linux"]));
  assert.deepEqual(buildPlatforms(["-mwl"], "linux"),
    new Set(["darwin", "win32", "linux"]));
});

test("buildPlatforms: a target list after the flag does not change the platform", () => {
  assert.deepEqual(buildPlatforms(["--win", "nsis"], "linux"), new Set(["win32"]));
  assert.deepEqual(buildPlatforms(["--mac", "dir"], "linux"), new Set(["darwin"]));
  assert.deepEqual(buildPlatforms(["--linux", "deb", "AppImage"], "win32"),
    new Set(["linux"]));
});

test("buildPlatforms: --flag=value form is still recognised by name", () => {
  assert.deepEqual(buildPlatforms(["--mac=dir"], "linux"), new Set(["darwin"]));
});

test("buildPlatforms: mixed invocation targets more than one platform", () => {
  assert.deepEqual(buildPlatforms(["--mac", "--win"], "linux"),
    new Set(["darwin", "win32"]));
});

test("buildPlatforms: no flags at all falls back to the host platform", () => {
  assert.deepEqual(buildPlatforms([], "darwin"), new Set(["darwin"]));
  assert.deepEqual(buildPlatforms([], "win32"), new Set(["win32"]));
  assert.deepEqual(buildPlatforms([], "linux"), new Set(["linux"]));
  assert.deepEqual(buildPlatforms(["--publish", "never"], "darwin"),
    new Set(["darwin"]), "unrelated flags must not be mistaken for a platform");
});

test("buildPlatforms: an exotic host with no flags yields an empty set", () => {
  // Platform.current() would throw on a host electron-builder itself does not
  // support; buildPlatforms must not invent a platform, so autoUpdateStamp
  // below can treat it as "not mac-only" rather than crash.
  assert.deepEqual(buildPlatforms([], "freebsd"), new Set());
});

test("buildPlatforms: unrecognised single-dash letters are not clustered", () => {
  // "-c" is the config flag, not a platform letter; must not be swallowed.
  assert.deepEqual(buildPlatforms(["-c", "config.json"], "linux"), new Set(["linux"]));
});


// ---------------------------------------------------------------------------
// autoUpdateStamp: the macOS signing verdict only ever authorises an update
// path when this invocation's platform set is mac-and-only-mac.
// ---------------------------------------------------------------------------

const SIGNED_PLAN = signingPlan({ ...CERT, ...NOTARY });
const UNSIGNED_PLAN = signingPlan({});
const SIGNED_NOT_NOTARIZED_PLAN = signingPlan({ ...CERT });

test("autoUpdateStamp: signed+notarized, mac-only targets stamp true", () => {
  for (const argv of [["--mac"], ["-m"], ["--macos"], ["--mac", "dir"]]) {
    const platforms = buildPlatforms(argv, "darwin");
    const stamp = autoUpdateStamp({ plan: SIGNED_PLAN, platforms });
    assert.equal(stamp.canAutoUpdate, true, `argv=${argv}`);
    assert.equal(stamp.fatal, false, `argv=${argv}`);
  }
});

test("autoUpdateStamp: signed+notarized but a Windows-only target stamps false", () => {
  for (const argv of [["--win"], ["-w"], ["--windows"], ["--win", "nsis"]]) {
    const platforms = buildPlatforms(argv, "darwin");
    const stamp = autoUpdateStamp({ plan: SIGNED_PLAN, platforms });
    assert.equal(stamp.canAutoUpdate, false, `argv=${argv}`);
    assert.equal(stamp.fatal, false, `argv=${argv}`);
  }
});

test("autoUpdateStamp: signed+notarized but a Linux-only target stamps false", () => {
  for (const argv of [["--linux"], ["-l"], ["--linux", "deb"]]) {
    const platforms = buildPlatforms(argv, "darwin");
    const stamp = autoUpdateStamp({ plan: SIGNED_PLAN, platforms });
    assert.equal(stamp.canAutoUpdate, false, `argv=${argv}`);
    assert.equal(stamp.fatal, false, `argv=${argv}`);
  }
});

test("autoUpdateStamp: unsigned build stamps false regardless of target", () => {
  for (const argv of [["--mac"], ["--win"], ["--linux"]]) {
    const platforms = buildPlatforms(argv, "darwin");
    const stamp = autoUpdateStamp({ plan: UNSIGNED_PLAN, platforms });
    assert.equal(stamp.canAutoUpdate, false, `argv=${argv}`);
    assert.equal(stamp.fatal, false, `argv=${argv}`);
  }
});

test("autoUpdateStamp: signed-but-not-notarized mac target still stamps false", () => {
  const platforms = buildPlatforms(["--mac"], "darwin");
  const stamp = autoUpdateStamp({ plan: SIGNED_NOT_NOTARIZED_PLAN, platforms });
  assert.equal(stamp.canAutoUpdate, false);
  assert.equal(stamp.fatal, false);
});

test("autoUpdateStamp: a signed+notarized run that ALSO emits a non-mac target is fatal", () => {
  // This is the exact shape the ticket reports: one credentialed invocation,
  // multiple platform outputs. There is no single correct stamp, so refuse
  // outright rather than guess.
  const platforms = buildPlatforms(["--mac", "--win"], "darwin");
  const stamp = autoUpdateStamp({ plan: SIGNED_PLAN, platforms });
  assert.equal(stamp.canAutoUpdate, false);
  assert.equal(stamp.fatal, true);
  assert.match(stamp.reason, /REFUSING/);
});

test("autoUpdateStamp: an unsigned run with mac+other targets is not fatal", () => {
  // Nothing shippable would auto-update either way, so there's no unsafe
  // stamp to refuse — only a credentialed run needs the hard stop.
  const platforms = buildPlatforms(["--mac", "--win"], "darwin");
  const stamp = autoUpdateStamp({ plan: UNSIGNED_PLAN, platforms });
  assert.equal(stamp.canAutoUpdate, false);
  assert.equal(stamp.fatal, false);
});

test("autoUpdateStamp: an empty platform set (exotic host) is not mac-only", () => {
  const stamp = autoUpdateStamp({ plan: SIGNED_PLAN, platforms: new Set() });
  assert.equal(stamp.canAutoUpdate, false);
  assert.equal(stamp.fatal, false);
  assert.match(stamp.reason, /none/);
});


// ---------------------------------------------------------------------------
// assertStampMatchesPlatform: the runtime backstop electron-builder's
// beforePack hook calls with the REAL electronPlatformName per platform, for
// any invocation shape buildPlatforms could not see from argv.
// ---------------------------------------------------------------------------

test("assertStampMatchesPlatform: throws for a non-mac platform stamped true", () => {
  assert.throws(() => assertStampMatchesPlatform("win32", true), /win32/);
  assert.throws(() => assertStampMatchesPlatform("linux", true), /linux/);
});

test("assertStampMatchesPlatform: passes for darwin true, and for false anywhere", () => {
  assert.doesNotThrow(() => assertStampMatchesPlatform("darwin", true));
  assert.doesNotThrow(() => assertStampMatchesPlatform("win32", false));
  assert.doesNotThrow(() => assertStampMatchesPlatform("linux", false));
  assert.doesNotThrow(() => assertStampMatchesPlatform("darwin", false));
});
