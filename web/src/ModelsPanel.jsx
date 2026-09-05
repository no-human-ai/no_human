import { useEffect, useState, useCallback } from "react";
import { fetchModels, saveModels, fetchCoderBackend, saveCoderBackend } from "./api.js";
import { modelsPanelView, pendingBody, resetBody, applyError, reviewerBackendView } from "./modelsPanelView.js";
import {
  backendPanelView,
  submitBody as backendSubmitBody,
  canSubmit as backendCanSubmit,
  showLocalFields as backendShowLocalFields,
  applyError as applyBackendError,
} from "./backendPanelView.js";

// Settings → Models pane's coder-backend row: the coder role's GLOBAL
// default backend (claude | codex | local | any future SUPPORTED_BACKENDS
// entry — the option list comes entirely from the GET /api/coder-backend
// payload, never a hardcoded list here). Independent fetch/save cycle from
// the five model-id rows below it, against GET/PUT /api/coder-backend, all
// decision-making delegated to backendPanelView.js — this component only
// renders what that module returns.
function CoderBackendRow() {
  const [payload, setPayload] = useState(undefined); // undefined = loading, null = unavailable
  const [pending, setPending] = useState(null);
  // Edited local-field values, keyed by config key (null entries = "unedited,
  // use the server's value"). Kept separate from `pending` so switching the
  // backend dropdown never discards a half-typed URL.
  const [localEdits, setLocalEdits] = useState({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    fetchCoderBackend().then((p) => setPayload(p));
  }, []);
  useEffect(() => { load(); }, [load]);

  if (payload === undefined) return <div className="settings-empty">Loading…</div>;

  const view = backendPanelView(payload);
  if (view.unavailable) {
    return (
      <div className="settings-empty">
        Coder backend selection is unavailable — this server build does not
        expose the coder-backend endpoint yet.
      </div>
    );
  }

  const selected = pending !== null ? pending : view.current;
  const lf = view.localFields;
  const showLocal = backendShowLocalFields(payload, pending);
  // Effective input values: an unedited field falls back to the server's
  // current value, so the inputs prefill and a Save with no field edits sends
  // nothing for them.
  const values = {};
  if (lf) for (const f of lf.fields) {
    values[f.key] = localEdits[f.key] !== undefined ? localEdits[f.key] : f.value;
  }

  function handleChange(value) {
    setPending(value);
    setError(null);
  }

  function handleFieldChange(key, value) {
    setLocalEdits((e) => ({ ...e, [key]: value }));
    setError(null);
  }

  async function commit() {
    const body = backendSubmitBody(payload, pending, values);
    if (!body) return;
    setSaving(true);
    setError(null);
    try {
      const refreshed = await saveCoderBackend(body);
      setPayload(refreshed);
      setPending(null);
      setLocalEdits({});
    } catch (e) {
      const reverted = applyBackendError(e.message);
      setPending(reverted.pending);
      setError(reverted.error);
    } finally {
      setSaving(false);
    }
  }

  const hasChanges = !!backendSubmitBody(payload, pending, values);
  // Belt-and-braces on top of the <option disabled> the <select> renders:
  // refuse a Save the same view-model (canSubmit) says isn't valid — an
  // unavailable non-local backend, or the local backend with a blank field.
  const selectedSubmittable = backendCanSubmit(payload, pending, values);

  return (
    <div className="models-row coder-backend-row">
      <label className="auth-label">
        Coder backend
        <select
          className="new-task-select"
          aria-label="Coder backend"
          value={selected}
          onChange={(e) => handleChange(e.target.value)}
        >
          {view.options.map((o) => {
            // The local backend stays selectable even when currently
            // unavailable — the operator picks it to reveal the two fields that
            // MAKE it available. Every other unavailable backend stays disabled.
            const configurable = !!lf && o.id === lf.backend;
            const disabled = o.disabled && !configurable;
            return (
              <option key={o.id} value={o.id} disabled={disabled} title={o.reason || undefined}>
                {o.label}{disabled ? " (unavailable)" : ""}
              </option>
            );
          })}
        </select>
      </label>
      {view.options
        .filter((o) => o.disabled && !(lf && o.id === lf.backend) && o.short)
        .map((o) => (
          <div key={o.id} className="ntm-hint" title={o.reason}>
            <span aria-hidden="true">ⓘ</span> {o.label}: {o.short}
          </div>
        ))}
      <span className="models-default">
        default: <code>{view.default}</code>
      </span>
      {showLocal && (
        <div className="local-backend-fields">
          <div className="ntm-hint">
            <span aria-hidden="true">ⓘ</span> You run your own
            Anthropic-compatible server (vLLM, llama.cpp, LM Studio, …) on
            localhost or an RFC1918 address; no_human points its coder at it.
            Enter the model id it exposes and its base URL below.{" "}
            <a
              href="https://getnohuman.com/docs#backends"
              target="_blank"
              rel="noreferrer noopener"
            >
              Local models docs ↗
            </a>
          </div>
          {lf.fields.map((f) => (
            <label className="auth-label" key={f.key}>
              {f.label}
              <input
                type="text"
                className="new-task-input"
                aria-label={f.label}
                placeholder={f.placeholder}
                value={values[f.key]}
                onChange={(e) => handleFieldChange(f.key, e.target.value)}
              />
            </label>
          ))}
          {!selectedSubmittable && (
            <div className="ntm-hint">
              Set both the local model id and base URL to enable Save.
            </div>
          )}
        </div>
      )}
      {view.showRestartBanner && (
        <div className="nh-alarm auth-alarm" role="alert">
          Restart required — the coder backend change is saved to{" "}
          <code>config.yaml</code>, but the running server has not picked it
          up (it never reloads its config mid-run). Restart with{" "}
          <code>nh stop && nh start</code> to switch.
        </div>
      )}
      {error && <div className="settings-error" role="alert">{error}</div>}
      <div className="integration-actions">
        <button
          type="button"
          className="btn btn-approve"
          disabled={!hasChanges || saving || !selectedSubmittable}
          onClick={commit}
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}

// Settings → Models (model picker part 3 of 3). One row per role (coder,
// reviewer, planner, supervisor, utility), fed entirely by GET /api/models —
// every id, price class, default, disabled reason and pinned-role note comes
// from the payload; this component renders it and nothing else. All of the
// decision-making (what's disabled, what a Save/Reset PUT body contains, how
// a 422 reverts) lives in modelsPanelView.js so it's testable without a
// renderer, same split as authPanelView.js/AuthPanel.
export default function ModelsPanel() {
  const [payload, setPayload] = useState(undefined); // undefined = loading, null = unavailable
  const [pending, setPending] = useState({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  // Constraint §6d — the reviewer's own backend-override picker, independent
  // of the five model-id dropdowns above: undefined = unedited, null =
  // operator explicitly picked "default", {backend, model} = an explicit
  // choice. See pendingBody's doc comment in modelsPanelView.js for the exact
  // three-state contract.
  const [pendingRoleBackend, setPendingRoleBackend] = useState(undefined);
  // The list of backends this install can construct at all (availability +
  // disabled reason) — the SAME GET /api/coder-backend payload CoderBackendRow
  // already fetches for the coder's own picker, reused here rather than a
  // second "which backends exist" answer invented for the reviewer.
  const [backendOptionsPayload, setBackendOptionsPayload] = useState(undefined);

  const load = useCallback(() => {
    fetchModels().then((p) => setPayload(p));
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    fetchCoderBackend().then((p) => setBackendOptionsPayload(p));
  }, []);

  if (payload === undefined) return <div className="settings-empty">Loading…</div>;

  const view = modelsPanelView(payload);
  if (view.unavailable) {
    return (
      <div className="memory-panel">
        <div className="settings-empty">
          Model settings are unavailable — this server build does not expose
          the models endpoint yet.
        </div>
      </div>
    );
  }

  const backendOptions = backendOptionsPayload
    ? backendPanelView(backendOptionsPayload).options
    : [];

  function selectedFor(row) {
    return pending[row.key] !== undefined ? pending[row.key] : row.saved;
  }

  function handleChange(row, value) {
    setPending((p) => ({ ...p, [row.key]: value }));
    setError(null);
  }

  async function commit(body) {
    setSaving(true);
    setError(null);
    try {
      const refreshed = await saveModels(body);
      setPayload(refreshed);
      setPending({});
      setPendingRoleBackend(undefined);
    } catch (e) {
      const reverted = applyError(pending, e.message);
      setPending(reverted.pending);
      // A refused PUT wrote nothing at all (apply_model_changes validates the
      // whole body before writing any of it) — the role-backend edit reverts
      // right alongside the five model dropdowns, same "truthful revert
      // everything" rule applyError already documents for those.
      setPendingRoleBackend(undefined);
      setError(reverted.error);
    } finally {
      setSaving(false);
    }
  }

  const dirty = pendingBody(payload, pending, pendingRoleBackend);
  const hasChanges = Object.keys(dirty).length > 0;
  // Computed the same way Save's body is: a Reset that would send an empty
  // PUT (disk already sits at every default) is disabled rather than a
  // click that silently does nothing.
  const resetDirty = resetBody(payload);
  const hasResetChanges = Object.keys(resetDirty).length > 0;

  // The reviewer row's saved/pending/selected derivation, all in one place —
  // `null` for every other role (they carry no `backend` block at all).
  // `submittable` here mirrors the server's own availability check (same
  // data CoderBackendRow's Save gate already uses) so Save disables
  // client-side on the identical rule the PUT would refuse with — never a
  // second, divergent guess.
  const reviewerView = reviewerBackendView(payload, pendingRoleBackend, backendOptions);
  const reviewerSubmittable = !reviewerView || reviewerView.submittable;

  return (
    <div className="memory-panel models-panel">
      <div className="memory-header">
        <h3 className="memory-title"><span className="panel-title-text">Models</span></h3>
      </div>

      <CoderBackendRow />

      {view.showRestartBanner && (
        <div className="nh-alarm auth-alarm" role="alert">
          Restart required — a model change is saved to <code>config.yaml</code>,
          but the running server has not picked it up (it never reloads its
          config mid-run). Restart with <code>nh stop && nh start</code> to
          switch.
        </div>
      )}

      {error && <div className="settings-error" role="alert">{error}</div>}

      <div className="models-rows">
        {view.rows.map((row) => {
          const current = selectedFor(row);
          const selectedOption = row.options.find((o) => o.id === current);
          return (
            <div className="models-row" key={row.role}>
              <label className="auth-label">
                {row.label}
                <select
                  className="new-task-select"
                  aria-label={row.label}
                  value={current}
                  onChange={(e) => handleChange(row, e.target.value)}
                >
                  {row.options.map((o) => (
                    <option key={o.id} value={o.id} disabled={o.disabled} title={o.reason || undefined}>
                      {o.id} ({o.priceLabel})
                    </option>
                  ))}
                </select>
              </label>
              {selectedOption && (
                <span className="integration-chip tone-neutral">{selectedOption.priceLabel}</span>
              )}
              <span className="models-default">
                default: <code>{row.default}</code>
              </span>
              {row.note && <div className="ntm-hint">{row.note}</div>}
              {row.pendingRestart && (
                <div className="ntm-hint" data-testid="pending-restart-hint">
                  Saved: <code>{row.saved}</code> — the running server still uses{" "}
                  <code>{row.current}</code> until restart.
                </div>
              )}
              {row.costNote && <div className="ntm-hint">{row.costNote}</div>}
              {row.role === "reviewer" && reviewerView && (
                <div className="reviewer-backend-override">
                  <div className="ntm-hint" data-testid="reviewer-backend-disclosure">
                    {reviewerView.saved.isDefault ? (
                      <>Reviewer backend: default (<code>{reviewerView.defaultModel}</code>)</>
                    ) : (
                      <>
                        Reviewer backend: <code>{reviewerView.saved.backend}</code>{" "}
                        <code>{reviewerView.saved.model}</code> — overrides the default
                        reviewer (<code>{reviewerView.defaultModel}</code>), chosen in Settings.
                      </>
                    )}
                  </div>
                  {reviewerView.unsaved && (
                    <div className="ntm-hint" data-testid="reviewer-backend-pending">
                      {reviewerView.selected.isDefault ? (
                        <>Pending: default — applies on Save.</>
                      ) : (
                        <>
                          Pending: <code>{reviewerView.selected.backend}</code>{" "}
                          <code>{reviewerView.selected.model}</code> — applies on Save.
                        </>
                      )}
                    </div>
                  )}
                  <label className="auth-label">
                    Reviewer backend override
                    <select
                      className="new-task-select"
                      aria-label="Reviewer backend override"
                      value={reviewerView.selected.isDefault ? "" : reviewerView.selected.backend}
                      onChange={(e) => {
                        const val = e.target.value;
                        setError(null);
                        if (val === "") { setPendingRoleBackend(null); return; }
                        setPendingRoleBackend({
                          backend: val,
                          model: reviewerView.selected.isDefault ? "" : reviewerView.selected.model,
                        });
                      }}
                    >
                      <option value="">default</option>
                      {backendOptions.map((o) => (
                        <option key={o.id} value={o.id} disabled={o.disabled} title={o.reason || undefined}>
                          {o.label}{o.disabled ? " (unavailable)" : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  {backendOptions
                    .filter((o) => o.disabled && o.short)
                    .map((o) => (
                      <div key={o.id} className="ntm-hint" title={o.reason}>
                        <span aria-hidden="true">ⓘ</span> {o.label}: {o.short}
                      </div>
                    ))}
                  {!reviewerView.selected.isDefault && (
                    <label className="auth-label">
                      Reviewer model
                      <input
                        type="text"
                        className="new-task-input"
                        aria-label="Reviewer model"
                        value={reviewerView.selected.model}
                        onChange={(e) => {
                          setError(null);
                          setPendingRoleBackend({ backend: reviewerView.selected.backend, model: e.target.value });
                        }}
                      />
                    </label>
                  )}
                  {!reviewerView.selected.isDefault && !reviewerView.submittable && (
                    <div className="ntm-hint">
                      Pick an available backend and set a model id to enable Save.
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="integration-actions">
        <button
          type="button"
          className="btn btn-approve"
          data-testid="models-save"
          disabled={!hasChanges || saving || !reviewerSubmittable}
          onClick={() => commit(dirty)}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className="btn btn-sendback"
          disabled={saving || !hasResetChanges}
          onClick={() => commit(resetDirty)}
        >
          Reset to defaults
        </button>
      </div>
    </div>
  );
}
