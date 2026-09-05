import { test } from "node:test";
import assert from "node:assert/strict";
import { workersPanelView, pendingBody } from "./workersPanelView.js";

// One fixture payload shared by every test — the shape GET/PUT
// /api/config/workers actually returns (see api/app.py::_workers_payload).
function payload(overrides = {}) {
  return {
    max_workers: 4,
    enabled: true,
    effective_max_workers: 4,
    warning: null,
    max_allowed: 64,
    restart_required: false,
    cpu_count: 12,
    hardware_ceiling: 4,
    ...overrides,
  };
}

test("a null payload is unavailable", () => {
  assert.deepEqual(workersPanelView(null), { unavailable: true });
  assert.deepEqual(workersPanelView(undefined), { unavailable: true });
});

test("hardware line names the core count and the derived ceiling", () => {
  const view = workersPanelView(payload({ cpu_count: 12, hardware_ceiling: 4 }));
  assert.equal(view.hardware.cpuCount, 12);
  assert.equal(view.hardware.ceiling, 4);
  assert.match(view.hardware.sentence, /12/);
  assert.match(view.hardware.sentence, /4/);
});

test("an older payload without the hardware keys renders no hardware line", () => {
  const p = payload();
  delete p.cpu_count;
  delete p.hardware_ceiling;
  const view = workersPanelView(p);
  assert.equal(view.hardware, null);
  assert.equal(view.unavailable, false);
});

test("mwValid rejects 0, non-integers and values above max_allowed", () => {
  const p = payload({ max_workers: 4, max_allowed: 64 });
  assert.equal(workersPanelView(p, { mwEdit: "0" }).mwValid, false);
  assert.equal(workersPanelView(p, { mwEdit: "abc" }).mwValid, false);
  assert.equal(workersPanelView(p, { mwEdit: "65" }).mwValid, false);
  assert.equal(workersPanelView(p, { mwEdit: "-3" }).mwValid, false);
  assert.equal(workersPanelView(p, { mwEdit: "8" }).mwValid, true);
});

test("pendingBody sends only the fields that actually changed", () => {
  const p = payload({ max_workers: 4, enabled: true });
  // Unedited.
  assert.deepEqual(pendingBody({ payload: p }), {});
  // Edited back to the original value.
  assert.deepEqual(pendingBody({ payload: p, mwEdit: "4", enEdit: true }), {});
  // A genuine change to one field only.
  assert.deepEqual(pendingBody({ payload: p, mwEdit: "8" }), { max_workers: 8 });
  assert.deepEqual(pendingBody({ payload: p, enEdit: false }), { enabled: false });
  // Both fields changed.
  assert.deepEqual(
    pendingBody({ payload: p, mwEdit: "8", enEdit: false }),
    { max_workers: 8, enabled: false },
  );
});

test("canSave is false while saving or invalid", () => {
  const p = payload({ max_workers: 4, max_allowed: 64 });
  // A real, valid change with nothing else going on: can save.
  assert.equal(workersPanelView(p, { mwEdit: "8" }).canSave, true);
  // Same edit, but a save is already in flight.
  assert.equal(workersPanelView(p, { mwEdit: "8", saving: true }).canSave, false);
  // An invalid edit never becomes savable, saving or not.
  assert.equal(workersPanelView(p, { mwEdit: "0" }).canSave, false);
  // No edit at all: nothing changed, so nothing to save.
  assert.equal(workersPanelView(p).canSave, false);
});

test("effectiveNote prefers the parallelism-off message, then the server warning, then the plain effective count", () => {
  const off = workersPanelView(payload({ enabled: false }), { enEdit: false });
  assert.match(off.effectiveNote, /Parallelism is off/);

  const warned = workersPanelView(
    payload({ enabled: true, effective_max_workers: 4, warning: "8 workers is above this machine's ceiling" }),
  );
  assert.match(warned.effectiveNote, /This machine will run 4 at a time/);
  assert.match(warned.effectiveNote, /above this machine's ceiling/);

  const plain = workersPanelView(payload({ enabled: true, effective_max_workers: 4, warning: null }));
  assert.match(plain.effectiveNote, /effective: 4 at a time/);
});

test("restartRequired mirrors the payload flag", () => {
  assert.equal(workersPanelView(payload({ restart_required: true })).restartRequired, true);
  assert.equal(workersPanelView(payload({ restart_required: false })).restartRequired, false);
  const p = payload();
  delete p.restart_required;
  assert.equal(workersPanelView(p).restartRequired, false);
});
