import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { basename } from "./pathBasename.js";

// A repo path can be Windows-shaped (backslashes) as well as POSIX-shaped
// (forward slashes) - a bare `.split("/").pop()` renders the whole backslash
// path as the "name" on Windows instead of just the last segment.

test("basename handles both separator styles and edge cases", () => {
  assert.equal(basename("C:\\Users\\me\\svc"), "svc");
  assert.equal(basename("/Users/x/svc/"), "svc");
  assert.equal(basename("C:\\"), "C:");
  assert.equal(basename(""), "");
  assert.equal(basename("/"), "");
  assert.equal(basename(null), "");
  assert.equal(basename(undefined), "");
  assert.equal(basename("svc"), "svc");
});

// Source-scan: pathBasename.js must be used at exactly the five call sites
// named by the task, and the two explicitly-excluded files (tool-call /
// git-diff paths, always "/"-separated) must keep their own inline
// `.split("/").pop()` rather than importing the new helper.

const SRC = dirname(fileURLToPath(import.meta.url));
const read = (f) => readFileSync(join(SRC, f), "utf8");

const IMPORTERS = [
  "Onboarding.jsx",
  "discoveredRepos.js",
  "TaskComposer.jsx",
  "Settings.jsx",
  "learningGroups.js",
];

test("pathBasename is imported by exactly the five repo-path call sites", () => {
  const IMPORT_RE = /from\s*"\.\/pathBasename\.js"/;
  const files = readdirSync(SRC).filter((f) => /\.(jsx?|mjs)$/.test(f) && !f.endsWith(".test.mjs"));
  const importers = files.filter((f) => IMPORT_RE.test(read(f))).sort();
  assert.deepEqual(importers, [...IMPORTERS].sort());
});

test("summaries.js and SlideOver.jsx are untouched (tool-call/git-diff paths are always POSIX)", () => {
  const summaries = read("summaries.js");
  assert.doesNotMatch(summaries, /from\s*"\.\/pathBasename\.js"/);
  assert.match(summaries, /\.split\("\/"\)\.pop\(\)/);

  const slideOver = read("SlideOver.jsx");
  assert.doesNotMatch(slideOver, /from\s*"\.\/pathBasename\.js"/);
  assert.match(slideOver, /\.split\("\/"\)\.pop\(\)/);
});
