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
      artifactTag: "",
      reason: `a Windows certificate is set in ${WINDOWS_CERTIFICATE_VAR}`,
    };
  }
  return {
    signed: false,
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
};
