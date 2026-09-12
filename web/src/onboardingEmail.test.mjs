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

test("source contains no catch/try — one failure path only, owned by guard()", () => {
  assert.ok(!/\bcatch\b/.test(source), "onboardingEmail.js must not add a second failure path");
  assert.ok(!/\btry\b/.test(source));
});

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
