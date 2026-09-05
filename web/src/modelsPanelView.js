import { titleCase } from "./titleCase.js";

// Pure view-model for the Settings "Models" pane (model picker part 3 of 3).
// Given the GET /api/models payload (see core/model_settings.py::models_payload),
// derive everything the pane renders and everything a Save/Reset click sends
// back. No React, no I/O, no model id / price / default / rule string of its
// own anywhere in this file — every value a row shows comes from the payload
// the server just sent; this module only reshapes it.
//
// Payload shape (read, not invented — model_settings.py::models_payload):
//   {"roles": [{"role","key","current","saved","default","note",
//               "options":[{"id","price_class":{"label","input_rate",
//                           "output_rate"},"is_default","note",
//                           "requires_backend","disabled_reason"}],
//               "cost_note",
//               "backend"?:{"backend","model","is_default"}}],
//    "restart_required": bool}
//
// `current` is what the RUNNING process is using (only changes on restart);
// `saved` is what a fresh read of config.yaml holds right now — the value a
// PUT actually diffs against server-side. `saved` may be absent (an older
// server): `savedOf` below falls back to `current` in that case, degrading
// to exactly today's behaviour rather than throwing.
//
// Constraint §6d: a role in `role_backend_settings.ROLE_BACKEND_ROLES` (today
// only "reviewer") additionally carries the optional "backend" block above —
// the server's own effective-backend answer, copied verbatim onto the row as
// `row.backend`; every other role's row has `backend: null`, exactly as it
// had no such field before this constraint landed.

// savedOf(r) -> the on-disk value for a raw payload role dict, falling back
// to `current` when the server omits `saved` (an older build) — the ONE
// place this fallback is spelled, reused by the row mapping below and by
// both PUT-body builders so they always agree on what "saved" means.
function savedOf(r) {
  return r.saved !== undefined ? r.saved : r.current;
}

// modelsPanelView(payload) -> {unavailable, showRestartBanner, rows}
//
// `unavailable` is true for a missing/empty payload (fetchModels() returned
// null, or an older server answered with a shape this pane doesn't
// recognise) — the caller renders the "this server build does not expose…"
// note instead of five empty rows.
export function modelsPanelView(payload) {
  const roles = payload?.roles;
  if (!Array.isArray(roles) || roles.length === 0) {
    return { unavailable: true, showRestartBanner: false, rows: [] };
  }
  const rows = roles.map((r) => ({
    role: r.role,
    key: r.key,
    label: titleCase(String(r.role || "")),
    current: r.current,
    saved: savedOf(r),
    // True only when the server told us the on-disk value AND it differs
    // from the running one — an older server (no `saved`) never sets this,
    // matching "no restart-pending info available" rather than guessing.
    pendingRestart: r.saved !== undefined && r.saved !== r.current,
    default: r.default,
    note: r.note || "",
    costNote: r.cost_note || "",
    // Only the reviewer row (ROLE_BACKEND_ROLES) carries this from the
    // server; every other role gets `null` — copied verbatim, never
    // re-derived, so "default" vs "chosen" is always the server's own
    // answer.
    backend: r.backend
      ? { backend: r.backend.backend, model: r.backend.model, isDefault: !!r.backend.is_default }
      : null,
    options: (r.options || []).map((o) => ({
      id: o.id,
      priceLabel: o.price_class?.label ?? "",
      disabled: !!o.requires_backend,
      reason: o.disabled_reason || "",
      isDefault: !!o.is_default,
    })),
  }));
  return {
    unavailable: false,
    showRestartBanner: !!payload.restart_required,
    rows,
  };
}

// The PUT body for a Save click: only the keys whose pending selection
// differs from the row's SAVED (on-disk) value — not `current` (the running
// process), which the server itself never diffs a PUT against
// (apply_model_changes reads on-disk). Diffing against `current` instead
// would make re-picking the value you just saved a silent no-op (the PUT
// body would be empty even though the running process still needs a
// restart to pick it up) and would make picking the still-running-but-
// already-superseded value look like a real change. `pending` is
// `{config_key: model_id}` for every row the user has touched (Reset never
// needs this — see resetBody below), keyed by `row.key` exactly as the
// payload spells it.
//
// `pendingRoleBackend` (constraint §6d, optional — omit it and this behaves
// exactly as before the amendment) is the reviewer backend picker's own
// pending edit, independent of the five model-id dropdowns above:
//   - `undefined` (the default when the argument is omitted): unedited —
//     never adds a "role_backends" key, whatever the server's current
//     reviewer backend is.
//   - `null`: the operator explicitly picked "default" — sends
//     `{role_backends: {reviewer: null}}` (clearing the entry) UNLESS the
//     reviewer is already on the default, in which case nothing is sent.
//   - `{backend, model}`: sends `{role_backends: {reviewer: {backend,
//     model}}}` UNLESS it is identical to the server's current explicit
//     choice already.
export function pendingBody(payload, pending, pendingRoleBackend) {
  const roles = payload?.roles;
  if (!Array.isArray(roles)) return {};
  const out = {};
  if (pending) {
    for (const r of roles) {
      const next = pending[r.key];
      if (next === undefined) continue;
      if (next === savedOf(r)) continue;
      out[r.key] = next;
    }
  }
  if (pendingRoleBackend !== undefined) {
    const reviewer = roles.find((r) => r.role === "reviewer");
    const current = reviewer && reviewer.backend ? reviewer.backend : null;
    const currentlyDefault = !current || current.is_default;
    if (pendingRoleBackend === null) {
      if (!currentlyDefault) out.role_backends = { reviewer: null };
    } else {
      const same =
        !currentlyDefault &&
        current.backend === pendingRoleBackend.backend &&
        current.model === pendingRoleBackend.model;
      if (!same) out.role_backends = { reviewer: pendingRoleBackend };
    }
  }
  return out;
}

// The PUT body for a Reset-to-defaults click: every role's `default` value,
// filtered to the ones that actually differ from the SAVED (on-disk) value —
// not `current` — an idempotent PUT (Reset when disk is already at every
// default) sends an empty body, which the server treats as a no-op write (no
// event, nothing on disk). Diffing against `current` instead would make
// Reset silently inert right after a save: the running process still
// reports its old (already-default) value, so `default !== current` is
// false even though disk holds a real non-default value that needs
// resetting.
//
// Constraint §6d: also clears the reviewer's explicit backend, if any —
// `{role_backends: {reviewer: null}}` — read straight off the payload's own
// `roles[].backend.is_default`, the same "reset means the server's default"
// rule the five model-id keys already follow above.
export function resetBody(payload) {
  const roles = payload?.roles;
  if (!Array.isArray(roles)) return {};
  const out = {};
  for (const r of roles) {
    if (r.default !== savedOf(r)) out[r.key] = r.default;
  }
  const reviewer = roles.find((r) => r.role === "reviewer");
  if (reviewer && reviewer.backend && reviewer.backend.is_default === false) {
    out.role_backends = { reviewer: null };
  }
  return out;
}

// reviewerBackendView(payload, pendingRoleBackend, backendOptions) -> the
// reviewer backend picker's whole saved/pending/selected derivation, pulled
// out of the JSX so it's testable without a renderer. `backendOptions` is
// the caller's own already-fetched `backendPanelView(...).options` list
// (this module still does no I/O of its own — no second "which backends
// exist" fetch invented here).
//
// Returns `null` when there is no reviewer role, or the reviewer role
// carries no `backend` block at all (an older server, or a role outside
// ROLE_BACKEND_ROLES) — the caller must not render the picker in that case,
// same degrade rule the rest of the pane already follows.
//
// Otherwise:
//   - `saved`: `{backend, model, isDefault}` straight off the reviewer row's
//     own `backend` field — the server's own answer, never re-derived;
//   - `defaultModel`: the reviewer row's `default` id, shown next to the
//     word "default";
//   - `pending`: `pendingRoleBackend`, echoed verbatim;
//   - `selected`: `saved` folded with `pending` — `undefined` keeps `saved`,
//     `null` becomes `{backend: "", model: "", isDefault: true}`, and an
//     explicit `{backend, model}` becomes `{..., isDefault: false}`;
//   - `unsaved`: true iff `pendingBody(payload, {}, pendingRoleBackend)`
//     would carry a `role_backends` key — reuses that function's own
//     three-state contract rather than re-deriving the comparison;
//   - `submittable`: true when `selected.isDefault`; otherwise true only if
//     the trimmed model is non-empty AND the selected backend is not a
//     `disabled` entry in `backendOptions`.
export function reviewerBackendView(payload, pendingRoleBackend, backendOptions = []) {
  const roles = payload?.roles;
  const reviewer = Array.isArray(roles) ? roles.find((r) => r.role === "reviewer") : null;
  if (!reviewer || !reviewer.backend) return null;

  const saved = {
    backend: reviewer.backend.backend,
    model: reviewer.backend.model,
    isDefault: !!reviewer.backend.is_default,
  };
  const selected =
    pendingRoleBackend === undefined
      ? saved
      : pendingRoleBackend === null
        ? { backend: "", model: "", isDefault: true }
        : { backend: pendingRoleBackend.backend, model: pendingRoleBackend.model, isDefault: false };
  const unsaved = "role_backends" in pendingBody(payload, {}, pendingRoleBackend);
  const submittable =
    selected.isDefault
      ? true
      : !!selected.model.trim() &&
        backendOptions.find((o) => o.id === selected.backend)?.disabled !== true;

  return {
    saved,
    defaultModel: reviewer.default,
    pending: pendingRoleBackend,
    selected,
    unsaved,
    submittable,
  };
}

// A failed Save (422, network error, or any other throw): the server wrote
// nothing (apply_model_changes validates every submitted key before writing
// any of them), so the truthful UI reverts every pending edit, not just the
// one field a heuristic might guess is at fault — `detail` is a single
// unattributed string that names no field.
export function applyError(_pending, detail) {
  return { pending: {}, error: detail };
}
