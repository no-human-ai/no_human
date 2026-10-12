// The projects step of the first-run wizard: how a project definition binds
// repos, which of them is the default, and what the wizard refuses to do with a
// definition that binds none.
//
// BUG (first external DMG tester): finished onboarding, landed on a board
// reading "Projects 0 / Repos 1". The wizard's own launch summary had said
// "Projects 1" one screen earlier.
//
// Root cause, and it is upstream of the skip: `addProject()` built every new
// definition as `{ name, repos: new Set() }` — EMPTY by construction. Binding a
// repo was a separate act (ticking a checkbox in the card), so the obvious
// sequence "type a name → + Add project → Continue" produced a definition with
// no repos every time. `finish()` then read `if (repoPaths.length > 0)` and
// dropped it with no request, no error and no message, while the summary step
// went on counting `projectDefs.length`. Two independent halves of the same
// defect: a default that is wrong, and a silent discard that hides it.
//
// Both are fixed here rather than in the JSX:
//   * `newProjectDef` seeds the definition with the repos the user just
//     onboarded — visibly, as ticked checkboxes they can untick — instead of
//     with nothing.
//   * `unboundProjects` names any definition that still binds nothing, so the
//     wizard can refuse out loud instead of discarding in silence.
//
// A second tester request lives here too: "one repo should be the default but
// configurable". The default repo of a project is its `primary_repo` — the
// column exists, TaskComposer already reads it, and POST /api/projects already
// accepts it, but onboarding never sent one, so the server fell back to
// repo_paths[0]: whichever checkbox happened to be ticked first. `primary` is
// now a tracked, choosable part of the definition and `projectPayload` sends it.

/** A new project definition, pre-bound to `repos` (the repos onboarded in the
 *  previous step). The first is the default/primary; both are editable in the
 *  card before launch. */
export function newProjectDef(name, repos = []) {
  const bound = [...repos];
  return { name, repos: new Set(bound), primary: bound.length ? bound[0] : null };
}

/** Bind/unbind one repo. Unbinding the primary re-points it at another bound
 *  repo rather than leaving a primary the project no longer contains. */
export function toggleProjectRepo(pd, repoPath) {
  const repos = new Set(pd.repos);
  if (repos.has(repoPath)) repos.delete(repoPath);
  else repos.add(repoPath);
  let primary = pd.primary;
  if (primary === null || !repos.has(primary)) {
    const rest = [...repos];
    primary = rest.length ? rest[0] : null;
  }
  return { ...pd, repos, primary };
}

/** Choose the default repo. A repo the project does not bind cannot be its
 *  primary — silently accepting one would put a project in the database whose
 *  primary is not in its own repo list. */
export function setPrimaryRepo(pd, repoPath) {
  if (!pd.repos.has(repoPath)) return pd;
  return { ...pd, primary: repoPath };
}

/** Unbind `repoPath` from every definition. The projects step lists only the
 *  repos still selected on the repos step, so a definition holding a deselected
 *  one would bind it INVISIBLY — no checkbox to see it by, no way to untick it,
 *  and it would still travel in the POST at launch. Called when a repo is
 *  deselected; idempotent, so a double-invoked React updater is harmless. */
export function dropRepoEverywhere(defs, repoPath) {
  return (defs || []).map(
    (pd) => (pd.repos.has(repoPath) ? toggleProjectRepo(pd, repoPath) : pd));
}

/** Names of the definitions that bind no repo — the ones `finish()` used to
 *  drop without saying so. */
export function unboundProjects(defs) {
  return (defs || []).filter((p) => p.repos.size === 0).map((p) => p.name);
}

/** What to tell the user instead of discarding those definitions. Empty string
 *  when there is nothing to refuse. */
export function unboundProjectsMessage(names) {
  if (!names || !names.length) return "";
  const quoted = names.map((n) => `"${n}"`).join(", ");
  return names.length === 1
    ? `Project ${quoted} has no repos, so it cannot be created. Go back to ` +
      `Projects and tick at least one repo for it, or remove it.`
    : `These projects have no repos, so they cannot be created: ${quoted}. Go ` +
      `back to Projects and tick at least one repo for each, or remove them.`;
}

/** How many repos a definition binds, whether `repos` is a Set (the wizard's
 *  live state) or a plain array (a server project shape). */
function repoCount(pd) {
  const r = pd && pd.repos;
  if (!r) return 0;
  return typeof r.size === "number" ? r.size : (r.length || 0);
}

/** The message that must block Continue on the Projects step (spec §3 B2), or
 *  null when nothing blocks. Fires ONLY when a project EXISTS with zero repos —
 *  the exact "Kika has no repos" case. NOT when there are no projects at all: a
 *  repo-less run has nothing to tick, and the old gate that fired there
 *  dead-ended those users (see the design note in Onboarding.jsx). Reuses
 *  unboundProjectsMessage so the wording matches finish()'s refusal. */
export function projectsBlockContinue(projects) {
  const names = (projects || []).filter((p) => repoCount(p) === 0).map((p) => p.name);
  return names.length ? unboundProjectsMessage(names) : null;
}

/** The Launch card's per-step readiness (spec §3 B2): one row per hard
 *  requirement, each with the step index to jump to when it is unmet. A step
 *  the user deferred (minimal path) is optional and omitted. */
export function launchReadiness({ projects, selectedRepos, deferred = [] }) {
  const repos = selectedRepos && typeof selectedRepos.size === "number"
    ? selectedRepos.size : (selectedRepos ? selectedRepos.length : 0);
  const rows = [
    { step: "repos", jumpTo: 2, ok: repos > 0,
      message: repos > 0 ? "" : "Pick at least one repository." },
    { step: "projects", jumpTo: 3, ok: projectsBlockContinue(projects) === null,
      message: projectsBlockContinue(projects) || "" },
  ];
  return rows.filter((r) => !deferred.includes(r.step));
}

/** The POST /api/projects body for one definition. `primary_repo` is always a
 *  repo the project binds; it is never omitted, so the server never has to fall
 *  back to "whichever one came first". */
export function projectPayload(pd) {
  const repo_paths = [...pd.repos];
  const primary_repo = repo_paths.includes(pd.primary)
    ? pd.primary
    : (repo_paths.length ? repo_paths[0] : null);
  return { name: pd.name, repo_paths, primary_repo };
}
