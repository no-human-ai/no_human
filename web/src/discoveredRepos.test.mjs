import { test } from "node:test";
import assert from "node:assert/strict";

import {
  repoBadges,
  discoveryMessage,
  searchEmptyMessage,
  filterRepos,
  DIRTY_TEXT,
  fromDetectedRepo,
  ambiguousNames,
} from "./discoveredRepos.js";

const repo = (over = {}) => ({
  path: "/Users/x/git/svc",
  name: "svc",
  is_git: true,
  branch: "main",
  detached: false,
  dirty: false,
  dirty_scan: "complete",
  ecosystem: "",
  ...over,
});

const result = (over = {}) => ({
  repos: [],
  roots_scanned: [],
  roots_missing: [],
  roots_refused: [],
  total_found: 0,
  limit: 200,
  capped: false,
  walk_truncated: false,
  note: "",
  elapsed_ms: 3,
  ...over,
});

const keys = (r) => repoBadges(r).map((b) => b.key);
const textOf = (r, key) => repoBadges(r).find((b) => b.key === key)?.text;

// --------------------------------------------------------------------------
// Badges
// --------------------------------------------------------------------------

test("a clean repo shows its branch and nothing alarming", () => {
  assert.deepEqual(keys(repo()), ["branch"]);
  assert.equal(textOf(repo(), "branch"), "main");
});

test("a dirty repo carries a warn-toned uncommitted-changes badge", () => {
  const b = repoBadges(repo({ dirty: true }));
  const d = b.find((x) => x.key === "dirty");
  assert.ok(d, "expected a dirty badge");
  assert.equal(d.text, DIRTY_TEXT);
  assert.equal(d.tone, "warn");
});

test("the dirty badge is what stops a user pointing a task at a repo mid-edit", () => {
  // The whole point of the flag: it must be present on the row, not only in a
  // tooltip the user never opens.
  assert.ok(keys(repo({ dirty: true })).includes("dirty"));
  assert.ok(!keys(repo({ dirty: false })).includes("dirty"));
});

test("a detached HEAD is labelled as such, not passed off as a branch name", () => {
  const t = textOf(repo({ detached: true, branch: "a1b2c3d4" }), "branch");
  assert.equal(t, "detached at a1b2c3d4");
});

test("a partial dirty scan admits what was not measured instead of claiming clean", () => {
  const b = repoBadges(repo({ dirty: false, dirty_scan: "partial" }));
  const s = b.find((x) => x.key === "scan");
  assert.ok(s, "a partial scan must produce its own badge");
  assert.match(s.text, /untracked/i);
  assert.ok(!/^clean$/i.test(s.text));
});

test("a completed scan adds no scan badge", () => {
  assert.ok(!keys(repo()).includes("scan"));
  assert.ok(!keys(repo({ dirty: true })).includes("scan"));
});

test("a dirty repo does not also advertise a partial scan (dirty is already proven)", () => {
  assert.ok(!keys(repo({ dirty: true, dirty_scan: "partial" })).includes("scan"));
});

test("a non-git directory says so and shows no branch", () => {
  const k = keys(repo({ is_git: false, branch: "", dirty_scan: "not-a-repo" }));
  assert.deepEqual(k, ["nogit"]);
  assert.equal(textOf(repo({ is_git: false }), "nogit"), "not a git repo");
});

test("the ecosystem rides along when the server knows it", () => {
  assert.equal(textOf(repo({ ecosystem: "python" }), "eco"), "python");
  assert.ok(!keys(repo({ ecosystem: "" })).includes("eco"));
});

test("no badge text uses an em dash", () => {
  const all = [
    repo({ dirty: true }), repo({ detached: true }), repo({ is_git: false }),
    repo({ dirty_scan: "partial" }), repo({ ecosystem: "node" }),
  ].flatMap(repoBadges);
  for (const b of all) assert.ok(!b.text.includes("—"), `em dash in ${b.text}`);
});

// --------------------------------------------------------------------------
// Message
// --------------------------------------------------------------------------

test("a capped result surfaces the server's own note verbatim", () => {
  const note = "Showing the first 2 of 9 repositories found - narrow the scan roots.";
  assert.equal(discoveryMessage(result({ capped: true, note, repos: [repo()] })), note);
});

test("no clone folders at all tells the user to type a path", () => {
  const m = discoveryMessage(result({ roots_missing: ["/Users/x/git"] }));
  assert.match(m, /type a repository path/i);
});

test("folders scanned but nothing found NAMES the searched roots, not a count", () => {
  // A count once read as "broken page" when the real story was "wrong
  // directory" (operator, 2026-08-09): the path itself is the diagnostic.
  const m = discoveryMessage(result({ roots_scanned: ["/Users/x/git", "/Users/x/Code"] }));
  assert.match(m, /\/Users\/x\/git, \/Users\/x\/Code/);
  assert.match(m, /type a repository path/i);
  assert.ok(!/\d+ folders?/.test(m), "a bare count must not replace the paths");
});

test("a single scanned root is named verbatim", () => {
  const m = discoveryMessage(result({ roots_scanned: ["/Users/x/git"] }));
  assert.match(m, /Searched \/Users\/x\/git and found no repositories/);
});

test("a healthy result with repos and no cap says nothing", () => {
  assert.equal(discoveryMessage(result({ repos: [repo()], roots_scanned: ["/Users/x/git"] })), "");
});

test("refused out-of-home roots are reported, never silently dropped", () => {
  const m = discoveryMessage(result({
    repos: [repo()], roots_scanned: ["/Users/x/git"], roots_refused: ["/etc"],
  }));
  assert.match(m, /outside your home/i);
});

test("a truncated search is admitted, never passed off as a complete list", () => {
  // The dangerous shape: rows came back, so the list LOOKS authoritative and
  // the user concludes their repo is not there.
  const m = discoveryMessage(result({
    repos: [repo()], roots_scanned: ["/Users/x/git"], walk_truncated: true,
  }));
  assert.match(m, /stopped early/i);
  assert.match(m, /type a repository path/i);
});

test("a complete search adds no truncation warning", () => {
  assert.equal(
    discoveryMessage(result({ repos: [repo()], roots_scanned: ["/Users/x/git"] })),
    "",
  );
});

test("a missing result object does not throw", () => {
  assert.equal(discoveryMessage(null), "");
  assert.equal(discoveryMessage(undefined), "");
});

test("no message uses an em dash", () => {
  const ms = [
    discoveryMessage(result({ roots_missing: ["/a"] })),
    discoveryMessage(result({ roots_scanned: ["/a"] })),
    discoveryMessage(result({ repos: [repo()], roots_scanned: ["/a"], roots_refused: ["/etc"] })),
  ];
  for (const m of ms) assert.ok(!m.includes("—"), `em dash in ${m}`);
});

// --------------------------------------------------------------------------
// searchEmptyMessage — the "Search another folder" empty state
// --------------------------------------------------------------------------

test("a refused search says it was not scanned, never that the folder is empty", () => {
  const m = searchEmptyMessage(result({
    repos: [], roots_refused: ["/etc"],
    refusals: [{ path: "/etc", reason: "outside home directory" }],
  }), "/etc");
  assert.match(m, /not scanned: outside home directory/);
  assert.doesNotMatch(m, /no git repositories there/);
});

test("a genuinely empty search still says so", () => {
  const m = searchEmptyMessage(result({ repos: [] }), "/Users/x/work");
  assert.equal(m, "Searched /Users/x/work — no git repositories there.");
});

test("no search and no refusal keeps the default empty state", () => {
  const m = searchEmptyMessage(result({ repos: [] }), "");
  assert.equal(m, "No repositories found. Search another folder above.");
});

test("a string-only roots_refused (older server) still yields a reason", () => {
  const m = searchEmptyMessage(result({ repos: [], roots_refused: ["/etc"] }), "/etc");
  assert.match(m, /not scanned: outside home directory/);
});

// --------------------------------------------------------------------------
// Filter
// --------------------------------------------------------------------------

test("an empty query keeps every row", () => {
  const rs = [repo(), repo({ name: "other", path: "/Users/x/Code/other" })];
  assert.equal(filterRepos(rs, "").length, 2);
  assert.equal(filterRepos(rs, "   ").length, 2);
});

test("the filter matches name or path, case-insensitively", () => {
  const rs = [
    repo({ name: "billing", path: "/Users/x/git/billing" }),
    repo({ name: "web", path: "/Users/x/Projects/acme-web" }),
  ];
  assert.deepEqual(filterRepos(rs, "BILL").map((r) => r.name), ["billing"]);
  assert.deepEqual(filterRepos(rs, "projects").map((r) => r.name), ["web"]);
  assert.deepEqual(filterRepos(rs, "zzz"), []);
});

test("the filter tolerates a missing list", () => {
  assert.deepEqual(filterRepos(null, "x"), []);
  assert.deepEqual(filterRepos(undefined, ""), []);
});

// --------------------------------------------------------------------------
// Rows from the older single-root scan
// --------------------------------------------------------------------------

test("a manually scanned repo is widened to the discovery shape", () => {
  const r = fromDetectedRepo({ path: "/Users/x/work/svc", name: "svc", ecosystem: "go" });
  assert.equal(r.path, "/Users/x/work/svc");
  assert.equal(r.name, "svc");
  assert.equal(r.ecosystem, "go");
  assert.equal(r.is_git, true);   // that scan only ever returns dirs holding .git
});

test("a manually scanned repo does not claim a branch or a clean tree it never measured", () => {
  const r = fromDetectedRepo({ path: "/p", name: "p" });
  assert.equal(r.branch, "");
  assert.equal(r.dirty, false);
  assert.equal(r.dirty_scan, "unknown");
  const b = repoBadges(r);
  assert.ok(!b.some((x) => x.key === "dirty"), "must not flag dirty on an unmeasured repo");
  assert.ok(!b.some((x) => x.key === "branch"), "must not show a branch it does not have");
  const scan = b.find((x) => x.key === "scan");
  assert.ok(scan, "an unmeasured repo must say the status was not checked");
  assert.match(scan.text, /not checked/i);
  assert.ok(!scan.text.includes("—"));
});

test("an unmeasured repo is not mistaken for a non-git directory", () => {
  const b = repoBadges(fromDetectedRepo({ path: "/p", name: "p" }));
  assert.ok(!b.some((x) => x.key === "nogit"));
});

// --------------------------------------------------------------------------
// Ambiguous names
// --------------------------------------------------------------------------

test("two checkouts of the same repo mark that name ambiguous", () => {
  const rs = [
    repo({ name: "svc", path: "/Users/x/git/svc" }),
    repo({ name: "svc", path: "/Users/x/work/svc" }),
  ];
  assert.ok(ambiguousNames(rs).has("svc"));
});

test("a unique basename is never marked ambiguous", () => {
  const rs = [
    repo({ name: "svc", path: "/Users/x/git/svc" }),
    repo({ name: "svc", path: "/Users/x/work/svc" }),
    repo({ name: "billing", path: "/Users/x/git/billing" }),
  ];
  assert.deepEqual(ambiguousNames(rs), new Set(["svc"]));
  assert.ok(!ambiguousNames(rs).has("billing"));
});

test("a single repo list marks nothing ambiguous", () => {
  assert.equal(ambiguousNames([repo()]).size, 0);
  assert.equal(ambiguousNames([]).size, 0);
});

test("a missing or ragged list does not throw", () => {
  assert.equal(ambiguousNames(null).size, 0);
  assert.equal(ambiguousNames(undefined).size, 0);
  assert.equal(ambiguousNames([null, {}]).size, 0);
});

test("collision detection falls back to the path's last segment when name is absent", () => {
  const rs = [{ path: "/a/svc" }, { path: "/b/svc" }];
  assert.ok(ambiguousNames(rs).has("svc"));
});

test("names differing only in case are distinct directories, not a collision", () => {
  const rs = [
    repo({ name: "svc", path: "/Users/x/git/svc" }),
    repo({ name: "Svc", path: "/Users/x/work/Svc" }),
  ];
  assert.equal(ambiguousNames(rs).size, 0);
});

test("marking a name ambiguous does not change its badges", () => {
  const rs = [
    repo({ name: "svc", path: "/Users/x/git/svc" }),
    repo({ name: "svc", path: "/Users/x/work/svc", dirty: true }),
  ];
  assert.ok(ambiguousNames(rs).has("svc"));
  assert.deepEqual(keys(rs[0]), ["branch"]);
  assert.deepEqual(keys(rs[1]), ["branch", "dirty"]);
});
