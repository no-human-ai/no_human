import { useEffect, useRef, useState, useCallback } from "react";
import { fetchIntegrations, testIntegration, saveIntegrationConfig } from "./api.js";
import {
  statusChip, healthBadge, KIND_LABEL, NAME_LABEL, CONFIG_HINT, SECRET_ENV_KEY,
} from "./integrationChip.js";
import { testResultView } from "./integrationTestResult.js";
import { IntegrationIcon } from "./integrationIcons.jsx";
import { useEscapeKey } from "./useEscapeKey.js";
import { secretState, fieldSecretLabel } from "./integrationSecret.js";
import FieldHint from "./FieldHint.jsx";
import { hintId as fieldHintId } from "./fieldHelp.js";

// Settings → Integrations. One card per integration: brand mark, kind, live
// status chip, a Configure form generated from the integration's `fields`
// spec (GET /api/integrations), and a Test-connection check. Secrets are
// never shown or prefilled — only whether they're set (`fields[].set`).

// github/gitlab/jenkins/circleci share the single `ci.*` config section (see
// no_human/integrations/__init__.py) — saving one of their forms is how the
// backend picks which CI backend is active, so it auto-pins ci.backend +
// ci.enabled alongside the field(s) just saved. The form must say so plainly.
//
// This set must equal `_CI_BACKEND_BY_NAME` in
// no_human/integrations/__init__.py, and integrations.test.mjs reads that file
// to check it. It listed circleci while the Python map did NOT, so this note
// promised an active CI backend for a form that pinned nothing — the panel
// said the gate was on and every PR went out ungated.
const CI_AUTOPIN = new Set(["github", "gitlab", "jenkins", "circleci"]);

export default function IntegrationsPanel() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(null);
  const [testing, setTesting] = useState(null);
  // Per-integration Test-connection result (name → { tone, icon, text }),
  // computed via testResultView so every click renders exactly one outcome —
  // never nothing (see integrationTestResult.js for the response-shape map,
  // including the ambient-CLI-auth case that used to render silently).
  const [testResults, setTestResults] = useState({});

  // Configure form state — one integration's form open at a time, mirroring
  // `expanded`/`testing` above.
  const [configuring, setConfiguring] = useState(null);
  const [formValues, setFormValues] = useState({});
  const [dirty, setDirty] = useState(new Set());
  const [fieldErrors, setFieldErrors] = useState({});
  const [saving, setSaving] = useState(null);
  const [saveError, setSaveError] = useState(null);
  const [justSaved, setJustSaved] = useState(false);

  const load = useCallback(() => {
    fetchIntegrations()
      .then((r) => setItems(r.integrations || []))
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);
  // Escape closes the expanded card AND, if a Configure form is open inside
  // it, closes that form and wipes its state — a typed-but-unsaved secret
  // must not linger in formValues after the user backs out.
  const closeOnEscape = useCallback(() => {
    setExpanded(null);
    setConfiguring(null);
    setFormValues({});
    setDirty(new Set());
    setFieldErrors({});
    setSaveError(null);
    setJustSaved(false);
  }, []);
  useEscapeKey(closeOnEscape, expanded !== null);

  async function runTest(name) {
    setTesting(name);
    setTestResults((r) => ({ ...r, [name]: null }));
    try {
      const status = await testIntegration(name);
      setItems((prev) => prev.map((it) => (it.name === name ? { ...it, ...status } : it)));
      setTestResults((r) => ({ ...r, [name]: testResultView(status) }));
    } catch (e) {
      setItems((prev) => prev.map((it) =>
        it.name === name ? { ...it, healthy: false, detail: e.message } : it));
      setTestResults((r) => ({ ...r, [name]: testResultView(null, e) }));
    } finally {
      setTesting(null);
    }
  }

  function toggleConfigure(it) {
    if (configuring === it.name) {
      setConfiguring(null);
      return;
    }
    setConfiguring(it.name);
    // Every field starts blank — secrets are never prefilled, and the API
    // never exposes a non-secret's current value either (only `set: bool`),
    // so there is nothing to prefill from for any field.
    const initial = {};
    for (const f of it.fields || []) initial[f.name] = "";
    setFormValues(initial);
    setDirty(new Set());
    setFieldErrors({});
    setSaveError(null);
    setJustSaved(false);
  }

  function handleFieldChange(name, value) {
    setFormValues((v) => ({ ...v, [name]: value }));
    setDirty((d) => new Set(d).add(name));
  }

  // Two rules, checked in order:
  //  1. Mirrors the server's no-newline rule (save_integration_config) so the
  //     user finds out before the round trip, not after a 422. Still relevant
  //     even though single-line inputs strip \n/\r on keystroke — programmatic
  //     value changes and paste can still slip one through before the strip.
  //  2. Required-field: a non-secret field the user typed into (it's in
  //     `dirty`) and then leaves empty on blur is genuinely missing — the API
  //     never reports which non-secret fields are optional vs. required, so
  //     "required whenever it's been dirtied and is now empty" is the closest
  //     derivable rule, and it only ever fires after interaction, never on
  //     first open. Secrets are exempt: blank there always means keep-current.
  function handleFieldBlur(name, value, secret) {
    setFieldErrors((prev) => {
      const next = { ...prev };
      if (/[\n\r]/.test(value)) {
        next[name] = "Can't contain line breaks — remove them and try again.";
      } else if (!secret && dirty.has(name) && value === "") {
        next[name] = "This field is required.";
      } else {
        delete next[name];
      }
      return next;
    });
  }

  function dirtyPayload(fields) {
    const out = {};
    for (const f of fields) {
      if (!dirty.has(f.name)) continue;
      const val = formValues[f.name] ?? "";
      if (f.secret && val === "") continue; // empty submit = keep current
      out[f.name] = val;
    }
    return out;
  }

  async function handleSave(it) {
    const fields = it.fields || [];
    if (Object.keys(fieldErrors).length > 0) return;
    const payload = dirtyPayload(fields);
    if (Object.keys(payload).length === 0) return;
    setSaving(it.name);
    setSaveError(null);
    setJustSaved(false);
    try {
      const refreshed = await saveIntegrationConfig(it.name, payload);
      setItems((prev) => prev.map((x) => (x.name === it.name ? { ...x, ...refreshed } : x)));
      setDirty(new Set());
      setFormValues((v) => {
        const cleared = { ...v };
        for (const k of Object.keys(payload)) cleared[k] = "";
        return cleared;
      });
      setJustSaved(true);
      setTimeout(() => setJustSaved(false), 2500);
    } catch (e) {
      setSaveError(e.message);
    } finally {
      setSaving(null);
    }
  }

  if (loading) {
    return (
      <div className="settings-loading">
        <span className="grill-spinner" /><span>Loading integrations…</span>
      </div>
    );
  }

  return (
    <div className="integrations-panel">
      <div className="ntm-hint" style={{ marginBottom: "16px" }}>
        Connect no_human to your issue tracker, CI, and notifications. Select
        Configure on a card to set it up, then test the connection here.
      </div>
      <div className="integrations-list">
        {items.map((it) => {
          const chip = statusChip(it);
          const badge = healthBadge(it);
          const secret = secretState(it);
          const isOpen = expanded === it.name;
          const isConfiguring = configuring === it.name;
          return (
            <div key={it.name} className={`integration-card${isOpen ? " open" : ""}`}>
              <button className="integration-head" aria-expanded={isOpen}
                      onClick={() => setExpanded(isOpen ? null : it.name)}>
                <span className="integration-mark" style={{ color: "var(--text-hi)" }}>
                  <IntegrationIcon name={it.name} size={22} />
                </span>
                <span className="integration-name">{NAME_LABEL[it.name] || it.name}</span>
                <span className="integration-kind">{KIND_LABEL[it.kind] || it.kind}</span>
                <span className={`integration-chip tone-${chip.tone}`}>{chip.label}</span>
                {badge && (
                  <span className={`integration-chip tone-${badge.tone}`}>{badge.label}</span>
                )}
                <span className="integration-chev" aria-hidden="true">›</span>
              </button>
              {isOpen && (
                <div className="integration-body">
                  <div className="integration-detail ph-no-capture">{it.detail || "—"}</div>
                  {badge && (
                    <div className="integration-detail ph-no-capture">{badge.detail}</div>
                  )}
                  {SECRET_ENV_KEY[it.name] && secret && (
                    <div className="integration-field">
                      <span className="ntm-label" style={{ marginBottom: 0 }}>Secret</span>
                      <code>{SECRET_ENV_KEY[it.name]}</code>
                      <span className={`integration-secret${secret.set ? " set" : ""}`}>
                        {secret.label}
                      </span>
                    </div>
                  )}
                  <div className="ntm-hint">Configured in <code>{CONFIG_HINT[it.name]}</code>.</div>
                  <div className="integration-actions">
                    <button className="btn btn-sendback btn-sm" disabled={testing === it.name || saving === it.name}
                            onClick={() => runTest(it.name)}>
                      {testing === it.name
                        ? <><span className="grill-spinner" /> Testing…</>
                        : "Test connection"}
                    </button>
                    {testing !== it.name && testResults[it.name] && (
                      <span className={`integration-result ${testResults[it.name].tone}`}
                            role="status" aria-live="polite">
                        {testResults[it.name].icon} {testResults[it.name].text}
                      </span>
                    )}
                    {(it.fields || []).length > 0 && (
                      <button type="button" className="btn btn-sendback btn-sm"
                              aria-expanded={isConfiguring}
                              onClick={() => toggleConfigure(it)}>
                        {isConfiguring ? "Close" : "Configure"}
                      </button>
                    )}
                  </div>

                  {isConfiguring && (
                    <IntegrationConfigForm
                      integration={it}
                      values={formValues}
                      dirty={dirty}
                      fieldErrors={fieldErrors}
                      saving={saving === it.name}
                      saveError={saveError}
                      justSaved={justSaved}
                      onChange={handleFieldChange}
                      onBlur={handleFieldBlur}
                      onSubmit={() => handleSave(it)}
                    />
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// One integration's settings form, generated from its `fields` spec. Secret
// fields render as password inputs and are never prefilled; non-secret
// fields are plain text and are ALSO never prefilled — the GET response only
// ever exposes a `set: bool` per field, never a current value, for either
// kind — so the "set" badge next to the label is the only signal of what's
// already configured.
function IntegrationConfigForm({
  integration, values, dirty, fieldErrors, saving, saveError, justSaved,
  onChange, onBlur, onSubmit,
}) {
  const fields = integration.fields || [];
  const hasChanges = fields.some((f) => {
    if (!dirty.has(f.name)) return false;
    if (f.secret && (values[f.name] ?? "") === "") return false; // empty submit = keep current
    return true;
  });
  const hasErrors = Object.keys(fieldErrors).length > 0;

  const fieldRefs = useRef({});
  const errorRef = useRef(null);

  // After a failed save, move focus to the first invalid field so an AT user
  // isn't left on the (now-disabled) Save button with no cue what to fix; if
  // nothing is field-specific (e.g. an auth/network failure with no local
  // validation error), focus the error region itself.
  useEffect(() => {
    if (!saveError) return;
    const firstInvalid = fields.find((f) => fieldErrors[f.name]);
    const target = firstInvalid ? fieldRefs.current[firstInvalid.name] : null;
    (target || errorRef.current)?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saveError]);

  return (
    <form className="integration-config-form"
          onSubmit={(e) => { e.preventDefault(); onSubmit(); }}>
      {fields.map((f) => {
        const inputId = `cfg-${integration.name}-${f.name}`;
        const hintId = f.help ? fieldHintId(integration.name, f.name) : null;
        const errorId = fieldErrors[f.name] ? `${inputId}-error` : null;
        const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;
        return (
          <div className="ntm-field" key={f.name}>
            <label className="ntm-label" htmlFor={inputId}>
              {f.label}
              {f.secret && (
                <span className={`integration-field-set${f.set ? "" : " unset"}`}>
                  {fieldSecretLabel(f)}
                </span>
              )}
            </label>
            <input
              id={inputId}
              ref={(el) => { fieldRefs.current[f.name] = el; }}
              className="new-task-input"
              type={f.secret ? "password" : "text"}
              autoComplete={f.secret ? "new-password" : "off"}
              placeholder={f.secret ? fieldSecretLabel(f) : ""}
              value={values[f.name] ?? ""}
              onChange={(e) => onChange(f.name, e.target.value)}
              onBlur={(e) => onBlur(f.name, e.target.value, f.secret)}
              aria-invalid={fieldErrors[f.name] ? "true" : undefined}
              aria-describedby={describedBy}
            />
            {f.help && <FieldHint id={hintId} text={f.help} url={f.help_url} />}
            {fieldErrors[f.name] && (
              <div className="integration-field-error" id={errorId}>{fieldErrors[f.name]}</div>
            )}
          </div>
        );
      })}

      {CI_AUTOPIN.has(integration.name) && (
        <div className="integration-ci-note">
          Saving here makes {NAME_LABEL[integration.name] || integration.name} your active CI
          backend and turns CI on for this workspace.
        </div>
      )}

      {saveError && (
        <div className="new-task-error" aria-live="polite" tabIndex={-1} ref={errorRef}>
          Couldn't save — {saveError}. Check the values and try again.
        </div>
      )}
      {justSaved && !saveError && <div className="integration-save-ok">✓ Saved</div>}

      <div className="integration-actions">
        <button type="submit" className="btn btn-approve btn-sm" disabled={saving || hasErrors || !hasChanges}>
          {saving ? <><span className="grill-spinner" /> Saving…</> : "Save changes"}
        </button>
      </div>
    </form>
  );
}
