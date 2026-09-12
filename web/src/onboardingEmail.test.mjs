import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { offlineBanner } from "./offlineRetry.js";
import {
  EMAIL_REJECT_MESSAGE,
  isWellFormedEmail,
  emailBlocksContinue,
  submitEmail,
} from "./onboardingEmail.js";

const SRC = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(SRC, "onboardingEmail.js"), "utf8");

// ── AC1: well-formedness gate, accept + reject paths ──────────────────────

test("accepts practically-valid addresses", () => {
  for (const v of ["a@b.co", "dana.lee@example.io", "x+tag@sub.example.co.uk"]) {
    assert.equal(isWellFormedEmail(v), true, v);
    assert.equal(emailBlocksContinue(v), null, v);
  }
});

test("rejects malformed addresses with the pinned message", () => {
  for (const v of ["", "   ", "nope", "@b.co", "a@", "a b@c.co", "a@b@c", "a@b", "a@b."]) {
    assert.equal(isWellFormedEmail(v), false, v);
    assert.equal(emailBlocksContinue(v), EMAIL_REJECT_MESSAGE, v);
  }
});

test("trims surrounding whitespace before validating", () => {
  assert.equal(isWellFormedEmail("  a@b.co  "), true);
});

// ── AC6: failure behaviour must match the wizard's existing contract ──────
// `guard()` in Onboarding.jsx never rethrows: it classifies a rejection into
// `offline` (network-level, via noteFetchFailure/isNetworkError) or `err`
// (HTTP-level), and `advance()` calls `next()` unconditionally regardless.
// submitEmail must NOT add a second, divergent failure path: no catch here,
// so guard() sees the same shapes of rejection it already handles for every
// other step.

test("submitEmail rethrows a network-level failure (TypeError) untouched", async () => {
  const err = new TypeError("Failed to fetch");
  const registerOnboardingEmail = async () => {
    throw err;
  };
  await assert.rejects(
    () => submitEmail("a@b.co", { registerOnboardingEmail }),
    (e) => e === err,
  );
});

test("submitEmail rethrows an HTTP-level failure (plain Error) untouched", async () => {
  const err = new Error("POST /api/onboarding/email → 500");
  const registerOnboardingEmail = async () => {
    throw err;
  };
  await assert.rejects(
    () => submitEmail("a@b.co", { registerOnboardingEmail }),
    (e) => e === err,
  );
});

test("submitEmail calls registerOnboardingEmail with the trimmed address", async () => {
  const calls = [];
  const registerOnboardingEmail = async (addr) => {
    calls.push(addr);
  };
  await submitEmail("  a@b.co  ", { registerOnboardingEmail });
  assert.deepEqual(calls, ["a@b.co"]);
});

// A `!/\bcatch\b/.test(source)` guard used to sit here. It is the same
// source-text class this change deletes twice on the Python side, and it was
// both evadable -- `.then(undefined, () => {})` reintroduces the second failure
// path without using the word -- and false-positive-prone: one comment
// containing "catch" turned it red with zero behaviour change. The rethrow
// tests below cover the real property, and they caught that evasion when the
// text guard did not.

test("a network rejection surfaces the exact pinned offline banner text, byte-identical to the wizard's existing banner", () => {
  // Same contract onboardingOffline.test.mjs pins for every other step: once
  // guard() classifies a TypeError("Failed to fetch") as a network failure,
  // the banner text shown is this exact string.
  const banner = offlineBanner({ offline: true, probing: false });
  assert.equal(banner.text, "The no_human server is not responding");
});

test("isNetworkError-shaped rejections from submitEmail are classifiable exactly like other steps", async () => {
  const { isNetworkError } = await import("./offlineRetry.js");
  const registerOnboardingEmail = async () => {
    throw new TypeError("Failed to fetch");
  };
  try {
    await submitEmail("a@b.co", { registerOnboardingEmail });
    assert.fail("expected submitEmail to reject");
  } catch (e) {
    assert.equal(isNetworkError(e), true);
  }
});


// ── The shared table. `tests/test_onboarding_email.py` reads the same JSON, so
// the two validators cannot drift apart, and each bound is pinned in BOTH
// directions. Measured before this existed: EMAIL_MAX_LEN could be tightened
// from 254 to 25 -- refusing `dana.lee+onboarding@example.com` on a REQUIRED
// step -- and all 1669 web tests stayed green. Seven single-change mutations of
// the three bounds survived. ───────────────────────────────────────────────
const sharedCases = JSON.parse(
  readFileSync(new URL("../../testdata/email_validation_cases.json", import.meta.url), "utf8"),
).cases;

test("the shared table is not vacuous in the accept direction", () => {
  const ok = sharedCases.filter((c) => c.valid);
  assert.ok(ok.length >= 5, `expected >=5 accept rows, got ${ok.length}`);
  const longest = ok.reduce((a, b) => (a.address.length >= b.address.length ? a : b));
  assert.equal(
    longest.address.length,
    254,
    "the table must assert a 254-character address is ACCEPTED — the only row an over-tightened whole-path cap cannot satisfy",
  );
});

for (const c of sharedCases) {
  test(`shared table: ${c.why}`, () => {
    assert.equal(
      isWellFormedEmail(c.address),
      c.valid,
      c.valid
        ? `a VALID address was refused — over-strict validation locks a user out of a required step (${c.why})`
        : `an INVALID address was accepted (${c.why})`,
    );
  });
}
