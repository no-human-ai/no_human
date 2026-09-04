import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  discoverRepos, onboardRepo,
  completeOnboarding, suggestPaths, createProject,
  generateDocs, fetchIntegrationSetup, saveIntegrationSetup,
  testIntegration,
  proveRepoSSE, confirmRepoProfile, fetchReadiness, setRepoUiEvidence,
  probeServer,
} from "./api.js";
import { kickoffWikiGeneration } from "./onboardingDocsKickoff.js";
import { isNetworkError, offlineBanner, createServerProbe } from "./offlineRetry.js";
import { repoBadges, discoveryMessage, ambiguousNames, rowName } from "./discoveredRepos.js";
// onboardingHistory.js (scanSummary/groupProposalsByProject) went with the
// removed AI-history/rules steps — the mining now happens from Settings.
import { optionValue } from "./pathSuggest.js";
import { splitRecent, relativeMtime, debounce } from "./repoRecency.js";
import { LegionLogo } from "./Logo.jsx";
import { KIND_LABEL, NAME_LABEL } from "./integrationChip.js";
import { IntegrationIcon } from "./integrationIcons.jsx";
import FieldHint from "./FieldHint.jsx";
import { hintId as fieldHintId } from "./fieldHelp.js";
import {
  draftFrom, changedValues, listToText, readiness, setupSummary, secretHint,
  switchLabel, effectiveEnabled,
} from "./integrationSetup.js";
import {
  backDisabled, backDisabledReason, forwardDisabled, canJumpTo, stepButtonLabel,
} from "./onboardingNav.js";
import { canStartMinimal } from "./onboardingMinimal.js";
import { summaryRepoCounts } from "./onboardingSummary.js";
import {
  newProjectDef, toggleProjectRepo as bindProjectRepo, setPrimaryRepo,
  dropRepoEverywhere, unboundProjects, unboundProjectsMessage, projectPayload,
  projectsBlockContinue, launchReadiness,
} from "./onboardingProjects.js";

// Input with live directory autocomplete (via /api/fs/suggest). As you type a
// path, matching sub-directories are offered through a native <datalist>.
// `onNetworkError` is `noteFetchFailure` handed down from the wizard:
// PathInput has no state of its own, so a network-level rejection here must
// propagate up to the wizard-level offline banner rather than reject unhandled.
function PathInput({ value, onChange, placeholder, autoFocus, onNetworkError }) {
  const [opts, setOpts] = useState([]);
  const listId = "pathlist-" + (placeholder || "p").replace(/\W/g, "");
  useEffect(() => {
    let live = true;
    const t = setTimeout(async () => {
      try {
        const res = await suggestPaths(value);
        if (live) setOpts(res.suggestions || []);
      } catch (e) {
        if (live && !onNetworkError?.(e)) setOpts([]);
      }
    }, 120);
    return () => { live = false; clearTimeout(t); };
  }, [value, onNetworkError]);
  return (
    <>
      <input
        className="ob-input" list={listId} value={value} placeholder={placeholder}
        autoFocus={autoFocus} spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
      />
      <datalist id={listId} className="ph-no-capture">
        {/* The option VALUE must match what the user is typing or the native
            datalist never surfaces it (a ~/-relative input can't match an
            absolute path). Rebuild it in the input's shape. is_repo was removed
            from /api/fs/suggest, so every entry is just a folder. */}
        {opts.map((o) => (
          <option key={o.path} value={optionValue(value, o.name)}>folder</option>
        ))}
      </datalist>
    </>
  );
}

// The first-run wizard. Warm-editorial, dark-first (see DESIGN.md).
// Every step wires to real /api/onboarding/* endpoints — no fake data.

const BASE_STEPS = [
  { key: "welcome",  title: "Welcome" },
  // The "You"/team step left the free-tier wizard on the operator's 2026-08-09
  // decision: the value it collected was write-only in the local product
  // (persisted at complete, read by nothing). It belongs to the future
  // free→team / free→cloud UPGRADE onboarding, where team scoping is real.
  { key: "repos",    title: "Repositories" },
  { key: "projects", title: "Projects" },
  // "Repo docs & wiki" left the wizard (operator, 2026-09-04): it was a step
  // that asked nothing decision-worthy of the user. The wiki is now enqueued
  // automatically, in the background, when Launch completes onboarding — see
  // kickoffWikiGeneration() in finish() below.
  { key: "integrations", title: "Integrations" },
  // "AI history" + "Rules review" left the wizard (operator, 2026-08-30): the
  // AI-learnings walk made onboarding long, and the work already lives in
  // Settings. The Settings "!" badge nudges the user to complete it there;
  // second-brain rules/learnings are viewed and added in the Settings panes.
  { key: "summary",  title: "Launch" },
];

// Telemetry ships ON by default and is no longer asked about in onboarding —
// the operator's 2026-08-26 decision removed the consent step entirely
// (telemetry.enabled ships True; it is inert without an endpoint, and the
// endpoint ships empty, so nothing leaves the machine). There is therefore no
// consent step, no consent state, and no telemetry mention in this wizard's UI.
// The privacy-policy/docs mention stays; only the UI mention goes.
export const repoName = (p) => (p || "").replace(/\/+$/, "").split("/").pop() || p;

export default function Onboarding({ onComplete }) {
  const [i, setI] = useState(0);
  const STEPS = BASE_STEPS;
  const [root, setRoot] = useState("");
  // The folder a manual "Search another folder" run actually scanned, so the
  // empty/searching state can name it ("" = the initial home+roots auto-scan).
  const [searchedPath, setSearchedPath] = useState("");
  const [detected, setDetected] = useState([]);
  // The auto-discovery response, kept whole so the roots it searched, the cap
  // note and the refused roots can all be reported (discoveryMessage).
  const [discovery, setDiscovery] = useState(null);
  const [manualScan, setManualScan] = useState(false);
  const [selectedRepos, setSelectedRepos] = useState(new Set());
  const [onboarded, setOnboarded] = useState({});   // path -> {ecosystem,test_cmd,...} | "busy"
  // no-human-67 follow-up: "Not now" on the visual-proof-walks suggestion is a
  // local, in-session dismiss only — it makes no API call and writes no
  // config, so re-opening the wizard (or a re-derive) can offer it again.
  const [uiEvidenceDismissed, setUiEvidenceDismissed] = useState(() => new Set());
  const [projectDefs, setProjectDefs] = useState([]);   // [{name, repos: Set, primary}]
  const [newProjName, setNewProjName] = useState("");
  // The repos ticked in the ADD-project form for the project being composed now
  // (F8 redesign). One project is created at a time: you name it, pick ITS repos
  // here, then Add drops it into the created-projects list below and this form
  // resets. Seeded from the repos selected on the repos step when the step opens,
  // so the common "one project, all my repos" case is one click.
  const [newProjRepos, setNewProjRepos] = useState(() => new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  // Incident 2026-09-04: the backend died mid-wizard and every step rendered
  // its own raw "Failed to fetch" string. `offline` is set the moment ANY
  // step loader hits a network-level rejection; the wizard-level banner then
  // owns the failure and no step renders the exception text. `reloadNonce`
  // is bumped once the server answers again, so the current step's loader
  // effects re-run and repopulate with live data.
  const [offline, setOffline] = useState(false);
  const [probing, setProbing] = useState(false);
  const [reloadNonce, setReloadNonce] = useState(0);
  const probeRef = useRef(null);
  const [integrations, setIntegrations] = useState(null);   // null=unloaded
  // Proving: path -> {status, lines[], elapsed, attempted, result, error}.
  // "unproven" is not a cosmetic state — a repo without a proven test command
  // still runs tasks, but its review gate has no test command to execute. So
  // the wizard offers the proof rather than deferring it to a CLI command the
  // user has no way to discover.
  const [proveState, setProveState] = useState({});
  const [proveCmd, setProveCmd] = useState({});   // path -> the human's corrected command
  const [readiness, setReadiness] = useState(null);
  const proveStreams = useRef({});
  // Re-entry guard for advance() (M1) — a click while a profile-then-advance
  // is mid-flight is ignored, without disabling Continue during the scan.
  const advancing = useRef(false);

  // Editable draft of the integrations step: {name: {field: value}}. Seeded
  // from the server's spec (draftFrom) and diffed against it on save, so only
  // what the user touched is written. A secret is never in here — the API
  // never advertises a credential field.
  const [intDraft, setIntDraft] = useState({});
  const [intSaving, setIntSaving] = useState(null);   // integration name
  const [intSaved, setIntSaved] = useState(null);     // integration name
  const [intError, setIntError] = useState({});       // name -> message
  // Post-save live-test result: name -> {testing} | {healthy: bool, detail}.
  // "Ready" (green) is shown ONLY when healthy === true here (or the server's
  // persisted spec.verified) — a saved key that has not passed a test reads
  // "Saved — not verified". See integrationSetup.js readiness.
  const [intTest, setIntTest] = useState({});
  // Which integration cards the user has expanded (F9). A card's body normally
  // opens when its switch turns on (effectiveEnabled). But a mute switch that
  // ships ON but is unconfigured (teams) reads as OFF and cannot be toggled ON
  // in the UI — so clicking it did NOTHING and its setup (the webhook note) was
  // unreachable. This tracks an explicit open so clicking any card reveals its
  // setup, the way slack/others do; toggling the switch flips it.
  const [openSetup, setOpenSetup] = useState(() => new Set());
  function toggleSetupOpen(name) {
    setOpenSetup((s) => {
      const n = new Set(s);
      n.has(name) ? n.delete(name) : n.add(name);
      return n;
    });
  }

  const step = STEPS[i];
  // The repo to offer a first task in: the server's readiness answer, never a
  // local guess — it is the same `is_usable` the orchestrator gates on.
  const firstTaskRepo = readiness && !readiness.error ? readiness.first_usable : null;
  // Which project definitions bind no repo. The summary used to count these in
  // with the rest and the launch dropped them without a word — the "Projects 1"
  // then "Projects 0" the first external tester saw. Named on the card, counted
  // honestly here, and refused by finish().
  const unbound = unboundProjects(projectDefs);
  // Two checkouts of the same repo render as two identical rows with no path
  // anywhere - basenames that collide get their full path shown so the user
  // can tell which checkout they are selecting.
  const ambiguousRepoNames = ambiguousNames(detected);
  // Recently-modified repos float to the top of the step as quick-add cards;
  // the rest keep the newest-first order the server already sorted them into.
  const nowEpoch = Math.floor(Date.now() / 1000);
  const { recent: recentRepos, rest: restRepos } = splitRecent(detected, nowEpoch);
  const next = () => setI((n) => Math.min(n + 1, STEPS.length - 1));
  const back = () => setI((n) => Math.max(n - 1, 0));
  // Everything the nav predicates in onboardingNav.js need. Kept as one object so
  // the Back/Continue/launch controls cannot drift apart.
  const navState = { index: i, lastIndex: STEPS.length - 1, busy };
  // The Projects step is the ONE place Continue is gated (spec §3 B2) — not
  // because projects are required (they are not; see the empty-state note) but
  // because a project that EXISTS with zero repos cannot be created and finish()
  // would refuse it at the end. Caught on the step that shows it, not six clicks
  // later. projectsBlockContinue fires ONLY on that case, never on "no projects".
  const projectsBlockMsg = step.key === "projects" ? projectsBlockContinue(projectDefs) : null;
  const continueBlocked = projectsBlockMsg !== null;

  // Advancing a step swaps the whole card underneath the user, and nothing moved focus
  // with it. Measured on the pre-change build, not assumed: the Continue button lives in
  // .ob-nav, a SIBLING of the card, so it is the same DOM node before and after and it
  // KEEPS focus — across the eight advances, focus stayed on that button 6 times, sat on
  // a step's own autofocused input once, and fell to <body> once (entering the repos
  // step, which is the one that kicks off an async guard() — consistent with the button
  // being disabled mid-flight, though that cause is inferred, not measured).
  // So the defect was never "focus is lost". It is that focus stays parked on a control
  // that sits AFTER the card in DOM order, so the next Tab walks out of the wizard
  // instead of into the new step's fields — and a screen reader is told nothing at all
  // while the card's entire contents change. Moving focus into the card, labelled
  // "Step N of 9: <title>", announces the change once and puts the next Tab inside it.
  const cardRef = useRef(null);
  // Roving-tabindex targets for the clickable step buttons (spec §3 B2): only the
  // current step is tabbable; ArrowLeft/ArrowRight move focus between the rest.
  const stepRefs = useRef([]);
  function onStepKeyDown(e) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    // Move relative to the button that HAS focus, not the wizard's current step —
    // roving focus walks the row, it doesn't teleport back to the active step.
    const focused = stepRefs.current.indexOf(document.activeElement);
    const from = focused === -1 ? i : focused;
    const delta = e.key === "ArrowRight" ? 1 : -1;
    const to = Math.min(STEPS.length - 1, Math.max(0, from + delta));
    stepRefs.current[to]?.focus();
  }
  // Compare the previous index rather than a "have we mounted" flag: <StrictMode>
  // (main.jsx) double-invokes effects on mount with refs preserved, so a boolean reads
  // as already-mounted on the second pass and steals focus on first paint in dev.
  const prevStep = useRef(i);
  useEffect(() => {
    if (prevStep.current === i) return;
    prevStep.current = i;
    const card = cardRef.current;
    // A step that autoFocuses its own input has already placed focus better than we can;
    // stealing it back to the container would undo that.
    if (card && !card.contains(document.activeElement)) card.focus();
  }, [i]);

  // While offline, probe /api/version every 3s (fixed cadence, no escalation
  // — the endpoint is cheap) until the server answers, then bump reloadNonce
  // so the current step's loader effects re-run with live data. Retries
  // indefinitely; only going back online or unmounting stops them.
  useEffect(() => {
    if (!offline) return;
    const p = createServerProbe({
      probe: probeServer,
      onStatus: (s) => setProbing(s === "probing"),
      onReconnect: () => { setOffline(false); setProbing(false); setReloadNonce((n) => n + 1); },
    });
    probeRef.current = p;
    p.start();
    return () => { probeRef.current = null; p.stop(); };
  }, [offline]);
  const obBanner = offlineBanner({ offline, probing });

  // The single choke point every step loader's catch routes through. Returns
  // true when the throw was the server being gone, in which case the caller
  // must NOT also set its own per-step error state — the wizard-level banner
  // owns the failure and no step renders the exception text.
  function noteFetchFailure(e) {
    if (!isNetworkError(e)) return false;
    setOffline(true);
    return true;
  }

  async function guard(fn) {
    setBusy(true); setErr(null);
    try { await fn(); } catch (e) { if (!noteFetchFailure(e)) setErr(e.message); } finally { setBusy(false); }
  }

  // Scan ONE folder the user typed. The single scanner refuses a root outside
  // home server-side, and the response already carries roots_scanned/note/etc.
  // for this one root, so it replaces the whole discovery state directly — no
  // hand-built roots_scanned like the retired detect path needed.
  async function scanFolder(r) {
    if (!r || !r.trim()) return;
    setSearchedPath(r.trim());
    await guard(async () => {
      const res = await discoverRepos({ root: r.trim() });
      setDetected(res.repos || []);
      setDiscovery(res);
    });
  }
  // Typing a folder scans it (400 ms after the last keystroke) instead of
  // making the user reach for "Search". "Search" stays as the explicit trigger.
  const debouncedScan = useMemo(() => debounce((r) => scanFolder(r), 400), []);
  useEffect(() => () => debouncedScan.cancel(), [debouncedScan]);

  // Entering the repos step discovers the user's repositories across every
  // conventional clone root, so the first thing they see is their own list -
  // not an empty box asking for a path they have to remember.
  useEffect(() => {
    if (step.key === "repos" && discovery === null) {
      guard(async () => {
        const res = await discoverRepos();
        setDiscovery(res);
        setDetected(res.repos || []);
      });
    }
    if (step.key === "integrations" && integrations === null) {
      fetchIntegrationSetup()
        .then((r) => {
          const specs = r.integrations || [];
          setIntegrations(specs);
          setIntDraft(draftFrom(specs));
        })
        // On a network failure `integrations` must stay `null` (unloaded) so
        // the `integrations === null` guard above permits a refetch once
        // `reloadNonce` bumps on reconnect, instead of a permanent [].
        .catch((e) => { if (!noteFetchFailure(e)) setIntegrations([]); });
    }
    // Entering Projects with the add form pristine seeds its repo picker with
    // the repos selected on the repos step — the "one project, all my repos"
    // path is then a single Add, and the picker still lets you narrow it.
    if (step.key === "projects" && !newProjName.trim()) {
      setNewProjRepos(new Set(selectedRepos));
    }
    // deps intentionally partial (was: eslint-disable react-hooks/exhaustive-deps — plugin never loaded here)
  }, [step.key, reloadNonce]);

  function setIntField(name, field, value) {
    setIntDraft((d) => ({ ...d, [name]: { ...(d[name] || {}), [field]: value } }));
    setIntSaved((s) => (s === name ? null : s));
  }

  // Save ONE integration's non-secret settings. The response is the refreshed
  // spec, so the card's state (on/off, "needs LINEAR_API_KEY", "Ready") comes
  // straight from what actually landed in config.yaml — not from what was
  // typed.
  async function saveIntegration(spec) {
    const values = changedValues(spec, intDraft);
    if (Object.keys(values).length === 0) return;
    setIntSaving(spec.name);
    setIntError((e) => ({ ...e, [spec.name]: null }));
    try {
      const refreshed = await saveIntegrationSetup(spec.name, values);
      setIntegrations((all) => (all || []).map((s) => (s.name === spec.name ? refreshed : s)));
      setIntDraft((d) => ({ ...d, [spec.name]: draftFrom([refreshed])[spec.name] }));
      setIntSaved(spec.name);
      // A saved key is not a working connection: run the live test so "Ready"
      // means a test passed, not that a value was typed (fail-closed — a probe
      // that cannot reach the network reports not-healthy, never green).
      setIntTest((t) => ({ ...t, [spec.name]: { testing: true } }));
      try {
        const res = await testIntegration(spec.name);
        setIntTest((t) => ({ ...t, [spec.name]:
          { healthy: res.healthy === true, detail: res.detail || "" } }));
      } catch (te) {
        setIntTest((t) => ({ ...t, [spec.name]:
          { healthy: false, detail: te.message || "test failed" } }));
      }
    } catch (e) {
      setIntError((er) => ({ ...er, [spec.name]: e.message }));
    } finally {
      setIntSaving(null);
    }
  }

  function toggleRepo(path) {
    const removing = selectedRepos.has(path);
    setSelectedRepos((s) => {
      const n = new Set(s);
      n.has(path) ? n.delete(path) : n.add(path);
      return n;
    });
    // Deselecting a repo also unbinds it from every project. The projects step
    // renders a checkbox only for the repos still selected here, so a binding
    // left behind is one nothing on screen can show or undo — and it would
    // still be POSTed at launch.
    if (removing) setProjectDefs((d) => dropRepoEverywhere(d, path));
  }

  async function onboardSelected() {
    await guard(async () => {
      for (const path of selectedRepos) {
        // Skip repos already profiled — re-running (e.g. after adding one more
        // repo, or from the M1 Continue-registers path) must never reset a
        // repo the user has already proven back to just-profiled.
        if (onboarded[path]) continue;
        setOnboarded((o) => ({ ...o, [path]: "busy" }));
        const res = await onboardRepo(path);
        setOnboarded((o) => ({ ...o, [path]: res }));
      }
    });
  }

  // M1: on the Repositories step, advancing IS registering. If repos are ticked
  // but not yet profiled, profile them first so the server's registered-repo
  // store (which the Launch summary reads) reflects them — "Continue" used to
  // just next() and skip registration, so Launch reported "Repos 0" for a repo
  // the user had clearly added. "Skip setup — open the board" stays the
  // explicit no-profiling exit; every other step advances unchanged.
  async function advance() {
    // Re-entry guard (synchronous ref, not the wizard-wide `busy` flag — Continue
    // must stay live during the background scan, per onboardingNav). Stops a
    // double-click from profiling-then-advancing twice.
    if (advancing.current) return;
    advancing.current = true;
    try {
      if (step.key === "repos" && [...selectedRepos].some((p) => !onboarded[p])) {
        await onboardSelected();
      }
      next();
    } finally {
      advancing.current = false;
    }
  }

  // Abort any live prove stream when the wizard unmounts. The server-side run
  // is bounded independently; this only stops us reading it.
  useEffect(() => () => {
    Object.values(proveStreams.current).forEach((s) => { try { s.close(); } catch { /* already gone */ } });
  }, []);

  function patchProve(path, patch) {
    setProveState((s) => {
      const cur = s[path] || { lines: [], status: "idle" };
      return { ...s, [path]: { ...cur, ...(typeof patch === "function" ? patch(cur) : patch) } };
    });
  }

  // Run the repo's test command for real and stream what it prints. `testCmd`
  // is the human's correction after a failure — it is sent verbatim and the
  // server proves that exact string, never a tidied-up version of it.
  function startProve(path, testCmd) {
    const prev = proveStreams.current[path];
    if (prev) { try { prev.close(); } catch { /* already gone */ } }
    setProveState((s) => ({
      ...s,
      [path]: { status: "running", lines: [], elapsed: 0, attempted: testCmd || "" },
    }));
    // Tracked in the closure, not in state: the `done` frame needs the command
    // that was actually attempted, and reading state there would read a stale
    // render's copy of it.
    let attempted = testCmd || "";
    proveStreams.current[path] = proveRepoSSE(
      { repo_path: path, test_cmd: testCmd || undefined },
      (f) => {
        if (f.kind === "output") {
          patchProve(path, (cur) => ({ lines: [...cur.lines.slice(-300), f.line] }));
        } else if (f.kind === "heartbeat") {
          patchProve(path, { elapsed: f.elapsed });
        } else if (f.kind === "prove_start") {
          if (f.cmd_kind === "test") attempted = f.command;
          patchProve(path, (cur) => ({
            lines: [...cur.lines, `$ ${f.command}`],
            attempted: f.cmd_kind === "test" ? f.command : cur.attempted,
          }));
        } else if (f.kind === "rewritten") {
          // Honesty: run_tests has a bounded self-correcting retry. If the
          // string that actually exited clean differs, say so rather than let
          // the user confirm against a command they never saw run.
          patchProve(path, (cur) => ({
            lines: [...cur.lines, `note: the runner retried this as: ${f.actual_command}`],
          }));
        } else if (f.kind === "prove_result") {
          patchProve(path, (cur) => ({
            lines: [...cur.lines, `→ ${f.command} exited ${f.exit_code}`],
          }));
        } else if (f.kind === "done") {
          patchProve(path, { status: f.test_proven ? "proven" : "failed", result: f });
          setOnboarded((o) => ({ ...o, [path]: { ...(o[path] || {}), ...f } }));
          if (!f.test_proven) {
            setProveCmd((c) => ({ ...c, [path]: c[path] ?? attempted }));
          }
        }
      },
      (e) => patchProve(path, { status: "error", error: e.message }),
    );
  }

  // Stop reading a prove that is taking too long. The ONLY way out of a running
  // prove used to be reloading the page — which discards the whole wizard
  // (selections, docs, project definitions are all React state, none of it
  // persisted). Back to "idle", so the Prove/Retry button and the editable
  // command come back exactly as they were before the run.
  //
  // Same close() the unmount cleanup uses, and the same caveat: the server-side
  // run is bounded independently, so this stops us READING it, not the tests
  // themselves. The log says so rather than claiming a kill we did not perform.
  function stopProve(path) {
    const s = proveStreams.current[path];
    if (s) { try { s.close(); } catch { /* already gone */ } }
    delete proveStreams.current[path];
    patchProve(path, (cur) => ({
      status: "idle",
      lines: [...cur.lines, "— stopped watching (the run itself finishes on its own)"],
    }));
  }

  async function confirmProved(path) {
    await guard(async () => {
      const res = await confirmRepoProfile(path);
      setOnboarded((o) => ({ ...o, [path]: { ...(o[path] || {}), ...res } }));
      patchProve(path, { status: "confirmed", result: res });
    });
  }

  // no-human-67 follow-up: the wizard's one-action confirm for the
  // ui_evidence suggestion. The server re-derives the suggestion itself and
  // writes ui_evidence to both project.yml and the DB row — this only merges
  // the response's fresh ui_evidence block back into local state so the
  // suggestion disappears once accepted.
  async function enableUiEvidence(path) {
    await guard(async () => {
      const res = await setRepoUiEvidence(path, true);
      setOnboarded((o) => ({
        ...o,
        [path]: { ...(o[path] || {}), ui_evidence: res.ui_evidence },
      }));
    });
  }

  // The summary reads readiness from the server, not from local state, so it
  // cannot claim "Ready." on the strength of a click that did not stick.
  useEffect(() => {
    if (step.key !== "summary") return;
    let cancelled = false;
    fetchReadiness()
      .then((r) => { if (!cancelled) setReadiness(r); })
      .catch((e) => { if (cancelled) return; if (!noteFetchFailure(e)) setReadiness({ error: true }); });
    return () => { cancelled = true; };
    // deps intentionally partial (matches this file's existing convention)
  }, [step.key, reloadNonce]);

  // Tick/untick a repo for the project being composed in the add form.
  function toggleNewProjRepo(repoPath) {
    setNewProjRepos((s) => {
      const n = new Set(s);
      n.has(repoPath) ? n.delete(repoPath) : n.add(repoPath);
      return n;
    });
  }

  // Add ONE project at a time (F8): it takes the name AND the repos ticked in
  // the form (newProjRepos), lands in the created-projects list, and the form
  // resets to a fresh copy of the selected repos for the next one. A definition
  // is bound to exactly the repos the user chose FOR IT — not to every repo, and
  // not to nothing (the silent-discard bug; see onboardingProjects.js).
  function addProject() {
    const name = newProjName.trim();
    if (!name || projectDefs.some((p) => p.name === name)) return;
    setProjectDefs([...projectDefs, newProjectDef(name, newProjRepos)]);
    setNewProjName("");
    setNewProjRepos(new Set(selectedRepos));
  }

  function toggleProjectRepo(projIdx, repoPath) {
    setProjectDefs(projectDefs.map(
      (p, i) => (i === projIdx ? bindProjectRepo(p, repoPath) : p)));
  }

  function chooseProjectPrimary(projIdx, repoPath) {
    setProjectDefs(projectDefs.map(
      (p, i) => (i === projIdx ? setPrimaryRepo(p, repoPath) : p)));
  }

  function removeProject(projIdx) {
    setProjectDefs(projectDefs.filter((_, i) => i !== projIdx));
  }

  // The minimal path (spec §3 B1): end onboarding right after the Repositories
  // step. The server creates a project named after the repo and records the
  // remaining steps as deferred (POST with minimal:true) — so nothing is
  // half-created here, and the board's FinishSetupCard picks up the rest.
  async function startMinimal() {
    await guard(async () => {
      const repo_path = [...selectedRepos][0];
      if (!repo_path) return;
      await completeOnboarding({ completed: true, minimal: true, repo_path });
      // Land on the board with the Finish-setup card (spec §3 B1) rather than
      // popping the composer — the deferred steps are the point of this path.
      onComplete({});
    });
  }

  async function finish() {
    await guard(async () => {
      // A project that binds no repo used to be dropped here silently, while
      // the summary above went on counting it. Refuse, by name, before anything
      // is written — so nothing is half-created and the user is told which
      // definition is the problem.
      if (unbound.length) throw new Error(unboundProjectsMessage(unbound));
      // Create projects via API. primary_repo travels with the payload: it is
      // the project's default repo in the composer, and without it the server
      // falls back to whichever repo was ticked first.
      for (const pd of projectDefs) {
        try {
          await createProject(projectPayload(pd));
        } catch (e) {
          // 409 = already exists, skip.
          if (!e.message.includes("already exists")) throw e;
        }
      }
      await completeOnboarding({
        // team stays null on the free tier — the upgrade onboarding owns it.
        team: null,
        repos: [...selectedRepos],
      });
      // Wiki generation is background work the user is never asked about
      // (operator, 2026-09-04). Fire-and-forget: the POST returns 202 with a
      // job id and the server runs it detached, so a failure here is advisory
      // only and must never block or fail the launch.
      kickoffWikiGeneration({ repos: selectedRepos, generate: generateDocs });
      // Hand the ready repo up so the app can open the composer on it instead
      // of dropping the user onto an empty board.
      onComplete(firstTaskRepo ? { firstTaskRepo } : {});
    });
  }

  // B1: the profile result line + Prove panel render for BOTH the recent-repo
  // quick-add cards and the full-list rows. The repos a first-time user adds
  // are the ones they work on — i.e. recently modified — i.e. the ones that
  // render as cards; the card renderer never read onboarded[path]/proveState,
  // so "Profile" changed a card by nothing and Launch pointed at a "Prove test
  // command" button that only existed on the (30-days-untouched) list rows.
  const profileStatus = (st, path) => {
    if (st === "busy") return (
      <span className="ob-repo-status"><span className="grill-spinner" style={{ width: 12, height: 12, verticalAlign: 'middle', marginRight: 4 }} />profiling…</span>
    );
    if (st && st !== "busy") return (
      <>
        <span className={`ob-repo-status ${st.is_usable ? "ok" : "warn"}`}>
          {st.ecosystem || "unknown"}{st.test_cmd ? ` · ${st.test_cmd}` : ""}
          {" · "}
          {st.is_usable ? "proven & confirmed"
            : st.test_proven ? "proven — confirm to use"
            : "not proven yet"}
        </span>
        {/* no-human-67 follow-up: a one-action confirm for enabling
            visual-proof walks, shown only when the server detected an
            `npm run dev` convention and ui_evidence isn't already
            configured — "Not now" is a local dismiss, no API call. */}
        {st.ui_evidence?.suggestion && !uiEvidenceDismissed.has(path) && (
          <div className="ob-row ob-ui-evidence-row">
            <span className="ob-note">Enable visual-proof walks?</span>
            <button type="button" className="ob-btn-ghost" disabled={busy}
                    onClick={() => enableUiEvidence(path)}>
              Enable
            </button>
            <button type="button" className="ob-btn-ghost" disabled={busy}
                    onClick={() => setUiEvidenceDismissed((s) => new Set(s).add(path))}>
              Not now
            </button>
          </div>
        )}
      </>
    );
    return null;
  };
  const proveBlock = (r) => {
    const st = onboarded[r.path];
    if (!st || st === "busy") return null;
    return (
      <ProvePanel
        repoPath={r.path}
        profile={st}
        prove={proveState[r.path]}
        editedCmd={proveCmd[r.path]}
        onEditCmd={(v) => setProveCmd((c) => ({ ...c, [r.path]: v }))}
        onProve={(cmd) => startProve(r.path, cmd)}
        onStop={() => stopProve(r.path)}
        onConfirm={() => confirmProved(r.path)}
        busy={busy}
      />
    );
  };

  return (
    <div className="ob-shell">
      <div className="ob-stage">
        {/* The wizard replaces the whole app shell (App.jsx returns it before the shell's
            own h1 element), so without this the document had no h1 at all on seven of the eight
            steps — the step headings are h2. Visually hidden: the visible headline is the
            step's own. */}
        <h1 className="sr-only">Set up no_human</h1>
        {/* Clickable step indicator (spec §3 B2): each step is a button that jumps
            straight to that step — a real user asked not to click Back six times.
            No step gates another (onboardingNav.js), so the only lock is `busy`
            (the terminal launch). Roving tabIndex: only the current step is
            tabbable; ArrowLeft/ArrowRight move focus. aria-current still marks
            where you are; stepButtonLabel carries the state a screen reader needs. */}
        <div className="ob-stepper" role="list" aria-label="Setup progress" onKeyDown={onStepKeyDown}>
          {STEPS.map((s, idx) => (
            <div key={s.key} role="listitem">
              <button
                type="button"
                ref={(el) => { stepRefs.current[idx] = el; }}
                aria-current={idx === i ? "step" : undefined}
                aria-label={stepButtonLabel(s, idx, i, STEPS.length)}
                tabIndex={idx === i ? 0 : -1}
                disabled={!canJumpTo({ from: i, to: idx, busy })}
                className={`ob-step${idx === i ? " current" : ""}${idx < i ? " done" : ""}`}
                onClick={() => { if (canJumpTo({ from: i, to: idx, busy })) setI(idx); }}
              >
                <span className="ob-step-dot" />
                <span className="ob-step-label">{s.title}</span>
              </button>
            </div>
          ))}
        </div>

        {/* Incident 2026-09-04: the server died mid-wizard and every step showed
            its own raw "Failed to fetch" string. This banner is wizard-level
            (above the step card, not per-step) so it stays put across step
            navigation and reads as one outage, not N. */}
        {obBanner && (
          <div className={obBanner.className} role={obBanner.role}>
            <span>{obBanner.text}</span>
            <span className="ob-offline-hint">{obBanner.hint}</span>
            <button
              type="button"
              className="ob-offline-retry"
              onClick={() => probeRef.current?.retryNow()}
            >
              {obBanner.retryLabel}
            </button>
          </div>
        )}

        <div
          className="ob-card"
          key={step.key}
          ref={cardRef}
          // Focus target for the step change above. tabIndex -1 keeps it out of the tab
          // order — it is a landing spot, not a control.
          tabIndex={-1}
          role="group"
          aria-label={`Step ${i + 1} of ${STEPS.length}: ${step.title}`}
        >
          {step.key === "welcome" && (
            <Stagger>
              <div className="ob-brand"><LegionLogo size={44} /><span>no_human</span></div>
              {/* h2, not h1: the wizard's h1 is the hidden one on .ob-stage, so every step
                  now reads h1 → h2 instead of this step alone owning the h1 and the other
                  seven starting at h2. Renders identically — verified pixel-identical —
                  but NOT merely because `.ob-h1` is a class-only selector: Tailwind
                  preflight is off here, so UA margins would otherwise differ (h1 0.67em
                  vs h2 0.83em). What actually makes the swap safe is the universal
                  `*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }`
                  reset near the top of styles.css. If that reset is ever narrowed, this
                  headline needs an explicit margin-top. */}
              {/* BINDING slogan, operator-pinned, do-not-reword. Two sentences, verbatim;
                  the second is set smaller/secondary but the words are unchanged. */}
              <h2 className="ob-h1">
                From ticket to reviewed pull request.
                <span className="ob-h1-sub">Free and open-source, on your machine.</span>
              </h2>
              <p className="ob-lede">
                no_human fields an <em>entire team of specialized agents at once</em>. Each
                one is carried from intake to an open PR on its own: no babysitting, no
                re-explaining, no steering every step. You just review and approve.
              </p>
              <div className="ob-agents">
                <div className="ob-agent ob-agent-supervisor">
                  <div className="ob-agent-role">Supervisor</div>
                  <p>Watches in real time — enforces your rules, blocks guesses.</p>
                </div>
                <div className="ob-agent ob-agent-planner">
                  <div className="ob-agent-role">Planner</div>
                  <p>Turns your task into a plan before it touches code.</p>
                </div>
                <div className="ob-agent ob-agent-agent">
                  <div className="ob-agent-role">Coder</div>
                  <p>Builds the change, writes the tests, and runs them.</p>
                </div>
                <div className="ob-agent ob-agent-reviewer">
                  <div className="ob-agent-role">Reviewer</div>
                  <p>Fresh eyes, told to refute “done.” Pass/fail with evidence.</p>
                </div>
                <div className="ob-agent ob-agent-watcher">
                  <div className="ob-agent-role">Watcher</div>
                  <p>After the PR opens — watches CI, fixes reds, folds in your review comments.</p>
                </div>
                <div className="ob-agent ob-agent-worker">
                  <div className="ob-agent-role">Orchestrator</div>
                  <p>Schedules each attempt, checkpoints progress, and enforces the bounded retry loop — no runaway sessions.</p>
                </div>
              </div>
              <div className="ob-flow">
                <span>intake</span>
                <span>spec</span>
                <span>plan</span>
                <span>build</span>
                <span>review</span>
                <span className="ob-flow-pr">open PR</span>
                <span className="ob-flow-you">you approve</span>
              </div>
            </Stagger>
          )}

          {step.key === "repos" && (
            <Stagger>
              <h2 className="ob-h2">Which repositories do you work on?</h2>
              <p className="ob-sub">
                {/* Home is ALWAYS searched at depth 1 (that is how repos directly
                    under $HOME are found — carried by discovery.home_direct, never
                    in roots_scanned), so say so: a user with repos under $HOME saw
                    only "…/git" and thought home was skipped. Name the roots, don't
                    count them — a count once read as "broken page" (wrong directory). */}
                {discovery?.roots_scanned?.length
                  ? `Searched your home folder and ${discovery.roots_scanned.join(", ")}.`
                  : "Searched your home folder and the usual clone roots (~/git, ~/code, …)."}
              </p>
              <div className="ob-row">
                <button className="ob-btn-ghost" disabled={busy}
                        onClick={() => guard(async () => {
                          const res = await discoverRepos();
                          setDiscovery(res);
                          setDetected(res.repos || []);
                        })}>
                  Re-scan
                </button>
                <button className="ob-btn-ghost" type="button"
                        aria-expanded={manualScan}
                        onClick={() => setManualScan((v) => !v)}>
                  {manualScan ? "Hide folder search" : "Search another folder"}
                </button>
              </div>
              {manualScan && (
                <div className="ob-row">
                  {/* Typing a folder scans it (debounced); "Search" is the
                      explicit trigger for the impatient. Both go through the one
                      scanner, which refuses a root outside home server-side. */}
                  <PathInput value={root}
                             onChange={(v) => { setRoot(v); debouncedScan(v); }}
                             placeholder="folder to search, e.g. ~/work"
                             onNetworkError={noteFetchFailure} />
                  <button className="ob-btn-ghost" disabled={busy || !root.trim()}
                          onClick={() => { debouncedScan.cancel(); scanFolder(root); }}>
                    Search
                  </button>
                </div>
              )}
              {manualScan && (
                <p className="ob-faint ob-scan-hint" style={{ margin: '0.35rem 0 0', fontSize: '0.8rem' }}>
                  Don’t see a repo above? The auto-scan skips <strong>Documents, Desktop and Downloads</strong> by default and stops a few folders deep — type its folder here (e.g. <code>~/Documents/my-app</code>) and it will be scanned.
                </p>
              )}
              {recentRepos.length > 0 && (
                <>
                  <p className="ob-recent-label">Recently worked on</p>
                  <div className="ob-repo-cards ph-no-capture">
                    {recentRepos.map((r) => {
                      const sel = selectedRepos.has(r.path);
                      return (
                        <div key={r.path} className={`ob-repo-card${sel ? " sel" : ""}`}>
                          <div className="ob-repo-card-name">{r.name}</div>
                          <div className="ob-repo-card-path">{r.path}</div>
                          <div className="ob-repo-card-meta">{relativeMtime(r.mtime, nowEpoch)}</div>
                          {/* Same handler as the checkbox below: Add just ticks
                              the repo. aria-label carries the repo name so a
                              screen reader announces which one. */}
                          <button type="button" className="ob-repo-card-add"
                                  aria-label={`${sel ? "Remove" : "Add"} ${r.name}`}
                                  onClick={() => toggleRepo(r.path)}>
                            {sel ? "Added ✓" : "Add"}
                          </button>
                          {/* B1: once profiled, the card expands to show its
                              profile result and the Prove panel — the same
                              capability the list rows have. */}
                          {profileStatus(onboarded[r.path], r.path)}
                          {proveBlock(r)}
                        </div>
                      );
                    })}
                  </div>
                </>
              )}
              <div className="ob-repolist ph-no-capture">
                {detected.length === 0 && <div className="ob-empty">{busy
                  ? <><span className="grill-spinner" style={{ width: 16, height: 16, verticalAlign: 'middle', marginRight: 8 }} />{searchedPath ? `Searching ${searchedPath}…` : "Looking for your repositories…"}</>
                  // An empty result reads as "broken" without saying WHAT was
                  // searched. Name the folder the user actually pointed at.
                  : searchedPath ? `Searched ${searchedPath} — no git repositories there.` : "No repositories found. Search another folder above."}</div>}
                {restRepos.map((r) => {
                  const st = onboarded[r.path];
                  return (
                    <div key={r.path} className="ob-repo-row ph-no-capture">
                    <label className={`ob-repo${selectedRepos.has(r.path) ? " sel" : ""}`}>
                      <input type="checkbox" checked={selectedRepos.has(r.path)} onChange={() => toggleRepo(r.path)} />
                      <span className="ob-repo-name">{r.name}</span>
                      {repoBadges(r).map((b) => (
                        <span key={b.key}
                              className={b.tone === "warn" ? "ob-tag ob-tag-warn" : "ob-tag"}>
                          {b.text}
                        </span>
                      ))}
                      {ambiguousRepoNames.has(rowName(r)) && (
                        <span className="ob-repo-path">{r.path}</span>
                      )}
                      {profileStatus(st, r.path)}
                    </label>
                    {proveBlock(r)}
                    </div>
                  );
                })}
              </div>
              {discoveryMessage(discovery) && (
                <p className="ob-note">{discoveryMessage(discovery)}</p>
              )}
              {selectedRepos.size > 0 && (
                <div className="ob-row ob-minimal-row">
                  {/* M1: Continue (the one primary) now profiles + advances, so
                      this is a SECONDARY action — profile the selected repos
                      HERE to see each result and prove its test command in-step
                      before moving on. Kept as ghost so only Continue is a
                      primary on the screen. */}
                  <button className="ob-btn-ghost" disabled={busy} onClick={onboardSelected}>
                    {busy ? "Profiling…" : `Profile ${selectedRepos.size} repo${selectedRepos.size > 1 ? "s" : ""} here`}
                  </button>
                  {/* Minimal path (spec §3 B1): a real user wanted to start after
                      one repo instead of six more steps. The server creates the
                      project and defers the rest; the board carries them. Kept
                      visually SECONDARY (ghost) with copy that says it ENDS setup
                      now — the old "Start with this repo" gave no such hint and
                      read as a mystery that finished onboarding. */}
                  <div className="ob-minimal-skip">
                    <button className="ob-btn-ghost" disabled={busy || !canStartMinimal({ selectedRepos })} onClick={startMinimal}>
                      {busy ? "Starting…" : "Skip setup — open the board"}
                    </button>
                    <span className="ob-note ob-minimal-hint">Uses this repo; add integrations, docs &amp; rules later from Settings.</span>
                  </div>
                </div>
              )}
              {/* m2: this profiling/proving explainer is only actionable once a
                  repo is selected — showing it before the user picks anything
                  is a dense caveat about a feature they haven't reached. */}
              {selectedRepos.size > 0 && (
                <p className="ob-note">
                  Profiling reads each repo's own declarations to find its install/test/lint
                  commands. Proving RUNS the test command here and shows you the output —
                  nothing is trusted until it exits clean. You can skip proving and come back
                  to it from the board, but until a repo is proven its tasks run with no test
                  command, so the review gate has no tests to execute.
                </p>
              )}
            </Stagger>
          )}

          {step.key === "projects" && (() => {
            // The repos the add form offers: the ones picked on the repos step
            // (the natural scope), falling back to every discovered repo when the
            // user selected none — so a repo-less run is never a dead-end.
            const pickerRepos = selectedRepos.size ? [...selectedRepos] : detected.map((r) => r.path);
            return (
            <Stagger>
              <h2 className="ob-h2">Group repos into projects</h2>
              <p className="ob-sub">A project is a named unit of work — it can span multiple repos. When you create a task, you pick a project. Add one at a time: name it, pick its repos, then Add.</p>

              {/* ONE add form (F8): name + this project's repos, then Add. Each
                  Add drops the project into the list below and clears the form —
                  no more one-full-repo-checklist-per-project. */}
              <div className="ob-project-card ph-no-capture" style={{ marginBottom: '0.9rem' }}>
                <div className="ob-row">
                  <input className="ob-input" value={newProjName} placeholder="Project name, e.g. checkout-api"
                         onChange={(e) => setNewProjName(e.target.value)}
                         onKeyDown={(e) => e.key === "Enter" && addProject()} />
                  <button className="ob-btn-ghost" onClick={addProject} disabled={!newProjName.trim()}>+ Add project</button>
                </div>
                {/* M2: a typed-but-unadded name is easy to lose — the compose
                    form pre-checks a repo and says "N selected", which reads as
                    "ready", but a project only exists after + Add project.
                    Continue discards the draft silently without this. */}
                {newProjName.trim() && (
                  <div className="ob-note" role="status" style={{ margin: '0.4rem 0 0' }}>
                    “{newProjName.trim()}” isn’t added yet — click <strong>+ Add project</strong> to keep it. Continuing without it discards the draft.
                  </div>
                )}
                <div className="ob-faint" style={{ margin: '0.5rem 0 0.25rem', fontSize: '0.8rem' }}>
                  Repos for this project <span className="ph-no-capture">({newProjRepos.size} selected)</span>
                </div>
                {pickerRepos.length === 0 ? (
                  <div className="ob-empty">
                    No repositories selected yet —{" "}
                    <button type="button" className="ob-btn-ghost ob-add-repos-jump"
                            onClick={() => setI(STEPS.findIndex((s) => s.key === "repos"))}>
                      Add repositories
                    </button>
                    {" "}to pick some, or add projects later in Settings.
                  </div>
                ) : (
                  <div className="ob-repolist ph-no-capture" style={{ maxHeight: '160px' }}>
                    {pickerRepos.map((rp) => (
                      <label key={rp} className={`ob-repo${newProjRepos.has(rp) ? " sel" : ""}`} style={{ padding: '0.25rem 0.5rem' }}>
                        <input type="checkbox" checked={newProjRepos.has(rp)} onChange={() => toggleNewProjRepo(rp)} />
                        <span className="ob-repo-name">{repoName(rp)}</span>
                      </label>
                    ))}
                  </div>
                )}
              </div>

              {/* The created projects — a COMPACT list, never an expanded
                  checklist each. Name, its repo count, a default-repo picker when
                  it spans more than one, and a remove. */}
              {/* M2: don't say "No projects yet" while the user is mid-compose
                  (a name typed, or repos pre-checked in the form above) — that
                  juxtaposition read as "my project vanished". Show it only when
                  the form is pristine. */}
              {projectDefs.length === 0 && !newProjName.trim() && newProjRepos.size === 0 && <div className="ob-empty">No projects yet — this step is optional. You can add one above or later in Settings.</div>}
              {projectDefs.map((pd, pi) => (
                <div key={pd.name} className="ob-project-card ph-no-capture" style={{ marginBottom: '0.5rem' }}>
                  <div className="ob-project-head">
                    <strong className="ph-no-capture">{pd.name}</strong>
                    <span className="ob-faint">{pd.repos.size} repo{pd.repos.size !== 1 ? "s" : ""}</span>
                    {/* "One repo should be the default but configurable" — the
                        default is the project's primary_repo, which becomes the
                        repo a task on this project runs in. */}
                    {pd.repos.size > 1 && (
                      <label className="ob-faint" style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', marginLeft: 'auto', fontSize: '0.8rem' }}>
                        Default
                        <select
                          className="ob-input" style={{ padding: '0.2rem 0.4rem', fontSize: '0.8rem' }}
                          value={pd.primary || ""}
                          onChange={(e) => chooseProjectPrimary(pi, e.target.value)}
                        >
                          {[...pd.repos].map((rp) => (
                            <option key={rp} value={rp}>{repoName(rp)}</option>
                          ))}
                        </select>
                      </label>
                    )}
                    <button className="ob-btn-ghost" style={{ marginLeft: pd.repos.size > 1 ? '0.4rem' : 'auto', fontSize: '0.75rem' }}
                            aria-label={`Remove project ${pd.name}`} onClick={() => removeProject(pi)}>✕</button>
                  </div>
                  {/* A project can only reach this list with the repos it was
                      created with; a zero-repo one is still refused, named here
                      and again at launch (the wizard used to print "Projects 1"
                      and create none). */}
                  {pd.repos.size === 0 && (
                    <div className="ob-empty" style={{ marginTop: '0.4rem' }}>
                      No repos — {pd.name} cannot be created until it has at least one. Remove it, or re-add it with a repo ticked.
                    </div>
                  )}
                </div>
              ))}
              {/* The refusal that disables Continue, said on this step rather than
                  only at launch (spec §3 B2). Fires only for a project that exists
                  with zero repos — never for "no projects", which is a valid state. */}
              {projectsBlockMsg && (
                <div className="ob-empty" role="status" style={{ marginTop: '0.6rem' }}>{projectsBlockMsg}</div>
              )}
              <p className="ob-note">You can add or edit projects later in Settings. Repos not assigned to any project can still be used via the "other" option when creating a task.</p>
            </Stagger>
            );
          })()}

          {step.key === "integrations" && (
            <Stagger>
              <h2 className="ob-h2">Connect your tools <span className="ob-faint">(optional)</span></h2>
              <p className="ob-sub">Turn on the ones you use and fill in their settings — this writes straight to your config. <strong>Keys and tokens are never taken here:</strong> each card names the <code>~/.no_human/.env</code> variable its credential belongs in, because config.yaml is world-readable. You can skip this and keep going.</p>
              <p className="ob-note">GitHub, GitLab and CI are configured per repository from its profile — there is nothing to enter for them here.</p>
              {integrations === null ? (
                <div className="ob-empty">Checking integrations…</div>
              ) : integrations.length === 0 ? (
                <div className="ob-empty">No integrations available.</div>
              ) : (
                <div className="ob-integrations">
                  {integrations.map((spec) => (
                    <IntegrationSetupCard
                      key={spec.name}
                      spec={spec}
                      draft={intDraft}
                      saving={intSaving === spec.name}
                      saved={intSaved === spec.name}
                      error={intError[spec.name]}
                      test={intTest[spec.name]}
                      onField={setIntField}
                      onSave={() => saveIntegration(spec)}
                      expanded={openSetup.has(spec.name)}
                      onToggleOpen={() => toggleSetupOpen(spec.name)}
                    />
                  ))}
                </div>
              )}
              <p className="ob-note">Everything here is also in Settings → Integrations, which additionally handles credentials and can test a live connection.</p>
            </Stagger>
          )}

          {step.key === "summary" && (
            <Stagger>
              {/* The headline states what is TRUE, not what we hope. "Ready."
                  used to print regardless of whether a single repo had a test
                  command anything could run. */}
              <h2 className="ob-h2">
                {readiness === null ? "Checking…"
                  : readiness.usable > 0 ? "Ready."
                  : "Almost ready."}
              </h2>
              {/* Both repo rows below come from summaryRepoCounts(readiness) —
                  the server's registered-repo store — not from selectedRepos
                  (this mount's checkboxes). They used to disagree: "Repos: 0"
                  beside "0 of 1" for the same registered repo whenever the
                  ticks didn't match what was actually persisted (reload,
                  re-run, reset). The launch payload below still POSTs
                  [...selectedRepos] — that is a separate, intentional
                  question ("what did THIS run of the wizard tick") and is out
                  of scope here; don't "fix" it back to selectedRepos.size. */}
              {(() => {
                const counts = summaryRepoCounts(readiness);
                return (
                  <ul className="ob-summary">
                    <li>
                      <span>Projects</span>
                      <b>{projectDefs.length}{unbound.length ? ` · ${unbound.length} with no repos` : ""}</b>
                    </li>
                    <li><span>Repos</span><b>{counts.repos}</b></li>
                    <li>
                      <span>Repos with a proven test command</span>
                      <b>{counts.proven}</b>
                    </li>
                    {/* Counted from the SERVER's refreshed specs, so this line
                        agrees with what is actually in config.yaml — "on" is the
                        switch, "ready" additionally has its credential and its
                        settings. Never claims more than the config supports.
                        `integrations` here is GET /api/integrations/setup,
                        which is deliberately scoped to the config-block
                        integrations (jira/linear/monday/slack/teams — the
                        issue_tracker + notifications kinds from
                        integrationChip.js's KIND_LABEL). github/gitlab/
                        jenkins/circleci (vcs + ci kinds) are configured
                        per-repo under `ci.*`, not here, and render in Settings
                        → Integrations instead — see Integrations.jsx. The
                        label below says "Tracker & notification", not
                        "Integrations", so this row never reads as the
                        product's full integration count. */}
                    <li>
                      <span>Tracker &amp; notification integrations on</span>
                      <b>{integrations === null
                        ? "—"
                        : `${setupSummary(integrations).on} of ${setupSummary(integrations).total}` +
                          (setupSummary(integrations).ready < setupSummary(integrations).on
                            ? ` · ${setupSummary(integrations).ready} ready`
                            : "")}</b>
                    </li>
                  </ul>
                );
              })()}
              {readiness && !readiness.error && readiness.usable === 0 && (
                <p className="ob-prove-verdict bad">
                  No repo has a proven test command yet. Your tasks will still run — but
                  their review gate will have no tests to execute, which is most of what
                  makes a result trustworthy. Go back to Repositories and hit
                  “Prove test command”, or do it any time from the board.
                </p>
              )}
              {readiness && !readiness.error && readiness.usable > 0
                && readiness.needs_proving.length > 0 && (
                <p className="ob-note">
                  {readiness.needs_proving.length} other {readiness.needs_proving.length === 1 ? "repo" : "repos"} still
                  {" "}{readiness.needs_proving.length === 1 ? "has no" : "have no"} proven test command — the board will show you.
                </p>
              )}
              {/* Per-step readiness with a "Fix →" that jumps straight to the
                  unmet step (spec §3 B2) — instead of the old "go Back to
                  Repositories" prose that made the user count steps. */}
              {(() => {
                const rows = launchReadiness({ projects: projectDefs, selectedRepos, deferred: [] });
                const unmet = rows.filter((r) => !r.ok);
                if (!unmet.length) return null;
                return (
                  <ul className="ob-readiness">
                    {unmet.map((r) => (
                      <li key={r.step}>
                        <span className="ob-readiness-msg">{r.message}</span>
                        <button type="button" className="ob-btn-ghost ob-readiness-fix" onClick={() => setI(r.jumpTo)}>Fix →</button>
                      </li>
                    ))}
                  </ul>
                );
              })()}
              <p className="ob-note">You can change any of this later in Settings.</p>
            </Stagger>
          )}

          {err && <div className="ob-error">{err}</div>}
        </div>

        <div className="ob-nav">
          {/* `busy` is one flag for every awaited call in the wizard, so gating Back
              on it made Back dead for the whole background repo scan that starts the
              instant the repos step opens — the defect the first external tester
              reported. onboardingNav.js explains why leaving that scan is free and
              why only the terminal launch keeps the lock. */}
          <button className="ob-btn-ghost" onClick={back}
                  disabled={backDisabled(navState)}
                  title={backDisabledReason(navState) || undefined}>Back</button>
          {/* Visible, not just a title= — browsers suppress tooltips on disabled
              controls, so a title alone would be an explanation nobody ever sees. */}
          {backDisabledReason(navState) && (
            <span className="ob-nav-note" role="status">{backDisabledReason(navState)}</span>
          )}
          <div className="ob-nav-spacer" />
          {i < STEPS.length - 1 ? (
            <button className="ob-btn" onClick={advance} disabled={forwardDisabled(navState) || continueBlocked}>Continue</button>
          ) : (
            // Onboarding used to end on an empty board. When a repo is actually
            // ready, end on the thing the whole setup was for.
            <button className="ob-btn ob-btn-go" onClick={finish} disabled={forwardDisabled(navState)}>
              {busy ? "Launching…"
                : firstTaskRepo ? `Create your first task in ${repoName(firstTaskRepo)}`
                : "Enter no_human"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** Prove one repo's test command, live.
 *
 * Three outcomes, all of them dead ends before this existed:
 *  - passes  -> "Use this repo" (the human gate; the server still re-checks)
 *  - fails   -> the output IS the explanation, and the command is editable so
 *               the user can correct it and retry instead of being stuck
 *  - skipped -> allowed; the repo simply runs without a proven test command
 *               until it is proved here or via `nh onboard <repo> --confirm`
 *
 * A fourth state used to have no exit at all: while it RUNS, this panel showed a
 * spinner and nothing else, so a monorepo's 20-minute suite left reloading the
 * page (which discards the whole wizard) as the only way on. `onStop` is that
 * exit.
 */
export function ProvePanel({ repoPath, profile, prove, editedCmd, onEditCmd,
                             onProve, onStop, onConfirm, busy }) {
  const logRef = useRef(null);
  const status = prove?.status || "idle";
  const lines = prove?.lines || [];

  // Follow the tail as output arrives; a log that does not scroll reads as hung.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length]);

  if (profile?.is_usable) {
    return (
      <div className="ob-prove ok">
        <span className="ob-prove-verdict ok">✓ Proven and confirmed</span>
        <code className="ob-prove-cmd">{profile.test_cmd}</code>
        <span className="ob-note"> — this command really ran and exited clean, so the review gate can run it.</span>
      </div>
    );
  }

  return (
    <div className={`ob-prove ${status}`}>
      <div className="ob-row">
        {status !== "running" && (
          <button className="ob-btn-ghost" type="button" disabled={busy}
                  onClick={() => onProve(editedCmd || undefined)}>
            {status === "idle" ? "Prove test command" : "Retry"}
          </button>
        )}
        {status === "running" && (
          <>
            <span className="ob-prove-verdict">
              <span className="grill-spinner" style={{ width: 12, height: 12, verticalAlign: "middle", marginRight: 6 }} />
              Running the tests…{prove?.elapsed ? ` ${prove.elapsed}s` : ""}
            </span>
            {onStop && (
              <button className="ob-btn-ghost" type="button" onClick={onStop}
                      title="Stop watching this run and go back to the command">
                Stop
              </button>
            )}
          </>
        )}
        {status === "proven" && (
          <button className="ob-btn" type="button" disabled={busy} onClick={onConfirm}>
            Use this repo
          </button>
        )}
        {status === "idle" && (
          <span className="ob-note">Runs {profile?.test_cmd ? <code>{profile.test_cmd}</code> : "the derived test command"} for real. This can take a few minutes.</span>
        )}
      </div>

      {lines.length > 0 && (
        <pre className="ob-prove-log" ref={logRef} aria-label="Test output"
             aria-live="polite" aria-busy={status === "running"}>
          {lines.join("\n")}
        </pre>
      )}

      {status === "proven" && (
        <p className="ob-prove-verdict ok">
          ✓ Passed. Confirm to let this command back the review gate.
        </p>
      )}

      {status === "failed" && (
        <>
          <p className="ob-prove-verdict bad">
            ✗ That command did not exit clean, so it is not proven. Nothing was faked —
            fix the command (or the repo) and retry.
          </p>
          <div className="ob-row">
            <input className="ob-input" value={editedCmd || ""}
                   aria-label="Test command to prove"
                   placeholder="the command that runs this repo's tests"
                   onChange={(e) => onEditCmd(e.target.value)} />
            <button className="ob-btn-ghost" type="button"
                    disabled={busy || !(editedCmd || "").trim()}
                    onClick={() => onProve(editedCmd.trim())}>
              Run this command
            </button>
          </div>
          <p className="ob-note">
            The exact string you type is what gets run and what gets recorded — it is
            never adjusted for you. You can also skip and continue; the board will
            remind you.
          </p>
        </>
      )}

      {status === "error" && (
        <p className="ob-prove-verdict bad ph-no-capture">Could not run the proof: {prove.error}</p>
      )}
    </div>
  );
}

// One integration's card in the "Connect your tools" step.
//
// Entirely generated from the server's spec: the on/off switch is whatever
// `enable_field` names, and every other control comes from `fields`, whose
// `kind` (bool | text | list) decides the input. Nothing here names an
// integration, so a new config block renders with no edit to this file.
//
// The credential rule is structural, not a convention: there is no branch
// that renders an input for a secret, because the API never describes one.
// Credentials are stated as env-var NAMES via secretHint().
function IntegrationSetupCard({ spec, draft, saving, saved, error, test, onField, onSave, expanded, onToggleOpen }) {
  const values = draft[spec.name] || {};
  // Verified this session (a passing /test) OR persisted on the server
  // (spec.verified from integrations.<name>.last_verified_at). Fail-closed:
  // while a test is in flight, or after a failing one, it is NOT verified.
  const verified = test && test.testing !== true
    ? test.healthy === true
    : (test && test.testing ? false : Boolean(spec.verified));
  // M3: pass the draft `values` so the status chip reflects the just-ticked
  // Enable immediately (flips "Off" → "On — needs settings"), instead of
  // lagging the checkbox until Save. The Launch summary (setupSummary) still
  // reads saved state, which is correct there.
  const ready = readiness(spec, { verified, values });
  const enableField = spec.enable_field;
  // A mute switch that ships ON (e.g. teams) reads as effectively off until
  // the integration is configured — see effectiveEnabled(). Unchecking this
  // box on a fresh install writes nothing (changedValues sees no diff from
  // the stored default), so a later-pasted webhook still fires; this is
  // presentation only.
  const isOn = effectiveEnabled(spec, values);
  const dirty = Object.keys(changedValues(spec, draft)).length > 0;
  const settings = (spec.fields || []).filter((f) => f.name !== enableField);
  const label = NAME_LABEL[spec.name] || spec.name;
  // Open the setup body when the switch is on OR the user has explicitly
  // expanded this card (F9). The explicit-open path is what rescues a mute
  // switch like teams, whose switch reads OFF-but-unconfigured and can't be
  // toggled ON in the UI: without it, clicking teams did nothing and its
  // webhook setup note was unreachable.
  const showBody = isOn || expanded;

  return (
    <div className={`ob-integration-card${isOn ? " on" : ""}`}>
      <div className="ob-integration">
        <span className="ob-integration-mark"><IntegrationIcon name={spec.name} size={20} /></span>
        <span className="ob-integration-name">{label}</span>
        <span className="ob-integration-kind">{KIND_LABEL[spec.kind] || spec.kind}</span>
        <span className={`integration-chip tone-${ready.tone}`}>{ready.label}</span>
        {enableField && (
          <label className="ob-integration-switch">
            <input
              type="checkbox"
              checked={isOn}
              // Flipping the switch also flips this card open/closed, so a mute
              // switch reveals its setup on click the way every other card does.
              onChange={(e) => { onField(spec.name, enableField, e.target.checked); onToggleOpen?.(); }}
            />
            <span>{switchLabel(spec, label)}</span>
          </label>
        )}
      </div>

      {showBody && (
        <div className="ob-integration-body">
          {settings.length > 0 && (
            <div className="ob-integration-fields">
              {settings.map((f) => {
                const id = `ob-int-${spec.name}-${f.name}`;
                const hId = f.help ? fieldHintId(spec.name, f.name) : null;
                if (f.kind === "bool") {
                  return (
                    <label className="ob-integration-check" key={f.name} htmlFor={id}>
                      <input id={id} type="checkbox"
                             checked={values[f.name] === true}
                             aria-describedby={hId || undefined}
                             onChange={(e) => onField(spec.name, f.name, e.target.checked)} />
                      <span>{f.label}</span>
                      {f.help && <FieldHint id={hId} text={f.help} url={f.help_url} />}
                    </label>
                  );
                }
                return (
                  <div className="ob-integration-field" key={f.name}>
                    <label className="ob-integration-label" htmlFor={id}>{f.label}</label>
                    {f.kind === "repo_select" ? (
                      // "Run tasks in repo": a dropdown over the operator's
                      // registered repo profiles, so a pulled-in ticket can only
                      // ever name a repo no_human knows (validated server-side).
                      <select
                        id={id}
                        className="ob-input"
                        aria-describedby={hId || undefined}
                        value={values[f.name] ?? ""}
                        onChange={(e) => onField(spec.name, f.name, e.target.value)}
                      >
                        <option value="">— none —</option>
                        {(f.options || []).map((opt) => (
                          <option key={opt} value={opt}>{opt}</option>
                        ))}
                      </select>
                    ) : (
                      <input
                        id={id}
                        className="ob-input"
                        type="text"
                        spellCheck={false}
                        // No password input exists on this card by construction:
                        // the API never advertises a credential field here.
                        autoComplete="off"
                        aria-describedby={hId || undefined}
                        value={f.kind === "list" ? listToText(values[f.name]) : (values[f.name] ?? "")}
                        onChange={(e) => onField(spec.name, f.name, e.target.value)}
                      />
                    )}
                    {f.help && <FieldHint id={hId} text={f.help} url={f.help_url} />}
                  </div>
                );
              })}
            </div>
          )}

          <p className="ob-integration-secret">
            <span aria-hidden="true">🔑</span> {secretHint(spec)}
          </p>

          <div className="ob-integration-actions">
            <button className="ob-btn" disabled={saving || !dirty} onClick={onSave}>
              {saving ? "Saving…" : "Save"}
            </button>
            {saved && !error && <span className="ob-integration-ok">✓ Saved to config.yaml</span>}
            {error && <span className="ob-integration-err">Couldn't save — {error}</span>}
          </div>
          {test && test.testing && (
            <p className="ob-integration-testing">Testing connection…</p>
          )}
          {test && test.testing !== true && (
            <p className={test.healthy ? "ob-integration-ok" : "ob-integration-err"}>
              {test.healthy ? "✓ Connected" : "✗ Not verified"}
              {test.detail ? ` — ${test.detail}` : ""}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function Stagger({ children }) {
  return (
    <div className="ob-stagger">
      {Array.isArray(children)
        ? children.map((c, idx) => <div key={idx} className="ob-rise" style={{ animationDelay: `${idx * 60}ms` }}>{c}</div>)
        : children}
    </div>
  );
}
