import { useEffect, useState, useCallback } from "react";
import { fetchWorkers, saveWorkers } from "./api.js";
import { workersPanelView, pendingBody } from "./workersPanelView.js";

// Settings → Workers pane: how many tasks run at once (concurrency.max_workers)
// and whether parallelism is on at all (concurrency.enabled), against
// GET/PUT /api/config/workers. Re-homed out of ModelsPanel.jsx's WorkersRow
// (task 05a9cee0-workers-panel, re-home) into its own top-level Settings
// section — same fetch/save cycle, now fed through workersPanelView.js so the
// derivations are testable without a renderer, plus a read-only hardware line
// naming this machine's detected core count and the server-derived ceiling
// (`pool_width_ceiling` — core/scheduler.py) that the effective pool is
// clamped against.
export default function WorkersPanel() {
  const [payload, setPayload] = useState(undefined); // undefined = loading, null = unavailable
  const [mwEdit, setMwEdit] = useState(null);   // string being typed, or null = unedited
  const [enEdit, setEnEdit] = useState(null);   // bool, or null = unedited
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    fetchWorkers().then((p) => setPayload(p));
  }, []);
  useEffect(() => { load(); }, [load]);

  if (payload === undefined) return <div className="settings-empty">Loading…</div>;

  const view = workersPanelView(payload, { mwEdit, enEdit, saving });
  if (view.unavailable) {
    return (
      <div className="settings-empty">
        Worker count is unavailable — this server build does not expose the
        workers endpoint yet.
      </div>
    );
  }

  async function commit() {
    const body = pendingBody({ payload, mwEdit, enEdit });
    if (Object.keys(body).length === 0) return;
    setSaving(true);
    setError(null);
    try {
      const refreshed = await saveWorkers(body);
      setPayload(refreshed);
      setMwEdit(null);
      setEnEdit(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="memory-panel workers-panel">
      <div className="memory-header">
        <h3 className="memory-title"><span className="panel-title-text">Workers</span></h3>
      </div>

      <div className="models-row workers-row">
        <label className="auth-label">
          Workers — how many tasks run at once
          <input
            type="number"
            className="new-task-input"
            aria-label="Number of workers"
            min={1}
            max={payload.max_allowed}
            value={view.mwValue}
            onChange={(e) => { setMwEdit(e.target.value); setError(null); }}
          />
        </label>
        <label className="auth-label workers-enable">
          <input
            type="checkbox"
            aria-label="Run tasks in parallel"
            checked={!!view.enValue}
            onChange={(e) => { setEnEdit(e.target.checked); setError(null); }}
          />{" "}
          Run tasks in parallel
        </label>
        {view.hardware && (
          <div className="ntm-hint">
            <span aria-hidden="true">ⓘ</span> {view.hardware.sentence}
          </div>
        )}
        <span className="models-default">{view.effectiveNote}</span>
        {view.restartRequired && (
          <div className="nh-alarm auth-alarm" role="alert">
            Restart required — the worker count is saved to{" "}
            <code>config.yaml</code>, but the running server sized its pool at
            start and does not resize mid-run. Restart with{" "}
            <code>nh stop && nh start</code> to apply.
          </div>
        )}
        {error && <div className="settings-error" role="alert">{error}</div>}
        <div className="integration-actions">
          <button
            type="button"
            className="btn btn-approve"
            disabled={!view.canSave}
            onClick={commit}
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
