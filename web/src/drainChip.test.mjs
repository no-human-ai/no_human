import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { drainChip, formatDrainEta, formatPausedUntil, pausedPresentation, PausedIndicator } from "./drainChip.js";

test("idle: 0 busy, 0 queued, no drain time", () => {
  const chip = drainChip({ workers_busy: 0, max_workers: 4, queue_depth: 0, est_drain_seconds: null });
  assert.equal(chip.text, "0/4 workers busy · 0 queued");
  assert.ok(!chip.text.includes("drain"), "idle must never claim a drain time");
  assert.equal(chip.tone, "ok");
});

test("partial load (50% capacity): some workers busy, a queue, and an ETA", () => {
  const chip = drainChip({ workers_busy: 2, max_workers: 4, queue_depth: 5, est_drain_seconds: 2400 });
  assert.equal(chip.text, "2/4 workers busy · 5 queued · ~40 min to drain");
});

test("full capacity: every worker busy", () => {
  const chip = drainChip({ workers_busy: 4, max_workers: 4, queue_depth: 8, est_drain_seconds: 5700 });
  assert.equal(chip.text, "4/4 workers busy · 8 queued · ~1.6 h to drain");
  assert.equal(chip.tone, "warn");
});

test("no-estimate: queue exists but est_drain_seconds is null", () => {
  const chip = drainChip({ workers_busy: 2, max_workers: 4, queue_depth: 3, est_drain_seconds: null });
  assert.equal(chip.text, "2/4 workers busy · 3 queued · no estimate");
  assert.ok(!/\d+\s*(min|h)/.test(chip.text), "must never fabricate a number");
});

test("server-unreachable overrides any cached counts", () => {
  const chip = drainChip({
    error: "ECONNREFUSED",
    workers_busy: 3,
    max_workers: 4,
    queue_depth: 5,
    est_drain_seconds: 2400,
  });
  assert.equal(chip.text, "server unreachable");
  assert.equal(chip.tone, "error");
});

test("quota pause overrides the normal busy/queued/ETA readout (2026-08-20 evidence)", () => {
  // Same numbers the live defect reported ("not stuck, 0 busy, 7 queued,
  // ETA 210 min") — with `paused` set, the chip must say why instead of
  // reciting the individually-true-but-misleading figures.
  const chip = drainChip({
    workers_busy: 0,
    max_workers: 4,
    queue_depth: 7,
    est_drain_seconds: 12600,
    paused: true,
    paused_reason: "quota",
    paused_until: "2026-08-20T17:20:00+00:00",
  });
  assert.ok(chip.text.startsWith("Paused — quota resets "));
  assert.ok(!chip.text.includes("workers busy"), "must not also recite the misleading busy/queued readout");
  assert.equal(chip.tone, "warn");
});

test("quota pause with a missing paused_until falls back to unknown time, never crashes", () => {
  const chip = drainChip({
    workers_busy: 0, max_workers: 4, queue_depth: 7,
    paused: true, paused_reason: "quota",
  });
  assert.equal(chip.text, "Paused — quota resets unknown time");
});

// Task 92e48491 (refile): this used to assert "Paused — quota resets unknown
// time" for a `paused: true` payload that carried NO `paused_reason` at all —
// i.e. it enshrined the exact bug (`paused_reason === "infra" ? ... :
// "quota"`, so anything not literally "infra", including an absent reason,
// rendered as a self-resolving quota cooldown). The fixed default is
// honest-unknown; see the pausedPresentation tests above for the full
// rationale and the lease_lost case this fallthrough hid.
test("paused with NO paused_reason at all renders unknown, never quota (this used to be the bug)", () => {
  const chip = drainChip({ workers_busy: 0, max_workers: 4, queue_depth: 7, paused: true });
  assert.equal(chip.text, "Paused — reason unknown");
  assert.ok(!chip.text.includes("quota"));
});

test("infra-breaker pause reports SDK/auth failures, not quota (independent review of PR #553, 2026-08-21)", () => {
  // The infra breaker (3 consecutive zero-token/auth SDK failures) arms the
  // same cooldown clock a quota park does; paused_reason distinguishes them
  // so the chip never blames a profile for an SDK/auth outage.
  const chip = drainChip({
    workers_busy: 0,
    max_workers: 4,
    queue_depth: 7,
    est_drain_seconds: 12600,
    paused: true,
    paused_reason: "infra",
    paused_until: "2026-08-20T17:20:00+00:00",
  });
  assert.ok(chip.text.startsWith("Paused — SDK/auth failures, resumes "));
  assert.ok(!chip.text.includes("quota"), "must not also claim it was quota");
  assert.equal(chip.tone, "warn");
});

test("formatPausedUntil formats an ISO timestamp as local HH:MM, and never fabricates a time", () => {
  const formatted = formatPausedUntil("2026-08-20T17:20:00+00:00");
  assert.match(formatted, /^\d{2}:\d{2}$/);
  assert.equal(formatPausedUntil(null), "unknown time");
  assert.equal(formatPausedUntil("not-a-date"), "unknown time");
});

test("formatDrainEta never fabricates a number when the estimate is missing", () => {
  assert.equal(formatDrainEta(null), "no estimate");
  assert.equal(formatDrainEta(undefined), "no estimate");
});

test("formatDrainEta rounds sub-minute up to 1, sub-hour to whole minutes, hours to 1 decimal", () => {
  assert.equal(formatDrainEta(12), "~1 min to drain");
  assert.equal(formatDrainEta(2400), "~40 min to drain");
  assert.equal(formatDrainEta(5700), "~1.6 h to drain");
});

test("tone is always one of the closed set of tokens (no CSS vars emitted)", () => {
  const cases = [
    { workers_busy: 0, max_workers: 4, queue_depth: 0, est_drain_seconds: null },
    { workers_busy: 2, max_workers: 4, queue_depth: 5, est_drain_seconds: 2400 },
    { workers_busy: 4, max_workers: 4, queue_depth: 8, est_drain_seconds: 5700 },
    { error: "ECONNREFUSED", workers_busy: 1, max_workers: 4, queue_depth: 1, est_drain_seconds: 60 },
    { workers_busy: 0, max_workers: 4, queue_depth: 7, paused: true, paused_until: "2026-08-20T17:20:00+00:00" },
  ];
  for (const c of cases) {
    assert.ok(["ok", "warn", "error"].includes(drainChip(c).tone));
  }
});

// --------------------------------------------------------------------------- #
// pausedPresentation / lease_lost (task 92e48491 refile): closed PR #251     #
// added this value but only wired the CLI, leaving both web surfaces        #
// (drainChip via App.jsx's old `!== "infra"` fallthrough, and App.jsx's own #
// twin-ternary sidebar indicator) rendering a lost lease as a self-         #
// resolving quota cooldown — confidently wrong. These lock the fix: the     #
// DEFAULT is honest first, then lease_lost gets its own explicit, time-free #
// presentation, in the one function both surfaces share.                    #
// --------------------------------------------------------------------------- #

test("pausedPresentation: lease_lost is a restart-only failure, never a cooldown with a resume time", () => {
  const p = pausedPresentation("lease_lost", { paused_until: "2026-08-20T17:20:00+00:00" });
  assert.equal(p.text, "Paused — pool lease lost; restart required");
  assert.ok(!/\d{2}:\d{2}/.test(p.text), "must never print a resume time — nothing resumes this on its own");
  assert.ok(!p.text.toLowerCase().includes("quota"), "must not read as a self-resolving quota cooldown");
  assert.equal(p.tone, "error");
});

test("pausedPresentation: an unrecognised paused_reason renders honestly unknown, never quota by default", () => {
  // This is the exact bug shape: the closed PR's App.jsx/drainChip.js used
  // `paused_reason === "infra" ? ... : "quota"`, so anything that was not
  // literally "infra" — including a brand-new value like lease_lost, or a
  // future reason nobody has written a branch for yet — fell through to a
  // quota-shaped, self-resolving message. The default branch must say
  // "unknown", not "quota", for both a genuinely novel string and a
  // missing/null reason.
  for (const reason of ["some_future_reason", null, undefined]) {
    const p = pausedPresentation(reason, { paused_until: "2026-08-20T17:20:00+00:00" });
    assert.equal(p.text, "Paused — reason unknown", `reason=${reason}`);
    assert.ok(!p.text.toLowerCase().includes("quota"), `reason=${reason} must not default to quota`);
    assert.equal(p.tone, "warn", `reason=${reason}`);
  }
});

test("drainChip: lease_lost pause overrides the normal busy/queued/ETA readout like quota/infra do", () => {
  const chip = drainChip({
    workers_busy: 0,
    max_workers: 4,
    queue_depth: 7,
    est_drain_seconds: 12600,
    paused: true,
    paused_reason: "lease_lost",
  });
  assert.equal(chip.text, "Paused — pool lease lost; restart required");
  assert.ok(!chip.text.includes("workers busy"));
  assert.equal(chip.tone, "error");
});

test("drainChip: an unrecognised paused_reason never renders as quota (regression guard for the closed PR's bug)", () => {
  const chip = drainChip({
    workers_busy: 0,
    max_workers: 4,
    queue_depth: 7,
    paused: true,
    paused_reason: "totally_novel_reason_nobody_wrote_a_branch_for",
  });
  assert.equal(chip.text, "Paused — reason unknown");
});

// --------------------------------------------------------------------------- #
// PausedIndicator: the sidebar's actual rendering (App.jsx renders this      #
// exact component — see App.jsx's sidebar pause block). A prior version of  #
// this test read App.jsx's source text and grepped for the old ternary's    #
// literals; a reviewer (independent review, this refile) flagged that as    #
// both evadable (a behavioral reintroduction of the bug that avoids the     #
// exact guarded strings would pass) and false-positive-prone (an unrelated  #
// dead-code edit could fail it). Rendering the real component and asserting #
// on its output text, per paused_reason, has neither problem: it exercises  #
// the same code App.jsx puts on screen.                                     #
// --------------------------------------------------------------------------- #

function renderPaused(paused_reason, extra = {}) {
  return renderToStaticMarkup(React.createElement(PausedIndicator, { paused_reason, ...extra }));
}

test("PausedIndicator: infra renders the SDK/auth-failure cooldown text (positive control)", () => {
  const html = renderPaused("infra", { paused_until: "2026-08-20T17:20:00+00:00" });
  assert.ok(html.includes("Paused — SDK/auth failures, resumes"), html);
  assert.ok(!html.includes("quota"), html);
  assert.match(html, /role="status"/);
});

test("PausedIndicator: quota renders the quota cooldown text (positive control)", () => {
  const html = renderPaused("quota", { paused_until: "2026-08-20T17:20:00+00:00" });
  assert.ok(html.includes("Paused — quota resets"), html);
  assert.match(html, /role="status"/);
});

test("PausedIndicator: lease_lost renders a restart-only failure, never a self-resolving cooldown", () => {
  const html = renderPaused("lease_lost", { paused_until: "2026-08-20T17:20:00+00:00" });
  assert.ok(html.includes("Paused — pool lease lost; restart required"), html);
  assert.ok(!html.toLowerCase().includes("cooldown"), html);
  assert.ok(!html.toLowerCase().includes("resumes"), html);
  assert.ok(!html.toLowerCase().includes("quota"), html);
});

test("PausedIndicator: a paused_reason this code has never seen renders honestly unknown", () => {
  const html = renderPaused("totally_novel_reason_nobody_wrote_a_branch_for", { paused_until: "2026-08-20T17:20:00+00:00" });
  assert.ok(html.includes("Paused — reason unknown"), html);
  assert.ok(!html.toLowerCase().includes("cooldown"), html);
  assert.ok(!html.toLowerCase().includes("resumes"), html);
  assert.ok(!html.toLowerCase().includes("quota"), html);
});

test("PausedIndicator: a missing/null paused_reason renders unknown too, never quota (this used to be the bug)", () => {
  const html = renderPaused(null, { paused_until: "2026-08-20T17:20:00+00:00" });
  assert.ok(html.includes("Paused — reason unknown"), html);
  assert.ok(!html.toLowerCase().includes("cooldown"), html);
  assert.ok(!html.toLowerCase().includes("resumes"), html);
  assert.ok(!html.toLowerCase().includes("quota"), html);
});
