import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { kickoffWikiGeneration } from "./onboardingDocsKickoff.js";

// The "Repo docs & wiki" step left the wizard on the operator's 2026-09-04
// ruling — wiki generation is now enqueued automatically, in the background,
// when Launch completes onboarding, asking the user nothing. See
// onboardingDocsKickoff.js and finish() in Onboarding.jsx.

// ── AC1: no orphan step ──────────────────────────────────────────────────

const here = fileURLToPath(new URL(".", import.meta.url));
const src = readFileSync(here + "Onboarding.jsx", "utf8");

test("the Docs step is gone from BASE_STEPS", () => {
  const base = src.match(/const BASE_STEPS = \[([\s\S]*?)\n\];/);
  assert.ok(base, "the base-step list must still exist as its own array");
  const keys = [...base[1].matchAll(/key: "(\w+)"/g)].map((m) => m[1]);
  assert.deepEqual(keys, ["welcome", "repos", "projects", "integrations", "summary"]);
});

test("no orphan docs step body, state or polling survives", () => {
  assert.doesNotMatch(src, /step\.key === "docs"/);
  assert.doesNotMatch(src, /wikiJobs/);
  assert.doesNotMatch(src, /detectedDocs/);
  assert.doesNotMatch(src, /detectDocs\(/);
  assert.doesNotMatch(src, /getDocsJob/);
  assert.doesNotMatch(src, /shouldPoll/);
  assert.doesNotMatch(src, /Wiki generated/);
});

// ── AC2: one docs job enqueued per selected repo, via the API-layer seam ───

test("one docs job is enqueued per selected repo", async () => {
  const calls = [];
  const generate = (rp) => { calls.push(rp); return Promise.resolve({ job_id: "j" + calls.length }); };
  const results = await kickoffWikiGeneration({ repos: new Set(["/a", "/b"]), generate, log: () => {} });
  assert.deepEqual(calls, ["/a", "/b"]);
  assert.deepEqual(results, [{ repo: "/a", ok: true }, { repo: "/b", ok: true }]);
});

test("no repos selected enqueues nothing and still resolves", async () => {
  const calls = [];
  const generate = (rp) => { calls.push(rp); return Promise.resolve({ job_id: "j" }); };
  const results = await kickoffWikiGeneration({ repos: new Set(), generate, log: () => {} });
  assert.deepEqual(calls, []);
  assert.deepEqual(results, []);
});

test("finish() enqueues through the API function, after completeOnboarding", () => {
  const finishStart = src.indexOf("async function finish()");
  assert.ok(finishStart > 0, "finish() must exist");
  const finishSlice = src.slice(finishStart, src.indexOf("\n  }\n", finishStart));
  assert.match(finishSlice, /kickoffWikiGeneration\(\{ repos: selectedRepos, generate: generateDocs \}\)/);
  const completeAt = finishSlice.indexOf("completeOnboarding(");
  const kickoffAt = finishSlice.indexOf("kickoffWikiGeneration(");
  assert.ok(completeAt > 0 && kickoffAt > completeAt,
    "the kickoff must be called after completeOnboarding resolves");
});

// ── AC3: a docs-job failure never blocks or errors the wizard completion ──

test("a rejected enqueue resolves rather than rejects", async () => {
  const logs = [];
  const generate = (rp) => (rp === "/b" ? Promise.reject(new Error("boom")) : Promise.resolve({ job_id: "j" }));
  const results = await kickoffWikiGeneration({ repos: new Set(["/a", "/b"]), generate, log: (...a) => logs.push(a) });
  assert.deepEqual(results, [{ repo: "/a", ok: true }, { repo: "/b", ok: false }]);
  const failureLogs = logs.filter((l) => String(l[0]).includes("failed"));
  assert.equal(failureLogs.length, 1, "the failure must be reported through the injected log");
});

test("a synchronously throwing generate is absorbed too", async () => {
  const generate = () => { throw new Error("sync boom"); };
  const results = await kickoffWikiGeneration({ repos: new Set(["/a"]), generate, log: () => {} });
  assert.deepEqual(results, [{ repo: "/a", ok: false }]);
});

test("the launch path does not await the kickoff and does not guard it in a try", () => {
  const finishStart = src.indexOf("async function finish()");
  const finishSlice = src.slice(finishStart, src.indexOf("\n  }\n", finishStart));
  assert.match(finishSlice, /\n\s+kickoffWikiGeneration\(/,
    "the kickoff call must not be preceded by await");
  const kickoffAt = finishSlice.indexOf("kickoffWikiGeneration(");
  const onCompleteAt = finishSlice.indexOf("onComplete(");
  assert.ok(onCompleteAt > kickoffAt,
    "onComplete must still follow the kickoff, unsequenced behind it");
});
