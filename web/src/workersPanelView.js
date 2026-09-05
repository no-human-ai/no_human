// Pure view-model for the Settings "Workers" pane — GET/PUT /api/config/workers
// (see src/no_human/api/app.py::_workers_payload). No React, no I/O: every
// value the panel renders, and every key a Save click sends, is derived here
// so it is testable without a renderer — mirrors modelsPanelView.js.
//
// Payload shape (read, not invented — api/app.py::_workers_payload):
//   {max_workers, enabled, effective_max_workers, warning, max_allowed,
//    restart_required, cpu_count?, hardware_ceiling?}
// `cpu_count`/`hardware_ceiling` are additive and absent on an older server
// (see `pool_width_ceiling` — core/scheduler.py) — `hardware` below degrades
// to `null` in that case, the same fallback shape `modelsPanelView`'s
// `savedOf` uses for a field an older server never sent.

// workersPanelView(payload, {mwEdit, enEdit, saving}) -> the whole pane's
// derived state. `mwEdit`/`enEdit` are the in-progress edits (string / bool,
// or `null` = unedited, use the payload's own value) — same shape the old
// WorkersRow component tracked in state.
export function workersPanelView(payload, { mwEdit = null, enEdit = null, saving = false } = {}) {
  if (!payload || payload.max_workers === undefined) {
    return { unavailable: true };
  }

  const mwValue = mwEdit !== null ? mwEdit : String(payload.max_workers);
  const enValue = enEdit !== null ? enEdit : !!payload.enabled;
  const mwInt = Number.parseInt(mwValue, 10);
  const mwValid = Number.isInteger(mwInt) && mwInt >= 1 && mwInt <= payload.max_allowed;
  const changed =
    (mwEdit !== null && mwInt !== payload.max_workers) ||
    (enEdit !== null && enValue !== !!payload.enabled);
  const canSave = changed && mwValid && !saving;

  const hasHardware = payload.cpu_count !== undefined && payload.hardware_ceiling !== undefined;
  const hardware = hasHardware
    ? {
        cpuCount: payload.cpu_count,
        ceiling: payload.hardware_ceiling,
        atCeiling: mwInt >= payload.hardware_ceiling,
        sentence:
          `This machine has ${payload.cpu_count} CPU cores; the derived ` +
          `ceiling is ${payload.hardware_ceiling} workers.`,
      }
    : null;

  const effectiveNote = !enValue
    ? "Parallelism is off — one task runs at a time, whatever the number " +
      "above. Turn it on to run up to that many."
    : payload.warning
      ? `This machine will run ${payload.effective_max_workers} at a time. ${payload.warning}`
      : `effective: ${payload.effective_max_workers} at a time`;

  return {
    unavailable: false,
    mwValue,
    enValue,
    mwInt,
    mwValid,
    hardware,
    effectiveNote,
    restartRequired: !!payload.restart_required,
    changed,
    canSave,
  };
}

// pendingBody({payload, mwEdit, enEdit}) -> the PUT body a Save click sends —
// only the keys that actually differ from the payload's own value, `{}` when
// nothing changed (including "edited back to the original value"). Same
// shape WorkersRow.commit built inline before this module existed.
export function pendingBody({ payload, mwEdit = null, enEdit = null }) {
  if (!payload) return {};
  const body = {};
  if (mwEdit !== null) {
    const mwInt = Number.parseInt(mwEdit, 10);
    if (mwInt !== payload.max_workers) body.max_workers = mwInt;
  }
  if (enEdit !== null && enEdit !== !!payload.enabled) body.enabled = enEdit;
  return body;
}
