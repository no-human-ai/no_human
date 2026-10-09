// Whether this build is shippable, decided from the environment ALONE.
//
// Why this is a module and not three lines inside electron-builder's config:
// the same decision has to be made in three places that cannot import each
// other — the electron-builder config (which identity to sign with), the DMG
// packaging script (what to NAME the artifact), and the runtime updater (may
// this app auto-update at all). Three copies of a security-relevant predicate
// is how you get a build that signs but names the file as if it were signed,
// or an updater that promises an install macOS will refuse. One pure function,
// three consumers, one set of tests.
//
// The failure this exists to prevent: a green build that produces an artifact
// Gatekeeper rejects. An unsigned build is legitimate (it is the ONLY thing
// possible before the Apple Developer membership is active) — it just must not
// be mistakable for a release. So unsigned builds still succeed, and are named
// so that nobody can ship one by accident.
//
// CommonJS on purpose: two consumers load it with require() — the
// electron-builder config (.cjs) and the DMG script — and require() of an
// ES module is an error on the Node this repo runs (v20). ESM consumers
// can import a CJS module either way.
//
// signingPlan() alone is NOT enough to decide `nhCanAutoUpdate`: it only
// reads the macOS signing environment, but a single electron-builder
// invocation can emit a signed mac target and an unsigned win/linux target
// side by side (`--mac --win`, or a future consolidated release job). The
// TARGET PLATFORM SET this invocation actually emits — not just whether
// Apple credentials happen to be exported — decides whether the macOS
// verdict applies. `buildPlatforms`/`autoUpdateStamp` below compute that;
// `assertStampMatchesPlatform` is the runtime backstop for any invocation
// shape `buildPlatforms` cannot see (the electron-builder Node API, a future
// flag alias) — it throws rather than let a wrong stamp reach a real target.
//
// Why a wrong stamp here is not merely "unverified" but actively unsafe,
// measured from source (electron-updater 6.x, node_modules/electron-updater/
// out/): NsisUpdater.verifySignature() (NsisUpdater.js:84-99) reads
// `publisherName` from the generated app-update.yml and returns null —
// meaning verification is SKIPPED, not failed — when that field is absent;
// our `win` target sets no certificateFile, so an unsigned Windows build's
// app-update.yml has no publisherName. A wrongly-true stamp there would
// therefore make electron-updater run a downloaded, unsigned installer with
// NO Authenticode check at all (not "unverified", literally none). Linux is
// safer only by accident: AppImageUpdater.js:18 refuses outright unless
// `process.env.APPIMAGE` is set, but DebUpdater (DebUpdater.js) has no
// signature check either — it just downloads the `.deb` and hands it to a
// privileged installer. Neither platform's electron-updater path performs
// the equivalent of Squirrel.Mac's code-signing gate, which is the actual
// reason this stamp must never be computed from the macOS plan alone.

/** Fully signed AND notarized — the only artifact a stranger can run. */
const SIGNED = "signed";
/** Has a Developer ID signature but was never sent to Apple. */
const SIGNED_NOT_NOTARIZED = "signed-not-notarized";
/** No signing material at all — the state before the membership exists. */
const UNSIGNED = "unsigned";

/**
 * The three credential sets @electron/notarize accepts, read verbatim from
 * app-builder-lib/scheme.json (electron-builder 26.15.3) rather than from
 * memory — the set changed across major versions and `altool`'s pair was
 * decommissioned by Apple on 2023-11-01.
 */
const NOTARY_CREDENTIAL_SETS = [
  ["APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER"],
  ["APPLE_ID", "APPLE_APP_SPECIFIC_PASSWORD", "APPLE_TEAM_ID"],
  // A keychain PROFILE (created via `notarytool store-credentials`) resolves
  // through notarytool's DEFAULT keychain search — it is a complete
  // credential on its own. APPLE_KEYCHAIN (a keychain PATH) is therefore
  // optional, and requiring it is actively hostile: passing
  // `--keychain <path> --keychain-profile <p>` makes notarytool fail with
  // "No Keychain password item found for profile", because the path search
  // does not see what the default search resolves. Measured on the
  // operator's Mac 2026-08-23.
  ["APPLE_KEYCHAIN_PROFILE"],
];

/** A var counts as absent when unset, empty, or whitespace-only. */
function present(env, name) {
  return typeof env[name] === "string" && env[name].trim() !== "";
}

/** Which notarization credential set is satisfied, or null. */
function notaryCredentialSet(env = {}) {
  for (const set of NOTARY_CREDENTIAL_SETS) {
    if (set.every((name) => present(env, name))) return set;
  }
  return null;
}

/**
 * What would actually be forwarded to notarytool for the satisfied
 * credential set, or null if none is satisfied. Keeps "what reaches
 * notarytool" testable without importing electron-builder.
 *
 * For set 3, APPLE_KEYCHAIN is forwarded ONLY when present — an absent
 * `keychain` key (not an `undefined` value) is the point: electron-builder
 * only appends `--keychain <path>` when the option key exists.
 */
function notarizeCredentials(env = {}) {
  const set = notaryCredentialSet(env);
  if (!set) return null;
  if (set === NOTARY_CREDENTIAL_SETS[0]) {
    return { key: env.APPLE_API_KEY, keyId: env.APPLE_API_KEY_ID, issuer: env.APPLE_API_ISSUER };
  }
  if (set === NOTARY_CREDENTIAL_SETS[1]) {
    return { appleId: env.APPLE_ID, appleIdPassword: env.APPLE_APP_SPECIFIC_PASSWORD, teamId: env.APPLE_TEAM_ID };
  }
  const opts = { keychainProfile: env.APPLE_KEYCHAIN_PROFILE };
  if (present(env, "APPLE_KEYCHAIN")) opts.keychain = env.APPLE_KEYCHAIN;
  return opts;
}

/**
 * Decide how this build must behave.
 *
 * Returns:
 *   mode         — one of SIGNED | SIGNED_NOT_NOTARIZED | UNSIGNED
 *   identity     — value for electron-builder's `mac.identity`. `null` is
 *                  electron-builder's documented "do not sign"; `undefined`
 *                  means "auto-discover from CSC_LINK/CSC_NAME", which is what
 *                  you want when credentials exist. These are NOT
 *                  interchangeable: `null` with a valid cert present still
 *                  produces an unsigned app.
 *   notarize     — value for `mac.notarize`. The option reads inverted in
 *                  electron-builder 26 (it is on when credentials exist), so
 *                  this is set explicitly in both directions.
 *   artifactTag  — suffix for the DMG filename. Empty ONLY for a real release.
 *   canAutoUpdate— may the shipped app offer to update itself.
 *   fatal        — the build must stop (NH_REQUIRE_SIGNED was set and unmet).
 *   reason       — one line, written to be read by a human in build output.
 */
function signingPlan(env = {}) {
  const hasIdentity = present(env, "CSC_LINK") || present(env, "CSC_NAME");
  const notary = notaryCredentialSet(env);
  const required = present(env, "NH_REQUIRE_SIGNED")
    && env.NH_REQUIRE_SIGNED !== "0";

  let plan;
  if (hasIdentity && notary) {
    plan = {
      mode: SIGNED,
      // Auto-discovery, NOT a hardcoded certificate name: the Team ID and the
      // exact "Developer ID Application: NAME (TEAMID)" string are unknown
      // until the membership is active, and a placeholder that shipped would
      // silently sign with the wrong identity or fail late.
      identity: undefined,
      notarize: true,
      artifactTag: "",
      canAutoUpdate: true,
      reason: `signed with the identity in ${present(env, "CSC_LINK") ? "CSC_LINK" : "CSC_NAME"}`
        + ` and notarized via ${notary[0]}`,
    };
  } else if (hasIdentity) {
    plan = {
      mode: SIGNED_NOT_NOTARIZED,
      identity: undefined,
      notarize: false,
      artifactTag: "-UNNOTARIZED",
      // Squirrel.Mac only needs a signature, but an un-notarized app is
      // refused by Gatekeeper on any machine that downloaded it — offering an
      // update that cannot be launched is worse than offering none.
      canAutoUpdate: false,
      reason: "a signing identity is set but NO notarization credentials are —"
        + " Gatekeeper rejects un-notarized downloads. Set one of: "
        + NOTARY_CREDENTIAL_SETS.map((s) => s.join("+")).join(" | "),
    };
  } else {
    plan = {
      mode: UNSIGNED,
      identity: null,
      notarize: false,
      artifactTag: "-UNSIGNED",
      canAutoUpdate: false,
      reason: "no CSC_LINK or CSC_NAME — this build is UNSIGNED. macOS will"
        + " refuse it on any machine but this one, and auto-update cannot work"
        + " (Squirrel.Mac requires a signature).",
    };
  }

  plan.fatal = required && plan.mode !== SIGNED;
  return plan;
}

//: The Windows certificate. `CSC_LINK`/`CSC_NAME` are deliberately NOT read
//: here. `CSC_NAME` is an Apple identity NAME — there is no Windows
//: certificate for it to point at — and `CSC_LINK` is whatever .p12 the macOS
//: lane was given, which cannot sign an exe either. Letting either one decide
//: the Windows filename is the defect: the variable that flips the name would
//: have nothing to do with the thing the name is claiming (#330).
const WINDOWS_CERTIFICATE_VAR = "WIN_CSC_LINK";

/** The Windows half of the plan: does the .exe name claim a signature?
 *
 *  Separate from {@link signingPlan} rather than a branch inside it, because
 *  the two answer different questions from different inputs and only share a
 *  filename convention. The macOS plan additionally decides notarization,
 *  auto-update permission and the electron-builder `identity` value, none of
 *  which exist on the Windows side.
 *
 *  Fail-safe direction: a Windows build signed through electron-builder's
 *  generic `CSC_LINK` fallback is tagged `-UNSIGNED` here, which UNDER-claims.
 *  A name that under-claims can be corrected by setting `WIN_CSC_LINK`; a name
 *  that over-claims is uploaded to a release page.
 */
function windowsSigningPlan(env = {}) {
  if (present(env, WINDOWS_CERTIFICATE_VAR)) {
    return {
      signed: true,
      // Windows has only two states: a certificate is present or it is not.
      // Notarization is an Apple concept — SIGNED_NOT_NOTARIZED can never be
      // the Windows verdict.
      mode: SIGNED,
      artifactTag: "",
      reason: `a Windows certificate is set in ${WINDOWS_CERTIFICATE_VAR}`,
    };
  }
  return {
    signed: false,
    mode: UNSIGNED,
    artifactTag: "-UNSIGNED",
    reason: `no ${WINDOWS_CERTIFICATE_VAR} — this Windows build is UNSIGNED.`
      + " SmartScreen will warn on it and auto-update is not offered.",
  };
}

/** The Windows block of build output. Same contract as {@link signingBanner}. */
function windowsSigningBanner(winPlan) {
  const rule = "─".repeat(72);
  const head = winPlan.signed
    ? "WINDOWS SIGNING: certificate present"
    : "WINDOWS SIGNING: UNSIGNED — NOT SHIPPABLE";
  const lines = [rule, head, winPlan.reason];
  if (winPlan.artifactTag) {
    lines.push(`The .exe will be tagged "${winPlan.artifactTag}" so it cannot`
      + " be mistaken for a release.");
  }
  // electron-builder prints "signing with signtool.exe path=..." for every exe
  // even when nothing is signed: that is its step name, not a result. A reader
  // grepping the log for "signing" gets the wrong answer, so say where the
  // real answer is.
  lines.push("The build log's \"signing with signtool.exe\" line is a step name,"
    + " not a signature — check the artifact with Get-AuthenticodeSignature.");
  lines.push(rule);
  return lines.join("\n");
}

/** The block of build output a human actually reads. Never silent. */
function signingBanner(plan) {
  const rule = "─".repeat(72);
  const head = plan.mode === SIGNED
    ? "SIGNING: release build (signed + notarized)"
    : `SIGNING: ${plan.mode.toUpperCase()} — NOT SHIPPABLE`;
  const lines = [rule, head, plan.reason];
  if (plan.artifactTag) {
    lines.push(`The artifact will be tagged "${plan.artifactTag}" so it cannot`
      + " be mistaken for a release.");
  }
  if (plan.fatal) {
    lines.push("NH_REQUIRE_SIGNED is set and this build is not signed+notarized"
      + " — refusing to continue.");
  }
  lines.push(rule);
  return lines.join("\n");
}

/**
 * Which platforms (`"darwin"` | `"win32"` | `"linux"`) this electron-builder
 * invocation actually targets, read from the CLI argv it was started with —
 * transcribed from electron-builder's own option table
 * (node_modules/electron-builder/out/builder.js), not guessed:
 *   --mac  aliases: -m, -o, --macos   (also accepts --mac=<target>)
 *   --win  aliases: -w, --windows
 *   --linux aliases: -l
 * All three are yargs `type: "array"` options, so a bare value following one
 * ("--mac dmg zip", "--win nsis") is a TARGET, not a flag, and is ignored
 * here because it does not start with "-". A single-dash letter CLUSTER
 * ("-mwl") is yargs' own shorthand for passing several of these boolean-ish
 * short flags at once, so it is treated the same as listing them separately.
 *
 * No platform flag at all means electron-builder falls back to
 * `Platform.current()` (builder.js) — the host this process runs on. An
 * unrecognised host (anything but darwin/win32/linux) yields an EMPTY set,
 * which downstream makes `autoUpdateStamp` stamp false: fail closed rather
 * than guess.
 */
function buildPlatforms(argv, hostPlatform) {
  const LETTER_TO_PLATFORM = { m: "darwin", o: "darwin", w: "win32", l: "linux" };
  const LONG_TO_PLATFORM = {
    mac: "darwin", macos: "darwin",
    win: "win32", windows: "win32",
    linux: "linux",
  };
  const platforms = new Set();
  for (const raw of argv) {
    if (typeof raw !== "string" || !raw.startsWith("-")) continue;
    if (raw.startsWith("--")) {
      const body = raw.slice(2);
      const eq = body.indexOf("=");
      const name = eq === -1 ? body : body.slice(0, eq);
      const platform = LONG_TO_PLATFORM[name];
      if (platform) platforms.add(platform);
      continue;
    }
    // Single dash: either one short alias ("-m") or a letter cluster
    // ("-mwl"). Only treat it as a platform flag when EVERY letter in it is
    // one of the platform aliases — "-c", "-p", "-x64" etc. must never be
    // misread as a platform selector.
    const letters = raw.slice(1);
    if (letters.length > 0 && [...letters].every((c) => c in LETTER_TO_PLATFORM)) {
      for (const c of letters) platforms.add(LETTER_TO_PLATFORM[c]);
    }
  }
  if (platforms.size === 0
      && (hostPlatform === "darwin" || hostPlatform === "win32" || hostPlatform === "linux")) {
    platforms.add(hostPlatform);
  }
  return platforms;
}

/**
 * The ONE `nhCanAutoUpdate` value stamped into the packaged app, derived from
 * the macOS signing plan AND the platform set this invocation emits.
 *
 *   canAutoUpdate — true only when the plan says signed+notarized AND every
 *                   targeted platform is macOS. A non-mac target's update
 *                   path is unverified regardless of Apple credentials.
 *   fatal         — a credentialed macOS build mixed with a non-mac target
 *                   in the SAME invocation: no single stamp is correct for
 *                   both artifacts (true would be wrong for the .exe/.deb,
 *                   false would be wrong for the .app), so this refuses
 *                   rather than silently picking one. A mixed invocation
 *                   WITHOUT credentials is not fatal — false is correct
 *                   everywhere in that set.
 *   reason        — one line for the build banner naming the parsed
 *                   platform set and why the value came out as it did.
 */
function autoUpdateStamp({ plan, platforms }) {
  const names = platforms.size > 0
    ? [...platforms].sort().join(", ")
    : "(none — unrecognised host platform)";
  const macOnly = platforms.size > 0 && [...platforms].every((p) => p === "darwin");
  const canAutoUpdate = plan.canAutoUpdate && macOnly;
  const fatal = plan.canAutoUpdate && platforms.has("darwin") && !macOnly;

  let reason;
  if (fatal) {
    reason = `nhCanAutoUpdate: REFUSING — this build is signed+notarized for macOS `
      + `and this invocation ALSO targets non-mac platforms (${names}); no single stamp `
      + "is correct for both. Run `--mac` and the other platform(s) as separate invocations.";
  } else if (canAutoUpdate) {
    reason = `nhCanAutoUpdate=true: signed+notarized, targeting only {${names}}`;
  } else if (plan.canAutoUpdate) {
    reason = `nhCanAutoUpdate=false: this invocation targets {${names}}, not macOS-only — `
      + "the update path there is unverified regardless of the Apple signing credentials present";
  } else {
    reason = `nhCanAutoUpdate=false: targeting {${names}}; ${plan.reason}`;
  }
  return { canAutoUpdate, fatal, reason };
}

/**
 * Fail-closed runtime backstop for any invocation shape {@link buildPlatforms}
 * could not see (the electron-builder Node API, a future flag alias):
 * `beforePack` is called per platform with the REAL `electronPlatformName`
 * before `extraMetadata` is stamped, so this is a genuine gate, not a
 * best-effort warning. Throws rather than let a wrong stamp reach a
 * non-macOS artifact.
 */
function assertStampMatchesPlatform(electronPlatformName, canAutoUpdate) {
  if (electronPlatformName !== "darwin" && canAutoUpdate === true) {
    throw new Error(
      `assertStampMatchesPlatform: refusing to pack ${electronPlatformName} with `
      + "nhCanAutoUpdate=true — that update path is unverified outside macOS.",
    );
  }
}

/**
 * Ordering used only to pick the WEAKEST (most honest) mode across a set of
 * platforms — never to compare across unrelated dimensions. Under-claiming
 * is the fail-safe direction (see windowsSigningPlan's doc comment); a name
 * that under-claims can be corrected, one that over-claims ships.
 */
const MODE_RANK = { [UNSIGNED]: 0, [SIGNED_NOT_NOTARIZED]: 1, [SIGNED]: 2 };

/**
 * What signing mode a SINGLE platform actually got, independent of any other
 * platform this invocation might also target. `nhSigning` (below) is built
 * from this per-platform truth, exactly as `nhCanAutoUpdate` is built from
 * `autoUpdateStamp` rather than the bare macOS `plan.canAutoUpdate` — see
 * this file's header for why the plan alone is not enough.
 *
 *   darwin — the macOS plan's mode (identity + notarization).
 *   win32  — the Windows plan's mode (a Windows certificate only).
 *   linux  — always UNSIGNED: this repo has no Linux signing at all — no
 *            certificate variable, no target option — so the honest answer
 *            is unsigned, not "unknown" (main.mjs's packagedSigning() would
 *            coerce anything falsy to "unsigned" anyway).
 *   anything else — UNSIGNED, fail closed for an unrecognised platform.
 */
function platformSigningMode(platform, { plan, winPlan }) {
  if (platform === "darwin") return plan.mode;
  if (platform === "win32") return winPlan.mode;
  return UNSIGNED;
}

/**
 * The ONE `nhSigning` value stamped into the packaged app for THIS
 * invocation's platform set — never the bare macOS `plan.mode`, which says
 * nothing about what a Windows or Linux artifact in the same invocation
 * actually got.
 *
 *   mode   — the WEAKEST per-platform mode across every platform targeted.
 *            A mac-only invocation stamps the macOS plan's mode unchanged;
 *            a win/linux-only invocation never claims macOS signing or
 *            notarization; a mixed invocation under-claims rather than
 *            over-claims (the same fail-safe direction as
 *            windowsSigningPlan, and there is no fatal exit here — that
 *            already exists in `autoUpdateStamp` for the dangerous
 *            signed+notarized-mac-mixed-with-non-mac shape).
 *   mixed  — true when the targeted platforms disagree on their own mode.
 *   reason — one line naming the platform set, each platform's own mode,
 *            and the chosen value.
 */
function signingStamp({ plan, winPlan, platforms }) {
  if (platforms.size === 0) {
    return {
      mode: UNSIGNED,
      mixed: false,
      reason: "nhSigning=unsigned: no recognised target platform — failing closed",
    };
  }
  const sorted = [...platforms].sort();
  const modes = sorted.map((p) => platformSigningMode(p, { plan, winPlan }));
  const mixed = new Set(modes).size > 1;
  const weakest = modes.reduce((a, b) => (MODE_RANK[b] < MODE_RANK[a] ? b : a));
  const perPlatform = sorted.map((p, i) => `${p}=${modes[i]}`).join(", ");
  const reason = `nhSigning=${weakest}: targeting {${sorted.join(", ")}} (${perPlatform})`
    + (mixed ? " — mixed modes across targets, stamping the weakest" : "");
  return { mode: weakest, mixed, reason };
}

/**
 * Fail-closed runtime backstop for any invocation shape {@link buildPlatforms}
 * could not see (the electron-builder Node API, a future flag alias), the
 * same role {@link assertStampMatchesPlatform} plays for `nhCanAutoUpdate`:
 * throws when the stamped mode claims MORE than the real platform actually
 * got.
 */
function assertSigningStampMatchesPlatform(electronPlatformName, stampedMode, { plan, winPlan }) {
  const actual = platformSigningMode(electronPlatformName, { plan, winPlan });
  if (MODE_RANK[stampedMode] > MODE_RANK[actual]) {
    throw new Error(
      `assertSigningStampMatchesPlatform: refusing to pack ${electronPlatformName} with `
      + `nhSigning=${stampedMode} — that platform's own signing mode is ${actual}.`,
    );
  }
}

module.exports = {
  SIGNED,
  SIGNED_NOT_NOTARIZED,
  UNSIGNED,
  NOTARY_CREDENTIAL_SETS,
  notaryCredentialSet,
  notarizeCredentials,
  signingPlan,
  signingBanner,
  WINDOWS_CERTIFICATE_VAR,
  windowsSigningPlan,
  windowsSigningBanner,
  buildPlatforms,
  autoUpdateStamp,
  assertStampMatchesPlatform,
  platformSigningMode,
  signingStamp,
  assertSigningStampMatchesPlatform,
};
