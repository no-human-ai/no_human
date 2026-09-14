import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { tickStallNotice } from "./tickStallNotice.js";
import { formatDuration } from "./formatDuration.js";

// The scheduler's tick loop can stop ticking with every error field (watcher_
// error, worker_error, lease_lost) reading None — the backend DETECTS this
// (scheduler.py health_snapshot -> app.py's /api/worker/status) but until
// this file's sibling module existed, nothing rendered it: a stalled board
// was indistinguishable from a quiet one. Compare loaded_code_stale, which
// gets a banner today; this pins the same treatment for tick_stalled.
//
// Every link is read from ITS OWN SOURCE, in the loadedCodeStale.test.mjs
// idiom: a rename breaks the test rather than silently breaking the banner.

const SRC = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(SRC, p), "utf8");

const app = read("App.jsx");
const scheduler = read("../../src/no_human/core/scheduler.py");
const api = read("../../src/no_human/api/app.py");

test("a stalled tick loop produces a notice", () => {
  const notice = tickStallNotice({
    running: true,
    tick_stalled: true,
    seconds_since_last_tick: 200.6,
    tick_stall_threshold_s: 60,
    inflight: 3,
    max_workers: 4,
  });
  assert.ok(notice, "a stalled loop must produce a non-null notice");
});

test("the board actually renders it", () => {
  assert.ok(
    app.includes('import { tickStallNotice } from "./tickStallNotice.js";'),
    "App.jsx must import the view-model",
  );
  assert.ok(
    /tickStallNotice\(workerStatus\)/.test(app),
    "App.jsx must call tickStallNotice(workerStatus)",
  );
  const block = app.match(/\{tickStall &&([\s\S]{0,300}?)\)\}/)?.[1];
  assert.ok(block, "App.jsx must have a {tickStall && ...} render block");
  assert.ok(block.includes("tickStall.className"), "className must come from the notice");
  assert.ok(block.includes("tickStall.role"), "role must come from the notice");
  assert.ok(block.includes("tickStall.title"), "title must come from the notice");
  assert.ok(block.includes("tickStall.text"), "text must come from the notice");
});

test("it sits in the same alarm stack as the sibling", () => {
  const tickIdx = app.indexOf("{tickStall &&");
  const staleIdx = app.indexOf("workerStatus?.loaded_code_stale &&");
  assert.ok(tickIdx !== -1, "the tickStall block must exist");
  assert.ok(staleIdx !== -1, "the loaded_code_stale block must exist");
  assert.ok(
    tickIdx < staleIdx,
    "the stall alarm must render before the advisory staleness banner",
  );
});

test("prominence is alert, not advisory", () => {
  const notice = tickStallNotice({
    running: true,
    tick_stalled: true,
    seconds_since_last_tick: 200.6,
    tick_stall_threshold_s: 60,
  });
  assert.equal(notice.role, "alert");
  assert.ok(notice.className.includes("nh-alarm"));
  assert.ok(!notice.className.includes("nh-stale"));
});

test("the visible text names the stall age", () => {
  const texts = [200.6, 65, 20000].map(
    (secs) =>
      tickStallNotice({
        running: true,
        tick_stalled: true,
        seconds_since_last_tick: secs,
        tick_stall_threshold_s: 60,
      }).text,
  );
  assert.ok(texts[0].includes(formatDuration(200.6)));
  assert.equal(formatDuration(200.6), "3m");
  assert.ok(texts[1].includes(formatDuration(65)));
  assert.equal(formatDuration(65), "1m");
  assert.ok(texts[2].includes(formatDuration(20000)));
  assert.equal(formatDuration(20000), "5h");
  assert.equal(new Set(texts).size, 3, "a constant string must not pass");
});

test("the title carries exact seconds and the threshold", () => {
  const notice = tickStallNotice({
    running: true,
    tick_stalled: true,
    seconds_since_last_tick: 200.6,
    tick_stall_threshold_s: 60,
  });
  assert.ok(notice.title.includes("200.6"));
  assert.ok(notice.title.includes("60"));
});

test("a healthy server gets nothing", () => {
  assert.equal(
    tickStallNotice({
      running: true,
      tick_stalled: false,
      seconds_since_last_tick: 2.1,
      idle_reason: "queue_empty",
    }),
    null,
  );
  assert.equal(
    tickStallNotice({
      running: true,
      tick_stalled: false,
      seconds_since_last_tick: null,
      never_ticked: false,
      seconds_since_start: 3,
    }),
    null,
    "the genuine first-poll window must not alarm",
  );
  assert.equal(tickStallNotice(null), null);
  assert.equal(tickStallNotice({}), null);
  assert.equal(
    tickStallNotice({ running: false, tick_stalled: true, seconds_since_last_tick: 999 }),
    null,
    "a worker-offline server already has its own banner",
  );
  assert.equal(tickStallNotice({ health_error: "boom" }), null);
});

test("a loop that never ticked reads differently and still names an age", () => {
  const stopped = tickStallNotice({
    running: true,
    tick_stalled: true,
    seconds_since_last_tick: 200.6,
    tick_stall_threshold_s: 60,
  });
  const neverTicked = tickStallNotice({
    running: true,
    tick_stalled: true,
    never_ticked: true,
    seconds_since_last_tick: null,
    seconds_since_start: 900,
  });
  assert.ok(neverTicked, "a loop that never ticked must still produce a notice");
  assert.notEqual(neverTicked.text, stopped.text);
  assert.ok(neverTicked.text.includes(formatDuration(900)));
  assert.equal(formatDuration(900), "15m");
  for (const field of [neverTicked.text, neverTicked.title]) {
    assert.ok(!/NaN/.test(field));
    assert.ok(!/\bnull\b/.test(field));
    assert.ok(!/\bundefined\b/.test(field));
  }
});

test("the endpoint the board polls emits the fields this reads", () => {
  for (const field of [
    '"tick_stalled":',
    '"never_ticked":',
    '"seconds_since_last_tick":',
    '"seconds_since_start":',
    '"tick_stall_threshold_s":',
  ]) {
    assert.ok(scheduler.includes(field), `health_snapshot must emit ${field}`);
  }
  assert.ok(
    /out\.update\(snapshot\(\)\)/.test(api),
    "/api/worker/status must merge health_snapshot() wholesale so these fields reach the wire",
  );
});
