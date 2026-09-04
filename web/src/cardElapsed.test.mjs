import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { elapsedChip, formatElapsed, ELAPSED_STATUSES, WARN_MS, ERROR_MS } from "./cardElapsed.js";

// Operator finding: tasks ran 4-5.6h invisibly on the board - a card in any
// ACTIVE state must show elapsed wall time since dispatch, turning amber past
// 2h and red past 4h. Like cardPrLink.test.mjs there is no React renderer in
// this harness, so the wiring assertions below pin JSX/CSS text only.

const here = fileURLToPath(new URL(".", import.meta.url));
const read = (f) => readFileSync(here + f, "utf8");

const NOW = Date.parse("2026-09-04T12:00:00Z");
const agoIso = (ms) => new Date(NOW - ms).toISOString();

test("every in-flight status gets a chip", () => {
  for (const status of ["context", "planning", "implementing", "reviewing", "testing"]) {
    const chip = elapsedChip({ status, created_at: agoIso(30 * 60_000) }, NOW);
    assert.notEqual(chip, null, status);
    assert.equal(chip.text, "30m", status);
  }
  assert.deepEqual(
    [...ELAPSED_STATUSES].sort(),
    ["context", "implementing", "planning", "reviewing", "testing"].sort(),
  );
});

test("no chip for terminal, parked or queued states", () => {
  const statuses = [
    "pending", "done", "failed", "escalated", "awaiting_approval",
    "awaiting_input", "blocked", "paused_quota", "compound_parent",
  ];
  for (const status of statuses) {
    const chip = elapsedChip({ status, created_at: agoIso(30 * 60_000) }, NOW);
    assert.equal(chip, null, status);
  }
  // Human-stop/cancel gates win even on an otherwise-active status.
  assert.equal(
    elapsedChip({ status: "implementing", created_at: agoIso(30 * 60_000), cancelled: true }, NOW),
    null,
  );
  assert.equal(
    elapsedChip({ status: "implementing", created_at: agoIso(30 * 60_000), blocker_human_stopped: true }, NOW),
    null,
  );
});

test("thresholds: 1h59m ok, 2h01m warn, 4h01m error", () => {
  const at = (ms) => elapsedChip({ status: "implementing", created_at: agoIso(ms) }, NOW).tone;
  assert.equal(at(1 * 3600_000 + 59 * 60_000), "ok");
  assert.equal(at(2 * 3600_000 + 1 * 60_000), "warn");
  assert.equal(at(4 * 3600_000 + 1 * 60_000), "error");
  // Boundaries: >= is inclusive.
  assert.equal(at(WARN_MS), "warn");
  assert.equal(at(ERROR_MS), "error");
});

test("formatElapsed renders h+m", () => {
  assert.equal(formatElapsed(0), "0m");
  assert.equal(formatElapsed(59 * 60_000), "59m");
  assert.equal(formatElapsed(1 * 3600_000 + 42 * 60_000), "1h 42m");
  assert.equal(formatElapsed(5 * 3600_000 + 36 * 60_000), "5h 36m");
});

test("missing or unparseable created_at, and clock skew, render no chip", () => {
  const base = { status: "implementing" };
  assert.equal(elapsedChip({ ...base }, NOW), null);
  assert.equal(elapsedChip({ ...base, created_at: "" }, NOW), null);
  assert.equal(elapsedChip({ ...base, created_at: "nonsense" }, NOW), null);
  // Future timestamp - clock skew - must not fabricate a negative duration.
  assert.equal(elapsedChip({ ...base, created_at: agoIso(-60_000) }, NOW), null);
  assert.equal(elapsedChip(null, NOW), null);
});

test("a naive 'YYYY-MM-DD HH:MM:SS' stamp is read as UTC", () => {
  // parseTimestamp.js documents: `new Date(s)` would read this as LOCAL time,
  // which in a UTC+3 box reports a task as 3h staler than it is. Routing
  // through parseTimestamp keeps this module immune to that bug.
  const created = "2026-09-04T11:30:00.000Z".replace("T", " ").replace(".000Z", "");
  const chip = elapsedChip({ status: "implementing", created_at: created }, NOW);
  assert.notEqual(chip, null);
  assert.equal(chip.text, "30m");
  assert.equal(chip.tone, "ok");
});

test("Board.jsx renders the chip in .card-meta and ticks client-side", () => {
  const src = read("Board.jsx");
  assert.match(src, /import \{ elapsedChip \} from "\.\/cardElapsed\.js";/);
  assert.match(src, /elapsedChip\(task, nowMs\)/);
  assert.match(src, /className=\{`card-elapsed tone-\$\{elapsed\.tone\}`\}/);
  // Chip renders inside .card-meta, before .card-age.
  const metaIdx = src.indexOf('className="card-meta"');
  const elapsedIdx = src.indexOf("card-elapsed tone-");
  const ageIdx = src.indexOf('<span className="card-age">');
  assert.ok(metaIdx !== -1 && elapsedIdx !== -1 && ageIdx !== -1);
  assert.ok(metaIdx < elapsedIdx && elapsedIdx < ageIdx);
  // 60s client-side tick, cleaned up.
  assert.match(src, /const ELAPSED_TICK_MS = 60_000;/);
  assert.match(src, /setInterval\(\(\) => setNowMs\(Date\.now\(\)\), ELAPSED_TICK_MS\)/);
  assert.match(src, /return \(\) => clearInterval\(id\);/);
  // No inline styles on the chip.
  assert.doesNotMatch(src, /card-elapsed[\s\S]{0,120}style=\{\{/);
});

test("the elapsed tones use real warning tokens, defined for both themes", () => {
  const css = read("styles.css");
  assert.match(css, /\.card-elapsed\.tone-warn\s*\{[^}]*var\(--warn\)/);
  assert.match(css, /\.card-elapsed\.tone-error\s*\{[^}]*var\(--danger\)/);
  const elapsedBlock = css.match(/\.card-elapsed[\s\S]*?tone-error[^}]*\}/)[0];
  assert.doesNotMatch(elapsedBlock, /--amber/);

  const lightBlock = css.match(/\[data-theme="light"\]\s*\{([^}]*)\}/)[1];
  assert.match(lightBlock, /--warn:/);
  assert.match(lightBlock, /--danger:/);
});
