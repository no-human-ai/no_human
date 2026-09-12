import test from "node:test";
import assert from "node:assert/strict";
import { REPLAY_EXCLUDED_PATHS, maskCapturedNetworkRequest } from "./replayScrub.js";

test("REPLAY_EXCLUDED_PATHS names the address-carrying endpoints", () => {
  assert.ok(REPLAY_EXCLUDED_PATHS.includes("/api/onboarding/email"));
  assert.ok(REPLAY_EXCLUDED_PATHS.includes("/api/onboarding/status"));
});

test("drops the onboarding email request in relative/absolute/query/trailing-slash forms", () => {
  const shapes = [
    { name: "/api/onboarding/email" },
    { url: "/api/onboarding/email" },
    { name: "http://localhost:8420/api/onboarding/email" },
    { name: "https://board.local/api/onboarding/email?x=1" },
    { name: "/api/onboarding/email/" },
    { name: "/api/onboarding/email?foo=bar&baz=qux" },
  ];
  for (const data of shapes) {
    assert.equal(maskCapturedNetworkRequest(data), null, JSON.stringify(data));
  }
});

test("drops the onboarding status request/response in the same forms", () => {
  const shapes = [
    { name: "/api/onboarding/status" },
    { name: "http://localhost:8420/api/onboarding/status" },
    { name: "/api/onboarding/status/" },
    { name: "/api/onboarding/status?ts=123" },
  ];
  for (const data of shapes) {
    assert.equal(maskCapturedNetworkRequest(data), null, JSON.stringify(data));
  }
});

test("passes unrelated endpoints through by identity", () => {
  for (const raw of ["/api/tasks", "/api/config", "/api/onboarding/complete", "/api/onboarding/repos/onboard"]) {
    const data = { name: raw, body: "irrelevant" };
    assert.equal(maskCapturedNetworkRequest(data), data);
  }
});

test("never throws on undefined/null/unparseable input, and fails CLOSED on anything matching-but-odd", () => {
  assert.doesNotThrow(() => maskCapturedNetworkRequest(undefined));
  assert.doesNotThrow(() => maskCapturedNetworkRequest(null));
  assert.doesNotThrow(() => maskCapturedNetworkRequest({}));
  assert.equal(maskCapturedNetworkRequest(undefined), undefined);
  assert.equal(maskCapturedNetworkRequest(null), null);
  // Malformed but still matching by substring: not a parseable URL at all,
  // yet contains the excluded path — must fail closed (excluded), not throw
  // and not pass through.
  const weird = { name: "::not a url:: /api/onboarding/email" };
  assert.equal(maskCapturedNetworkRequest(weird), null);
  // A hostile getter that throws must still fail closed.
  const hostile = {
    get name() {
      throw new Error("boom");
    },
  };
  assert.equal(maskCapturedNetworkRequest(hostile), null);
});
