import { useState, useEffect, useRef } from "react";
import { fetchProjects, fetchConfig, fetchIntegrations, scaffoldRepo, discoverRepos } from "./api.js";
import { repoBadges, discoveryMessage, filterRepos } from "./discoveredRepos.js";
import { COMPOSER_KINDS, kindByValue, needsPrUrl } from "./composerKinds.js";
import { splitPrompt } from "./promptSplit.js";
import { greetingName } from "./greeting.js";
import { hasPrRef } from "./prRefs.js";
import { keepFocusInDialog } from "./keepFocusInDialog.js";
import { formatBytes } from "./formatBytes.js";
import { pluralize } from "./pluralize.js";
import { useEscapeKey } from "./useEscapeKey.js";
import { loadDraft, saveDraft, clearDraft, mergeWithSeed } from "./composerDraft.js";
import { coderBackendCaption, effectiveCoderBackend } from "./coderBackendCaption.js";
import { shortReason } from "./backendPanelView.js";
import PathInput from "./PathInput.jsx";
import QueueNotice from "./QueueNotice.jsx";

// The new-task composer (Task 5A) — one prompt, kind chips, inline controls.
//
// FIRST screen migrated to Tailwind (Task 5.0 bridge): utilities resolve to the
// same CSS variables the plain-CSS screens use, so light/dark come from the
// existing [data-theme] blocks.
//
// It owns the composed spec and hands it to the parent, which runs the intake
// grill exactly as the old form branch did. It never creates the task itself.
// The parent re-seeds it via `initial` so a failed grill never loses the prompt.

// ONE control system. Two Preflight-off hazards are handled here, once:
//   1. `border` alone sets border-WIDTH. Preflight normally supplies
//      `border-style: solid`; without it the computed style stays `none`, which
//      forces the width back to 0 — the border vanishes on a <div>, and a <button>
//      falls back to the UA `outset` bevel. Every bordered control states
//      `border-solid` explicitly. (Measured in Chromium: 0px/none without it.)
//   2. A control that states no background keeps the native `buttonface` grey.
// Surface rule: a control sits on `bg-panel` (the input surface) or on `bg-card`
// (the dialog) and takes the OTHER token as its fill, so its edge always reads.
// (Never use an /opacity modifier on a bridged colour: the tokens are hex, not
// channel triplets, so `bg-accent/30` would not resolve.)
// Every button in this dialog is built from CTL, so the keyboard focus ring lives
// here rather than on six call sites: measured 2026-08-29 against the built bundle,
// all six (attach, ×, Cancel, the two kind chips, Start) were falling back to the
// UA ring. Same token and offset the plain-CSS screens use (styles.css
// ":focus-visible — keyboard navigation outlines"). The text field and the select
// pill deliberately keep their own border-colour focus state instead; see below.
const CTL =
  "inline-flex h-10 shrink-0 cursor-pointer items-center justify-center rounded-full " +
  "border border-solid font-ui text-sm transition-colors " +
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 " +
  "focus-visible:outline-accent";
const ON_PANEL = `${CTL} border-line bg-card text-text-muted hover:bg-hover hover:text-text`;
const ON_CARD = `${CTL} border-line bg-panel text-text-muted hover:bg-hover hover:text-text`;
// `text-base` is the --base token: dark ink on the light-blue dark-theme accent,
// near-white on the strong light-theme accent — the trick .btn-approve already
// used. Plain `text-white` reads at 2.85:1 on the dark accent, a WCAG AA failure
// on the primary action.
const ACCENT = `${CTL} border-accent bg-accent text-base hover:border-accent-600 hover:bg-accent-600`;
const GHOST = `${CTL} border-transparent bg-transparent text-text-muted hover:text-text`;

const SELECT =
  "w-full cursor-pointer appearance-none border-0 bg-transparent p-0 pr-6 font-ui " +
  "text-sm text-text outline-none";

const TEXT_FIELD =
  "h-10 w-full rounded-full border border-solid border-line bg-panel px-5 font-mono " +
  "text-sm text-text outline-none transition-colors placeholder:text-text-muted " +
  "focus:border-accent";

// A <select> dressed as a control: the native caret is stripped and redrawn in a
// token colour. `outline-none` on the <select> would leave a keyboard user with no
// focus indication at all, so the wrapper carries the focus state instead.
function SelectPill({ children, onPanel = false, grow = false, ...rest }) {
  return (
    <div
      className={
        `${onPanel ? ON_PANEL : ON_CARD} relative px-5 focus-within:border-accent ` +
        (grow ? "min-w-0 flex-1 justify-start" : "")
      }
    >
      <select className={SELECT} {...rest}>
        {children}
      </select>
      <span
        aria-hidden="true"
        className="pointer-events-none absolute right-4 font-ui text-xs text-text-muted"
      >
        ▾
      </span>
    </div>
  );
}

export default function TaskComposer({ busy, error, initial, notice, queueRemaining = 0, onStopQueue = null, onOpenBacklog, onStart, onClose }) {
  // Seeded from `initial`: the parent unmounts this component for the duration of
  // the grill, so a grill that FAILS would otherwise drop the operator back into an
  // empty composer — prompt, attachments, kind and PR URL all gone.
  const [prompt, setPrompt] = useState(initial?.prompt ?? "");
  const [kind, setKind] = useState(initial?.kind ?? "feature");
  const [prUrl, setPrUrl] = useState(initial?.prUrl ?? "");
  const [priority, setPriority] = useState(initial?.priority ?? "medium");
  const [planApproval, setPlanApproval] = useState(initial?.planApproval ?? false);
  // Coder-backend picker. "" = untouched — leave it out of the request and
  // let the server fall back to worker.backend, exactly like before this
  // control existed. Options are never hardcoded here: they come from
  // config.coder_backends, fetched below (GET /api/config, sourced from
  // agent.backend.SUPPORTED_BACKENDS) — a backend added there appears in
  // this <select> with no change to this file.
  const [backend, setBackend] = useState(initial?.backend ?? "");
  const [files, setFiles] = useState(initial?.files ?? []);
  // undefined = fetch in flight · [] = genuinely none. The initial-[]
  // ambiguity showed 'No projects yet' during the load window and then
  // REFLOWED the repo control under the operator (same loading≠empty
  // class as the Stats est-loader; probed live: false for ~1-4s).
  const [projects, setProjects] = useState(undefined);
  const [selectedProjectId, setSelectedProjectId] = useState(initial?.projectId ?? "");
  const [repoPath, setRepoPath] = useState(initial?.repoPath ?? "");
  const [customRepo, setCustomRepo] = useState(Boolean(initial?.customRepo));
  // Draft recovery (composerDraft.js, part 1). A lazy initializer reads
  // localStorage on the very first render, ahead of any effect.
  const [draft, setDraft] = useState(() => {
    try {
      return loadDraft();
    } catch {
      return null;
    }
  });
  const [draftDismissed, setDraftDismissed] = useState(false);
  // The seed this composer started from, captured once. The save effect below
  // compares against this — not against hardcoded defaults — so an untouched
  // composer never manufactures a "draft": kind/priority always carry a
  // non-empty default, so writing them as-is on every render would persist a
  // 4-key object and re-show the banner for nothing the operator typed.
  const seedRef = useRef({
    prompt: initial?.prompt ?? "",
    kind: initial?.kind ?? "feature",
    priority: initial?.priority ?? "medium",
    repoPath: initial?.repoPath ?? "",
  });
  // Set once, by handleSubmit only (never by an effect): a successful submit
  // must permanently stop this mounted instance from re-persisting the very
  // values it just turned into a task. Safe as a ref here because it is armed
  // by a real event handler, not by mount-order bookkeeping inside an effect
  // (that pattern is what let StrictMode's double mount-effect invoke clobber
  // a just-loaded draft in an earlier version of this fix).
  const draftDoneRef = useRef(false);
  // "Create a new repo" (plan Task 5) - an inline disclosure inside free-text
  // repo mode, same progressive-disclosure idiom as the Jira panel above.
  const [createRepoOpen, setCreateRepoOpen] = useState(false);
  const [newRepoParent, setNewRepoParent] = useState("");
  const [newRepoName, setNewRepoName] = useState("");
  const [newRepoBusy, setNewRepoBusy] = useState(false);
  const [newRepoError, setNewRepoError] = useState(null);
  // Tracks the false->true create-repo success transition so focus can be
  // returned to the repository input exactly once (see focusedAfterCreateRef
  // below) rather than on every unrelated re-render.
  const [repoCreated, setRepoCreated] = useState(false);
  // Auto-discovered repositories (GET /api/repos/discover). `null` until the
  // fetch settles, so an empty list is never shown as "nothing found" while it
  // is still in flight - the same loading≠empty rule as `projects` above.
  const [discovered, setDiscovered] = useState(null);
  const [discoveredOpen, setDiscoveredOpen] = useState(false);
  const [discoveredQuery, setDiscoveredQuery] = useState("");
  const [config, setConfig] = useState(null);
  // Task 1.6 — a task started from a tracker ticket. `source`/`externalId`
  // ride along invisibly (never controls the operator sees) so the created
  // task carries Task.source = "jira" and the dedup key the poller matches on
  // (source="jira", external_id=KEY). They are pure pass-through now: the
  // Backlog page (Backlog.jsx) is what picks a ticket, and it seeds them
  // through `initial` — nothing inside this composer ever changes them, which
  // is also what makes a grill-fail re-seed round-trip them unharmed.
  const source = initial?.source ?? "board";
  const externalId = initial?.externalId ?? null;
  // Is Jira configured? Decides only whether the Backlog pointer below is
  // worth showing — an operator with no tracker never sees the affordance.
  const [jiraConfigured, setJiraConfigured] = useState(false);
  const fileInputRef = useRef(null);
  const dialogRef = useRef(null);
  // Captured up front, like Board.jsx closeTask captures e.currentTarget
  // before its target unmounts: the "Create repo" button and its whole panel
  // unmount on success, so the focus target must be a ref to the surviving
  // free-text repository PathInput, never something read off the click event.
  const repoInputRef = useRef(null);
  // One-shot guard for the success-transition focus effect below — without
  // it, a StrictMode double-invoke or an unrelated re-render while
  // repoCreated is still true would steal focus back from wherever the
  // operator has since moved it.
  const focusedAfterCreateRef = useRef(false);

  // Escape closes the composer — suppressed while a submit is in flight so it
  // cannot discard a task that is already being created.
  // Live view of the typed repo path for the projects-resolve effect (state
  // captured at mount would be stale by resolve time).
  const repoPathRef = useRef("");
  useEffect(() => { repoPathRef.current = repoPath; });

  // Persist the form on every relevant change; composerDraft.js's saveDraft
  // debounces the actual write. Two guards, both load-bearing:
  // (1) a submitted instance never re-persists (draftDoneRef); (2) nothing is
  // written while every field still equals the seed this composer started
  // from, so a fresh, untouched composer never manufactures a "draft" that
  // re-shows the banner for nothing typed.
  //
  // A loaded, unresolved draft (the banner still showing) does NOT gate this
  // effect on its own: typing while it is up is itself a decision — it
  // diverges from the seed, so it falls through the guard above, and is
  // treated as an implicit dismissal of the banner (the operator is now
  // composing fresh text, not reading the old one) so the new text is
  // persisted under the same key rather than silently dropped. Only an
  // UNTOUCHED banner (fields still equal the seed) blocks the write —
  // otherwise the very act of mounting with a draft present would overwrite
  // it with the seed before the operator ever sees the banner.
  useEffect(() => {
    if (draftDoneRef.current) return;
    const seed = seedRef.current;
    const changed =
      prompt !== seed.prompt || kind !== seed.kind || priority !== seed.priority || repoPath !== seed.repoPath;
    if (!changed) return;
    if (draft && Object.keys(draft).length > 0 && !draftDismissed) {
      setDraftDismissed(true);
    }
    saveDraft({ prompt, kind, priority, repoPath });
  }, [prompt, kind, priority, repoPath, draft, draftDismissed]);

  // Create-repo success moves focus to the repository input exactly once, on
  // the false->true transition. Deps are `[repoCreated]` only, so unrelated
  // state updates (typing in the prompt, Jira polling, projects loading)
  // never re-run this and can't steal focus back from wherever the operator
  // has since moved it; the ref flag additionally absorbs a StrictMode
  // double-invoke of the same transition.
  useEffect(() => {
    if (!repoCreated) { focusedAfterCreateRef.current = false; return; }
    if (focusedAfterCreateRef.current) return;
    focusedAfterCreateRef.current = true;
    repoInputRef.current?.focus();
  }, [repoCreated]);

  // Discovery runs once per composer, in the background. It is a convenience
  // over the typed path, so a failure is swallowed: the field, its autocomplete
  // and the create-repo panel all still work with no discovered list at all.
  useEffect(() => {
    let live = true;
    discoverRepos()
      .then((res) => { if (live) setDiscovered(res); })
      .catch(() => { if (live) setDiscovered({ repos: [], failed: true }); });
    return () => { live = false; };
  }, []);

  // Open the list unprompted in exactly one case: there is no saved project to
  // pick, so the alternative is an empty path field typed from memory - the
  // problem discovery exists to solve. Whenever a project IS selectable the
  // list stays collapsed, so it never pushes the rest of the form down for a
  // user who did not ask for it. One-shot: reopening after a manual close
  // would fight the user.
  const autoOpenedRef = useRef(false);
  useEffect(() => {
    if (autoOpenedRef.current || !Array.isArray(projects) || !discovered) return;
    autoOpenedRef.current = true;
    if (projects.length === 0 && (discovered.repos || []).length > 0) {
      setDiscoveredOpen(true);
    }
  }, [projects, discovered]);

  // Escape closes the composer — suppressed while a submit is in flight so it
  // cannot discard a task that is already being created. (It used to have to
  // close a nested Jira panel first; that panel is now the Backlog page.)
  useEscapeKey(onClose, !busy);

  // Jira configured? Fetched once — the Backlog pointer below is hidden
  // entirely (not just disabled) when it isn't.
  useEffect(() => {
    fetchIntegrations()
      .then((r) => {
        const jira = (r.integrations || []).find((i) => i.name === "jira");
        setJiraConfigured(Boolean(jira?.configured));
      })
      .catch(() => {}); // best-effort, like the greeting's fetchConfig below
  }, []);

  useEffect(() => {
    fetchProjects().then((p) => {
      setProjects(p || []);
      // A path TYPED during the load window must survive the picker's
      // arrival: without this, resolve swapped the control and submit
      // silently shipped the auto-defaulted project instead of the typed
      // repo (PR #116 review, medium — probe-proven). Non-empty free text
      // pins custom mode; the existing toggle proves the preserve path.
      setCustomRepo((cur) => cur || Boolean(repoPathRef.current?.trim()));
      // Only default the project when none is chosen — a re-seeded composer must
      // keep the operator's pick.
      setSelectedProjectId((cur) => cur || (p && p.length > 0 ? p[0].id : ""));
    });
    // The greeting is best-effort: no config, no name, no error.
    fetchConfig().then(setConfig).catch(() => {});
  }, []);

  // `role="dialog" aria-modal` promises the rest of the page is inert, but no
  // browser enforces that for Tab. Keep focus inside, and hand it back on close.
  useEffect(() => {
    const previous = document.activeElement;
    function onKeyDown(e) {
      if (e.key !== "Tab" || !dialogRef.current) return;
      const items = [...dialogRef.current.querySelectorAll("button, select, textarea, input")]
        .filter((el) => !el.disabled && el.type !== "file" && el.offsetParent !== null);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (previous && typeof previous.focus === "function") previous.focus();
    };
  }, []);

  // Non-empty is the gate — a draft of only non-allowlisted keys loads as
  // `{}` and must not show the banner.
  const showDraftBanner = Boolean(draft) && Object.keys(draft).length > 0 && !draftDismissed;
  const { title, description } = splitPrompt(prompt);
  const discoveredRepos = discovered?.repos ?? [];
  const discoveryNote = discoveryMessage(discovered);
  const name = greetingName(config);
  const project = (projects ?? []).find((p) => p.id === selectedProjectId);
  const selectedChip = kindByValue(kind);

  // A code_review task is failed by the backend unless a PR/MR ref reaches it in
  // the title or description, so submit stays closed until one does. The prompt
  // counts too — pasting the URL there must never be blocked by the URL field.
  const prRefMissing = needsPrUrl(kind) && !hasPrRef(prompt) && !hasPrRef(prUrl);
  const hasRepo = customRepo ? repoPath.trim() : selectedProjectId || repoPath.trim();
  // Server-derived — never a second copy of SUPPORTED_BACKENDS/
  // CLAUDE_PINNED_ROLES. Empty until GET /api/config resolves, so the
  // picker below offers nothing (not a guess) while it is in flight.
  const backendOptions = config?.coder_backends ?? [];
  const claudePinnedRoles = config?.claude_pinned_roles ?? [];
  // "" (the picker's config-default option) is not necessarily claude — an
  // install can set worker.backend: codex and leave the picker untouched, so
  // the disclosure must gate on the EFFECTIVE backend (an explicit pick, or
  // else the server's own resolved value vs its own default), never on the
  // picker alone. effectiveCoderBackend owns that precedence from
  // GET /api/config's coder_backend_effective/coder_backend_default — no
  // second copy of it here.
  const effectiveBackend = effectiveCoderBackend(backend, config);
  const backendCaption = coderBackendCaption(effectiveBackend, claudePinnedRoles);
  // `coder_backend_availability` is core.backend_settings.describe_backend's
  // output for every SUPPORTED_BACKENDS entry — the SAME call
  // core.runtime.assert_task_backend_usable makes that build_orchestrator
  // runs before the first coder turn (that check now also refuses a 'local'
  // config missing llm.local_model, not just an unset/unsafe
  // llm.local_base_url). Never a second frontend rule (the 'local'-only,
  // base_url-truthiness-only check this replaced could disagree with the
  // server — e.g. an unset OPENAI credential for 'codex' was invisible to
  // it). Keyed by id so a lookup miss (older server, no field yet) degrades
  // to "no opinion" rather than blocking everything.
  const backendAvailability = new Map(
    (config?.coder_backend_availability ?? []).map((o) => [o.id, o]),
  );
  const selectedBackendInfo = backend ? backendAvailability.get(backend) : undefined;
  // Fail closed at compose time (never a silent fallback to claude), but
  // only once the server has actually answered — an empty/missing field
  // (older server, GET /api/config still in flight) must never block
  // submit, since that would be blocking on ABSENT evidence, not a refusal.
  const selectedBackendUnavailable = selectedBackendInfo
    ? !selectedBackendInfo.available
    : false;
  const canSubmit =
    Boolean(title) && Boolean(hasRepo) && !prRefMissing && !selectedBackendUnavailable && !busy;
  // With no projects, the free-text path IS the only input — a toggle there would
  // be a no-op button whose only effect is wiping what was typed.
  const loaded = projects !== undefined;
  const showRepoToggle = loaded && projects.length > 0;
  // While loading, keep the free-text field (no swap-under-cursor when the
  // picker arrives is unavoidable, but the false claim below is).
  const freeTextRepo = customRepo || !loaded || projects.length === 0;

  async function handleCreateRepo() {
    if (newRepoBusy) return;
    setNewRepoBusy(true);
    setNewRepoError(null);
    // Re-arm the one-shot focus effect for this attempt — scaffolding is
    // non-idempotent server-side, so a second create must announce/focus
    // again rather than silently no-op on an already-true flag.
    setRepoCreated(false);
    try {
      const res = await scaffoldRepo(newRepoParent.trim(), newRepoName.trim());
      // Success: the new path becomes THE repo for this task and the normal
      // free-text flow resumes - the composer never re-asks for it.
      setRepoPath(res.repo_path);
      setCreateRepoOpen(false);
      setNewRepoParent("");
      setNewRepoName("");
      setRepoCreated(true);
    } catch (err) {
      // The backend's `detail` says WHICH check failed - show it verbatim.
      setNewRepoError(err.message);
    } finally {
      setNewRepoBusy(false);
    }
  }

  // Applies mergeWithSeed's documented rule (seed wins per present field) and
  // marks the draft resolved so the save effect above resumes persisting.
  function restoreDraft() {
    const merged = mergeWithSeed(draft, initial);
    if (merged.prompt !== undefined) setPrompt(merged.prompt);
    if (merged.kind !== undefined) setKind(merged.kind);
    if (merged.priority !== undefined) setPriority(merged.priority);
    if (merged.repoPath !== undefined) {
      setRepoPath(merged.repoPath);
      // Mirrors the fetchProjects resolve rule above: non-empty free text pins
      // custom mode, so a restored path is never silently dropped in favour
      // of the auto-defaulted project.
      if (merged.repoPath) setCustomRepo(true);
    }
    setDraftDismissed(true);
  }

  // Shared by the Discard button and the × close affordance (same effect).
  // clearDraft() also cancels any save already armed, so a 300ms-old pending
  // write can't resurrect the draft right after this clears it.
  function discardDraft() {
    clearDraft();
    setDraft(null);
    setDraftDismissed(true);
  }

  function handleSubmit(e) {
    if (e) e.preventDefault();
    if (!canSubmit) return;
    // Successful submit: stop this instance from ever re-persisting the
    // values it just turned into a task, and clear the stored draft so the
    // banner does not come back on a re-seed (grill failure) or next mount.
    draftDoneRef.current = true;
    clearDraft();
    setDraft(null);
    // The PR URL rides in the description — that is where parse_pr_refs looks.
    const fullDescription =
      [description, needsPrUrl(kind) ? prUrl.trim() : ""].filter(Boolean).join("\n\n") || null;
    onStart({
      title,
      description: fullDescription,
      kind,
      priority,
      planApproval,
      // "" (untouched) is sent as-is: CreateTaskRequest.backend is falsy-or-
      // None-means-default, so this never overrides worker.backend unless the
      // operator actually picked something.
      backend,
      files,
      repoPath: freeTextRepo ? repoPath.trim() || null : repoPath || null,
      projectId: !customRepo && selectedProjectId ? selectedProjectId : null,
      // Echoed back as `initial` if the grill fails, so nothing typed is lost.
      prompt,
      prUrl,
      customRepo,
      source,
      externalId,
      // Task 7: "Follow up" seeds this via followUpSeed(task) — not user-
      // editable in this form, so it rides through as a plain pass-through
      // rather than its own piece of state.
      followsId: initial?.followsId ?? null,
    });
  }

  return (
    <div
      // data-nested-modal: any dialog that can sit above the task drawer must claim Escape,
      // or the drawer's own handler closes IT too and the typed prompt is gone.
      data-nested-modal
      // No backdrop-click close: a friend testing the app lost a long typed
      // prompt to a stray click outside the box. Escape, the × above and Cancel
      // are the deliberate ways out; none of them is reachable by accident.
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm sm:items-center sm:p-8"
      onMouseDown={keepFocusInDialog}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="New task"
        className="relative max-h-[90vh] w-full max-w-5xl overflow-y-auto rounded-3xl border border-solid border-line bg-card px-6 py-10 shadow-2xl sm:px-14 sm:py-12"
       
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className={`${GHOST} absolute right-5 top-5 w-10 text-lg hover:bg-hover`}
        >
          ×
        </button>

        {/* A ticket-shaped task starts on the Backlog page, not here. This
            composer used to hide a Jira picker behind a disclosure inside the
            New Task dialog — two places to start work, and the one holding the
            actual backlog was the one you had to already know about. The
            pointer stays only because the composer is where an operator lands
            from the "+ New Task" button; the list itself lives one nav row
            away, beside Done and Failed. Hidden when Jira is unconfigured, and
            when this composer was ALREADY seeded from a ticket (source ===
            "jira") — pointing back at the page you just came from is noise. */}
        {jiraConfigured && source !== "jira" && onOpenBacklog && (
          <div className="mb-4 flex justify-center">
            <button
              type="button"
              onClick={onOpenBacklog}
              className={`${GHOST} gap-2 rounded-full border border-solid border-line px-4 hover:bg-hover hover:text-text`}
            >
              Working from a ticket? Open the Backlog
            </button>
          </div>
        )}

        {/* Queue position when several tickets were started at once — the
            operator is told WHICH ticket this is, not left guessing, and the
            only way to abandon the REST of the run sits next to it (Escape
            cancels this ticket alone; see backlogSelection.js). */}
        <QueueNotice notice={notice} remaining={queueRemaining} onStopAll={onStopQueue} />

        <div className="mb-9 text-center">
          <h2 className="font-display text-4xl font-semibold tracking-tight text-text-hi sm:text-5xl">
            {name ? `Hey there, ${name}` : "Hey there"}
          </h2>
          <p className="mt-3 font-ui text-base text-text-muted">What should I work on?</p>
        </div>

        {showDraftBanner && (
          <div
            role="status"
            className="mb-4 flex flex-wrap items-center gap-2 rounded-2xl border border-solid border-line bg-panel px-4 py-3"
          >
            <p className="min-w-0 flex-1 font-ui text-sm text-text-muted">You have an unsent draft from last time.</p>
            <button type="button" className={`${ACCENT} px-5`} onClick={restoreDraft}>Restore draft</button>
            <button type="button" className={`${ON_CARD} px-5`} onClick={discardDraft}>Discard</button>
            <button
              type="button"
              aria-label="Dismiss draft notice"
              className={`${GHOST} w-10 text-lg`}
              onClick={discardDraft}
            >
              ×
            </button>
          </div>
        )}

        <form onSubmit={handleSubmit} className={busy ? "pointer-events-none opacity-60" : ""}>
          {/* What shape of work this is. One choice out of many → radios, not toggles.
              N5: the two most consequential groups (kind, repo) sat unlabeled
              outside the card while priority (least consequential) sat inside —
              visible eyebrows give the hierarchy back without moving anything. */}
          <p className="mt-0 px-1 font-ui text-xs uppercase tracking-wide text-text-dim" id="kind-eyebrow">Kind</p>
          <div role="radiogroup" aria-labelledby="kind-eyebrow" className="mt-1.5 flex flex-wrap gap-2">
            {COMPOSER_KINDS.map((chip) => {
              const selected = chip.kind === kind;
              return (
                <button
                  key={chip.kind}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  onClick={() => setKind(chip.kind)}
                  className={`${selected ? ACCENT : ON_CARD} px-5`}
                >
                  {chip.label}
                </button>
              );
            })}
          </div>

          {/* The hint lived only in a `title` tooltip — invisible to keyboard and
              touch users. Show the selected kind's meaning instead. */}
          {selectedChip && (
            <p className="mt-2 px-5 font-ui text-sm text-text-muted">{selectedChip.hint}</p>
          )}

          {needsPrUrl(kind) && (
            /* ph-no-capture: the PR URL names the operator's repo. */
            <div className="mt-3 ph-no-capture">
              <input
                className={TEXT_FIELD}
                placeholder="https://github.com/owner/repo/pull/123"
                value={prUrl}
                onChange={(e) => setPrUrl(e.target.value)}
                aria-label="PR or MR URL"
                aria-describedby="pr-url-hint"
              />
              <p
                id="pr-url-hint"
                role={prRefMissing ? "alert" : undefined}
                className={`mt-2 px-5 font-ui text-sm ${prRefMissing ? "" : "text-text-muted"}`}
                style={prRefMissing ? { color: "var(--red)" } : undefined}
              >
                {prRefMissing
                  ? "Paste the PR/MR to review — a full URL, or a “host/owner/repo PR #123” reference."
                  : "The agent fetches this diff, reviews it, and drafts comments for your approval."}
              </p>
            </div>
          )}

          {/* Where the work happens. */}
          <p className="mt-3 px-1 font-ui text-xs uppercase tracking-wide text-text-dim" id="repo-eyebrow">Repository</p>
          {/* ph-no-capture: repo names/paths are operator content. */}
          <div className="mt-1.5 flex flex-wrap items-center gap-2 ph-no-capture">
            {!freeTextRepo ? (
              <SelectPill
                grow
                value={selectedProjectId}
                onChange={(e) => {
                  setSelectedProjectId(e.target.value);
                  // Otherwise a repo picked inside the OLD project rides along.
                  setRepoPath("");
                }}
                aria-label="Project"
              >
                {(projects ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.repo_paths.length} {pluralize(p.repo_paths.length, "repo")})
                  </option>
                ))}
              </SelectPill>
            ) : (
              <PathInput
                ref={repoInputRef}
                className={`${TEXT_FIELD} min-w-0 flex-1`}
                placeholder="~/git/my-project"
                value={repoPath}
                onChange={setRepoPath}
                listId="composer-pathlist"
                aria-label="Repository path"
                aria-describedby="repo-path-hint"
              />
            )}

            {!freeTextRepo && project && project.repo_paths.length > 1 && (
              <SelectPill
                value={repoPath || project.primary_repo || project.repo_paths[0]}
                onChange={(e) => setRepoPath(e.target.value)}
                aria-label="Repository"
              >
                {project.repo_paths.map((rp) => (
                  <option key={rp} value={rp}>
                    {rp.split("/").pop()}
                    {rp === project.primary_repo ? " (primary)" : ""}
                  </option>
                ))}
              </SelectPill>
            )}

            {freeTextRepo && (
              <button
                type="button"
                className={`${ON_CARD} px-5`}
                aria-expanded={createRepoOpen}
                aria-controls="create-repo-panel"
                onClick={() => {
                  setCreateRepoOpen((v) => !v);
                  setNewRepoError(null);
                  setRepoCreated(false);
                }}
              >
                create a new repo
              </button>
            )}

            {showRepoToggle && (
              <button
                type="button"
                className={`${ON_CARD} px-5`}
                onClick={() => {
                  const next = !customRepo;
                  setCustomRepo(next);
                  // Leaving custom mode clears the free text and restores a project;
                  // ENTERING it keeps whatever was already typed.
                  if (!next) {
                    setRepoPath("");
                    setSelectedProjectId(projects[0]?.id || "");
                    // The create-repo disclosure only exists in free-text mode.
                    setCreateRepoOpen(false);
                    setNewRepoError(null);
                    setRepoCreated(false);
                  }
                }}
              >
                {customRepo ? "use a saved project" : "use another repo"}
              </button>
            )}
          </div>

          {freeTextRepo && createRepoOpen && (
            <div
              id="create-repo-panel"
              className="mt-2 rounded-2xl border border-solid border-line bg-panel p-4"
            >
              <div className="flex flex-wrap items-center gap-2">
                <PathInput
                  className={`${TEXT_FIELD} min-w-0 flex-1`}
                  placeholder="~/git"
                  value={newRepoParent}
                  onChange={setNewRepoParent}
                  listId="composer-newrepo-parent"
                  aria-label="Parent directory"
                  // Enter here must never implicitly submit the composer form
                  // (PathInput spreads this onto its <input>). With both
                  // fields filled it creates the repo, like the name field;
                  // otherwise it is a no-op.
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      if (newRepoParent.trim() && newRepoName.trim()) handleCreateRepo();
                    }
                  }}
                />
                <input
                  className={`${TEXT_FIELD} w-full sm:w-56`}
                  placeholder="my-new-repo"
                  value={newRepoName}
                  onChange={(e) => setNewRepoName(e.target.value)}
                  // Enter here must create the repo, not implicitly submit the
                  // whole composer form through the "Next" button.
                  onKeyDown={(e) => {
                    if (e.key === "Enter") { e.preventDefault(); handleCreateRepo(); }
                  }}
                  spellCheck={false}
                  aria-label="New repository name"
                />
                <button
                  type="button"
                  className={`${ON_PANEL} px-5`}
                  disabled={newRepoBusy || !newRepoParent.trim() || !newRepoName.trim()}
                  onClick={handleCreateRepo}
                >
                  {newRepoBusy ? "Creating…" : "Create repo"}
                </button>
              </div>
              <p className="mt-2 px-5 font-ui text-sm text-text-muted">
                Makes parent/name, runs git init, commits a README, and saves it
                as a project - then the task continues in it.
              </p>
              {newRepoError && (
                <p
                  role="alert"
                  className="mt-2 rounded-xl px-4 py-2 font-ui text-sm"
                  style={{ color: "var(--red)", background: "var(--red-dim)" }}
                >
                  {newRepoError}
                </p>
              )}
            </div>
          )}

          {freeTextRepo && (
            <p id="repo-path-hint" className="mt-2 px-5 font-ui text-sm text-text-muted">
              Must be a path to a git repository — e.g. ~/git/my-project.
              {discoveredRepos.length > 0 && (
                <>
                  {" "}
                  <button
                    type="button"
                    className="underline underline-offset-2 hover:text-text"
                    aria-expanded={discoveredOpen}
                    aria-controls="discovered-repos-panel"
                    onClick={() => setDiscoveredOpen((v) => !v)}
                  >
                    {discoveredOpen
                      ? "hide the repositories found on this machine"
                      : `or pick one of ${discoveredRepos.length} ${pluralize(discoveredRepos.length, "repository", "repositories")} found on this machine`}
                  </button>
                </>
              )}
            </p>
          )}

          {freeTextRepo && discoveredOpen && (
            <div
              id="discovered-repos-panel"
              className="mt-2 rounded-2xl border border-solid border-line bg-panel p-4"
            >
              {discoveredRepos.length > 8 && (
                <input
                  className={`${TEXT_FIELD} mb-2 w-full`}
                  placeholder="Filter by name or path"
                  value={discoveredQuery}
                  onChange={(e) => setDiscoveredQuery(e.target.value)}
                  spellCheck={false}
                  aria-label="Filter discovered repositories"
                  // Enter must filter, never submit the composer.
                  onKeyDown={(e) => { if (e.key === "Enter") e.preventDefault(); }}
                />
              )}
              <ul className="max-h-64 list-none overflow-y-auto">
                {filterRepos(discoveredRepos, discoveredQuery).map((r) => (
                  <li key={r.path}>
                    <button
                      type="button"
                      data-discovered-repo={r.path}
                      aria-current={repoPath === r.path ? "true" : undefined}
                      className={`flex w-full flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded-xl px-3 py-2 text-left hover:bg-hover ${repoPath === r.path ? "bg-hover" : ""}`}
                      onClick={() => {
                        setRepoPath(r.path);
                        setDiscoveredOpen(false);
                      }}
                    >
                      <span className="font-ui text-base text-text">{r.name}</span>
                      {repoBadges(r).map((b) => (
                        <span
                          key={b.key}
                          className="font-ui text-xs"
                          style={{ color: b.tone === "warn" ? "var(--amber)" : "var(--text-dim)" }}
                        >
                          {b.text}
                        </span>
                      ))}
                      <span className="w-full font-mono text-xs text-text-dim">{r.path}</span>
                    </button>
                  </li>
                ))}
              </ul>
              {discoveryNote && (
                <p className="mt-2 px-3 font-ui text-sm text-text-muted">{discoveryNote}</p>
              )}
            </div>
          )}

          {/* The create-repo panel that would normally carry this message
              unmounts on success, so the announcement renders here — outside
              it — reusing the same role="alert" pattern as newRepoError. */}
          {repoCreated && (
            <p
              role="alert"
              className="mt-2 rounded-xl px-4 py-2 font-ui text-sm"
              style={{ color: "var(--green)", background: "var(--green-dim)" }}
            >
              Repository created
            </p>
          )}

          {loaded && projects.length === 0 && (
            <p className="mt-2 px-5 font-ui text-sm text-text-muted">
              No projects yet — give the path of the repo to work in.
            </p>
          )}

          {/* One large input surface: the prompt and the controls that qualify it.
              ph-no-capture: the whole composer surface (prompt textarea +
              attachments) is operator content — excluded from session replay. */}
          <div className="mt-4 rounded-2xl border border-solid border-line bg-panel p-4 transition-colors focus-within:border-accent sm:p-5 ph-no-capture">
            <textarea
              className="min-h-[180px] w-full resize-none border-0 bg-transparent px-2 py-1 font-ui text-lg leading-relaxed text-text outline-none placeholder:text-text-muted sm:min-h-[200px]"
              autoFocus
              placeholder="Describe the task. The first line becomes its title."
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => {
                // Cmd/Ctrl+Enter submits, the way every chat composer does.
                if ((e.metaKey || e.ctrlKey) && e.key === "Enter") handleSubmit(e);
              }}
            />

            <div className="mt-3 flex flex-wrap items-center gap-2">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                className="hidden"
                onChange={(e) => setFiles(Array.from(e.target.files || []))}
              />
              <button
                type="button"
                className={`${ON_PANEL} w-10 text-lg`}
                onClick={() => fileInputRef.current?.click()}
                title="Attach screenshots or documents — the agent reads them"
                aria-label="Attach files"
              >
                +
              </button>

              <SelectPill
                onPanel
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
                aria-label="Priority"
              >
                <option value="high">high priority</option>
                <option value="medium">medium priority</option>
                <option value="low">low priority</option>
              </SelectPill>

              {/* Coder-backend picker. Options come from the server
                  (GET /api/config's coder_backends, sourced from
                  agent.backend.SUPPORTED_BACKENDS) — never a hardcoded list
                  here, so a backend added there shows up with no web change.
                  A backend this install cannot run right now is disabled AT
                  THE POINT OF CHOICE (coder_backend_availability, the same
                  check core.runtime.assert_task_backend_usable runs), not
                  just blocked at submit. Affects the coder ONLY: the roles
                  in CLAUDE_PINNED_ROLES stay on Claude regardless of this
                  choice — the tooltip above always says so; the disclosure
                  paragraph below is conditional, shown only when the
                  EFFECTIVE backend (this pick, or else the config's own
                  resolved value) is non-default. */}
              {backendOptions.length > 0 && (
                <SelectPill
                  onPanel
                  value={backend}
                  onChange={(e) => setBackend(e.target.value)}
                  aria-label="Coder backend"
                  title={
                    claudePinnedRoles.length > 0
                      ? `Affects the CODER only. ${claudePinnedRoles.join(", ")} always run on Claude, ` +
                        "no matter what you pick here — a local or Codex model never reviews its own work."
                      : "Affects the coder only."
                  }
                >
                  <option value="">worker.backend (config default)</option>
                  {backendOptions.map((b) => {
                    const info = backendAvailability.get(b);
                    const unavailable = info ? !info.available : false;
                    return (
                      <option
                        key={b}
                        value={b}
                        disabled={unavailable}
                        title={unavailable ? info.reason : undefined}
                      >
                        {b} (coder only){unavailable ? " — unavailable" : ""}
                      </option>
                    );
                  })}
                </SelectPill>
              )}

              {/* GAP 1: the one toggle that answers "I am not letting an agent
                  burn millions of tokens on its own reading of my ticket".
                  Off by default - an unattended run stays unattended. */}
              {/* focus-within, not focus-visible: the visible affordance is this
                  pill, but Tab lands on the native checkbox inside it, which was
                  drawing Chrome's own light-blue ring (2.81:1 on --bg-card — under
                  WCAG 1.4.11's 3:1, the same defect .memory-card-header was fixed
                  for in styles.css). Same wrapper-carries-the-focus-state trick as
                  SelectPill above. */}
              <label
                className={`${ON_PANEL} flex cursor-pointer items-center gap-2 px-3 ` +
                  "focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 " +
                  "focus-within:outline-accent"}
                title="Stop after planning and show me the plan before any implementation token is spent"
              >
                <input
                  type="checkbox"
                  checked={planApproval}
                  onChange={(e) => setPlanApproval(e.target.checked)}
                />
                <span>review plan first</span>
              </label>

              <div className="ml-auto flex items-center gap-2">
                <button type="button" className={`${GHOST} px-5`} onClick={onClose}>
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={!canSubmit}
                  className={`${ACCENT} px-6 font-medium disabled:cursor-not-allowed disabled:opacity-40`}
                >
                  {busy ? "Exploring repo…" : "Next →"}
                </button>
              </div>
            </div>
          </div>

          {/* Shown only once the choice can actually differ from the config
              default (the SelectPill's title tooltip says the same thing on
              hover, at every step) — that's the moment a reviewer or an
              operator glancing at the form could otherwise assume this
              picks who reviews the work, not just who codes it. On the
              default there is nothing non-default to disclose, so the
              caption stays out of the primary flow. coderBackendCaption
              owns the copy and the gate (see CLAUDE_PINNED_ROLES / d35aa60e). */}
          {backendOptions.length > 0 && backendCaption && (
            <p className="mt-2 px-5 font-ui text-sm text-text-muted">{backendCaption}</p>
          )}

          {selectedBackendUnavailable && (
            <p
              role="alert"
              className="mt-2 px-5 font-ui text-sm"
              style={{ color: "var(--red)" }}
            >
              {shortReason(selectedBackendInfo.reason) || `The '${backend}' coder backend is not available on this install.`}{" "}
              Pick a different backend.
            </p>
          )}

          {files.length > 0 && (
            <div className="mt-3 px-5 font-ui text-sm text-text-muted ph-no-capture">
              {files.map((f) => `${f.name} (${formatBytes(f.size)})`).join(", ")}
            </div>
          )}

          {error && (
            <div
              className="mt-4 rounded-2xl px-5 py-4 font-ui text-sm ph-no-capture"
              style={{ color: "var(--red)", background: "var(--red-dim)" }}
            >
              {error}
            </div>
          )}
        </form>
      </div>
    </div>
  );
}
