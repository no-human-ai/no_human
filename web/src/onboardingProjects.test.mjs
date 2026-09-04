import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  newProjectDef, toggleProjectRepo, setPrimaryRepo, dropRepoEverywhere,
  unboundProjects, unboundProjectsMessage, projectPayload,
  projectsBlockContinue, launchReadiness,
} from "./onboardingProjects.js";

// BUG (first external DMG tester): finished onboarding, landed on a board
// reading "Projects 0 / Repos 1" — after a launch summary that had just said
// "Projects 1". `finish()` guarded project creation with
// `if (repoPaths.length > 0)`, and `addProject()` built every definition with
// `repos: new Set()`, so the obvious sequence (name → + Add project →
// Continue) produced a definition that was dropped with no request, no error
// and no message.
//
// Second report, same step: "one repo should be the default but configurable".
// That default is the project's primary_repo. Onboarding called
// createProject({name, repo_paths}) only, so the server fell back to
// repo_paths[0] — the order the checkboxes happened to be ticked in.
//
// Three layers are guarded, because any one of them can be green while the bug
// is live: the pure functions, the fact that Onboarding.jsx actually uses them
// (this project's `node --test` harness has no React renderer, so that half is
// a source assertion — see onboardingNav.test.mjs), and, on the Python side,
// that POST /api/projects persists the primary_repo the client now sends
// (tests/test_projects.py::test_api_create_project_persists_explicit_primary).

const SRC = dirname(fileURLToPath(import.meta.url));
const onboarding = readFileSync(join(SRC, "Onboarding.jsx"), "utf8");

// ── the root cause: a new definition is not empty ──────────────────────────

test("a new project is seeded with the repos just onboarded", () => {
  const pd = newProjectDef("checkout", new Set(["/git/api", "/git/web"]));
  assert.deepEqual([...pd.repos], ["/git/api", "/git/web"]);
  assert.equal(pd.primary, "/git/api");
});

test("a new project with nothing to bind is still well-formed", () => {
  const pd = newProjectDef("empty", new Set());
  assert.equal(pd.repos.size, 0);
  assert.equal(pd.primary, null);
});

test("the seeded set is a copy — untick one project, the others keep theirs", () => {
  const repos = new Set(["/git/api", "/git/web"]);
  const a = newProjectDef("a", repos);
  const b = newProjectDef("b", repos);
  const a2 = toggleProjectRepo(a, "/git/web");
  assert.deepEqual([...a2.repos], ["/git/api"]);
  assert.deepEqual([...b.repos], ["/git/api", "/git/web"], "sibling def mutated");
  assert.deepEqual([...repos], ["/git/api", "/git/web"], "source set mutated");
});

// ── nothing vanishes silently ──────────────────────────────────────────────

test("a definition binding no repo is named, not dropped", () => {
  const defs = [
    newProjectDef("bound", new Set(["/git/api"])),
    newProjectDef("orphan", new Set()),
  ];
  assert.deepEqual(unboundProjects(defs), ["orphan"]);
});

test("unticking every repo makes a seeded definition unbound again", () => {
  let pd = newProjectDef("checkout", new Set(["/git/api"]));
  assert.deepEqual(unboundProjects([pd]), []);
  pd = toggleProjectRepo(pd, "/git/api");
  assert.deepEqual(unboundProjects([pd]), ["checkout"],
    "unticking the last repo must be refused at launch, not swallowed");
});

test("the refusal names the project and says what to do about it", () => {
  const msg = unboundProjectsMessage(["orphan"]);
  assert.match(msg, /"orphan"/);
  assert.match(msg, /no repos/i);
  assert.match(msg, /tick at least one repo|remove it/i);
});

test("the refusal handles several projects and stays empty when there is none", () => {
  const msg = unboundProjectsMessage(["a", "b"]);
  assert.match(msg, /"a", "b"/);
  assert.equal(unboundProjectsMessage([]), "");
  assert.equal(unboundProjectsMessage(undefined), "");
});

// ── a binding is never held for a repo the card cannot show ────────────────

test("deselecting a repo unbinds it from every project", () => {
  const defs = [
    newProjectDef("a", new Set(["/git/api", "/git/web"])),
    newProjectDef("b", new Set(["/git/web"])),
  ];
  const after = dropRepoEverywhere(defs, "/git/web");
  assert.deepEqual([...after[0].repos], ["/git/api"]);
  assert.deepEqual([...after[1].repos], []);
  assert.deepEqual(unboundProjects(after), ["b"],
    "a project emptied by a deselect must be refused, not silently dropped");
});

test("deselecting the default repo re-points it, so no stale primary is POSTed", () => {
  const [pd] = dropRepoEverywhere(
    [newProjectDef("a", new Set(["/git/api", "/git/web"]))], "/git/api");
  assert.equal(pd.primary, "/git/web");
  assert.equal(projectPayload(pd).primary_repo, "/git/web");
});

test("deselecting a repo no project binds changes nothing", () => {
  const defs = [newProjectDef("a", new Set(["/git/api"]))];
  const after = dropRepoEverywhere(defs, "/git/other");
  assert.equal(after[0], defs[0], "untouched defs must keep identity");
  assert.deepEqual(dropRepoEverywhere([], "/git/x"), []);
  assert.deepEqual(dropRepoEverywhere(undefined, "/git/x"), []);
});

test("dropping the same repo twice is idempotent (StrictMode double-invoke)", () => {
  const once = dropRepoEverywhere(
    [newProjectDef("a", new Set(["/git/api", "/git/web"]))], "/git/web");
  const twice = dropRepoEverywhere(once, "/git/web");
  assert.deepEqual([...twice[0].repos], ["/git/api"]);
  assert.equal(twice[0].primary, "/git/api");
});

// ── the default repo is chosen, not accidental ─────────────────────────────

test("the payload carries primary_repo, so the server never has to guess", () => {
  const pd = newProjectDef("checkout", new Set(["/git/api", "/git/web"]));
  assert.deepEqual(projectPayload(pd), {
    name: "checkout",
    repo_paths: ["/git/api", "/git/web"],
    primary_repo: "/git/api",
  });
});

test("choosing a different default changes primary_repo, not the repo list", () => {
  const pd = setPrimaryRepo(
    newProjectDef("checkout", new Set(["/git/api", "/git/web"])), "/git/web");
  const body = projectPayload(pd);
  assert.equal(body.primary_repo, "/git/web");
  assert.deepEqual(body.repo_paths, ["/git/api", "/git/web"],
    "the default is a choice among the bound repos, not a re-ordering");
});

test("a repo the project does not bind cannot become its default", () => {
  const pd = newProjectDef("checkout", new Set(["/git/api"]));
  assert.equal(setPrimaryRepo(pd, "/git/elsewhere").primary, "/git/api");
  assert.equal(projectPayload(pd).primary_repo, "/git/api");
});

test("unticking the default re-points it at a repo the project still binds", () => {
  let pd = newProjectDef("checkout", new Set(["/git/api", "/git/web"]));
  pd = toggleProjectRepo(pd, "/git/api");   // untick the current primary
  assert.equal(pd.primary, "/git/web");
  assert.equal(projectPayload(pd).primary_repo, "/git/web",
    "a project must never be stored with a primary it does not contain");
});

test("re-ticking into an empty project makes that repo the default", () => {
  let pd = newProjectDef("checkout", new Set());
  pd = toggleProjectRepo(pd, "/git/api");
  assert.equal(pd.primary, "/git/api");
});

// ── the wizard actually uses all of it ─────────────────────────────────────

test("Onboarding.jsx builds new project defs through newProjectDef", () => {
  // F8: one project at a time — addProject binds the repos the user ticked FOR
  // THAT project in the add form (newProjRepos), not every selected repo and not
  // nothing. The picker is seeded from selectedRepos when the step opens, so the
  // common "one project, all my repos" case is still a single Add.
  assert.match(onboarding, /newProjectDef\(name, newProjRepos\)/,
    "addProject must bind the add form's chosen repos");
  assert.match(onboarding, /setNewProjRepos\(new Set\(selectedRepos\)\)/,
    "the add form's repo picker must seed/reset from the repos selected on the repos step");
  assert.doesNotMatch(onboarding, /repos:\s*new Set\(\)/,
    "the empty-by-construction default is the root cause and must not return");
});

test("finish() refuses unbound projects instead of skipping them", () => {
  assert.match(onboarding, /const unbound = unboundProjects\(projectDefs\)/);
  assert.match(onboarding, /if \(unbound\.length\) throw new Error\(unboundProjectsMessage\(unbound\)\)/);
  assert.doesNotMatch(onboarding, /if \(repoPaths\.length > 0\)/,
    "the silent skip must be gone, not merely bypassed");
});

test("the launch summary counts repo-less projects honestly", () => {
  const summary = onboarding.slice(onboarding.indexOf('step.key === "summary"'));
  const line = summary.slice(summary.indexOf("<span>Projects</span>"),
                             summary.indexOf("<span>Repos</span>"));
  assert.match(line, /unbound\.length \? ` · \$\{unbound\.length\} with no repos`/,
    "the summary said 'Projects 1' and the launch then created none");
});

test("finish() refuses BEFORE it writes anything", () => {
  const body = onboarding.slice(onboarding.indexOf("async function finish()"));
  const refusal = body.indexOf("unboundProjectsMessage");
  for (const call of ["createProject(", "completeOnboarding("]) {
    const at = body.indexOf(call);
    assert.ok(at > refusal && refusal !== -1,
      `${call} must not run before the unbound-project refusal`);
  }
});

test("finish() posts the payload with primary_repo, not a bare pair", () => {
  assert.match(onboarding, /createProject\(projectPayload\(pd\)\)/);
  assert.doesNotMatch(onboarding, /createProject\(\{\s*name: pd\.name, repo_paths: repoPaths\s*\}\)/,
    "the primary-less call must be gone");
});

test("deselecting a repo on the repos step unbinds it from the project defs", () => {
  assert.match(onboarding, /if \(removing\) setProjectDefs\(\(d\) => dropRepoEverywhere\(d, path\)\)/,
    "toggleRepo must prune the defs, or a deselected repo is bound invisibly");
});

test("the projects step shows the unbound state and offers the default picker", () => {
  const step = onboarding.slice(onboarding.indexOf('step.key === "projects"'),
                                onboarding.indexOf('{step.key === "integrations" && ('));
  assert.match(step, /pd\.repos\.size === 0/,
    "an unbound project must say so on the card, next to the empty tick-list");
  assert.match(step, /chooseProjectPrimary\(pi, e\.target\.value\)/,
    "the default repo must be configurable in the wizard");
});

// ── the empty state must not promise a gate that does not exist ────────────
//
// Measured on a live wizard: the empty state read "No projects defined yet. Add
// at least one to continue." and Continue was enabled the whole time
// (contDisabled:false). Users invented a project name they did not need.
//
// The copy is what was wrong, not the missing gate. Three things say this step
// is optional BY DESIGN: the step's own closing note ("You can add or edit
// projects later in Settings. Repos not assigned to any project can still be
// used via the 'other' option"), TaskComposer's "No projects yet — give the path
// of the repo to work in", and finish(), which requires no project at all. And a
// real gate would DEAD-END the user who selected no repos: the only project they
// could add would bind none, addProject seeds from selectedRepos, and finish()
// refuses exactly those definitions by name — a wizard you cannot leave.
test("the projects empty state does not claim a gate the nav does not enforce", () => {
  const step = onboarding.slice(onboarding.indexOf('step.key === "projects"'),
                                onboarding.indexOf('{step.key === "integrations" && ('));
  assert.doesNotMatch(step, /Add at least one to continue/,
    "Continue is never disabled on this step — see onboardingNav.forwardDisabled");
  assert.match(step, /No projects yet — this step is optional/,
    "the empty state must say what is true: nothing here is required");
});

test("the ONLY thing that gates Continue on this step is a project with zero repos", () => {
  // spec §3 B2 added the one legitimate gate: a project that EXISTS with no repos
  // cannot be created (finish() refuses it), so Continue is blocked until it is
  // fixed on the step that shows it. What must NEVER come back is the "no
  // projects" gate that dead-ended repo-less users — projectsBlockContinue fires
  // only on the zero-repo-project case, and the empty-state copy stays optional.
  assert.match(onboarding,
    /const continueBlocked = projectsBlockMsg !== null;/,
    "the gate must be the tested predicate, not an inline count");
  assert.match(onboarding,
    /const projectsBlockMsg = step\.key === "projects" \? projectsBlockContinue\(projectDefs\) : null;/,
    "the block message must come from projectsBlockContinue, which fires only on a repo-less project");
  assert.match(onboarding, /disabled=\{forwardDisabled\(navState\) \|\| continueBlocked\}>Continue<\/button>/,
    "Continue delegates to forwardDisabled AND the projects gate — never a raw count");
  assert.doesNotMatch(onboarding, /projectDefs\.length === 0[^)]*disabled/,
    "a bare 'no projects' gate must never return — it dead-ends repo-less users");
});

// ── validate at the step + launch readiness (spec §3 B2) ────────────────────

test("Continue is blocked while a project has zero repos, and only then", () => {
  assert.match(projectsBlockContinue([{ name: "Kika", repos: [] }]), /Kika.*no repos/);
  assert.equal(projectsBlockContinue([{ name: "K", repos: ["/r"] }]), null);
  assert.equal(projectsBlockContinue([]), null, "no projects is a valid, non-blocking state");
});

test("projectsBlockContinue reads Set-shaped repos too (the wizard's live state)", () => {
  assert.match(projectsBlockContinue([{ name: "Kika", repos: new Set() }]), /Kika.*no repos/);
  assert.equal(projectsBlockContinue([{ name: "K", repos: new Set(["/r"]) }]), null);
});

test("launch readiness lists each failing step with a jump index", () => {
  const rows = launchReadiness({ projects: [{ name: "Kika", repos: [] }], selectedRepos: new Set(["/r"]), deferred: [] });
  assert.deepEqual(rows.filter((r) => !r.ok).map((r) => r.jumpTo), [2]);
});

test("launch readiness is all-clear when repos are picked and no project is empty", () => {
  const rows = launchReadiness({ projects: [{ name: "K", repos: ["/r"] }], selectedRepos: new Set(["/r"]), deferred: [] });
  assert.deepEqual(rows.filter((r) => !r.ok), []);
});

test("a deferred step is optional and drops out of the readiness list", () => {
  const rows = launchReadiness({ projects: [], selectedRepos: new Set(), deferred: ["repos"] });
  assert.equal(rows.some((r) => r.step === "repos"), false);
});

test("the Launch card renders launchReadiness rows with a Fix button that jumps", () => {
  assert.match(onboarding, /launchReadiness\(\{ projects: projectDefs, selectedRepos, deferred: \[\] \}\)/,
    "the Launch card must source its readiness from the tested helper");
  assert.match(onboarding, /onClick=\{\(\) => setI\(r\.jumpTo\)\}>Fix →<\/button>/,
    "each unmet step must offer a Fix that jumps to it, not prose telling the user to go Back");
});

test("the Projects empty state offers an Add-repositories button that jumps to the repos step (N2)", () => {
  // Feedback N2: from the Projects step it wasn't clear how to add a repo; the
  // empty state must be a button that jumps back, not prose telling the user to
  // click Back many times.
  assert.match(onboarding, /ob-add-repos-jump/);
  assert.match(onboarding, /setI\(STEPS\.findIndex\(\(s\) => s\.key === "repos"\)\)/);
});

test("the repo folder-search shows the N6 discovery hint (Documents/Desktop skipped)", () => {
  // Feedback N6: repos under Documents/Desktop or deep subdirs don't auto-appear;
  // the hint tells the user why and that typing the path finds them.
  assert.match(onboarding, /ob-scan-hint/);
  assert.match(onboarding, /Documents, Desktop and Downloads/);
});
