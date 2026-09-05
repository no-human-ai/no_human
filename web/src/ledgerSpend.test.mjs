import test from "node:test";
import assert from "node:assert/strict";
import { deriveSpendDisplay, perShippedCost, derivePerShippedDisplay } from "./ledgerSpend.js";

test("subscription mode: tokens primary, dollars marked estimated", () => {
  const d = deriveSpendDisplay(12_400_000, 34, 34, "subscription");
  assert.equal(d.format, "tokens");
  assert.match(d.primary, /tok/);
  assert.match(d.primary, /12\.40M/);
  const combined = `${d.primary} ${d.secondary}`;
  assert.match(combined, /\$/);
  assert.match(combined, /est\./);
  assert.doesNotMatch(combined, /spent/);
});

test("api_key mode: dollar figure primary, labeled spent, no token mention", () => {
  const d = deriveSpendDisplay(12_400_000, 34, 33.96, "api_key");
  assert.equal(d.format, "dollars");
  assert.equal(d.primary, "$33.96");
  assert.equal(d.secondary, "spent");
  const combined = `${d.primary} ${d.secondary}`;
  assert.match(combined, /spent/);
  assert.doesNotMatch(combined, /tok/);
});

test("absent auth_mode falls back to subscription behavior", () => {
  const d = deriveSpendDisplay(500_000, 1.2, 1.2, undefined);
  assert.equal(d.format, "tokens");
  const combined = `${d.primary} ${d.secondary}`;
  assert.match(combined, /tok/);
  assert.match(combined, /est\./);
  assert.doesNotMatch(combined, /spent/);
});

test("unrecognized auth_mode also falls back to subscription behavior", () => {
  const d = deriveSpendDisplay(500_000, 1.2, 1.2, "enterprise");
  assert.equal(d.format, "tokens");
  assert.match(d.secondary, /est\./);
});

test("perShippedCost divides window cost by shipped count", () => {
  assert.equal(perShippedCost(4, 20), 5);
});

test("perShippedCost is null when shipped count is missing/zero", () => {
  assert.equal(perShippedCost(0, 20), null);
  assert.equal(perShippedCost(null, 20), null);
  assert.equal(perShippedCost(undefined, 20), null);
});

test("perShippedCost is null when cost data is missing", () => {
  assert.equal(perShippedCost(3, null), null);
  assert.equal(perShippedCost(3, undefined), null);
  assert.equal(perShippedCost(3, 0), null);
  assert.equal(perShippedCost(3, NaN), null);
});

// ── derivePerShippedDisplay — the per-PR figure, subscription-mode tokens-lead follow-up ──

test("subscription mode: per-PR figure leads with tokens, dollars marked est.", () => {
  const text = derivePerShippedDisplay(3, 2.43, 3_000_000, "subscription");
  assert.match(text, /1\.00M tok\/PR/);
  assert.match(text, /~\$0\.81 est\./);
});

test("undefined auth_mode falls back to the same tokens-lead per-PR display", () => {
  const text = derivePerShippedDisplay(3, 2.43, 3_000_000, undefined);
  assert.match(text, /1\.00M tok\/PR/);
  assert.match(text, /~\$0\.81 est\./);
});

test("api_key mode: per-PR figure stays a plain dollar figure, no regression", () => {
  const text = derivePerShippedDisplay(3, 2.43, 3_000_000, "api_key");
  assert.equal(text, "(~$0.81/PR)");
});

test("derivePerShippedDisplay is null exactly when perShippedCost is null", () => {
  assert.equal(derivePerShippedDisplay(0, 20, 1_000_000, "subscription"), null);
  assert.equal(derivePerShippedDisplay(3, null, 1_000_000, "subscription"), null);
  assert.equal(derivePerShippedDisplay(3, 0, 1_000_000, "subscription"), null);
});

test("subscription mode with no usable token count still marks the dollar figure est.", () => {
  const text = derivePerShippedDisplay(3, 2.43, null, "subscription");
  assert.equal(text, "(~$0.81/PR est.)");
});
