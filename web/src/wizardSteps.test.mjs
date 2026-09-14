// Unit coverage for e2e/wizardSteps.mjs — the parser the onboarding-consent-
// step.mjs walk uses to derive its expected step count/labels from
// Onboarding.jsx's own BASE_STEPS, instead of a hand-typed literal that can
// (and did) go stale. Lives in src/ because `npm test` is
// `node --test src/*.test.mjs`, and nothing under web/e2e runs in CI — this
// file is the only CI-covered guard on the parser's fail-closed behaviour.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parseBaseSteps, wizardSteps, STEP_TITLES } from "../e2e/wizardSteps.mjs";

const here = fileURLToPath(new URL(".", import.meta.url));
const onboardingSrc = readFileSync(here + "Onboarding.jsx", "utf8");
const consentWalkSrc = readFileSync(here + "../e2e/onboarding-consent-step.mjs", "utf8");

test("parses the live BASE_STEPS from Onboarding.jsx", () => {
  const steps = parseBaseSteps(onboardingSrc);
  assert.deepEqual(
    steps.map((s) => s.key),
    ["welcome", "email", "repos", "projects", "integrations", "discord", "summary"]
  );
  assert.deepEqual(
    steps.map((s) => s.title),
    ["Welcome", "Email", "Repositories", "Projects", "Integrations", "Community", "Launch"]
  );
  // wizardSteps()/STEP_TITLES are the bound-to-the-real-file forms the walk
  // actually imports — pin that they agree with the pure parse.
  assert.deepEqual(wizardSteps(), steps);
  assert.deepEqual(STEP_TITLES, steps.map((s) => s.title));
});

test("throws when BASE_STEPS is absent", () => {
  assert.throws(() => parseBaseSteps("const x = 1;"));
});

test("ignores commented-out entries", () => {
  const src = `const BASE_STEPS = [
    { key: "welcome", title: "Welcome" },
    // { key: "ghost", title: "Ghost" },
    { key: "summary", title: "Launch" },
  ];`;
  const steps = parseBaseSteps(src);
  assert.deepEqual(steps.map((s) => s.key), ["welcome", "summary"]);
});

test("throws on an entry shape it cannot parse (residue check)", () => {
  const src = `const BASE_STEPS = [
    { key: "welcome", title: "Welcome" },
    { key: "odd", title: "Odd", optional: true },
    { key: "summary", title: "Launch" },
  ];`;
  assert.throws(() => parseBaseSteps(src), /residue|could not match/);
});

test("throws on duplicate keys", () => {
  const src = `const BASE_STEPS = [
    { key: "welcome", title: "Welcome" },
    { key: "welcome", title: "Again" },
  ];`;
  assert.throws(() => parseBaseSteps(src), /duplicate key/);
});

test("throws on duplicate titles", () => {
  const src = `const BASE_STEPS = [
    { key: "welcome", title: "Same" },
    { key: "other", title: "Same" },
  ];`;
  assert.throws(() => parseBaseSteps(src), /duplicate title/);
});

test("bracket-matches past a nested literal", () => {
  const src = `const BASE_STEPS = [
    { key: "welcome", title: "Welcome" },
    { key: "summary", title: "Launch" },
  ];
  const OTHER = ["not", "part", "of", "steps"];`;
  const steps = parseBaseSteps(src);
  assert.deepEqual(steps.map((s) => s.key), ["welcome", "summary"]);
});

test("adding a step to a scratch copy changes the derived list", () => {
  const injected = onboardingSrc.replace(
    '{ key: "summary",  title: "Launch" },',
    '{ key: "scratch",  title: "Scratch" },\n  { key: "summary",  title: "Launch" },'
  );
  assert.notEqual(injected, onboardingSrc, "the splice point must exist in the real file");
  const before = parseBaseSteps(onboardingSrc).map((s) => s.title);
  const after = parseBaseSteps(injected).map((s) => s.title);
  assert.notDeepEqual(after, before);
  assert.ok(after.includes("Scratch"));
  assert.equal(after.length, before.length + 1);
});

test("removing a step from a scratch copy changes the derived list", () => {
  const removed = onboardingSrc.replace('{ key: "discord",  title: "Community" },\n', "");
  assert.notEqual(removed, onboardingSrc, "the splice point must exist in the real file");
  const before = parseBaseSteps(onboardingSrc).map((s) => s.title);
  const after = parseBaseSteps(removed).map((s) => s.title);
  assert.notDeepEqual(after, before);
  assert.ok(!after.includes("Community"));
  assert.equal(after.length, before.length - 1);
});

test("renaming a step's title in a scratch copy changes the derived list even though the count stays the same", () => {
  const renamed = onboardingSrc.replace(
    '{ key: "discord",  title: "Community" },',
    '{ key: "discord",  title: "Discord" },'
  );
  assert.notEqual(renamed, onboardingSrc, "the splice point must exist in the real file");
  const before = parseBaseSteps(onboardingSrc).map((s) => s.title);
  const after = parseBaseSteps(renamed).map((s) => s.title);
  assert.equal(after.length, before.length, "a rename must not change the count");
  assert.notDeepEqual(after, before, "but ordered-title equality must still catch it");
});

test("the walk embeds no hardcoded step count", () => {
  assert.ok(
    consentWalkSrc.includes('from "./wizardSteps.mjs"'),
    "onboarding-consent-step.mjs must derive its expectation from wizardSteps.mjs"
  );
  assert.ok(
    !/const\s+BASE_STEPS_COUNT/.test(consentWalkSrc),
    "the old hardcoded BASE_STEPS_COUNT declaration must be gone (mentioning its name in an explanatory comment is fine)"
  );
  // No check(...) name/detail may hardcode a bare digit standing in for the
  // step count — the count must always be rendered via ${EXPECTED.length}.
  const checkCalls = [...consentWalkSrc.matchAll(/check\(`([^`]*)`/g)].map((m) => m[1]);
  const countLikeButLiteral = checkCalls.filter(
    (name) => /\bBASE_STEPS\b/.test(name) && /\(\d+\s*steps?\)/.test(name) && !name.includes("${EXPECTED.length}")
  );
  assert.deepEqual(countLikeButLiteral, []);
});
