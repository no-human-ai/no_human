// Parses `BASE_STEPS` out of `Onboarding.jsx`'s SOURCE TEXT so the e2e walk's
// expected step count/labels can never drift from the wizard's own
// definition again. This is a real defect the hard way: onboarding-consent-
// step.mjs used to hardcode `BASE_STEPS_COUNT = 8` in a comment naming
// welcome/repos/projects/docs/integrations/history/rules/summary — three of
// which (docs, history, rules) left the wizard on 2026-09-04 / 2026-08-30,
// while discord joined it on 2026-09-12 and email on 2026-09-13, and the
// literal was never updated. `Onboarding.jsx` is JSX (plain `node` cannot
// `import` it) and `BASE_STEPS` is a module-private `const`, so the only way
// to derive the expectation without editing `Onboarding.jsx` (out of scope
// for the walk fix) is to parse its source text — the same idiom already
// used by src/onboardingEmailStep.test.mjs, src/onboardingDiscord.test.mjs,
// src/onboardingConsent.test.mjs and src/onboardingDocsKickoff.test.mjs.
//
// Fails CLOSED throughout: any shape this parser does not recognize throws,
// rather than silently returning a short/empty list that would make the
// walk's assertion vacuously pass.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const ONBOARDING_SOURCE = fileURLToPath(new URL("../src/Onboarding.jsx", import.meta.url));

function stripComments(text) {
  // Line comments and block comments only — the array body holds no string
  // literals containing "//" or "/*", so this is safe over this slice.
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
}

export function parseBaseSteps(src) {
  const startMarker = "const BASE_STEPS = [";
  const start = src.indexOf(startMarker);
  if (start === -1) {
    throw new Error(
      "BASE_STEPS was renamed or moved in Onboarding.jsx — wizardSteps.mjs's parser must be updated to match"
    );
  }

  // Bracket-count from the opening `[` (not a lazy regex to the next `];`)
  // so a nested array/object literal inside the array cannot truncate the
  // slice early.
  const bodyStart = start + startMarker.length;
  let depth = 1;
  let i = bodyStart;
  for (; i < src.length && depth > 0; i++) {
    if (src[i] === "[") depth++;
    else if (src[i] === "]") depth--;
  }
  if (depth !== 0) {
    throw new Error("BASE_STEPS array in Onboarding.jsx is not bracket-balanced — could not find its closing ]");
  }
  const rawBody = src.slice(bodyStart, i - 1);
  const body = stripComments(rawBody);

  const entryRe = /\{\s*key:\s*["'`]([^"'`]+)["'`]\s*,\s*title:\s*["'`]([^"'`]+)["'`]\s*,?\s*\}/g;
  const steps = [];
  let consumed = body;
  let m;
  while ((m = entryRe.exec(body)) !== null) {
    const [whole, key, title] = m;
    if (!key.trim() || !title.trim()) {
      throw new Error(`BASE_STEPS entry has an empty key or title: ${whole}`);
    }
    steps.push({ key, title });
    consumed = consumed.replace(whole, "");
  }

  if (steps.length === 0) {
    throw new Error("BASE_STEPS parsed to zero entries — regex is out of sync with Onboarding.jsx's entry shape");
  }
  if (steps.length < 2) {
    throw new Error("BASE_STEPS parsed to fewer than two entries — a wizard cannot have one step; parser is broken");
  }

  const keys = steps.map((s) => s.key);
  const dupKeys = keys.filter((k, idx) => keys.indexOf(k) !== idx);
  if (dupKeys.length > 0) {
    throw new Error(`BASE_STEPS has duplicate key(s): ${[...new Set(dupKeys)].join(", ")}`);
  }
  const titles = steps.map((s) => s.title);
  const dupTitles = titles.filter((t, idx) => titles.indexOf(t) !== idx);
  if (dupTitles.length > 0) {
    throw new Error(`BASE_STEPS has duplicate title(s): ${[...new Set(dupTitles)].join(", ")} — label equality could not tell these steps apart`);
  }

  // Residue check: after removing every matched entry's text, no other
  // `key:` should remain. Without this, an entry with an extra field (e.g.
  // `{ key: "x", title: "X", optional: true }`) silently fails to match and
  // becomes an invisible step, and the walk then asserts a too-short
  // expected list and fails confusingly rather than erroring clearly here.
  if (/key:\s*["'`]/.test(consumed)) {
    throw new Error(
      "BASE_STEPS contains an entry the parser could not match (unexpected shape) — " +
        "residue after removing recognized entries still contains a `key:` field"
    );
  }

  return steps;
}

export function wizardSteps() {
  const src = readFileSync(ONBOARDING_SOURCE, "utf8");
  return parseBaseSteps(src);
}

export const STEP_TITLES = wizardSteps().map((s) => s.title);
