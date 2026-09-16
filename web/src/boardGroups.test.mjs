import test from "node:test";
import assert from "node:assert/strict";
import { groupFailedByTitle } from "./boardGroups.js";

test("same-title failed cards collapse to the newest with a count", () => {
  const tasks = [
    { id: "a", title: "Per-PR CI_GATE", created_at: "2026-07-08" },
    { id: "b", title: "Per-PR CI_GATE", created_at: "2026-07-10" },
    { id: "c", title: "Per-PR CI_GATE", created_at: "2026-07-09" },
    { id: "d", title: "Other bug", created_at: "2026-07-10" },
  ];
  const groups = groupFailedByTitle(tasks);
  assert.equal(groups.length, 2);
  const ci_gate = groups.find((g) => g.task.id === "b");
  assert.equal(ci_gate.collapsedCount, 2);
  assert.deepEqual(ci_gate.olderIds, ["c", "a"]);  // newest-first
  const other = groups.find((g) => g.task.id === "d");
  assert.equal(other.collapsedCount, 0);
});

test("untitled tasks never collapse into each other", () => {
  const groups = groupFailedByTitle([
    { id: "x", title: "", created_at: "1" },
    { id: "y", title: "", created_at: "2" },
  ]);
  assert.equal(groups.length, 2);
});

// A cancelled task also ends in `failed` status, so a same-title group can be HEADED by a
// cancel that merely happens to be newer — burying the one real failure inside "+N older",
// where nothing (not the lane's priority ordering, not the operator) can see it.
// The group's headline must be the failure.
test("a group headed by a newer cancel promotes the real failure to the representative", () => {
  const rows = groupFailedByTitle([
    { id: "cancel_new", title: "Per-PR CI_GATE", status: "failed", cancelled: true, created_at: "2026-07-13T11:26:04Z" },
    { id: "real_fail", title: "Per-PR CI_GATE", status: "failed", cancelled: false, created_at: "2026-07-13T11:05:57Z" },
    { id: "cancel_old", title: "Per-PR CI_GATE", status: "failed", cancelled: true, created_at: "2026-07-13T10:00:00Z" },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].task.id, "real_fail", "the real failure must head the group");
  assert.equal(rows[0].collapsedCount, 2);
  // The cancels stay reachable through it, newest first.
  assert.deepEqual(rows[0].olderIds, ["cancel_new", "cancel_old"]);
});

// A partial_success (salvaged) task points at a real, human-reachable commit
// on a branch — the one card in a same-title group that must never end up
// buried behind "+N older" under a newer plain failure or cancel.
test("a group headed by a newer plain failure still promotes the salvaged task", () => {
  const rows = groupFailedByTitle([
    { id: "fail_new", title: "Per-PR CI_GATE", status: "failed", cancelled: false, created_at: "2026-07-13T11:26:04Z" },
    { id: "salvaged", title: "Per-PR CI_GATE", status: "partial_success", created_at: "2026-07-13T11:05:57Z" },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].task.id, "salvaged", "the salvaged commit must head the group, even though it is older");
  assert.deepEqual(rows[0].olderIds, ["fail_new"]);
});

test("with several real failures the NEWEST real failure heads the group", () => {
  const rows = groupFailedByTitle([
    { id: "f_old", title: "T", status: "failed", created_at: "2026-07-10T00:00:00Z" },
    { id: "f_new", title: "T", status: "failed", created_at: "2026-07-12T00:00:00Z" },
    { id: "c_newest", title: "T", status: "failed", cancelled: true, created_at: "2026-07-13T00:00:00Z" },
  ]);
  assert.equal(rows[0].task.id, "f_new");
  assert.equal(rows[0].collapsedCount, 2);
});

test("an all-cancelled group still shows its newest cancel", () => {
  const rows = groupFailedByTitle([
    { id: "c1", title: "T", status: "failed", cancelled: true, created_at: "2026-07-10T00:00:00Z" },
    { id: "c2", title: "T", status: "failed", cancelled: true, created_at: "2026-07-12T00:00:00Z" },
  ]);
  assert.equal(rows[0].task.id, "c2");
  assert.equal(rows[0].collapsedCount, 1);
});

// Mixed-format regression: the DB stores both naive-space 'YYYY-MM-DD HH:MM:SS'
// and iso-offset '...+00:00' created_at values in the same column, and
// ' ' < 'T' lexically — a raw string `>` always ranked a naive-space row as
// older than an iso-offset row from the same date, regardless of actual age.
test("a genuinely newer naive-space row still outranks an older iso-offset one", () => {
  const rows = groupFailedByTitle([
    { id: "iso-old", title: "Mixed", status: "failed", created_at: "2026-01-01T10:00:00+00:00" },
    { id: "naive-new", title: "Mixed", status: "failed", created_at: "2026-01-01 11:00:00" },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].task.id, "naive-new", "the genuinely newer naive-space row must head the group");
  assert.deepEqual(rows[0].olderIds, ["iso-old"]);
});
