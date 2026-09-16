import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { WALKS } from "../e2e/manifest.mjs";

// Guards e2e/manifest.mjs — the single source of truth for which e2e walks
// run in CI (lane: "ci") vs. by hand only (lane: "manual"). No readFileSync
// of any .jsx/component source here: this test only ever inspects the
// manifest's own data and the e2e/ directory listing, on purpose — a
// regex-over-source guard is exactly what burned this repo before (see
// .no_human/PLAN.md), so this file never reads application source at all.

const here = dirname(fileURLToPath(import.meta.url));
const E2E_DIR = join(here, "..", "e2e");

// Every real walk file in e2e/, excluding the two files that are not
// themselves walks: run-all.mjs (the runner) and manifest.mjs (this data).
const walkFilesOnDisk = readdirSync(E2E_DIR)
  .filter((f) => (f.endsWith(".mjs")))
  .filter((f) => f !== "run-all.mjs" && f !== "manifest.mjs");

test("every manual-lane walk states a substantive reason (>=40 chars)", () => {
  for (const w of WALKS) {
    if (w.lane !== "manual") continue;
    assert.ok(
      typeof w.reason === "string" && w.reason.trim().length >= 40,
      `manifest entry for ${w.file} is lane:"manual" but has no substantive reason`
    );
  }
});

test("every walk lane is either ci or manual", () => {
  for (const w of WALKS) {
    assert.ok(
      w.lane === "ci" || w.lane === "manual",
      `manifest entry for ${w.file} has an unrecognized lane: ${w.lane}`
    );
  }
});

test("every walk file on disk appears in the manifest exactly once, and vice versa", () => {
  const manifestFiles = WALKS.map((w) => w.file);
  const counts = new Map();
  for (const f of manifestFiles) counts.set(f, (counts.get(f) || 0) + 1);

  for (const f of walkFilesOnDisk) {
    assert.ok(counts.has(f), `${f} exists in e2e/ but is not listed in manifest.mjs`);
    assert.equal(counts.get(f), 1, `${f} is listed in manifest.mjs ${counts.get(f)} times, expected exactly 1`);
  }
  const onDisk = new Set(walkFilesOnDisk);
  for (const f of manifestFiles) {
    assert.ok(onDisk.has(f), `manifest.mjs lists ${f}, but no such file exists in e2e/`);
  }
});

test("every manifest file path actually exists on disk", () => {
  for (const w of WALKS) {
    assert.ok(existsSync(join(E2E_DIR, w.file)), `manifest.mjs lists ${w.file}, which does not exist at ${E2E_DIR}`);
  }
});
