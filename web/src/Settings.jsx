import { useEffect, useState, useCallback, useRef } from "react";
import keepFocusInDialog from "./keepFocusInDialog.js";
import PathInput from "./PathInput.jsx";
import {
  addRule, addSkill, confirmLearning, fetchLearnings,
  fetchRules, fetchSkills, rejectLearning, removeRule, removeSkill,
  fetchProjects, createProject, updateProject, deleteProject,
  fetchProfiles, detectRepos, onboardRepo,
  fetchAuthStatus, setAuthToken, setCodexMode, setCodexKey, fetchVersion,
  fetchRetireCandidates, retireLearning, restoreLearning,
  pauseLearning, deleteLearning, fetchConfig,
  fetchQuarantineCounts, fetchTelemetryConsent, saveTelemetryConsent,
} from "./api.js";
import { archiveBadge, archivedCount, visibleMemories } from "./memoryArchive.js";
import { quarantineFooterLabel } from "./quarantineFooter.js";
import { capName, PROFILE_CAP, TOKENVAR_CAP } from "./capName.js";
import { authPanelView } from "./authPanelView.js";
import { groupLearningsByProject } from "./learningGroups.js";
import {
  BULK_CONFIRM_CAP,
  bulkConfirmIds,
  filterLearnings,
  learningEvidence,
  learningOrigin,
  learningOriginTaskId,
  learningScope,
  memoryUsageSummary,
} from "./learningCard.js";
import { retireCandidates } from "./learningRetire.js";
import { useEscapeKey } from "./useEscapeKey.js";
import { pluralize } from "./pluralize.js";
import { updateNotice } from "./updateNotice.js";
import IntegrationsPanel from "./Integrations.jsx";
import ModelsPanel from "./ModelsPanel.jsx";
import WorkersPanel from "./WorkersPanel.jsx";

const SECTIONS = [
  { key: "projects",  label: "Projects" },
  { key: "rules",     label: "Rules" },
  { key: "skills",    label: "Skills" },
  // Operator directive (D3, 2026-08-31; hotfix 2026-09-01): the learnings
  // pane is now "Second brain" everywhere — this is its ONLY surface (the
  // sidebar row and its own page were removed from App.jsx). Section `key`
  // stays "learnings" — it is the SettingsOverlay routing key, an internal
  // identifier the operator never sees, and changing it would break every
  // `?tab=learnings` deep link (FinishSetupCard, the "!" nudge) for no
  // visible benefit.
  { key: "learnings", label: "Second brain" },
  { key: "integrations", label: "Integrations" },
  { key: "models",    label: "Models" },
  { key: "workers",   label: "Workers" },
  { key: "account",   label: "Account" },
  { key: "updates",   label: "Updates" },
];
// Usage insights (telemetry) is ON by default and no longer surfaced in the UI
// (operator decision, 2026-08-26): the consent pane was dropped from the nav so
// nothing here mentions it. UsageInsightsPanel is kept below but unrendered.

// The Updates panel. All of the decision-making lives in updateNotice.js so it
// can be tested without a renderer; this component only renders the result and
// forwards clicks to the desktop shell over the preload bridge.
//
// In a plain browser there is no shell, so there are no actions — the panel
// says so rather than showing a button that cannot work.
function UpdatesPanel() {
  const [update, setUpdate] = useState(null);
  const [busy, setBusy] = useState(false);
  // In a plain browser there is no preload bridge, so `desktop.version` is
  // undefined and the panel said "You are running no_human unknown in a
  // browser". The server IS the installed package, so it can be asked - and
  // it also knows whether that package is actually published on the channel
  // the panel would tell the operator to `pip install` from.
  const [versionInfo, setVersionInfo] = useState(null);
  const desktop = typeof window !== "undefined" ? window.nhDesktop : undefined;
  const inShell = Boolean(desktop?.shell);

  useEffect(() => desktop?.onUpdate?.((payload) => setUpdate(payload)), [desktop]);

  useEffect(() => {
    if (inShell) return undefined;
    let live = true;
    // Best-effort, exactly like the composer's greeting: a failed lookup leaves
    // the version unknown, which is what it always was. Never fabricated.
    fetchVersion().then((v) => { if (live) setVersionInfo(v); }).catch(() => {});
    return () => { live = false; };
  }, [inShell]);

  const view = updateNotice({
    inShell,
    current: desktop?.version ?? versionInfo?.version,
    update,
    channel: versionInfo,
  });

  const run = useCallback(async (fn) => {
    if (!fn) return;
    setBusy(true);
    try {
      const result = await fn();
      if (result) setUpdate(result);
    } catch (err) {
      setUpdate({ mode: "failed", error: String(err?.message ?? err) });
    } finally {
      setBusy(false);
    }
  }, []);

  const label = {
    check: "Check for updates",
    download: "Download now",
    install: "Restart and install",
    later: "Later",
    "download-page": "Open downloads",
  };

  const onAction = (action) => {
    if (action === "check") return run(() => desktop?.checkForUpdates?.());
    if (action === "download") return run(() => desktop?.downloadUpdate?.());
    if (action === "install") return run(() => desktop?.installUpdate?.());
    if (action === "later") return run(() => desktop?.deferUpdate?.(update?.latest));
    if (action === "download-page") {
      window.open("https://github.com/no-human-ai/no_human/releases", "_blank",
        "noopener,noreferrer");
      return undefined;
    }
    return undefined;
  };

  return (
    <div className="memory-panel">
      <div className="memory-header">
        <h3 className="memory-title"><span className="panel-title-text">Updates</span></h3>
      </div>
      <div className={`nh-alarm update-notice update-${view.tone}`}
           role={view.tone === "error" ? "alert" : "status"}>
        <strong>{view.title}</strong>
        <div className="update-detail">{view.detail}</div>
      </div>
      {view.actions.length > 0 && (
        <div className="update-actions">
          {view.actions.map((action) => (
            <button key={action} type="button" disabled={busy}
                    className={action === "later" ? "btn" : "btn btn-approve"}
                    onClick={() => onAction(action)}>
              {label[action] ?? action}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Usage insights: the telemetry CONSENT toggle. On by default (opt-out); the
// server owns the write (and mints the anonymous instance id on first enable).
// The two-line description matches the published privacy policy exactly — do
// not soften or embellish it here.
function UsageInsightsPanel() {
  const [enabled, setEnabled] = useState(null); // null = still loading
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let live = true;
    fetchTelemetryConsent()
      .then((c) => { if (live) setEnabled(Boolean(c?.enabled)); })
      .catch((err) => { if (live) setError(String(err?.message ?? err)); });
    return () => { live = false; };
  }, []);

  async function toggle() {
    if (busy || enabled === null) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const out = await saveTelemetryConsent(!enabled);
      setEnabled(Boolean(out?.telemetry?.enabled));
      setSaved(true);
    } catch (err) {
      setError(String(err?.message ?? err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="memory-panel">
      <div className="memory-header">
        <h3 className="memory-title"><span className="panel-title-text">Usage insights</span></h3>
      </div>
      <p className="font-ui text-sm text-text-muted">
        On by default. When on: anonymous usage events and masked recordings
        of the app&apos;s own interface. Never your code. Turn it off here.
      </p>
      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          role="switch"
          aria-checked={enabled === true}
          aria-label="Share usage insights"
          disabled={busy || enabled === null}
          className={enabled ? "btn btn-approve" : "btn"}
          onClick={toggle}
        >
          {enabled === null ? "Loading…" : enabled ? "On" : "Off"}
        </button>
        {saved && (
          <span className="font-ui text-xs text-text-muted" role="status">
            Saved — takes effect after the board reloads.
          </span>
        )}
      </div>
      {error && (
        <div className="nh-alarm mt-3" role="alert">{error}</div>
      )}
    </div>
  );
}

// The Account panel: which OAuth profile pays, whether the running server has
// drifted from the configured one, and a WRITE-ONLY editor for the token. The
// API never returns a token, so there is nothing to reveal and no masked echo —
// the field is cleared the instant it is submitted and never logged.
function AuthPanel() {
  const [status, setStatus] = useState(undefined); // undefined = loading, null = unavailable
  const [profile, setProfile] = useState("");
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetchAuthStatus().then((s) => {
      setStatus(s);
      if (s?.configured_profile) setProfile((p) => p || s.configured_profile);
    });
  }, []);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!token.trim() || saving) return;
    setSaving(true); setError(null); setSaved(false);
    try {
      const next = await setAuthToken(profile, token);
      setToken("");           // write-only — never keep the token after submit
      setStatus(next);
      setSaved(true);
    } catch (err) {
      setError(err.message);  // the backend's 422 detail, rendered verbatim
    } finally {
      setSaving(false);
    }
  }

  if (status === undefined) return <div className="settings-empty">Loading…</div>;
  if (status === null) {
    return (
      <div className="memory-panel">
        <div className="settings-empty">
          Account status is unavailable — this server build does not expose the
          auth endpoints yet.
        </div>
      </div>
    );
  }

  const profiles = status.profiles || [];
  const view = authPanelView(status);
  return (
    <div className="memory-panel auth-panel">
      <div className="memory-header">
        <h3 className="memory-title"><span className="panel-title-text">Account</span></h3>
      </div>

      {/* Claude auth as a labeled sub-section, parallel to the Codex one below,
          so the two providers read as consistent cards under Account rather than
          one loose block (Claude) and one titled card (Codex). */}
      <div className="auth-provider auth-claude">
      <div className="memory-header">
        <h3 className="memory-title"><span className="panel-title-text">Claude</span></h3>
      </div>

      {view.showMeteredAlarm && (
        <div className="nh-alarm auth-alarm" role="alert">
          An <code>ANTHROPIC_API_KEY</code> is set in no_human&apos;s
          environment while the configured mode is subscription — startup
          scrubs the key so a run bills exactly one path. Unset it where the
          server runs, or switch billing to the key with{" "}
          <code>llm.auth_mode: api_key</code>.
        </div>
      )}
      {view.mode === "subscription" && view.showRestartBanner && (
        <div className="nh-alarm auth-alarm" role="alert">
          Restart required — the running server is still billing
          {" "}&ldquo;{capName(status.active_profile, PROFILE_CAP)}&rdquo;, but
          {" "}&ldquo;{capName(status.configured_profile, PROFILE_CAP)}&rdquo; is now configured.
          Restart no_human to switch.
        </div>
      )}
      {view.mode === "api_key" && view.showRestartBanner && (
        <div className="nh-alarm auth-alarm" role="alert">
          Restart required to switch billing to your API key.
        </div>
      )}

      {view.mode === "api_key" && (
        <dl className="auth-status">
          <div><dt>Billing path</dt><dd>Using your personal Anthropic API key</dd></div>
          <div><dt>Key source</dt><dd><code>~/.no_human/.env</code> (chmod 600)</dd></div>
          <div><dt>API key set</dt><dd>{view.apiKeyPresent ? "yes" : "no"}</dd></div>
        </dl>
      )}

      {view.showOAuthForm && (
        <>
          <dl className="auth-status">
            <div><dt>Configured profile</dt><dd>{capName(status.configured_profile, PROFILE_CAP)}</dd></div>
            <div><dt>Active (billing) profile</dt><dd>{capName(status.active_profile, PROFILE_CAP)}</dd></div>
            <div><dt>Token variable</dt><dd><code>{capName(status.token_var, TOKENVAR_CAP)}</code></dd></div>
            <div><dt>Token set</dt><dd>{status.token_present ? "yes" : "no"}</dd></div>
          </dl>

          <form className="auth-form" onSubmit={handleSubmit} autoComplete="off">
            <label className="auth-label">Profile
              <select className="new-task-select" value={profile} aria-label="Profile"
                      onChange={(e) => setProfile(e.target.value)}>
                {profiles.map((p) => <option key={p.name} value={p.name}>{capName(p.name, PROFILE_CAP)}</option>)}
              </select>
            </label>
            <label className="auth-label">OAuth token
              <input className="new-task-input" type="password" autoComplete="off"
                     spellCheck={false} value={token} aria-label="OAuth token"
                     placeholder="CLAUDE_CODE_OAUTH_TOKEN (subscription or enterprise)"
                     onChange={(e) => { setToken(e.target.value); setSaved(false); setError(null); }} />
            </label>
            <p className="auth-hint">
              A subscription or enterprise OAuth token — not an API key. Stored
              write-only and never shown again.
            </p>
            {error && <div className="settings-error" role="alert">{error}</div>}
            {saved && <div className="auth-saved" role="status">Saved.</div>}
            <button className="btn btn-approve" type="submit" disabled={!token.trim() || saving}>
              {saving ? "Saving…" : "Save token"}
            </button>
          </form>
        </>
      )}
      </div>{/* /auth-claude */}

      <CodexSection codex={status.codex} onStatus={setStatus} />
    </div>
  );
}

// The Codex coder backend, first-class in the Account panel alongside Claude:
// its auth mode, credential presence and a write-only key field (api_key mode)
// or a `codex login` session indicator (subscription mode). Degrades to nothing
// when the payload lacks `codex` (an older server), so no crash there.
function CodexSection({ codex, onStatus }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [saved, setSaved] = useState(false);

  if (!codex) return null;

  async function apply(fn) {
    setBusy(true); setError(null); setSaved(false);
    try {
      const next = await fn();
      onStatus(next);
      setSaved(true);
    } catch (err) {
      setError(err.message);   // the backend's 422 detail, verbatim
    } finally {
      setBusy(false);
    }
  }

  async function switchMode(mode) {
    if (busy || mode === codex.auth_mode) return;
    await apply(() => setCodexMode(mode));
  }

  async function saveKey(e) {
    e.preventDefault();
    if (!key.trim() || busy) return;
    await apply(async () => {
      const next = await setCodexKey(key);
      setKey("");              // write-only — never keep the key after submit
      return next;
    });
  }

  return (
    <div className="auth-provider auth-codex">
      <div className="memory-header">
        <h3 className="memory-title"><span className="panel-title-text">Codex</span></h3>
      </div>

      {codex.restart_required && (
        <div className="nh-alarm auth-alarm" role="alert">
          Restart required — the running server is still using the previous
          Codex auth mode. Restart no_human to switch.
        </div>
      )}

      <dl className="auth-status">
        <div><dt>Auth mode</dt><dd>{codex.auth_mode || "—"}</dd></div>
        <div><dt>Model</dt><dd><code>{codex.model || "—"}</code></dd></div>
      </dl>

      <div className="auth-codex-modes" role="group" aria-label="Codex auth mode">
        <button type="button" className="btn"
                aria-pressed={codex.auth_mode === "api_key"}
                disabled={busy || codex.auth_mode === "api_key"}
                onClick={() => switchMode("api_key")}>
          Your OpenAI API key
        </button>
        <button type="button" className="btn"
                aria-pressed={codex.auth_mode === "subscription"}
                disabled={busy || codex.auth_mode === "subscription"}
                onClick={() => switchMode("subscription")}>
          ChatGPT subscription
        </button>
      </div>

      {codex.auth_mode === "api_key" && (
        <>
          <dl className="auth-status">
            <div><dt>API key set</dt><dd>{codex.api_key_present ? "yes" : "no"}</dd></div>
            <div><dt>Key source</dt><dd><code>~/.no_human/.env</code> (chmod 600)</dd></div>
          </dl>
          <form className="auth-form" onSubmit={saveKey} autoComplete="off">
            <label className="auth-label">OpenAI API key
              <input className="new-task-input" type="password" autoComplete="off"
                     spellCheck={false} value={key} aria-label="OpenAI API key"
                     placeholder="OPENAI_API_KEY (sk-…)"
                     onChange={(e) => { setKey(e.target.value); setSaved(false); setError(null); }} />
            </label>
            <p className="auth-hint">
              Your own OpenAI API key. Stored write-only in the private .env and
              never shown again — never in config.yaml.
            </p>
            {error && <div className="settings-error" role="alert">{error}</div>}
            {saved && <div className="auth-saved" role="status">Saved.</div>}
            <button className="btn btn-approve" type="submit" disabled={!key.trim() || busy}>
              {busy ? "Saving…" : "Save key"}
            </button>
          </form>
        </>
      )}

      {codex.auth_mode === "subscription" && (
        <>
          <dl className="auth-status">
            <div><dt>Signed in via <code>codex login</code></dt>
              <dd>
                {codex.subscription_session_present === true && "✓ signed in"}
                {codex.subscription_session_present === false && "✗ run codex login"}
                {codex.subscription_session_present == null && "— unknown (codex CLI not found)"}
              </dd>
            </div>
          </dl>
          <p className="auth-hint">
            no_human holds no OpenAI credential in this mode — sign in yourself
            with <code>codex login</code>. There is no key to enter here.
          </p>
          {error && <div className="settings-error" role="alert">{error}</div>}
        </>
      )}
    </div>
  );
}

// Settings, modeled on the Claude macOS desktop app: an overlay dialog with a
// left section list and the section's panel content on the right — not a
// routed page. Mirrors the SlideOver focus/Escape pattern (SlideOver.jsx),
// so it behaves like the app's other modal surfaces.
export default function SettingsOverlay({ onClose, initialTab, onOpenTask, onSecondBrainOpened }) {
  // A Finish-setup deep link (App passes initialTab) opens the overlay on the
  // matching pane. An unknown tab (e.g. "docs"/"history", which have no pane of
  // their own yet) falls back to Projects rather than a blank content area.
  const [section, setSection] = useState(
    SECTIONS.some((s) => s.key === initialTab) ? initialTab : "projects");
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const triggerRef = useRef(null);

  // Capture the element that had focus when the overlay opened (the trigger,
  // e.g. the sidenav "Settings" button) and restore focus to it on close —
  // whether that's Escape, the × button, or the backdrop, they all tear down
  // this component the same way, so a single mount/unmount effect covers all
  // three. Guard against the trigger having been unmounted in the meantime.
  useEffect(() => {
    triggerRef.current = document.activeElement;
    return () => {
      const trigger = triggerRef.current;
      if (trigger && trigger !== document.body && document.contains(trigger) && typeof trigger.focus === "function") {
        trigger.focus();
      }
    };
  }, []);

  // Move focus into the overlay when it OPENS — mount-only, for the same reason
  // SlideOver's is: App renders <SettingsOverlay onClose={() => ...} /> with a new
  // arrow every render, so sharing this call with the [onClose] effect below made
  // every background re-render (the 10s worker-status poll) steal focus out of the
  // field the operator was typing into — a token or repo path — and park it on the
  // close button.
  useEffect(() => { closeRef.current?.focus(); }, []);

  // Self-heal ONLY when the focused control was REMOVED — see SlideOver.jsx for
  // why "focus is on <body>" is the wrong question: the operator clicking a
  // heading inside this overlay lands focus on <body> legitimately, and healing
  // from there is the focus theft this whole change exists to remove.
  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return undefined;
    // See SlideOver.jsx: `focusin` fires reliably, the blur on a REMOVED node
    // does not, and a DOM mutation is the only trigger that means "something
    // disappeared" rather than "the component re-rendered".
    //
    // Unlike the drawer, THIS panel contains its nested modals (add rule / add
    // skill / new project) as descendants — measured: 45 nodes under the panel
    // against 167 under its parent. So the scope is the panel itself. An earlier
    // version copied the drawer's comment and its `el.parentNode` verbatim, which
    // watched the whole app shell and matched `[data-nested-modal]`
    // DOCUMENT-WIDE — including the drawer's own overlays and the composer,
    // none of which belong to Settings.
    const scope = el;
    const ours = (n) => Boolean(n && el.contains(n));
    let last = null;
    const onFocusIn = (e) => { if (ours(e.target)) last = e.target; };
    scope.addEventListener("focusin", onFocusIn);
    const obs = new MutationObserver(() => {
      if (!last || document.contains(last)) return;
      // Whatever we remembered is gone. Forget it FIRST, whichever way this
      // goes: leaving it set kept the heal armed against a detached node for the
      // rest of the dialog's life, so an unrelated mutation minutes later stole
      // focus — and retained the detached subtree too.
      last = null;
      const active = document.activeElement;
      if (active && active !== document.body && !el.contains(active)) return;
      closeRef.current?.focus();
    });
    obs.observe(scope, { childList: true, subtree: true });
    return () => { scope.removeEventListener("focusin", onFocusIn); obs.disconnect(); };
  }, []);

  useEffect(() => {
    function onKeyDown(e) {
      if (e.key === "Escape") {
        // A modal ABOVE the overlay (add-rule/add-skill/add-project) owns the
        // key — closing both would discard whatever the operator just typed.
        if (document.querySelector("[data-nested-modal]")) return;
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const el = dialogRef.current;
      if (!el) return;
      // `first`/`last` must be elements that can ACTUALLY take focus. Two ways this trap
      // has leaked: a focusable type missing from the selector (`summary` — the learnings
      // group headers), and a listed element that cannot be focused, so activeElement can
      // never equal it and the wrap branch never fires (a collapsed <details>'s buttons,
      // or a disabled field). Hence both the wider selector and the visibility filter.
      const focusable = Array.from(
        el.querySelectorAll(
          'button, [href], input, select, summary, textarea, [tabindex]:not([tabindex="-1"])',
        )
      ).filter((n) => {
        if (n.disabled || n.getClientRects().length === 0) return false;
        // Content of a COLLAPSED <details> cannot take focus, but this app's CSS still
        // lays it out — it keeps its client rects and computes contentVisibility:visible,
        // so no style-based test sees it. Ask the <details> instead. Without this, the
        // learnings cards of a collapsed group become `last`, activeElement can never
        // equal them, the wrap never fires, and Tab leaves the modal.
        const d = n.closest("details");
        return !(d && !d.open && n.tagName !== "SUMMARY");
      });
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey) {
        if (document.activeElement === first) { e.preventDefault(); last.focus(); }
      } else {
        if (document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const activeLabel = SECTIONS.find((s) => s.key === section)?.label || "Settings";

  return (
    <>
      {/* No backdrop close: a stray click here used to discard the whole
          overlay AND any nested modal under it, with everything typed in them. */}
      <div className="settings-overlay-backdrop" onMouseDown={keepFocusInDialog} />
      <div
        className="settings-overlay"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-overlay-title"
        ref={dialogRef}
        // The backdrop above is a SIBLING of this panel, so it never sees a
        // mousedown inside the panel — and most of the panel is headings,
        // labels and padding. Without this, clicking any of them while typing
        // a token or a repo path strands the caret and drops every keystroke.
        onMouseDown={keepFocusInDialog}
      >
        <div className="settings-overlay-left">
          <div className="settings-overlay-group-header">Settings</div>
          <nav className="settings-overlay-nav">
            {SECTIONS.map((s) => (
              <button
                key={s.key}
                className={`settings-overlay-navitem${section === s.key ? " active" : ""}`}
                aria-current={section === s.key ? "true" : undefined}
                onClick={() => setSection(s.key)}
              >
                {s.label}
              </button>
            ))}
          </nav>
        </div>
        <div className="settings-overlay-right">
          <div className="settings-overlay-header">
            <h2 className="settings-overlay-title" id="settings-overlay-title">{activeLabel}</h2>
            <button className="settings-overlay-close" onClick={onClose} ref={closeRef} aria-label="Close settings">✕</button>
          </div>
          <div className="settings-overlay-body">
            {section === "projects"  && <ProjectsPanel />}
            {section === "rules"     && <MemoryList kind="rules" fetchFn={fetchRules} addFn={addRule} removeFn={removeRule} />}
            {section === "skills"    && <MemoryList kind="skills" fetchFn={fetchSkills} addFn={addSkill} removeFn={removeSkill} />}
            {section === "learnings" && (
              <LearningsPanel
                onOpenTask={onOpenTask}
                onFirstOpen={onSecondBrainOpened}
                onNavigateSection={setSection}
              />
            )}
            {section === "integrations" && <IntegrationsPanel />}
            {section === "models"      && <ModelsPanel />}
            {section === "workers"     && <WorkersPanel />}
            {section === "account"     && <AuthPanel />}
            {section === "updates"     && <UpdatesPanel />}
          </div>
        </div>
      </div>
    </>
  );
}

/* ── Rules / Skills list ─────────────────────────────────────────────────── */

function MemoryList({ kind, fetchFn, addFn, removeFn }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [error, setError] = useState(null);
  const [quarantined, setQuarantined] = useState(0);
  const [showArchived, setShowArchived] = useState(false);
  const [dismissed, setDismissed] = useState(() => new Set());

  const load = useCallback(() => {
    setLoading(true);
    // Fetches BOTH live and archived once — toggling "Show archived" is then
    // a pure client-side filter (visibleMemories), no extra roundtrip.
    fetchFn({ includeArchived: true })
      .then((data) => { setItems(data); setDismissed(new Set()); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
    // Best-effort: a footer count that fails to load says nothing, rather
    // than surfacing a second error banner over the list that DID load.
    fetchQuarantineCounts()
      .then((c) => setQuarantined(c[kind] || 0))
      .catch(() => {});
  }, [fetchFn, kind]);

  useEffect(() => { load(); }, [load]);

  async function handleRemove(id) {
    if (!window.confirm(`Remove this ${kind.slice(0, -1)}?`)) return;
    try {
      await removeFn(id);
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleRestore(id) {
    try {
      await restoreLearning(id);
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  // Client-side only — writes nothing server-side, the same "not now"
  // contract as the retire? section's dismissal (learningRetire.js).
  function handleDismiss(id) {
    setDismissed((prev) => new Set(prev).add(id));
  }

  const archivedTotal = archivedCount(items);
  const visible = visibleMemories(items, { showArchived, dismissedIds: [...dismissed] });

  return (
    <div className="memory-panel">
      <div className="memory-header">
        {/* The overlay header (Settings.jsx) already shows the active section
            label — this h3's own title text would be a near-duplicate heading,
            so it's scoped-hidden under .settings-overlay-body (styles.css)
            while staying intact for any non-overlay reuse of this panel. */}
        <h3 className="memory-title">
          <span className="panel-title-text">{kind === "rules" ? "Confirmed Rules" : "Confirmed Skills"}</span>
          {!loading && <span className="memory-count">{visible.length}</span>}
        </h3>
        {!loading && archivedTotal > 0 && (
          <label className="memory-archive-toggle">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(e) => setShowArchived(e.target.checked)}
            />
            {" "}Show archived ({archivedTotal})
          </label>
        )}
        <button className="btn btn-new-task" onClick={() => setShowAdd(true)}>
          + Add {kind.slice(0, -1)}
        </button>
      </div>
      {error && <div className="settings-error">{error}</div>}
      {loading ? (
        <div className="settings-empty">Loading…</div>
      ) : visible.length === 0 ? (
        <div className="settings-empty">
          No confirmed {kind} yet. Add one to inject it into every agent prompt.
        </div>
      ) : (
        <div className="memory-list">
          {visible.map((item) => (
            <MemoryCard
              key={item.id}
              item={item}
              onRemove={handleRemove}
              onRestore={handleRestore}
              onDismiss={handleDismiss}
            />
          ))}
        </div>
      )}
      {!loading && quarantineFooterLabel(quarantined) && (
        <div className="settings-empty">{quarantineFooterLabel(quarantined)}</div>
      )}
      {showAdd && (
        <AddMemoryModal
          kind={kind}
          onClose={() => setShowAdd(false)}
          onSaved={() => { setShowAdd(false); load(); }}
          addFn={addFn}
        />
      )}
    </div>
  );
}

// Memory lifecycle A: use_count / last_used_at / outcome histogram, shared by
// the Rules, Skills and Learnings cards — one row, one place, so the three
// panels can't drift on how the same ledger is read.
function MemoryUsageRow({ item }) {
  const u = memoryUsageSummary(item);
  if (u.useCount === 0) {
    return <div className="memory-usage-row memory-usage-empty">not yet injected into a prompt</div>;
  }
  const last = u.lastUsedAt ? String(u.lastUsedAt).slice(0, 19) : "unknown";
  return (
    <div className="memory-usage-row">
      <span className="memory-usage-count">used {u.useCount}x</span>
      <span className="memory-usage-last"> · last {last}</span>
      {u.total > 0 && (
        <span className="memory-usage-outcomes">
          {" "}· {u.successPct}% success / {u.failurePct}% failure
          {u.cancelledPct > 0 ? ` / ${u.cancelledPct}% cancelled` : ""}
          {u.timeoutPct > 0 ? ` / ${u.timeoutPct}% timeout` : ""}
        </span>
      )}
      <div className="memory-usage-label">{u.label}</div>
    </div>
  );
}

function MemoryCard({ item, onRemove, onRestore, onDismiss }) {
  const tags = (() => {
    try {
      const t = typeof item.tags === "string" ? JSON.parse(item.tags) : item.tags;
      return Array.isArray(t) ? t : [];
    } catch { return []; }
  })();
  const badge = archiveBadge(item);

  return (
    <div className="memory-card ph-no-capture">
      <div className="memory-card-header">
        <span className="memory-card-id">{(item.id || "").slice(0, 8)}</span>
        <span className="memory-card-type">{item.type}</span>
        {badge && (
          <span className="memory-card-badge" title={badge.title}>{badge.label}</span>
        )}
        <button className="memory-card-remove" onClick={() => onRemove(item.id)} title="Remove">✕</button>
      </div>
      <div className="memory-card-title">{item.title}</div>
      {item.content && <div className="memory-card-content">{item.content}</div>}
      {tags.length > 0 && (
        <div className="memory-card-tags">
          {tags.map((t, i) => <span key={i} className="memory-tag">{t}</span>)}
        </div>
      )}
      <MemoryUsageRow item={item} />
      {badge && (
        <div className="memory-card-actions">
          <button className="btn btn-sm" onClick={() => onRestore(item.id)}>Restore</button>
          <button className="btn btn-sm btn-ghost" onClick={() => onDismiss(item.id)}>Dismiss</button>
        </div>
      )}
    </div>
  );
}

function AddMemoryModal({ kind, onClose, onSaved, addFn }) {
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEscapeKey(onClose, !busy);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!title.trim() || !content.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await addFn({
        title: title.trim(),
        content: content.trim(),
        tags: tags.split(",").map(t => t.trim()).filter(Boolean),
      });
      onSaved();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="sendback-overlay" data-nested-modal
         onMouseDown={keepFocusInDialog}>
      <div className="new-task-modal">
        <div className="sendback-label">Add {kind.slice(0, -1)}</div>
        <form onSubmit={handleSubmit}>
          <div className="ntm-field">
            <label className="ntm-label">Title</label>
            <input
              className="new-task-input"
              placeholder="Short, descriptive title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              autoFocus
            />
          </div>
          <div className="ntm-field">
            <label className="ntm-label">Content</label>
            <textarea
              className="sendback-textarea"
              placeholder="The full rule or skill description"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={4}
            />
          </div>
          <div className="ntm-field">
            <label className="ntm-label">Tags</label>
            <input
              className="new-task-input"
              placeholder="Comma-separated, e.g. python, testing (optional)"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
            />
          </div>
          {error && <div className="new-task-error">{error}</div>}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-approve" disabled={!title.trim() || !content.trim() || busy}>
              {busy ? "\u2026" : "Add"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ── Second brain (D3.2) ─────────────────────────────────────────────────── */
//
// One entry point, two renderings. `config learning.auto_manage` (default
// True) is the kill switch the D3 operator directive requires to exist
// BEFORE the auto-activation default flips — it governs `HarvestJob`
// server-side, and this component mirrors it on the UI side: the confirm
// queue it restores has to actually be reachable, or the switch would
// silence auto-activation while leaving every proposal it stops auto-
// activating permanently invisible (queued, unconfirmed, no UI to confirm
// it from). So the panel branches:
//   - `auto_manage: true`  (default) → SecondBrainPanel: no pending queue,
//     no Confirm buttons, no bulk bar — everything auto-activates or
//     auto-archives, and the human's only actions are Pause/Delete/Restore.
//   - `auto_manage: false` (kill switch) → LegacyLearningQueuePanel: the
//     pre-D3 confirm queue, byte-for-byte unchanged, so flipping the switch
//     back really does "restore the confirm queue" the directive names, not
//     just the write path behind it.
export function LearningsPanel({ onOpenTask, onFirstOpen, onNavigateSection } = {}) {
  const [autoManage, setAutoManage] = useState(true);
  const [dailyCap, setDailyCap] = useState(10);

  // D2.1: the "!" nudge on the Settings sidebar row clears only once the
  // REAL Second-brain pane — SecondBrainPanel, the one with the explainer —
  // has actually rendered, not merely because Settings opened on some other
  // section (that clear used to live, unconditionally, in App.jsx's
  // openSettings()). Review fix (M4): a bare mount-only effect here fired
  // regardless of which of the two panels below ends up rendering, so a
  // kill-switched install (`auto_manage: false`, LegacyLearningQueuePanel —
  // no explainer at all) spent the flag on a pane the operator never saw. So
  // the call is folded into the SAME decision that picks the panel below,
  // gated on the very `isAutoManaged` value that decision reads — in the
  // success path when it resolves true, and in the failure path too (a fetch
  // failure keeps the auto-managed DEFAULT, matching `setAutoManage`'s own
  // documented fallback, so the flag is spent there as well — never left
  // permanently un-spendable just because the network hiccuped).
  useEffect(() => {
    let alive = true;
    fetchConfig()
      .then((cfg) => {
        if (!alive) return;
        const lc = (cfg && cfg.learning) || {};
        // Mirrors the Python default (`config.py`: `"auto_manage": True`) —
        // only an explicit `false` restores the legacy queue; a missing key
        // (older config, or a fetch that raced ahead of a default merge)
        // must not be misread as the kill switch being armed.
        const isAutoManaged = lc.auto_manage !== false;
        setAutoManage(isAutoManaged);
        setDailyCap(Number(lc.auto_activate_daily_cap) || 10);
        if (isAutoManaged) onFirstOpen?.();
      })
      .catch(() => {
        // best-effort: keep the auto-managed default — and spend the flag to
        // match, since that default is exactly what renders below.
        if (alive) onFirstOpen?.();
      });
    return () => { alive = false; };
  }, []);

  return autoManage
    ? <SecondBrainPanel dailyCap={dailyCap} onOpenTask={onOpenTask} onNavigateSection={onNavigateSection} />
    : <LegacyLearningQueuePanel />;
}

// The new, understandable surface (D3 design point 2): the D2 explainer,
// then "what it learned" — search + plain-language rows — then the
// Auto-managed line. No pending queue, no Confirm/Reject, no bulk bar: a
// harvested learning that passes the screens is already active by the time
// this list would show it (`LearningQueue.auto_activate`), and one that
// doesn't was already archived, not queued — there is nothing left here for
// a human to triage. The 90-day retire suggestion (Memory lifecycle C) is
// likewise gone from THIS panel: for an auto-activated row it is no longer a
// suggestion, `retire.py:sweep_auto_activated` retires it outright, and an
// operator-pinned/manually-added row is exempt by construction (never
// selected by that sweep) — so there is no "retire?" decision left for
// either kind of row to ask the human about.
function SecondBrainPanel({ dailyCap, onOpenTask, onNavigateSection }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  // Review-round fix: EVERY row used to share one `busyId`, so clicking
  // Pause on row B while row A's own POST was still in flight silently did
  // nothing — B never went disabled, so the click looked dropped rather
  // than "wait your turn". Each row now guards only ITSELF; unrelated rows
  // stay fully clickable, matching what a caller can actually observe (its
  // OWN button, not some other row's).
  const [busyIds, setBusyIds] = useState(() => new Set());
  const [quarantined, setQuarantined] = useState(0);
  // Review-round fix (#1): Delete only ARCHIVES server-side
  // (`LearningQueue.delete`) — recoverable in principle — but this panel
  // used to fetch active+paused only, so a deleted row vanished with no
  // confirm and no way back except knowing its id. Same shape as the
  // Rules/Skills `MemoryList` panel just below: fetch archived rows too,
  // keep them out of the main list, and reveal them from an archived-count
  // footer reusing that panel's own archiveBadge/archivedCount/
  // visibleMemories helpers — not a new pattern invented for this one panel.
  const [showArchived, setShowArchived] = useState(false);
  // Manual add (operator ask: "they can add stuff there") — operator-pinned,
  // confirmed on arrival, and NEVER auto-retired (`curator.py`'s pinned-
  // exempt rule): the one control this panel keeps unconditionally.
  const [newRule, setNewRule] = useState("");
  const [adding, setAdding] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    // `includePaused: true` — review deferral (2026-09-01): a paused row
    // must stay visible here (its own `Paused` chip) so Restore is
    // discoverable, even though `list_memories`'s default (and every
    // injection read) still excludes it. `includeArchived: true` — review-
    // round fix #1, same reasoning, for a Delete-archived row.
    fetchLearnings({ active: true, includePaused: true, includeArchived: true })
      .then((rows) => { setItems(rows); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
    // Best-effort, same as the legacy panel: a footer count that fails to
    // load is silent, not a second error banner over a list that loaded fine.
    fetchQuarantineCounts()
      .then((c) => setQuarantined(c.learnings || 0))
      .catch(() => {});
  }, []);

  useEffect(() => { load(); }, [load]);

  async function addBrainRule() {
    const content = newRule.trim();
    if (!content || adding) return;
    setAdding(true);
    setError(null);
    try {
      // Title = the first line, capped; the whole text is the rule body. A
      // hand-written rule is confirmed on arrival, so it lands straight in
      // the list.
      const title = content.split("\n")[0].slice(0, 80);
      await addRule({ title, content });
      setNewRule("");
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setAdding(false);
    }
  }

  async function runAction(id, action) {
    if (busyIds.has(id)) return;
    setBusyIds((s) => new Set(s).add(id));
    try {
      await action(id);
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyIds((s) => { const n = new Set(s); n.delete(id); return n; });
    }
  }

  const handlePause = (id) => runAction(id, pauseLearning);
  const handleDelete = (id) => runAction(id, deleteLearning);
  const handleRestore = (id) => runAction(id, restoreLearning);

  // `liveItems` excludes archived (same predicate `visibleMemories` already
  // applies for Rules/Skills when its "Show archived" box is unchecked) —
  // everything the header count, the search filter and the main list work
  // off of. `archivedItems` is the other side of that same split, reserved
  // for the footer below; `archivedCount` is the Rules/Skills panel's own
  // helper, unchanged.
  const liveItems = visibleMemories(items, { showArchived: false });
  const archivedTotal = archivedCount(items);
  const archivedItems = items.filter((it) => it && it.archived);
  const visible = filterLearnings(liveItems, query);

  return (
    <div className="memory-panel second-brain-panel">
      <div className="memory-header">
        {/* See MemoryList: redundant with the overlay header's own title. */}
        <h3 className="memory-title">
          <span className="panel-title-text">Second brain</span>
          {!loading && <span className="memory-count">{liveItems.length}</span>}
        </h3>
      </div>
      {/* D2 explainer, VERBATIM from the spec — this is the "!" nudge's real
          destination, not a rewrite of it. */}
      <p className="learning-explainer">
        Your second brain. no_human learns from every task — what worked, what
        broke, your repo's rules — and applies it automatically to the next
        task. Nothing to approve. Review or pause anything here.
      </p>
      {/* D2 review addenda: the setup actions the "!" nudge used to promise
          ("pick models per role", "seed rules") belong here, one click from
          the explainer that sent the operator to this pane — not buried back
          behind a second navigation. Both deep-link via the same section
          switch the overlay's own left nav uses (setSection, threaded down
          as onNavigateSection), not a new nav mechanism. */}
      <div className="second-brain-setup-actions">
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => onNavigateSection?.("models")}
        >Pick models per role</button>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => onNavigateSection?.("rules")}
        >Seed rules</button>
      </div>
      <p className="learning-auto-line">
        Auto-managed — up to{" "}
        <strong className="second-brain-cap">{dailyCap}</strong> new learnings
        activate a day; ones it activated on its own retire automatically
        after 90 days unused. Rules you add yourself are never auto-retired.
      </p>
      {/* Manual add (operator ask: "add stuff there"). */}
      <div className="learning-add">
        <textarea
          className="learning-add-input"
          rows={2}
          placeholder="Add a rule the AI should always follow — e.g. &quot;Always run the linter before opening a PR.&quot;"
          value={newRule}
          onChange={(e) => setNewRule(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) addBrainRule(); }}
        />
        <button
          type="button"
          className="btn btn-approve btn-sm"
          disabled={!newRule.trim() || adding}
          onClick={addBrainRule}
        >
          {adding ? "Adding…" : "Add rule"}
        </button>
      </div>
      {error && <div className="settings-error">{error}</div>}
      {!loading && liveItems.length > 0 && (
        <div className="learning-toolbar">
          <input
            type="search"
            className="learning-filter"
            placeholder="Filter by title, content, type, origin or project…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Filter what your second brain learned"
          />
          <span className="learning-filter-count">
            {visible.length === liveItems.length
              ? `${liveItems.length}`
              : `${visible.length} of ${liveItems.length}`}
          </span>
        </div>
      )}
      {loading ? (
        <div className="settings-empty">Loading…</div>
      ) : liveItems.length === 0 ? (
        <div className="settings-empty">
          Nothing learned yet. Your second brain fills in as tasks run —
          check back after a few have gone through.
        </div>
      ) : visible.length === 0 ? (
        <div className="settings-empty">
          No learning matches “{query}”.
        </div>
      ) : (
        <div className="memory-list second-brain-list">
          {visible.map((item) => (
            <SecondBrainRow
              key={item.id}
              item={item}
              busy={busyIds.has(item.id)}
              onPause={handlePause}
              onDelete={handleDelete}
              onRestore={handleRestore}
              onOpenTask={onOpenTask}
            />
          ))}
        </div>
      )}
      {/* Review-round fix (#1): Delete is one click and (until now) looked
          irreversible — the row just vanished. Same "Show archived (N)"
          idiom the Rules/Skills panel already uses (`.memory-archive-toggle`,
          reused verbatim below), so a deleted learning is exactly as
          recoverable, in exactly the same place an operator already knows
          to look for a Rules/Skills undo. */}
      {!loading && archivedTotal > 0 && (
        <label className="memory-archive-toggle second-brain-archived-toggle">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
          />
          {" "}Show archived ({archivedTotal})
        </label>
      )}
      {showArchived && archivedItems.length > 0 && (
        <div className="memory-list second-brain-list">
          {archivedItems.map((item) => (
            <SecondBrainRow
              key={item.id}
              item={item}
              busy={busyIds.has(item.id)}
              onPause={handlePause}
              onDelete={handleDelete}
              onRestore={handleRestore}
              onOpenTask={onOpenTask}
            />
          ))}
        </div>
      )}
      {!loading && quarantineFooterLabel(quarantined) && (
        <div className="settings-empty">{quarantineFooterLabel(quarantined)}</div>
      )}
    </div>
  );
}

// One row: plain-language text · origin task link · used N× · Pause/Delete
// (D3.2 spec, verbatim shape). A paused OR archived row is "inert" — it
// shows Restore instead of Pause, and never Delete (an archived row has
// nothing further to delete). Review deferral (2026-09-01): "a paused row
// must remain VISIBLE ... so restore is discoverable" — extended in the
// review round to an archived (Delete-archived) row for the same reason.
function SecondBrainRow({ item, busy, onPause, onDelete, onRestore, onOpenTask }) {
  const taskId = learningOriginTaskId(item);
  const useCount = Number(item.use_count || 0);
  const paused = Boolean(item.paused);
  const archived = Boolean(item.archived);
  const inert = paused || archived;
  // Reuses the Rules/Skills card's own blast-radius-free label — "Archived"
  // or "Superseded", the latter naming the survivor id — rather than a
  // second, second-brain-only vocabulary for the identical server state.
  const badge = archived ? archiveBadge(item) : null;
  const text = item.content || item.title || "(no text recorded)";
  const label = (item.title || text).slice(0, 60);

  return (
    <div className={`memory-card second-brain-row ph-no-capture${inert ? " paused" : ""}`}>
      <div className="second-brain-row-text">{text}</div>
      <div className="second-brain-row-meta">
        {/* Review-round fix (minor): a control with no handler is a DEAD
            control — underlined, focusable, and inert on click, which reads
            as broken rather than as "not wired up yet". Render the same
            fact as plain text instead whenever there is nowhere for the
            click to go. */}
        {taskId && onOpenTask ? (
          <button
            type="button"
            className="second-brain-task-link"
            onClick={() => onOpenTask(taskId)}
            aria-label={`Open the task this learning came from (${taskId.slice(0, 8)})`}
          >
            from task {taskId.slice(0, 8)}
          </button>
        ) : taskId ? (
          <span>from task {taskId.slice(0, 8)}</span>
        ) : (
          <span className="second-brain-origin-unknown">origin not recorded</span>
        )}
        {/* Review-round fix: `aria-label` on a bare `<span>` is silently
            IGNORED by assistive tech — a `<span>`'s implicit ARIA role is
            `generic`, and the spec prohibits naming a generic role, so the
            label above was never announced despite compiling and looking
            correct in markup. The fix is the standard split: the glyph
            stays visible and `aria-hidden` (screen readers must not read
            "used 3 times" then "used 3×" back to back), and a
            visually-hidden `.sr-only` sibling carries the one string a
            screen reader actually announces. */}
        <span className="second-brain-used" aria-hidden="true">used {useCount}×</span>
        <span className="sr-only">{`used ${useCount} time${useCount === 1 ? "" : "s"}`}</span>
        {paused && !archived && <span className="second-brain-paused-chip">Paused</span>}
        {badge && <span className="second-brain-paused-chip" title={badge.title}>{badge.label}</span>}
      </div>
      <div className="second-brain-row-actions">
        {inert ? (
          <button
            type="button"
            className="btn btn-sm"
            disabled={busy}
            onClick={() => onRestore(item.id)}
            aria-label={`Restore "${label}"`}
          >
            {busy ? "Restoring…" : "Restore"}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-sm"
            disabled={busy}
            onClick={() => onPause(item.id)}
            aria-label={`Pause "${label}"`}
          >
            {busy ? "Pausing…" : "Pause"}
          </button>
        )}
        {!archived && (
          <button
            type="button"
            className="btn btn-cancel btn-sm"
            disabled={busy}
            onClick={() => onDelete(item.id)}
            aria-label={`Delete "${label}"`}
          >
            {busy ? "Deleting…" : "Delete"}
          </button>
        )}
      </div>
    </div>
  );
}

// The pre-D3 confirm queue, unchanged, restored whenever
// `learning.auto_manage: false`. See LearningsPanel's own comment above for
// why this has to keep working rather than merely being kept for history.
function LegacyLearningQueuePanel() {
  const [pending, setPending] = useState([]);
  const [active, setActive] = useState([]);
  const [view, setView] = useState("pending");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(() => new Set());
  const [bulk, setBulk] = useState(null);   // {done, total} while confirming
  const [quarantined, setQuarantined] = useState(0);
  // Manual add (operator ask: "they can add stuff there"). A hand-written rule
  // is confirmed on arrival (POST /api/rules), so it lands straight in Active.
  const [newRule, setNewRule] = useState("");
  const [adding, setAdding] = useState(false);

  // Memory lifecycle C: the retire? section — stale ACTIVE rules, suggest
  // only. `dismissed` is purely client-side (a page reload forgets it — the
  // server never learns of a "not now", per the suggest-only contract) and
  // `retiring` tracks the one id an in-flight POST /retire is for, so a
  // double-click cannot fire it twice.
  const [retireCands, setRetireCands] = useState([]);
  const [dismissed, setDismissed] = useState(() => new Set());
  const [retiring, setRetiring] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    Promise.all([
      fetchLearnings({ active: false }),
      fetchLearnings({ active: true }),
      fetchRetireCandidates({ days: 90 }),
    ])
      .then(([p, a, r]) => {
        setPending(p); setActive(a); setRetireCands(r); setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
    // Best-effort, same as MemoryList: a failed footer count is silent, not
    // a second error banner over a queue that loaded fine.
    fetchQuarantineCounts()
      .then((c) => setQuarantined(c.learnings || 0))
      .catch(() => {});
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleRetire(id) {
    if (retiring) return;
    setRetiring(id);
    try {
      await retireLearning(id);
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setRetiring(null);
    }
  }

  function dismissRetireCandidate(id) {
    setDismissed((s) => new Set(s).add(id));
  }

  const retireVisible = retireCandidates(retireCands, [...dismissed], { days: 90 });

  async function handleAction(id, action) {
    try {
      if (action === "confirm") await confirmLearning(id);
      else await rejectLearning(id);
      setSelected((s) => { const n = new Set(s); n.delete(id); return n; });
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function addBrainRule() {
    const content = newRule.trim();
    if (!content || adding) return;
    setAdding(true);
    setError(null);
    try {
      // Title = the first line, capped; the whole text is the rule body. A
      // hand-written rule is confirmed on arrival, so switch to Active to show it.
      const title = content.split("\n")[0].slice(0, 80);
      await addRule({ title, content });
      setNewRule("");
      setView("active");
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setAdding(false);
    }
  }

  function toggleSelect(id) {
    setSelected((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  }

  const items = view === "pending" ? pending : active;
  const visible = filterLearnings(items, query);
  // Only what is on screen can be confirmed in bulk — see bulkConfirmIds: a
  // selection survives a filter change, and confirming a rule the operator
  // cannot see is exactly the blast-radius problem the card warns about.
  const batch = view === "pending" ? bulkConfirmIds(visible, selected) : [];

  async function confirmSelected() {
    if (!batch.length || bulk) return;
    setBulk({ done: 0, total: batch.length });
    // SEQUENTIAL, not Promise.all. Every confirm is a write against a store
    // that serialises writes behind one connection, and a running task needs
    // that connection too; firing 50 at once queues 50 deep in front of it.
    // The first failure stops the batch and says how far it got, rather than
    // reporting one error for an unknown amount of applied change.
    let done = 0;
    try {
      for (const id of batch) {
        await confirmLearning(id);
        done += 1;
        setBulk({ done, total: batch.length });
      }
      setError(null);
    } catch (e) {
      setError(`${e.message} — ${done} of ${batch.length} confirmed`);
    } finally {
      setBulk(null);
      setSelected(new Set());
      load();
    }
  }

  return (
    <div className="memory-panel">
      <div className="memory-header">
        {/* See MemoryList: redundant with the overlay header's own title. */}
        <h3 className="memory-title">
          <span className="panel-title-text">Learning Queue</span>
          {!loading && (
            <span className="memory-count">
              {pending.length} pending · {active.length} active
            </span>
          )}
        </h3>
        {/* Which of the two is showing was carried only by the `active` class, i.e. by
            colour — so a screen reader announced two identical "Pending" / "Active"
            buttons with no way to tell which list was on screen. aria-pressed is the
            toggle-button state; the nav above uses aria-current because it is navigation,
            these switch a view in place. */}
        <div className="learnings-toggle">
          <button
            className={`settings-tab sm${view === "pending" ? " active" : ""}`}
            aria-pressed={view === "pending"}
            onClick={() => setView("pending")}
          >
            Pending
          </button>
          <button
            className={`settings-tab sm${view === "active" ? " active" : ""}`}
            aria-pressed={view === "active"}
            onClick={() => setView("active")}
          >
            Active
          </button>
        </div>
      </div>
      {/* Answers the operator's two questions — "is it actually used?" and "who
          maintains it?". Surfacing real behaviour, not a promise: confirmed
          rules/learnings are loaded into the coder + reviewer prompt on every
          task (_load_active_memories); HarvestJob mines new ones from your
          tasks in the background; RetirementSweepJob retires unconfirmed ones
          after 45 days — so the brain maintains itself without asking you. */}
      <p className="learning-explainer">
        Your second brain. <strong>Active</strong> rules and learnings are
        applied automatically — the coder and reviewer read them on every task.
        New lessons are harvested from your tasks in the background, and unused
        ones retire on their own. Nothing acts until you confirm it.
      </p>
      {/* Manual add (operator ask: "add stuff there"). */}
      <div className="learning-add">
        <textarea
          className="learning-add-input"
          rows={2}
          placeholder="Add a rule the AI should always follow — e.g. &quot;Always run the linter before opening a PR.&quot;"
          value={newRule}
          onChange={(e) => setNewRule(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) addBrainRule(); }}
        />
        <button
          type="button"
          className="btn btn-approve btn-sm"
          disabled={!newRule.trim() || adding}
          onClick={addBrainRule}
        >
          {adding ? "Adding…" : "Add rule"}
        </button>
      </div>
      {error && <div className="settings-error">{error}</div>}
      {/* 329 pending rows are not triageable one click at a time, and not by
          scrolling either. The filter narrows; the bulk bar acts on what the
          filter left on screen. */}
      {!loading && items.length > 0 && (
        <div className="learning-toolbar">
          <input
            type="search"
            className="learning-filter"
            placeholder="Filter by title, content, type, origin or project…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Filter learnings"
          />
          <span className="learning-filter-count">
            {visible.length === items.length
              ? `${items.length}`
              : `${visible.length} of ${items.length}`}
          </span>
          {view === "pending" && (
            <button
              className="btn btn-approve btn-sm"
              disabled={!batch.length || !!bulk}
              onClick={confirmSelected}
            >
              {bulk
                ? `Confirming ${bulk.done}/${bulk.total}…`
                : `Confirm selected${batch.length ? ` (${batch.length})` : ""}`}
            </button>
          )}
        </div>
      )}
      {/* Said out loud rather than silently truncating: the batch is capped
          and the operator would otherwise believe a click applied all of a
          larger selection. */}
      {view === "pending" && batch.length >= BULK_CONFIRM_CAP && (
        <div className="learning-cap-note">
          {BULK_CONFIRM_CAP} at a time — click again for the rest.
        </div>
      )}
      {loading ? (
        <div className="settings-empty">Loading…</div>
      ) : items.length === 0 ? (
        <div className="settings-empty">
          {view === "pending"
            ? "No pending proposals. The agent hasn't proposed new learnings yet."
            : "No active learnings. Confirm pending proposals to activate them."}
        </div>
      ) : visible.length === 0 ? (
        <div className="settings-empty">
          No learning matches “{query}”.
        </div>
      ) : (
        <div className="learning-groups">
          {groupLearningsByProject(visible).map((group) => (
            <details key={group.key} className="learning-group" open>
              <summary className="learning-group-header">
                <span className="learning-group-label">{group.label}</span>
                <span className="memory-count">{group.count}</span>
              </summary>
              <div className="memory-list">
                {group.items.map((item) => (
                  <LearningCard
                    key={item.id}
                    item={item}
                    isPending={view === "pending"}
                    onAction={handleAction}
                    selected={selected.has(item.id)}
                    onToggleSelect={toggleSelect}
                  />
                ))}
              </div>
            </details>
          ))}
        </div>
      )}
      {/* Memory lifecycle C: suggest-only. Nothing here ever auto-archives —
          each row is the human's own explicit yes (Retire) or a client-side
          "not now" (Dismiss) that writes nothing server-side. */}
      {view === "active" && !loading && retireVisible.length > 0 && (
        <div className="retire-candidates">
          <h4 className="retire-candidates-title">
            retire? — {retireVisible.length} confirmed rule
            {retireVisible.length === 1 ? "" : "s"} unused for 90+ days
          </h4>
          <div className="memory-list">
            {retireVisible.map((item) => (
              <div key={item.id} className="memory-card retire-candidate-card ph-no-capture">
                <div className="memory-card-header">
                  <span className="memory-type-badge">{item.type}</span>
                  <span className="memory-title">{item.title}</span>
                </div>
                <div className="retire-candidate-label">{item.label}</div>
                <div className="memory-card-actions">
                  <button
                    className="btn btn-sm"
                    disabled={retiring === item.id}
                    onClick={() => handleRetire(item.id)}
                  >
                    {retiring === item.id ? "Retiring…" : "Retire"}
                  </button>
                  <button
                    className="btn btn-sm btn-ghost"
                    onClick={() => dismissRetireCandidate(item.id)}
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      {!loading && quarantineFooterLabel(quarantined) && (
        <div className="settings-empty">{quarantineFooterLabel(quarantined)}</div>
      )}
    </div>
  );
}

function LearningCard({ item, isPending, onAction, selected, onToggleSelect }) {
  // BLAST RADIUS, on the same card as the Confirm button. `GET /api/learnings`
  // has always returned every column; this card rendered four of them and
  // dropped the rest, so a one-click confirm of a GLOBAL rule — one that
  // injects into every project, forever — looked exactly like confirming a
  // rule scoped to one repo. `nh learnings` has printed that warning in yellow
  // since B2; the two surfaces asked for the same decision with different
  // facts on screen. Logic in learningCard.js, tested next to it.
  const scope = learningScope(item);
  const origin = learningOrigin(item);
  const evidence = learningEvidence(item.evidence);
  return (
    <div className={`memory-card learning-card ph-no-capture${selected ? " selected" : ""}`}>
      <div className="memory-card-header">
        {isPending && (
          // NEVER pre-ticked. The server sends `selected: false` on every
          // proposal and says why (a real user was shown their own home
          // address already checked, one click from standing guidance); this
          // is the same stance for the same reason.
          <label className="learning-select">
            <input
              type="checkbox"
              checked={selected}
              onChange={() => onToggleSelect(item.id)}
            />
            <span className="sr-only">Select {item.title}</span>
          </label>
        )}
        <span className="memory-card-id">{(item.id || "").slice(0, 8)}</span>
        <span className="memory-card-type">{item.type}</span>
        {origin && <span className="learning-origin">{origin}</span>}
      </div>
      <div className="memory-card-title">{item.title}</div>
      <div className={`learning-scope ${scope.kind}`}>
        {scope.kind === "global" ? "scope: " : "scope: "}
        {scope.label}
        {scope.detail && <span className="learning-scope-id"> · {scope.detail}</span>}
      </div>
      {evidence && <div className="learning-evidence">evidence: {evidence}</div>}
      {item.content && <div className="memory-card-content">{item.content}</div>}
      <MemoryUsageRow item={item} />
      {isPending && (
        <div className="learning-actions">
          <button
            className="btn btn-approve btn-sm"
            onClick={() => onAction(item.id, "confirm")}
          >
            Confirm
          </button>
          <button
            className="btn btn-cancel btn-sm"
            onClick={() => onAction(item.id, "reject")}
          >
            Reject
          </button>
        </div>
      )}
    </div>
  );
}

/* ── Projects panel ──────────────────────────────────────────────────────── */

function ProjectsPanel() {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showAdd, setShowAdd] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    fetchProjects()
      .then((data) => { setProjects(data || []); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleDelete(id, name) {
    if (!window.confirm(`Delete project "${name}"? This does not delete the repos.`)) return;
    try {
      await deleteProject(id);
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className="memory-panel">
      <div className="memory-header">
        {/* See MemoryList: redundant with the overlay header's own title. */}
        <h3 className="memory-title">
          <span className="panel-title-text">Projects</span>
          {!loading && <span className="memory-count">{projects.length}</span>}
        </h3>
        <button className="btn btn-new-task" onClick={() => setShowAdd(true)}>
          + New Project
        </button>
      </div>
      <div className="config-hint">
        A project groups multiple repos into a single unit of work. When creating a task, you pick a project.
      </div>
      {error && <div className="settings-error">{error}</div>}
      {loading ? (
        <div className="settings-loading">
          <span className="grill-spinner" />
          <span>Loading projects…</span>
        </div>
      ) : projects.length === 0 ? (
        <div className="settings-empty">
          No projects yet. Create one to group your repos.
        </div>
      ) : (
        <div className="memory-list">
          {projects.map((proj) => (
            <ProjectCard
              key={proj.id}
              project={proj}
              onDelete={() => handleDelete(proj.id, proj.name)}
              onUpdated={load}
            />
          ))}
        </div>
      )}
      {showAdd && (
        <AddProjectModal
          onClose={() => setShowAdd(false)}
          onSaved={() => { setShowAdd(false); load(); }}
        />
      )}
    </div>
  );
}

function ProjectCard({ project, onDelete, onUpdated }) {
  const [expanded, setExpanded] = useState(false);
  const [addingRepo, setAddingRepo] = useState(false);
  const [newRepoPath, setNewRepoPath] = useState("");
  const [profiling, setProfiling] = useState(false);
  const [error, setError] = useState(null);

  async function handleAddRepo() {
    const path = newRepoPath.trim();
    if (!path) return;
    setProfiling(true);
    setError(null);
    try {
      await onboardRepo(path);
      const updated = [...project.repo_paths, path];
      await updateProject(project.id, { repo_paths: updated });
      setNewRepoPath("");
      setAddingRepo(false);
      onUpdated();
    } catch (e) {
      setError(e.message);
    } finally {
      setProfiling(false);
    }
  }

  async function handleRemoveRepo(repoPath) {
    if (!window.confirm(`Remove "${repoPath.split('/').pop()}" from this project?`)) return;
    try {
      const updated = project.repo_paths.filter(r => r !== repoPath);
      await updateProject(project.id, { repo_paths: updated });
      onUpdated();
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleSetPrimary(repoPath) {
    try {
      await updateProject(project.id, { primary_repo: repoPath });
      onUpdated();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className={`memory-card project-card ph-no-capture${expanded ? " expanded" : ""}`}>
      {/* A <div> with an onClick was keyboard-inert, and expanding a project is the ONLY route
          to its repo list and test-plan editor — the whole panel was mouse-only. */}
      <div
        className="memory-card-header"
        style={{ cursor: 'pointer' }}
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
      >
        <span className="memory-card-title" style={{ flex: 1, marginBottom: 0 }}>{project.name}</span>
        <span className="memory-card-type">{project.repo_paths.length} {pluralize(project.repo_paths.length, "repo")}</span>
        <span className="project-expand-icon">{expanded ? '\u25BE' : '\u25B8'}</span>
        <button className="memory-card-remove" onClick={(e) => { e.stopPropagation(); onDelete(); }} title="Delete project">{'\u2715'}</button>
      </div>
      {expanded && (
        <div className="project-expanded-body">
          {project.repo_paths.length === 0 ? (
            <div className="ntm-hint">No repos in this project yet.</div>
          ) : (
            <div className="project-repos">
              {project.repo_paths.map((rp) => (
                <div key={rp} className="project-repo-row">
                  <span className="project-repo-name">{rp.split('/').pop()}</span>
                  <span className="project-repo-path ph-no-capture">{rp}</span>
                  {rp === project.primary_repo ? (
                    <span className="project-repo-primary">primary</span>
                  ) : (
                    <button className="project-repo-action" onClick={() => handleSetPrimary(rp)} title="Set as primary">{'\u2605'}</button>
                  )}
                  <button className="project-repo-action danger" onClick={() => handleRemoveRepo(rp)} title="Remove repo">{'\u2715'}</button>
                </div>
              ))}
            </div>
          )}
          {error && <div className="new-task-error">{error}</div>}
          {addingRepo ? (
            <div className="project-add-repo">
              <PathInput className="new-task-input" style={{ flex: 1 }} autoFocus listId="settings-addrepo-pathlist" value={newRepoPath} onChange={setNewRepoPath} placeholder="Repo path, e.g. ~/git/my-repo" />
              <button className="btn btn-approve btn-sm" disabled={!newRepoPath.trim() || profiling} onClick={handleAddRepo}>
                {profiling ? <><span className="grill-spinner" /> Profiling…</> : 'Add'}
              </button>
              <button className="btn btn-sendback btn-sm" onClick={() => { setAddingRepo(false); setNewRepoPath(""); }}>Cancel</button>
            </div>
          ) : (
            <button className="btn btn-sendback btn-sm" style={{ marginTop: '10px' }} onClick={() => setAddingRepo(true)}>+ Add repo</button>
          )}
          <TestPlanEditor project={project} onUpdated={onUpdated} />
        </div>
      )}
    </div>
  );
}


/* ── Test-plan editor (PR5) ─────────────────────────────────────────────── */

const GATING_OPTIONS = ["blocking", "advisory", "wake_gated"];
const RUNNER_OPTIONS = ["local", "ci"];
const CI_BACKEND_OPTIONS = ["gitlab", "jenkins"];

function TestPlanEditor({ project, onUpdated }) {
  const layers = project.test_layers || [];
  const [adding, setAdding] = useState(false);
  const [newLayer, setNewLayer] = useState({ name: "", command: "", gating: "blocking", repo: "", runner: "local", ciBackend: "gitlab", ciProject: "", ciBranch: "", ciVars: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  async function handleRemoveLayer(idx) {
    const updated = layers.filter((_, i) => i !== idx);
    setSaving(true); setError(null);
    try {
      await updateProject(project.id, { test_layers: updated });
      onUpdated();
    } catch (e) { setError(e.message); }
    finally { setSaving(false); }
  }

  async function handleAddLayer(e) {
    if (e) e.preventDefault();
    const { name, command, gating, repo, runner, ciBackend, ciProject, ciBranch, ciVars } = newLayer;
    if (!name.trim()) return;
    if (runner === "local" && !command.trim()) return;
    if (runner === "ci" && !ciProject.trim()) return;
    const layer = {
      name: name.trim(),
      command: command.trim(),
      gating,
      runner,
      timeout: runner === "ci" ? 3600 : 300,
      depends_on: [],
    };
    if (repo.trim()) layer.repo = repo.trim();
    if (runner === "ci") {
      const variables = {};
      ciVars.split("\n").forEach(line => {
        const [k, ...rest] = line.split(":");
        if (k && rest.length) variables[k.trim()] = rest.join(":").trim();
      });
      layer.ci = {
        backend: ciBackend,
        [ciBackend === "jenkins" ? "job" : "project"]: ciProject.trim(),
        ...(ciBranch.trim() ? { branch: ciBranch.trim() } : {}),
        ...(Object.keys(variables).length ? { variables } : {}),
        timeout_minutes: 60,
      };
      if (ciBackend === "jenkins") layer.ci.mode = "human_gated";
    }
    const updated = [...layers, layer];
    setSaving(true); setError(null);
    try {
      await updateProject(project.id, { test_layers: updated });
      setNewLayer({ name: "", command: "", gating: "blocking", repo: "", runner: "local", ciBackend: "gitlab", ciProject: "", ciBranch: "", ciVars: "" });
      setAdding(false);
      onUpdated();
    } catch (e) { setError(e.message); }
    finally { setSaving(false); }
  }

  return (
    <div className="project-expanded-body">
      <div className="memory-header" style={{ marginBottom: '8px' }}>
        <span className="ntm-label" style={{ marginBottom: 0 }}>Test Plan</span>
        {!adding && (
          <button className="btn btn-sendback btn-sm" onClick={() => setAdding(true)}>+ Add layer</button>
        )}
      </div>
      {error && <div className="new-task-error">{error}</div>}
      {layers.length === 0 && !adding && (
        <div className="ntm-hint">
          No test layers configured. The orchestrator will use the profile&apos;s test command.
        </div>
      )}
      {layers.map((l, idx) => (
        <div key={idx} className="project-repo-row">
          <span className="project-repo-name">{l.name}</span>
          {l.runner === "ci" ? (
            <code className="project-repo-path ph-no-capture" style={{ color: 'var(--accent)' }}
              title={l.ci ? JSON.stringify(l.ci, null, 2) : ""}>
              ci:{l.ci?.backend || "?"} {"\u2192"} {l.ci?.project || l.ci?.job || "?"}
            </code>
          ) : (
            <code className="project-repo-path ph-no-capture">{l.command}</code>
          )}
          <span className={`memory-tag${l.gating !== "blocking" ? " advisory" : ""}`}>
            {l.gating}
          </span>
          {l.repo && (
            <span className="project-repo-path ph-no-capture" style={{ flex: 'none' }} title={l.repo}>
              {"\u2197"} {l.repo.split("/").pop()}
            </span>
          )}
          <button className="project-repo-action danger" disabled={saving} onClick={() => handleRemoveLayer(idx)} title="Remove layer">{"\u2715"}</button>
        </div>
      ))}
      {adding && (
        <form onSubmit={handleAddLayer} className="test-layer-form">
          <div className="new-task-row">
            <input className="new-task-input" style={{ flex: 1, marginBottom: 0 }} placeholder="Layer name (e.g. unit, integration, ci_gate-e2e)" value={newLayer.name}
              onChange={(e) => setNewLayer({ ...newLayer, name: e.target.value })} autoFocus />
            <select className="new-task-select" style={{ flex: 'none', width: 80 }} aria-label="Test runner" value={newLayer.runner}
              onChange={(e) => setNewLayer({ ...newLayer, runner: e.target.value })}>
              {RUNNER_OPTIONS.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
            <select className="new-task-select" style={{ flex: 'none', width: 120 }} aria-label="Gating" value={newLayer.gating}
              onChange={(e) => setNewLayer({ ...newLayer, gating: e.target.value })}>
              {GATING_OPTIONS.map(g => <option key={g} value={g}>{g}</option>)}
            </select>
          </div>
          {newLayer.runner === "local" ? (
            <>
              <input className="new-task-input" style={{ marginBottom: '6px' }} placeholder="Test command (e.g. uv run pytest -q)" value={newLayer.command}
                onChange={(e) => setNewLayer({ ...newLayer, command: e.target.value })} />
              <input className="new-task-input" style={{ marginBottom: '6px' }} placeholder="Cross-repo path (optional, e.g. ~/git/tests-repo)" value={newLayer.repo}
                onChange={(e) => setNewLayer({ ...newLayer, repo: e.target.value })} />
            </>
          ) : (
            <>
              <div className="new-task-row" style={{ marginTop: '6px' }}>
                <select className="new-task-select" style={{ flex: 'none', width: 100 }} aria-label="CI backend" value={newLayer.ciBackend}
                  onChange={(e) => setNewLayer({ ...newLayer, ciBackend: e.target.value })}>
                  {CI_BACKEND_OPTIONS.map(b => <option key={b} value={b}>{b}</option>)}
                </select>
                <input className="new-task-input" style={{ flex: 1, marginBottom: 0 }}
                  placeholder={newLayer.ciBackend === "jenkins" ? "Jenkins job path" : "GitLab project (e.g. group/my-service)"}
                  value={newLayer.ciProject}
                  onChange={(e) => setNewLayer({ ...newLayer, ciProject: e.target.value })} />
              </div>
              <input className="new-task-input" style={{ marginTop: '6px', marginBottom: '6px' }} placeholder="CI branch (default: main)" value={newLayer.ciBranch}
                onChange={(e) => setNewLayer({ ...newLayer, ciBranch: e.target.value })} />
              <textarea className="new-task-input" rows={3} style={{ fontFamily: 'var(--font-mono)', fontSize: '12px' }}
                placeholder={"Pipeline variables (one per line, KEY:VALUE)\ne.g. METHOD:create\nDESTROY:true"}
                value={newLayer.ciVars}
                onChange={(e) => setNewLayer({ ...newLayer, ciVars: e.target.value })} />
            </>
          )}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback btn-sm" onClick={() => setAdding(false)}>Cancel</button>
            <button type="submit" className="btn btn-approve btn-sm" disabled={
              !newLayer.name.trim() || saving ||
              (newLayer.runner === "local" && !newLayer.command.trim()) ||
              (newLayer.runner === "ci" && !newLayer.ciProject.trim())
            }>
              {saving ? "\u2026" : "Add Layer"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

function AddProjectModal({ onClose, onSaved }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [scanRoot, setScanRoot] = useState("~/git");
  const [detected, setDetected] = useState([]);
  const [selectedRepos, setSelectedRepos] = useState(new Set());
  // Escape was DEAD here while its sibling AddMemoryModal had it: this modal
  // carries `data-nested-modal`, which makes the overlay's own Escape handler
  // stand down for it, and nothing took over. With backdrop-close removed,
  // Cancel was the only way out — while the code and the composer's spec both
  // state "Escape and Cancel stay as the deliberate exits".
  useEscapeKey(onClose, !busy);
  const [scanning, setScanning] = useState(false);

  async function handleScan() {
    setScanning(true);
    setError(null);
    try {
      const res = await detectRepos(scanRoot);
      setDetected(res.repos || []);
    } catch (e) {
      setError(e.message);
    } finally {
      setScanning(false);
    }
  }

  function toggleRepo(path) {
    setSelectedRepos((s) => {
      const n = new Set(s);
      n.has(path) ? n.delete(path) : n.add(path);
      return n;
    });
  }

  async function handleCreate(e) {
    if (e) e.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const repoPaths = [...selectedRepos];
      for (const rp of repoPaths) {
        try { await onboardRepo(rp); } catch { /* already profiled or best-effort */ }
      }
      await createProject({ name: name.trim(), repo_paths: repoPaths });
      onSaved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="sendback-overlay" data-nested-modal
         onMouseDown={keepFocusInDialog}>
      <div className="new-task-modal" style={{ maxWidth: 520 }}>
        <div className="sendback-label">New Project</div>
        <form onSubmit={handleCreate}>
          <div className="ntm-field">
            <label className="ntm-label">Project Name</label>
            <input
              className="new-task-input ntm-title"
              placeholder="e.g. my-service"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
            />
          </div>
          <hr className="ntm-section-divider" />
          <div className="ntm-field">
            <label className="ntm-label">Scan for repos</label>
            <div className="new-task-row">
              <PathInput className="new-task-input" style={{ flex: 1 }} autoFocus listId="settings-scanroot-pathlist" value={scanRoot} onChange={setScanRoot} placeholder="Scan root, e.g. ~/git" />
              <button type="button" className="btn btn-sendback btn-sm" disabled={scanning} onClick={handleScan}>
                {scanning ? <><span className="grill-spinner" /> Scanning…</> : 'Scan'}
              </button>
            </div>
          </div>
          {detected.length > 0 && (
            <div className="ob-repolist ph-no-capture" style={{ maxHeight: '180px', margin: '8px 0' }}>
              {detected.map((r) => (
                <label key={r.path} className={`ob-repo${selectedRepos.has(r.path) ? " sel" : ""}`} style={{ padding: '4px 8px' }}>
                  <input type="checkbox" checked={selectedRepos.has(r.path)} onChange={() => toggleRepo(r.path)} />
                  <span className="ob-repo-name">{r.name}</span>
                  {r.ecosystem && <span className="ob-tag">{r.ecosystem}</span>}
                </label>
              ))}
            </div>
          )}
          {error && <div className="new-task-error">{error}</div>}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-approve" disabled={!name.trim() || busy}>
              {busy ? <><span className="grill-spinner" /> Creating…</> : 'Create Project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

