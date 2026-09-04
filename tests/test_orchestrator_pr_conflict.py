"""Tests for the "rebase round cannot resolve a generated-artefact conflict"
bugfix: `WakeWatcher._check_pr_conflict` must enumerate conflicting paths,
name them in the `pr_conflict` event, and resolve mechanically (no coder
round) when every conflicting path is a derived artefact. Only
`RELEASE_MANIFEST.txt` unconditionally qualifies (`dc.DERIVED_ARTEFACTS`) --
it is fully rebuilt from the tree by `export_guard.py approve`.
`EXPORT_CLASSIFICATION.txt` sits right next to it in the export gate and is
NOT derived in general: its per-rule win-COUNTS are hand-maintained and no
command re-tallies them, so a conflict touching it -- alone, or mixed with
the manifest -- must still open a coder round BY DEFAULT. The one narrow
exception (`dc.classification_count_only`, exercised end to end below and in
`test_derived_conflict_count_only.py`): when every conflicting hunk in it
differs from the other side ONLY in the numeric count digits -- same verb,
same pattern, same everything else -- the conflict is arithmetic, not a hand
decision, and is repaired with the EXISTING
`approve_merge.reconcile_merge_count_drift` (never a second implementation).
Any edit to a pattern, verb, comment, or an added/removed/reordered rule
line still falls through to a coder round exactly as before.

The scratch repo built by `_repo()` below is a real, from-scratch git
repository with its own bare `origin`, self-contained STUB
`scripts/export_guard.py` / `scripts/build_public_export.py` (the real
`export_guard.py` unconditionally scans a private term inventory on every
`approve`, which a throwaway test repo cannot satisfy), a two-line
`RELEASE_MANIFEST.txt` and a minimal `EXPORT_CLASSIFICATION.txt` that
carries one COUNTED glob rule (`ship   1  src/base*.py`). The stub
`export_guard.py verify` re-tallies that counted rule against the tracked
tree and refuses on a mismatch, the same way the real one does -- so a test
that drives two branches into an identically-auto-merged (hence
non-conflicting) count line can actually exhibit the count-drift bug the
mechanical resolver must not paper over. Tests build branches with real git
commits so that `merge_tree_conflicts` sees a genuine conflict, never a
mocked one.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus
from no_human.vcs import GitError, GitRepo, commit_with_manifest_repair
from no_human.vcs import approve_merge
from no_human.vcs import derived_conflict as dc
from no_human.vcs import manifest_repair
from no_human.vcs.approve_merge import reconcile_commit_count_drift
from no_human.vcs.budget_conflict import run_budget_test
from tests.test_vcs import _HOOK_CHECK_SRC


# ---------------------------------------------------------------------------
# git plumbing helpers
# ---------------------------------------------------------------------------

def _run(args: list[str], *, cwd) -> subprocess.CompletedProcess:
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{args} failed rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def _git(cwd, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd)


_BUILD_PUBLIC_EXPORT_STUB = '''\
"""Minimal stand-in for scripts/build_public_export.py, used only by the
test fixture's scratch repo. Exposes exactly the surface
`_ship_classified_paths` (src/no_human/vcs/approve_merge.py) and the stub
`export_guard.py` need: classification parsing + classifying, nothing about
term scanning or tree verification.

Rules may optionally carry a hand-maintained win-COUNT (`ship   1  glob`),
mirroring the real EXPORT_CLASSIFICATION.txt format. `check_counts()` is the
stub's only re-tallying logic -- deliberately just a comparison against the
tree, never a rewrite: nothing here regenerates a drifted count, matching
`scripts/export_guard.py`, which has no count-rewriting path either.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

CLASSIFICATION_NAME = "EXPORT_CLASSIFICATION.txt"
RELEASE_MANIFEST_NAME = "RELEASE_MANIFEST.txt"


@dataclass
class Rule:
    # Field names/order mirror the REAL scripts/build_public_export.py Rule
    # exactly (verb, declared, pattern, lineno) -- `approve_merge.
    # reconcile_commit_count_drift` dynamically imports this module (or the
    # real one) and reads those attribute names plus `Classification.wins`,
    # so a test stub with different names would AttributeError instead of
    # exercising the reconciler.
    verb: str
    declared: int | None
    pattern: str
    lineno: int


@dataclass
class Classification:
    shipped: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    unclassified: list = field(default_factory=list)
    wins: dict = field(default_factory=dict)   # rule.lineno -> real count


def parse_classification(text: str) -> list:
    rules = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 2 or parts[0] not in ("ship", "drop"):
            continue
        verb = parts[0]
        if len(parts) >= 3 and parts[1].isdigit():
            rules.append(Rule(verb=verb, declared=int(parts[1]), pattern=parts[2], lineno=lineno))
        else:
            # Uncounted rule (no hand-maintained tally) -- declared is None
            # and never checked/rewritten.
            rules.append(Rule(verb=verb, declared=None, pattern=parts[1], lineno=lineno))
    return rules


def classify(rules, paths):
    out = Classification(wins={rule.lineno: 0 for rule in rules})
    for path in paths:
        winner = None
        for rule in rules:
            if fnmatch.fnmatch(path, rule.pattern):
                winner = rule
        if winner is None:
            out.unclassified.append(path)
            continue
        out.wins[winner.lineno] += 1
        (out.shipped if winner.verb == "ship" else out.dropped).append(path)
    out.shipped.sort()
    out.dropped.sort()
    return out


def check_counts(rules, paths) -> list:
    """Re-tally each COUNTED rule's win-count against `paths` (mirrors "last
    matching rule wins" from `classify()`) and report every rule whose
    declared count doesn't match the tree. This never rewrites a count --
    there is no regenerator, by design (see module docstring)."""
    cls = classify(rules, paths)
    problems = []
    for rule in rules:
        if rule.declared is None:
            continue
        actual = cls.wins.get(rule.lineno, 0)
        if actual != rule.declared:
            problems.append(
                f"EXPORT_CLASSIFICATION.txt:{rule.lineno}: `{rule.verb} {rule.declared}  "
                f"{rule.pattern}` actually wins {actual} file(s)."
            )
    return problems
'''


_EXPORT_GUARD_STUB = '''\
"""Minimal stand-in for scripts/export_guard.py, used only by the test
fixture's scratch repo. Implements just `approve <paths...>` and `verify`
against the stub build_public_export.py -- no term scanning, no
`_script_repo_root()` git lookup (this always runs with an explicit cwd).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import build_public_export as builder

MANIFEST_HEADER = "# generated pins -- do not hand edit\\n"


def _tracked(root: Path) -> list[str]:
    import subprocess
    out = subprocess.run(["git", "ls-files"], cwd=str(root), capture_output=True, text=True).stdout
    return [p for p in out.splitlines() if p]


def _rules(root: Path):
    text = (root / builder.CLASSIFICATION_NAME).read_text(encoding="utf-8")
    return builder.parse_classification(text)


def _classification(root: Path):
    return builder.classify(_rules(root), _tracked(root))


def _write_pins(root: Path, pins: dict) -> None:
    lines = [MANIFEST_HEADER]
    for path in sorted(pins):
        lines.append(f"{pins[path]}  {path}\\n")
    (root / builder.RELEASE_MANIFEST_NAME).write_text("".join(lines), encoding="utf-8")


def _read_pins(root: Path) -> dict:
    manifest = root / builder.RELEASE_MANIFEST_NAME
    if not manifest.exists():
        return {}
    pins = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        digest, _, path = line.partition("  ")
        if path:
            pins[path] = digest
    return pins


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cmd_approve(args) -> int:
    root = args.root
    # Same order and same phrasing as the real guard (scripts/export_guard.py
    # `_cmd_approve` -> build_public_export.classification_errors): a count
    # that drifted refuses BEFORE any pin is written, rc 2.
    drift = builder.check_counts(_rules(root), _tracked(root))
    if drift:
        # Real export_guard.py writes refusals to STDERR (manifest_repair.py's
        # reactive AND proactive paths read `proc.stderr` to find the
        # count-drift message).
        sys.stderr.write("approve: REFUSED -- fix EXPORT_CLASSIFICATION.txt first -- "
                         "approvals on top of a wrong classification pin the wrong review:\\n"
                         + "\\n".join(f"  {d}" for d in drift) + "\\n")
        return 2
    cls = _classification(root)
    shipped = set(cls.shipped)
    pins = _read_pins(root)

    if not args.paths and not args.all and not args.prune:
        sys.stderr.write(
            "approve: REFUSED -- approved 0 file(s); pass PATH(s) or --all "
            "(or --prune) -- nothing to approve was given\\n"
        )
        return 2

    if args.paths:
        targets = list(dict.fromkeys(args.paths))
        bad = [p for p in targets if p not in shipped]
        if bad:
            sys.stderr.write(
                "approve: REFUSED -- not ship-classified (classify each in "
                f"{builder.CLASSIFICATION_NAME} first):\\n"
                + "\\n".join(f"  {p}" for p in bad) + "\\n"
            )
            return 2
    elif args.all:
        # Same shape as the real guard: new-or-changed pins only, matching
        # `_APPROVED_RE`/`_PRUNED_RE` in manifest_repair.py so the proactive
        # path can parse this stub's stdout exactly as it parses the real
        # guard's.
        targets = sorted(
            p for p in shipped if pins.get(p) != _sha256(root / p))
    else:
        targets = []

    pruned = sorted(set(pins) - shipped)
    if args.prune:
        for p in pruned:
            del pins[p]
            sys.stdout.write(f"pruned    {p} (no longer ships)\\n")

    for p in targets:
        digest = _sha256(root / p)
        was = pins.get(p)
        pins[p] = digest
        state = "unchanged" if was == digest else (f"was {was[:12]}" if was else "new")
        sys.stdout.write(f"approved  {digest[:12]}  {p} ({state})\\n")

    _write_pins(root, pins)
    return 0


def _cmd_verify(args) -> int:
    root = args.root
    rules = _rules(root)
    tracked = _tracked(root)
    cls = builder.classify(rules, tracked)
    pins = _read_pins(root)
    problems = []
    shipped = [p for p in cls.shipped if p != builder.RELEASE_MANIFEST_NAME]
    for p in shipped:
        if p not in pins:
            problems.append(f"missing pin: {p}")
        elif pins[p] != _sha256(root / p):
            problems.append(f"stale pin: {p}")
    for p in pins:
        if p not in shipped:
            problems.append(f"pinned but not shipped: {p}")
    problems.extend(builder.check_counts(rules, tracked))
    if problems:
        sys.stdout.write("verify: FAILED\\n" + "\\n".join(f"  {p}" for p in problems) + "\\n")
        return 1
    sys.stdout.write(f"verify: OK -- {len(shipped)} shipped == {len(pins)} pins\\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_approve = sub.add_parser("approve")
    p_approve.add_argument("paths", nargs="*")
    p_approve.add_argument("--all", action="store_true")
    p_approve.add_argument("--prune", action="store_true")
    sub.add_parser("verify")
    args = parser.parse_args()
    args.root = args.root.resolve()
    if args.cmd == "approve":
        return _cmd_approve(args)
    return _cmd_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _repo(tmp_path: Path) -> Path:
    """Build a from-scratch repo (+ bare origin, pushed) with the stub export
    gate wired, one ship-classified source file, and a pinned manifest.
    Returns the working-tree path. `main` is pushed with `-u` so
    `@{upstream}` resolves (required by `pr_watcher._base_tips`).
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _run(["git", "init", "-q", "--bare", str(origin)], cwd=tmp_path)
    _run(["git", "init", "-q", "-b", "main", str(work)], cwd=tmp_path)
    _git(work, "config", "user.email", "a@example.com")
    _git(work, "config", "user.name", "a")

    scripts = work / "scripts"
    scripts.mkdir()
    (scripts / "build_public_export.py").write_text(_BUILD_PUBLIC_EXPORT_STUB, encoding="utf-8")
    (scripts / "export_guard.py").write_text(_EXPORT_GUARD_STUB, encoding="utf-8")
    (work / "EXPORT_CLASSIFICATION.txt").write_text(
        # `src/base*.py` is a COUNTED rule (last-match-wins over the general
        # `ship src/**`, so it "wins" the tally for files it also matches) --
        # this is the rule the count-drift tests bump. Files added by other
        # tests (on_feature.py, feat_a.py, ...) don't start with "base", so
        # they never touch this rule's declared count.
        # Models the real repo: the classification file itself is DROPPED (never
        # pinned), and the tests rule carries a COUNT so a drift in a drop rule
        # is a real thing `verify` can catch.
        "ship src/**\nship   1  src/base*.py\ndrop   1  tests/**\ndrop   1  EXPORT_CLASSIFICATION.txt\n", encoding="utf-8"
    )
    (work / "RELEASE_MANIFEST.txt").write_text("# generated pins -- do not hand edit\n", encoding="utf-8")
    src = work / "src"
    src.mkdir()
    (src / "base.py").write_text("base\n", encoding="utf-8")
    tests_dir = work / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("test x\n", encoding="utf-8")

    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _approve(work, ["src/base.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin base.py")

    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


def _approve(work: Path, paths: list[str], *, expect_ok: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        [sys.executable, "scripts/export_guard.py", "approve", *paths],
        cwd=str(work), capture_output=True, text=True,
    )
    if expect_ok:
        # The stub guard can refuse (count drift, not ship-classified); a
        # fixture that silently fails to pin surfaces two git commands later
        # as "nothing to commit" — name it here instead.
        assert r.returncode == 0, f"approve refused in a fixture:\n{r.stdout}{r.stderr}"
    return r


def _verify(work: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/export_guard.py", "verify"],
        cwd=str(work), capture_output=True, text=True,
    )


def _worktree(work: Path, dest: Path, branch: str, base: str = "main") -> None:
    _git(work, "worktree", "add", "-q", "-b", branch, str(dest), base)


def _push_branch(work: Path, wt: Path, branch: str) -> None:
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
    _git(work, "push", "-q", "origin", f"{sha}:refs/heads/{branch}")
    # also make the local branch ref (created by `worktree add -b`) match,
    # and set its upstream so future @{upstream} lookups on it work too.
    _git(work, "branch", "-q", "--set-upstream-to", f"origin/{branch}", branch)


_BUDGET_STUB = '''\
"""Throwaway structural-budget-ratchet stub for the mechanical-conflict
fixture repo -- NOT the real tests/test_structural_budget.py (that file's
OUT-OF-SCOPE thresholds are untouched by this task). Mirrors just enough of
the real file's shape -- a FROZEN_FUNCTION_LINES allow-list dict plus a
scan_tree(root) matching the real one's call/return contract -- for
src/no_human/vcs/budget_conflict.py's `load_scanner`/`measure`/
`run_budget_test` to operate on it. The threshold is set low enough that a
couple of inserted lines in this fixture's own src/no_human/growing.py is
enough to grow the one frozen entry.
"""
from __future__ import annotations

import ast
from pathlib import Path

MAX_FUNCTION_LINES = 2

FROZEN_FUNCTION_LINES = {
    "growing.py:grow": 3,
}
FROZEN_FUNCTION_CC = {}
FROZEN_FILE_LINES = {}


def scan_tree(root):
    function_lines = {}
    function_cc = {}
    file_lines = {}
    files = sorted(Path(root).rglob("*.py"))
    total_functions = 0
    for path in files:
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                total_functions += 1
                span = node.end_lineno - node.lineno + 1
                if span > MAX_FUNCTION_LINES:
                    function_lines[f"{rel}:{node.name}"] = span
    return function_lines, function_cc, file_lines, len(files), total_functions


def test_frozen_entries_match_the_measured_tree():
    src = Path(__file__).resolve().parent.parent / "src" / "no_human"
    function_lines, _cc, _fl, _files, _fns = scan_tree(src)
    for key, frozen_value in FROZEN_FUNCTION_LINES.items():
        measured = function_lines.get(key)
        assert measured == frozen_value, (
            f"{key}: frozen at {frozen_value}, tree measures {measured}"
        )
    for key, measured in function_lines.items():
        assert key in FROZEN_FUNCTION_LINES, f"new offender not frozen: {key}={measured}"
'''


def _repo_with_budget_stub(tmp_path: Path) -> Path:
    """`_repo()` plus a `src/no_human/` subtree (one function, `grow`,
    already over the stub's own threshold) and a throwaway
    `tests/test_structural_budget.py` (`_BUDGET_STUB`) so tests can drive the
    mechanical structural-budget conflict resolution end to end.
    `EXPORT_CLASSIFICATION.txt`'s `drop tests/**` count is bumped 1 -> 2 in
    the same commit (a second tracked file now matches it) -- `_repo()`
    itself, and every OTHER test that calls it directly, is untouched.
    """
    work = _repo(tmp_path)
    no_human = work / "src" / "no_human"
    no_human.mkdir()
    (no_human / "growing.py").write_text(
        "def grow():\n"
        "    a = 1\n"
        "    return a\n",
        encoding="utf-8",
    )
    (work / "tests" / "test_structural_budget.py").write_text(_BUDGET_STUB, encoding="utf-8")
    _bump_drop_count(work, "tests/**", 2)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "add src/no_human/growing.py + budget stub")
    _approve(work, ["src/no_human/growing.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin growing.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    return work


async def _approval_task(store, repo_path: str, *, branch="feature", base="main"):
    t = Task.new("conflict", repo_path=repo_path)
    t.context = {
        "pr_watch": "https://code.example.com/dev/x/pull/26",
        "pr_branch": branch,
        "base_branch": base,
    }
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, mergeable="CONFLICTING", merge_state="DIRTY", events=None,
             derived_resolver=None):
    async def pr_mergeable(url):
        return {"mergeable": mergeable, "mergeStateStatus": merge_state}
    return WakeWatcher(
        store, {},
        pr_mergeable=pr_mergeable,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
        derived_resolver=derived_resolver,
    )


def _use_stub_export_guard(monkeypatch):
    """Point derived_conflict._export_guard_argv() at the real interpreter +
    the stub script, instead of `uv run python ...`."""
    monkeypatch.setattr(dc, "_export_guard_argv", lambda: [sys.executable, "scripts/export_guard.py"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_the_pr_conflict_event_names_the_conflicting_paths(store, tmp_path, monkeypatch):
    """A source-only conflict (negative control shape) still names the path
    in the pr_conflict event before opening a coder round."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "src" / "base.py").write_text("base\nfeature change\n", encoding="utf-8")
    _git(wt, "commit", "-qam", "feature edits base.py")
    _push_branch(work, wt, "feature")

    (work / "src" / "base.py").write_text("base\nmain change\n", encoding="utf-8")
    _git(work, "commit", "-qam", "main also edits base.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    kinds = [k for k, _ in events]
    assert "pr_conflict" in kinds
    text = next(txt for k, txt in events if k == "pr_conflict")
    assert "src/base.py" in text


async def test_an_unenumerable_conflict_escalates_instead_of_opening_a_round(store):
    """repo_path unresolvable (as in the pre-existing fake-repo tests):
    conflicting_paths() returns None without raising, on both the first
    attempt and the post-fetch retry -- an unknown, so it escalates instead
    of opening a coder round on an unresolved question (bugfix: this used to
    fall through to `_resume` with a bare "could not enumerate" event)."""
    events = []
    t = await _approval_task(store, "/tmp/does-not-exist")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "escalated_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict" not in kinds
    text = next(txt for k, txt in events if k == "escalated_pr_conflict")
    assert "could not enumerate" not in text
    assert "no coder round opened" in text

    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    assert stored.blocker["category"] == "NOVEL_UNKNOWN"
    assert stored.blocker["evidence"]
    assert not (stored.context or {}).get("send_back_feedback")


async def test_a_manifest_only_conflict_is_resolved_without_a_coder_session(store, tmp_path, monkeypatch):
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    # feature: adds a new ship-classified file, pins it into the manifest.
    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    _git(wt_f, "add", "src/on_feature.py")
    _git(wt_f, "commit", "-qm", "add on_feature.py")
    _approve(wt_f, ["src/on_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt_f, "feature")

    # main: independently adds a different ship-classified file, pins it too
    # -- both edits append to the same short manifest, so they collide on
    # the exact same insertion point (verified empirically: two independent
    # end-of-file appends to a short file are a genuine git merge conflict,
    # not an auto-merge).
    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    _git(work, "commit", "-qm", "add on_main.py")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    # sanity: confirm the conflict is real and confined to the manifest.
    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"RELEASE_MANIFEST.txt"}

    events = []
    t = await _approval_task(store, str(work))
    resolver_calls = []

    def spying_resolver(repo_path, branch, base_tip_sha, remote="origin",
                         eligible=dc.DERIVED_ARTEFACTS):
        resolver_calls.append((repo_path, branch, base_tip_sha))
        return dc.resolve_derived_conflict(repo_path, branch, base_tip_sha,
                                            remote=remote, eligible=eligible)

    w = _watcher(store, events=events, derived_resolver=spying_resolver)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resolved_pr_conflict"
    assert len(resolver_calls) == 1
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" in kinds
    assert "resumed" not in kinds  # no coder round
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "RELEASE_MANIFEST.txt" in text

    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL  # unchanged by mechanical resolution

    # the pushed feature branch's manifest now pins both files and verifies clean.
    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "feature")
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr
    pins_text = (wt_check / "RELEASE_MANIFEST.txt").read_text(encoding="utf-8")
    assert "src/on_feature.py" in pins_text
    assert "src/on_main.py" in pins_text


async def test_a_raising_enumeration_recovers_after_a_ref_fetch_and_resolves_mechanically(
        store, tmp_path, monkeypatch):
    """conflicting_paths() raises once (simulating a stale/missing ref) then
    succeeds once `fetch_conflict_refs` has run -- the mechanical resolver is
    still reached and no coder round is opened."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    _git(wt_f, "add", "src/on_feature.py")
    _git(wt_f, "commit", "-qm", "add on_feature.py")
    _approve(wt_f, ["src/on_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    _git(work, "commit", "-qm", "add on_main.py")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    # sanity: confirm the conflict is real and confined to the manifest.
    assert await dc.conflicting_paths(str(work), "main", "feature") == {"RELEASE_MANIFEST.txt"}

    real_conflicting_paths = dc.conflicting_paths
    real_fetch = dc.fetch_conflict_refs
    calls = {"n": 0}
    fetch_calls = []

    async def flaky(repo_path, base_tip, branch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bad object main")
        return await real_conflicting_paths(repo_path, base_tip, branch)

    async def spying_fetch(repo_path, base, branch):
        fetch_calls.append((repo_path, base, branch))
        return await real_fetch(repo_path, base, branch)

    monkeypatch.setattr(dc, "conflicting_paths", flaky)
    monkeypatch.setattr(dc, "fetch_conflict_refs", spying_fetch)

    events = []
    t = await _approval_task(store, str(work))
    resolver_calls = []

    def spying_resolver(repo_path, branch, base_tip_sha, remote="origin",
                         eligible=dc.DERIVED_ARTEFACTS):
        resolver_calls.append((repo_path, branch, base_tip_sha))
        return dc.resolve_derived_conflict(repo_path, branch, base_tip_sha,
                                            remote=remote, eligible=eligible)

    w = _watcher(store, events=events, derived_resolver=spying_resolver)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert fetch_calls == [(str(work), "main", "feature")]
    assert calls["n"] == 2
    assert result == "resolved_pr_conflict"
    assert len(resolver_calls) == 1
    kinds = [k for k, _ in events]
    assert "pr_conflict" not in kinds  # no coder round
    assert "pr_conflict_resolved" in kinds

    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL  # unchanged, not IMPLEMENTING
    assert stored.context.get("pr_conflict_enumerate_error")
    assert "bad object main" in stored.context["pr_conflict_enumerate_error"]


async def test_an_always_raising_enumeration_escalates_instead_of_opening_a_coder_round(
        store, tmp_path, monkeypatch):
    """conflicting_paths() raises on both the first attempt and the retry
    after a failed fetch -- the repro for the bugfix: escalate NOVEL_UNKNOWN
    with the exception text, never open a coder round. Uses a real repo (not
    the unresolvable-path shape of the None-return test above) so this
    exercises the actual raise-handling branch, distinct from a plain None
    return."""
    work = _repo(tmp_path)

    async def always_raises(repo_path, base_tip, branch):
        raise RuntimeError("fatal: bad object refs/heads/main")

    async def fetch_always_fails(repo_path, base, branch):
        return False

    monkeypatch.setattr(dc, "conflicting_paths", always_raises)
    monkeypatch.setattr(dc, "fetch_conflict_refs", fetch_always_fails)

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(
        t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "escalated_pr_conflict"

    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    assert stored.blocker["category"] == "NOVEL_UNKNOWN"
    assert "bad object refs/heads/main" in stored.blocker["evidence"]
    assert stored.context.get("pr_conflict_enumerate_error")
    assert "bad object refs/heads/main" in stored.context["pr_conflict_enumerate_error"]

    kinds = [k for k, _ in events]
    assert "pr_conflict" not in kinds  # no coder round
    assert not (stored.context or {}).get("send_back_feedback")

    text = next(txt for k, txt in events if k == "escalated_pr_conflict")
    assert "bad object refs/heads/main" in text
    assert "could not enumerate" not in text

    persisted = await store.list_events(t.id)
    escalate_events = [e for e in persisted if e.get("kind") == "escalated_pr_conflict"]
    assert escalate_events
    assert "bad object refs/heads/main" in escalate_events[0].get("error", "")


async def test_a_successful_enumeration_never_fetches_or_escalates(store, tmp_path, monkeypatch):
    """A source-only conflict that enumerates cleanly on the first try must
    never call `fetch_conflict_refs` and must never escalate -- the
    fetch/retry machinery is purely a failure-recovery path."""
    work = _repo(tmp_path)
    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "src" / "base.py").write_text("base\nfeature change\n", encoding="utf-8")
    _git(wt, "commit", "-qam", "feature edits base.py")
    _push_branch(work, wt, "feature")

    (work / "src" / "base.py").write_text("base\nmain change\n", encoding="utf-8")
    _git(work, "commit", "-qam", "main also edits base.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    fetch_calls = []

    async def spy_fetch(repo_path, base, branch):
        fetch_calls.append((repo_path, base, branch))
        return True

    monkeypatch.setattr(dc, "fetch_conflict_refs", spy_fetch)

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    assert fetch_calls == []
    kinds = [k for k, _ in events]
    assert "pr_conflict" in kinds
    assert "escalated_pr_conflict" not in kinds
    text = next(txt for k, txt in events if k == "pr_conflict")
    assert "src/base.py" in text

    persisted = await store.list_events(t.id)
    pr_conflict_events = [e for e in persisted if e.get("kind") == "pr_conflict"]
    assert pr_conflict_events
    assert "error" not in pr_conflict_events[0]


def test_derived_artefacts_is_exact_repo_root_paths():
    """RELEASE_MANIFEST.txt only: EXPORT_CLASSIFICATION.txt's win-counts are
    hand-maintained and no command re-tallies them, so it is not eligible
    for mechanical (`--ours`) conflict resolution."""
    assert dc.DERIVED_ARTEFACTS == frozenset({"RELEASE_MANIFEST.txt"})


async def test_a_mixed_ship_and_drop_change_pins_only_the_ship_path(store, tmp_path, monkeypatch):
    """A drop-classified file's changed content must not block mechanical
    resolution: export_guard refuses to approve it (not ship-classified),
    that refusal is handled gracefully (committed unpinned), and the
    ship-classified file in the same branch still gets pinned."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    (wt_f / "tests" / "test_x.py").write_text("test x changed\n", encoding="utf-8")
    _git(wt_f, "add", "-A")
    _git(wt_f, "commit", "-qm", "add ship file + change drop file")
    _approve(wt_f, ["src/on_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    _git(work, "commit", "-qm", "add on_main.py")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"RELEASE_MANIFEST.txt"}

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resolved_pr_conflict"
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "unpinned" in text
    assert "tests/test_x.py" in text

    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "feature")
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr
    pins_text = (wt_check / "RELEASE_MANIFEST.txt").read_text(encoding="utf-8")
    assert "src/on_feature.py" in pins_text
    assert "tests/test_x.py" not in pins_text
    # the drop-classified file's own change still landed on the branch, just unpinned.
    assert (wt_check / "tests" / "test_x.py").read_text(encoding="utf-8") == "test x changed\n"


async def test_a_source_conflict_still_opens_a_coder_round_exactly_as_today(store, tmp_path, monkeypatch):
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "src" / "base.py").write_text("base\nfeature change\n", encoding="utf-8")
    _git(wt, "commit", "-qam", "feature edits base.py")
    _push_branch(work, wt, "feature")

    (work / "src" / "base.py").write_text("base\nmain change\n", encoding="utf-8")
    _git(work, "commit", "-qam", "main also edits base.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"src/base.py"}

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    assert resolver_calls == []  # mechanical path never entered
    kinds = [k for k, _ in events]
    assert "pr_conflict" in kinds
    assert "pr_conflict_resolved" not in kinds
    assert "escalated_pr_conflict" not in kinds
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.IMPLEMENTING


async def test_a_derived_shaped_conflict_with_an_unresolvable_base_tip_escalates_not_a_coder_round(
        store, tmp_path, monkeypatch):
    """Review finding on PR #568: after `mechanically_resolvable` replaced
    `all_derived`, a conflict confined to the derived/classification files
    whose base tip could NOT be resolved (the ref vanished between
    enumeration's own resolve and the watcher's) left `eligible=None` and
    fell through to a PAID coder round. Main escalated it ("could not resolve
    the base tip to a commit"). A coder cannot fix these files; the honest
    outcome is the escalation. Fails only wake.py's resolve call — the
    enumeration's own call (inside `conflicting_paths`) still succeeds."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "RELEASE_MANIFEST.txt").write_text("pin feature\n", encoding="utf-8")
    _git(wt, "commit", "-qam", "feature re-pins")
    _push_branch(work, wt, "feature")
    (work / "RELEASE_MANIFEST.txt").write_text("pin main\n", encoding="utf-8")
    _git(work, "commit", "-qam", "main re-pins")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    real_resolve = dc.resolve_base_tip
    calls = {"n": 0}

    async def vanishing_ref(repo_path, base_branch):
        # First caller is `conflicting_paths` (enumeration) -> real answer;
        # the watcher's own call is the one that comes back empty.
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_resolve(repo_path, base_branch)
        return None

    monkeypatch.setattr(dc, "resolve_base_tip", vanishing_ref)
    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"RELEASE_MANIFEST.txt"}
    calls["n"] = 0  # the watcher's enumeration is call #1 again

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(
        t, "https://code.example.com/dev/x/pull/27", "DIRTY", branch="feature")

    assert result == "escalated_pr_conflict", result
    assert resolver_calls == []             # nothing to resolve against
    kinds = [k for k, _ in events]
    assert "escalated_pr_conflict" in kinds
    assert "resumed" not in kinds           # never a coder round
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    assert "could not resolve the base tip" in (stored.blocker or {}).get("evidence", "")


async def test_a_mixed_derived_and_source_conflict_opens_a_coder_round(store, tmp_path, monkeypatch):
    """Not every conflicting path is derived: unchanged behaviour, even
    though RELEASE_MANIFEST.txt is one of the conflicting paths too."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "base.py").write_text("base\nfeature change\n", encoding="utf-8")
    (wt_f / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    _git(wt_f, "add", "-A")
    _git(wt_f, "commit", "-qm", "feature edits base.py and adds on_feature.py")
    _approve(wt_f, ["src/on_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "base.py").write_text("base\nmain change\n", encoding="utf-8")
    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "main edits base.py and adds on_main.py")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"src/base.py", "RELEASE_MANIFEST.txt"}

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    assert resolver_calls == []
    text = next(txt for k, txt in events if k == "pr_conflict")
    assert "src/base.py" in text and "RELEASE_MANIFEST.txt" in text


async def test_a_failing_verify_escalates_and_pushes_nothing(store, tmp_path, monkeypatch):
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    _git(wt_f, "add", "src/on_feature.py")
    _git(wt_f, "commit", "-qm", "add on_feature.py")
    _approve(wt_f, ["src/on_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    _git(work, "commit", "-qm", "add on_main.py")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    before_sha = _git(work, "ls-remote", "origin", "refs/heads/feature").stdout.strip()

    def failing_resolver(repo_path, branch, base_tip_sha, remote="origin",
                          eligible=dc.DERIVED_ARTEFACTS):
        return dc.DerivedResolution(ok=False, step="verify", detail="synthetic failure for the test")

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events, derived_resolver=failing_resolver)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "escalated_pr_conflict"
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    assert stored.blocker["category"] == "NOVEL_UNKNOWN"
    assert "verify" in stored.blocker["question"]

    after_sha = _git(work, "ls-remote", "origin", "refs/heads/feature").stdout.strip()
    assert after_sha == before_sha  # nothing pushed

    kinds = [k for k, _ in events]
    assert "escalated_pr_conflict" in kinds
    assert "pr_conflict_resolved" not in kinds
    assert "resumed" not in kinds


async def test_two_concurrent_branches_collide_on_the_manifest_and_resolve_mechanically(store, tmp_path, monkeypatch):
    """End-to-end: two independently-built branches append distinct
    ship-classified files, both regenerate the manifest, and merging one
    into the other's base produces a REAL git conflict confined to
    RELEASE_MANIFEST.txt -- resolved mechanically, no coder session."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "feat_a.py").write_text("feature a\n", encoding="utf-8")
    _git(wt_a, "add", "src/feat_a.py")
    _git(wt_a, "commit", "-qm", "add feat_a.py")
    _approve(wt_a, ["src/feat_a.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin feat_a.py")
    _push_branch(work, wt_a, "branch-a")

    (work / "src" / "feat_b.py").write_text("feature b\n", encoding="utf-8")
    _git(work, "add", "src/feat_b.py")
    _git(work, "commit", "-qm", "add feat_b.py")
    _approve(work, ["src/feat_b.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin feat_b.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    merged_conflicts = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert merged_conflicts == {"RELEASE_MANIFEST.txt"}

    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    no_coder_session = []
    w._resume = lambda task: no_coder_session.append(task) or pytest.fail("coder round opened")  # type: ignore[assignment]

    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resolved_pr_conflict"
    assert no_coder_session == []


async def test_two_concurrent_count_bumps_are_reconciled_by_merge_arithmetic(
    store, tmp_path, monkeypatch,
):
    """The Issue-1 regression scenario: two branches each add a file
    matching the SAME counted glob rule and each independently (and, alone,
    correctly) bump that rule's declared count by one. Because both edits
    are the identical text change ("1" -> "2"), git auto-merges
    EXPORT_CLASSIFICATION.txt cleanly -- it is NOT a conflicting path -- so
    the only conflict is RELEASE_MANIFEST.txt, `all_derived()` is True, and
    mechanical resolution is attempted. But the merged tree actually holds
    base.py + base_two.py + base_three.py == 3 files matching
    `src/base*.py`, not the 2 the auto-merged count declares. `export_guard
    `export_guard verify` used to catch that drift and refuse, and this test
    pinned "the resolver must NOT report this as resolved" -- the task then
    escalated to a human, who did the arithmetic by hand (2026-08-20, task
    c309a6a3 / PR #511, a review-PASSED delivery). That was the defect, not
    the doctrine: two reviewed counts meeting is arithmetic, not a hand
    decision -- base (2) + (branch (2) - merge-base (1)) == real (3) -- so the
    resolver now rewrites the number under exactly that equality and refuses
    anything else (negative control below). The stub guard refuses `approve`
    on a drifted count with the real guard's phrasing, so this exercises the
    real refuse -> reconcile -> re-approve path, not a shortcut.
    """
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(wt_a, "add", "src/base_two.py")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump counted rule 1 -> 2")
    _approve(wt_a, ["src/base_two.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py")
    _push_branch(work, wt_a, "branch-a")

    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    _git(work, "add", "src/base_three.py")
    _bump_count(work, "src/base*.py", 2)
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "add base_three.py, bump counted rule 1 -> 2")
    _approve(work, ["src/base_three.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin base_three.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    # sanity: the identical count bump auto-merges -- EXPORT_CLASSIFICATION.txt
    # is not a conflicting path, only the manifest is, and it IS eligible.
    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert paths == {"RELEASE_MANIFEST.txt"}
    assert dc.all_derived(paths)

    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resolved_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" in kinds and "resumed" not in kinds
    # The human gate must be able to SEE that a hand-maintained file was
    # edited, and by what arithmetic.
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "EXPORT_CLASSIFICATION.txt count reconciled" in text and "2 -> 3" in text, text
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL
    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "branch-a")
    assert "ship   3  src/base*.py" in (wt_check / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr


async def test_a_count_only_classification_conflict_is_resolved_without_a_coder_session(
    store, tmp_path, monkeypatch,
):
    """The bugfix scenario: EXPORT_CLASSIFICATION.txt is ITSELF a conflicting
    path (unlike the identical-bump case above, which auto-merges cleanly) --
    branch-a bumps the counted rule 1 -> 2 (for the one file it adds), main
    bumps the SAME rule 1 -> 3 (for the two files it adds). The two edits are
    different text on the same line, so git conflicts on it for real. But the
    only difference between the two conflicting hunks is the digit -- same
    verb, same pattern -- so this is arithmetic, not a hand decision, and
    `mechanically_resolvable` must say so and the resolver must repair it with
    the SAME `reconcile_merge_count_drift` arithmetic as the clean-auto-merge
    case, never a second implementation."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(wt_a, "add", "src/base_two.py")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump counted rule 1 -> 2")
    _approve(wt_a, ["src/base_two.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py")
    _push_branch(work, wt_a, "branch-a")

    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    (work / "src" / "base_four.py").write_text("base four\n", encoding="utf-8")
    _git(work, "add", "src/base_three.py", "src/base_four.py")
    _bump_count(work, "src/base*.py", 3)
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "add base_three.py + base_four.py, bump counted rule 1 -> 3")
    _approve(work, ["src/base_three.py", "src/base_four.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin base_three.py + base_four.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    # sanity: this time the count bumps genuinely conflict (different digits
    # on the same line) -- EXPORT_CLASSIFICATION.txt IS a conflicting path,
    # `all_derived()` is False (it never considers this file), but the
    # count-only shape check says the conflict is still mechanical.
    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert dc.CLASSIFICATION_NAME in paths and paths <= (dc.DERIVED_ARTEFACTS | {dc.CLASSIFICATION_NAME})
    assert not dc.all_derived(paths)
    base_tip = await dc.resolve_base_tip(str(work), "main")
    eligible = await dc.mechanically_resolvable(str(work), paths, base_tip, "branch-a")
    assert eligible == paths

    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resolved_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" in kinds and "resumed" not in kinds
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "EXPORT_CLASSIFICATION.txt count reconciled" in text, text
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL
    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "branch-a")
    assert "ship   4  src/base*.py" in (wt_check / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr


# --------------------------------------------------------------------------- #
# Every concurrent PR conflicts on the frozen structural-budget bumps too --  #
# a genuine end-to-end conflict + merge + subprocess-pytest proof, not just   #
# the isolated conflict-SHAPE unit tests in test_budget_conflict_numeric_     #
# only.py.                                                                    #
# --------------------------------------------------------------------------- #


async def test_a_manifest_plus_budget_conflict_is_resolved_without_a_coder_session(
    store, tmp_path, monkeypatch,
):
    """Two branches each independently grow src/no_human/growing.py's
    `grow()` function -- non-overlapping edits, so growing.py itself
    auto-merges cleanly -- and each re-pins it (RELEASE_MANIFEST.txt, a
    derived artefact, conflicts on the two different sha256 pin lines) and
    each bumps the SAME FROZEN_FUNCTION_LINES entry to its OWN branch's
    (different, and in BOTH cases wrong) declared value (tests/
    test_structural_budget.py conflicts on the entry line). Neither side's
    declared number is right -- only the merged tree's own measurement is --
    proved here for real: a genuine merge plus a genuine `pytest` subprocess
    run on the pushed tree, not just the isolated shape unit test already
    covered by test_budget_conflict_numeric_only.py."""
    _use_stub_export_guard(monkeypatch)
    work = _repo_with_budget_stub(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "no_human" / "growing.py").write_text(
        "def grow():\n"
        "    b = 0\n"
        "    a = 1\n"
        "    return a\n",
        encoding="utf-8",
    )
    budget_path_f = wt_f / "tests" / "test_structural_budget.py"
    budget_path_f.write_text(
        budget_path_f.read_text(encoding="utf-8").replace(
            '"growing.py:grow": 3,', '"growing.py:grow": 4,'
        ),
        encoding="utf-8",
    )
    _git(wt_f, "add", "-A")
    _git(wt_f, "commit", "-qm", "feature grows grow() at the top")
    _approve(wt_f, ["src/no_human/growing.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin growing.py (feature)")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "no_human" / "growing.py").write_text(
        "def grow():\n"
        "    a = 1\n"
        "    c = 2\n"
        "    d = 3\n"
        "    return a\n",
        encoding="utf-8",
    )
    budget_path_m = work / "tests" / "test_structural_budget.py"
    budget_path_m.write_text(
        budget_path_m.read_text(encoding="utf-8").replace(
            '"growing.py:grow": 3,', '"growing.py:grow": 5,'
        ),
        encoding="utf-8",
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "main grows grow() at the bottom")
    _approve(work, ["src/no_human/growing.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin growing.py (main)")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    # sanity: growing.py itself auto-merges (non-overlapping insertions);
    # the conflict is confined to the manifest pin + the frozen entry line.
    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"RELEASE_MANIFEST.txt", dc.BUDGET_TEST_PATH}

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resolved_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" in kinds and "resumed" not in kinds
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "RELEASE_MANIFEST.txt" in text and dc.BUDGET_TEST_PATH in text
    assert "structural budget re-anchored" in text, text

    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL  # unchanged by mechanical resolution

    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "feature")
    grown = (wt_check / "src" / "no_human" / "growing.py").read_text(encoding="utf-8")
    assert grown == (
        "def grow():\n"
        "    b = 0\n"
        "    a = 1\n"
        "    c = 2\n"
        "    d = 3\n"
        "    return a\n"
    )
    frozen_text = (wt_check / "tests" / "test_structural_budget.py").read_text(encoding="utf-8")
    # the TRUE merged-tree measurement (6 lines) -- neither branch's own
    # declared, wrong, number (4 or 5).
    assert '"growing.py:grow": 6,' in frozen_text
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr

    # literal proof: pytest tests/test_structural_budget.py passes on the
    # pushed (merged) tree -- the same `run_budget_test` helper the resolver
    # itself runs before pushing, never a re-implemented pytest invocation.
    ok, detail = run_budget_test(str(wt_check))
    assert ok, detail


async def test_a_budget_conflict_mixed_with_a_source_file_opens_a_coder_round(
    store, tmp_path, monkeypatch,
):
    """Not every conflicting path is mechanically resolvable: src/base.py is
    a genuine hand-decision conflict, so this must still open the existing
    bounded coder round -- unchanged -- even though RELEASE_MANIFEST.txt and
    tests/test_structural_budget.py are ALSO among the conflicting paths."""
    _use_stub_export_guard(monkeypatch)
    work = _repo_with_budget_stub(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "base.py").write_text("base\nfeature change\n", encoding="utf-8")
    (wt_f / "src" / "no_human" / "growing.py").write_text(
        "def grow():\n"
        "    b = 0\n"
        "    a = 1\n"
        "    return a\n",
        encoding="utf-8",
    )
    budget_path_f = wt_f / "tests" / "test_structural_budget.py"
    budget_path_f.write_text(
        budget_path_f.read_text(encoding="utf-8").replace(
            '"growing.py:grow": 3,', '"growing.py:grow": 4,'
        ),
        encoding="utf-8",
    )
    _git(wt_f, "add", "-A")
    _git(wt_f, "commit", "-qm", "feature edits base.py and grows grow()")
    _approve(wt_f, ["src/no_human/growing.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin growing.py (feature)")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "base.py").write_text("base\nmain change\n", encoding="utf-8")
    (work / "src" / "no_human" / "growing.py").write_text(
        "def grow():\n"
        "    a = 1\n"
        "    c = 2\n"
        "    d = 3\n"
        "    return a\n",
        encoding="utf-8",
    )
    budget_path_m = work / "tests" / "test_structural_budget.py"
    budget_path_m.write_text(
        budget_path_m.read_text(encoding="utf-8").replace(
            '"growing.py:grow": 3,', '"growing.py:grow": 5,'
        ),
        encoding="utf-8",
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "main edits base.py and grows grow()")
    _approve(work, ["src/no_human/growing.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin growing.py (main)")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"src/base.py", "RELEASE_MANIFEST.txt", dc.BUDGET_TEST_PATH}

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    assert resolver_calls == []
    kinds = [k for k, _ in events]
    assert "pr_conflict" in kinds and "pr_conflict_resolved" not in kinds
    text = next(txt for k, txt in events if k == "pr_conflict")
    assert "src/base.py" in text and "RELEASE_MANIFEST.txt" in text and dc.BUDGET_TEST_PATH in text


async def test_a_classification_count_and_budget_conflict_resolve_together(
    store, tmp_path, monkeypatch,
):
    """A three-way mechanical conflict: EXPORT_CLASSIFICATION.txt (count-only
    arithmetic), RELEASE_MANIFEST.txt (derived, regenerated wholesale) and
    tests/test_structural_budget.py (numeric-only FROZEN_* re-anchoring) all
    conflict on the SAME PR from independent edits on two branches -- and all
    three resolve together in one mechanical pass, one pr_conflict_resolved
    event, no coder round."""
    _use_stub_export_guard(monkeypatch)
    work = _repo_with_budget_stub(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    (wt_a / "src" / "no_human" / "growing.py").write_text(
        "def grow():\n"
        "    b = 0\n"
        "    a = 1\n"
        "    return a\n",
        encoding="utf-8",
    )
    budget_path_a = wt_a / "tests" / "test_structural_budget.py"
    budget_path_a.write_text(
        budget_path_a.read_text(encoding="utf-8").replace(
            '"growing.py:grow": 3,', '"growing.py:grow": 4,'
        ),
        encoding="utf-8",
    )
    _git(wt_a, "add", "src/base_two.py")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "-A")
    _git(wt_a, "commit", "-qm", "branch-a: add base_two.py, bump counted rule 1 -> 2, grow grow()")
    _approve(wt_a, ["src/base_two.py", "src/no_human/growing.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py + growing.py (branch-a)")
    _push_branch(work, wt_a, "branch-a")

    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    (work / "src" / "base_four.py").write_text("base four\n", encoding="utf-8")
    (work / "src" / "no_human" / "growing.py").write_text(
        "def grow():\n"
        "    a = 1\n"
        "    c = 2\n"
        "    d = 3\n"
        "    return a\n",
        encoding="utf-8",
    )
    budget_path_m = work / "tests" / "test_structural_budget.py"
    budget_path_m.write_text(
        budget_path_m.read_text(encoding="utf-8").replace(
            '"growing.py:grow": 3,', '"growing.py:grow": 5,'
        ),
        encoding="utf-8",
    )
    _git(work, "add", "src/base_three.py", "src/base_four.py")
    _bump_count(work, "src/base*.py", 3)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "main: add base_three.py + base_four.py, bump counted rule 1 -> 3, grow grow()")
    _approve(work, ["src/base_three.py", "src/base_four.py", "src/no_human/growing.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin base_three.py + base_four.py + growing.py (main)")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert paths == {dc.CLASSIFICATION_NAME, "RELEASE_MANIFEST.txt", dc.BUDGET_TEST_PATH}
    base_tip = await dc.resolve_base_tip(str(work), "main")
    eligible = await dc.mechanically_resolvable(str(work), paths, base_tip, "branch-a")
    assert eligible == paths

    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resolved_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" in kinds and "resumed" not in kinds
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "EXPORT_CLASSIFICATION.txt count reconciled" in text, text
    assert "structural budget re-anchored" in text, text
    assert "RELEASE_MANIFEST.txt" in text and dc.BUDGET_TEST_PATH in text

    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL

    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "branch-a")
    assert "ship   4  src/base*.py" in (wt_check / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    frozen_text = (wt_check / "tests" / "test_structural_budget.py").read_text(encoding="utf-8")
    assert '"growing.py:grow": 6,' in frozen_text
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr
    ok, detail = run_budget_test(str(wt_check))
    assert ok, detail


async def test_a_count_conflict_that_also_flips_a_verb_opens_a_coder_round(store, tmp_path, monkeypatch):
    """Same shape as the count-only conflict above, EXCEPT one side also
    flips the rule's verdict (ship -> drop) instead of only changing the
    digit. That is a hand decision, not arithmetic, so this must still open
    a coder round -- the shape check must be exact-except-count, not merely
    'touches the same line'."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(wt_a, "add", "src/base_two.py")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump counted rule 1 -> 2")
    _approve(wt_a, ["src/base_two.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py")
    _push_branch(work, wt_a, "branch-a")

    cls = work / "EXPORT_CLASSIFICATION.txt"
    cls.write_text(
        cls.read_text(encoding="utf-8").replace("ship   1  src/base*.py", "drop   1  src/base*.py"),
        encoding="utf-8",
    )
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "main reclassifies the counted rule to drop")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert paths == {"EXPORT_CLASSIFICATION.txt"}
    assert not dc.all_derived(paths)
    base_tip = await dc.resolve_base_tip(str(work), "main")
    assert await dc.mechanically_resolvable(str(work), paths, base_tip, "branch-a") is None

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resumed"
    assert resolver_calls == []
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.IMPLEMENTING


async def test_a_count_only_conflict_whose_arithmetic_fails_escalates(store, tmp_path, monkeypatch):
    """A count-only conflict (same verb, same pattern, only the digit
    differs) whose two declared counts don't satisfy the merge-base
    arithmetic must escalate honestly, never guess and push. Main bumps to 5
    instead of the correct 3 for the two files it adds -- a hand mistake, not
    a derivable number."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(wt_a, "add", "src/base_two.py")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump counted rule 1 -> 2")
    _approve(wt_a, ["src/base_two.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py")
    _push_branch(work, wt_a, "branch-a")

    # Hand-pin instead of `_approve()` here: main's own declared count (5) is
    # wrong for main's own tree (3 files: base.py + the two new ones), so the
    # stub `approve` would correctly refuse it as internally inconsistent --
    # that is a DIFFERENT bug than the one under test. This mirrors
    # `test_a_count_drift_that_is_not_merge_arithmetic_still_refuses` above:
    # a hand mistake that only shows up once merged with branch-a.
    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    (work / "src" / "base_four.py").write_text("base four\n", encoding="utf-8")
    _git(work, "add", "src/base_three.py", "src/base_four.py")
    _bump_count(work, "src/base*.py", 5)
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "add base_three.py + base_four.py, bump counted rule 1 -> 5 (wrong)")
    pins = work / "RELEASE_MANIFEST.txt"
    pins.write_text(
        pins.read_text(encoding="utf-8")
        + "0" * 64 + "  src/base_four.py\n"
        + "0" * 64 + "  src/base_three.py\n",
        encoding="utf-8",
    )
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "hand pin base_three.py + base_four.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    base_tip = await dc.resolve_base_tip(str(work), "main")
    eligible = await dc.mechanically_resolvable(str(work), paths, base_tip, "branch-a")
    assert eligible == paths

    before = _git(work, "rev-parse", "origin/branch-a").stdout.strip()
    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result != "resolved_pr_conflict"
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    evidence = (stored.blocker.get("evidence") or "") + (stored.blocker.get("question") or "")
    assert "not merge arithmetic" in evidence or "not a mechanical merge" in evidence, stored.blocker
    assert _git(work, "rev-parse", "origin/branch-a").stdout.strip() == before, "arithmetic failed but something was pushed"


def test_the_count_repair_reuses_reconcile_merge_count_drift(tmp_path):
    """The fix must never grow a second arithmetic implementation --
    `derived_conflict` imports and calls the EXISTING
    `approve_merge.reconcile_merge_count_drift`, proven here by identity, not
    by behaviour (behaviour is covered by the end-to-end tests above)."""
    from no_human.vcs import approve_merge

    assert dc.reconcile_merge_count_drift is approve_merge.reconcile_merge_count_drift


async def test_an_export_classification_conflict_alone_opens_a_coder_round(store, tmp_path, monkeypatch):
    """A conflict confined to EXPORT_CLASSIFICATION.txt is not mechanically
    resolvable: its counts are hand-maintained and no command rebuilds them,
    so even though it sits in the export gate next to RELEASE_MANIFEST.txt,
    a coder round opens exactly as for a source-file conflict."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    _cls = wt / "EXPORT_CLASSIFICATION.txt"
    _cls.write_text(_cls.read_text(encoding="utf-8").replace("drop   1  tests/**", "drop docs/**\ndrop   1  tests/**"), encoding="utf-8")
    _git(wt, "commit", "-qam", "feature reclassifies the counted rule to 5")
    _push_branch(work, wt, "feature")

    _cls = work / "EXPORT_CLASSIFICATION.txt"
    _cls.write_text(_cls.read_text(encoding="utf-8").replace("drop   1  tests/**", "drop dist/**\ndrop   1  tests/**"), encoding="utf-8")
    _git(work, "commit", "-qam", "main reclassifies the counted rule to 7")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"EXPORT_CLASSIFICATION.txt"}
    assert not dc.all_derived(paths)
    base_tip = await dc.resolve_base_tip(str(work), "main")
    assert await dc.mechanically_resolvable(str(work), paths, base_tip, "feature") is None

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    assert resolver_calls == []
    kinds = [k for k, _ in events]
    assert "pr_conflict" in kinds
    assert "pr_conflict_resolved" not in kinds
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.IMPLEMENTING


async def test_an_export_classification_conflict_mixed_with_manifest_opens_a_coder_round(store, tmp_path, monkeypatch):
    """Not every conflicting path is derived even when RELEASE_MANIFEST.txt
    is one of them: a mixed EXPORT_CLASSIFICATION.txt + manifest conflict
    still opens a coder round, matching the mixed source+manifest case."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    _git(wt, "add", "src/on_feature.py")
    _cls = wt / "EXPORT_CLASSIFICATION.txt"
    _cls.write_text(_cls.read_text(encoding="utf-8").replace("drop   1  tests/**", "drop docs/**\ndrop   1  tests/**"), encoding="utf-8")
    _git(wt, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt, "commit", "-qm", "feature adds on_feature.py, adds a drop rule for docs/ (counts stay correct)")
    _approve(wt, ["src/on_feature.py"])
    _git(wt, "add", "RELEASE_MANIFEST.txt")
    _git(wt, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt, "feature")

    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    _cls = work / "EXPORT_CLASSIFICATION.txt"
    _cls.write_text(_cls.read_text(encoding="utf-8").replace("drop   1  tests/**", "drop dist/**\ndrop   1  tests/**"), encoding="utf-8")
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "main adds on_main.py, adds a drop rule for dist/ (counts stay correct)")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"RELEASE_MANIFEST.txt", "EXPORT_CLASSIFICATION.txt"}
    assert not dc.all_derived(paths)
    base_tip = await dc.resolve_base_tip(str(work), "main")
    assert await dc.mechanically_resolvable(str(work), paths, base_tip, "feature") is None

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    assert resolver_calls == []
    text = next(txt for k, txt in events if k == "pr_conflict")
    assert "RELEASE_MANIFEST.txt" in text and "EXPORT_CLASSIFICATION.txt" in text
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.IMPLEMENTING


# --------------------------------------------------------------------------- #
# A merge of two reviewed COUNT bumps is arithmetic, not a hand decision      #
# --------------------------------------------------------------------------- #


def _bump_count(root: Path, pattern: str, new: int) -> None:
    text = (root / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    import re as _re
    text = _re.sub(rf"^(ship\s+)\d+(\s+{_re.escape(pattern)})$", rf"\g<1>{new}\2", text, flags=_re.M)
    (root / "EXPORT_CLASSIFICATION.txt").write_text(text, encoding="utf-8")


def _bump_drop_count(root: Path, pattern: str, new: int) -> None:
    text = (root / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    import re as _re
    text = _re.sub(rf"^(drop\s+)\d+(\s+{_re.escape(pattern)})$", rf"\g<1>{new}\2", text, flags=_re.M)
    (root / "EXPORT_CLASSIFICATION.txt").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# A manifest-only conflict must PRUNE a stale pin, not escalate finished work #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_manifest_only_conflict_with_a_stale_pin_is_pruned_not_escalated(
    store, tmp_path, monkeypatch,
):
    """Defect 1: a shipped file removed on one side of a finished PR leaves
    the OTHER side's regenerated manifest still pinning it -- `export_guard.
    py approve <paths>` only ever writes pins for the paths it is given, it
    never drops one for a path that stopped shipping. Base tip's export gate
    never ran `approve --prune` for src/old_ship.py's removal (a wholly
    ordinary, pre-existing state -- exactly what auto-pruning on conflict
    exists to make unnecessary to fix by hand), so the merged tree's
    regenerated manifest still pins a file that no longer exists anywhere.
    `verify`'s "pinned but not shipped" refusal is real and correct -- the
    fix is not to weaken that gate, it is to prune BEFORE verify runs so a
    finished, correctly-reviewed PR does not escalate over a mechanical
    artefact. RED today: escalates at step 'verify'. src/base.py's pin (it
    still ships) must survive untouched -- prune must never drop a live pin."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    (work / "src" / "old_ship.py").write_text("old ship\n", encoding="utf-8")
    _git(work, "add", "src/old_ship.py")
    _git(work, "commit", "-qm", "add old_ship.py")
    _approve(work, ["src/old_ship.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin old_ship.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "feat_a.py").write_text("feature a\n", encoding="utf-8")
    _git(wt_a, "add", "src/feat_a.py")
    _git(wt_a, "commit", "-qm", "add feat_a.py")
    _approve(wt_a, ["src/feat_a.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin feat_a.py")
    _push_branch(work, wt_a, "branch-a")

    _git(work, "rm", "-q", "src/old_ship.py")
    (work / "src" / "feat_b.py").write_text("feature b\n", encoding="utf-8")
    _git(work, "add", "src/feat_b.py")
    _git(work, "commit", "-qm", "remove old_ship.py, add feat_b.py")
    _approve(work, ["src/feat_b.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin feat_b.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert paths == {"RELEASE_MANIFEST.txt"}
    assert dc.all_derived(paths)

    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resolved_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" in kinds and "resumed" not in kinds
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL

    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "branch-a")
    manifest = (wt_check / "RELEASE_MANIFEST.txt").read_text(encoding="utf-8")
    assert "src/old_ship.py" not in manifest, manifest       # stale pin pruned
    assert "src/base.py" in manifest                         # still-shipping pin survives
    assert "src/feat_a.py" in manifest and "src/feat_b.py" in manifest
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr


@pytest.mark.asyncio
async def test_a_manifest_only_conflict_reconciles_a_cleanly_merged_count_drift(
    store, tmp_path, monkeypatch,
):
    """Defect 2, the DROP-classified counterpart of
    `test_two_concurrent_count_bumps_are_reconciled_by_merge_arithmetic`
    above. Neither branch adds a ship-classified file (both add a file under
    the COUNTED `drop 1 tests/**` rule instead), so `shipped_changed` is
    empty and step 5's existing reconcile hook never runs at all -- and
    because neither side touches EXPORT_CLASSIFICATION.txt differently (the
    identical "1" -> "2" bump auto-merges cleanly, exactly like the
    ship-classified case), the classification file is never itself a
    conflicting path either. So each side also makes an unrelated, distinct
    manifest edit (as a real coder's re-approve comment would) to force
    RELEASE_MANIFEST.txt itself to conflict -- the only conflicting path,
    eligible for mechanical resolution. RED today: the merged tree holds 3
    files matching `tests/**` (test_x.py + test_y.py + test_w.py) against a
    declared count of 2 -- correct merge arithmetic, base (2) + branch (2) -
    merge-base (1) == 3 -- but because the classification file itself was
    never part of the conflict, today's step-7 backstop is gated off
    (`CLASSIFICATION_NAME in eligible` is False for a manifest-only
    conflict), so this finished, correctly-arithmetic PR escalates instead
    of being reconciled the same way the ship-classified case already is."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "tests" / "test_y.py").write_text("test y\n", encoding="utf-8")
    _git(wt_a, "add", "tests/test_y.py")
    _bump_drop_count(wt_a, "tests/**", 2)
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    pins = wt_a / "RELEASE_MANIFEST.txt"
    pins.write_text(pins.read_text(encoding="utf-8") + "# re-approved on branch-a\n", encoding="utf-8")
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "add tests/test_y.py, bump counted drop rule 1 -> 2")
    _push_branch(work, wt_a, "branch-a")

    (work / "tests" / "test_w.py").write_text("test w\n", encoding="utf-8")
    _git(work, "add", "tests/test_w.py")
    _bump_drop_count(work, "tests/**", 2)
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    pins = work / "RELEASE_MANIFEST.txt"
    pins.write_text(pins.read_text(encoding="utf-8") + "# main re-approved\n", encoding="utf-8")
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "add tests/test_w.py, bump counted drop rule 1 -> 2")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    # sanity: the identical count bump auto-merges -- EXPORT_CLASSIFICATION.txt
    # is not a conflicting path, only the manifest is (forced by the two
    # distinct re-approve comments), and it IS eligible.
    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert paths == {"RELEASE_MANIFEST.txt"}
    assert dc.all_derived(paths)

    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resolved_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" in kinds and "resumed" not in kinds
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "EXPORT_CLASSIFICATION.txt count reconciled" in text and "2 -> 3" in text, text
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL
    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "branch-a")
    assert "drop   3  tests/**" in (wt_check / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr


@pytest.mark.asyncio
async def test_a_count_drift_that_is_not_merge_arithmetic_still_refuses(store, tmp_path, monkeypatch):
    """Negative control: main added a file WITHOUT bumping the count (a stale
    count on one side is a hand problem, not merge arithmetic) — the resolver
    must refuse at 'regenerate' and show the arithmetic, never guess."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "base_feature.py").write_text("f\n", encoding="utf-8")
    _bump_count(wt_f, "src/base*.py", 2)
    _git(wt_f, "add", "-A")
    _git(wt_f, "commit", "-qm", "add base_feature.py (count 1->2)")
    _approve(wt_f, ["src/base_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin base_feature.py")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "base_main.py").write_text("m\n", encoding="utf-8")   # count left at 1
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "add base_main.py (count NOT bumped)")
    pins = (work / "RELEASE_MANIFEST.txt")
    pins.write_text(pins.read_text(encoding="utf-8") + "0" * 64 + "  src/base_main.py\n", encoding="utf-8")
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "hand pin (conflicts with feature's manifest)")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    base_tip = _git(work, "rev-parse", "origin/main").stdout.strip()
    res = dc.resolve_derived_conflict(str(work), "feature", base_tip, remote="origin")
    assert not res.ok and res.step == "regenerate"
    assert "not a mechanical merge" in res.detail and "real 3" in res.detail, res.detail


# --------------------------------------------------------------------------- #
# The reconcile's refusal guards are not decorative                           #
# --------------------------------------------------------------------------- #


def _three_way_repo(tmp_path):
    """A repo whose HEAD is its own base/branch/merge-base (the guards under
    test fire before any arithmetic), with the stub guard wired."""
    from no_human.vcs.approve_merge import reconcile_merge_count_drift
    work = _repo(tmp_path)
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    return work, sha, reconcile_merge_count_drift


def test_reconcile_refuses_when_the_refusal_names_no_drift(tmp_path):
    work, sha, reconcile = _three_way_repo(tmp_path)
    ok, note = reconcile(work, sha, sha, "approve: REFUSED -- not ship-classified")
    assert not ok and "no count drift" in note


def test_reconcile_refuses_a_rule_absent_on_a_side(tmp_path):
    work, sha, reconcile = _three_way_repo(tmp_path)
    ok, note = reconcile(work, sha, sha,
                         "EXPORT_CLASSIFICATION.txt:9: `ship 1  nope/*.py` actually wins 2 file(s).")
    assert not ok and "not present on every side" in note


def test_reconcile_refuses_a_duplicated_rule(tmp_path):
    work, sha, reconcile = _three_way_repo(tmp_path)
    cls = work / "EXPORT_CLASSIFICATION.txt"
    cls.write_text(cls.read_text(encoding="utf-8") + "ship   0  src/base*.py\n", encoding="utf-8")
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "duplicate rule")
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    ok, note = reconcile(work, sha, sha,
                         "EXPORT_CLASSIFICATION.txt:2: `ship 1  src/base*.py` actually wins 2 file(s).")
    assert not ok and "more than once" in note


def test_reconcile_refuses_when_the_named_line_is_not_in_the_file(tmp_path):
    """The refusal says the rule declares 7; the merged file declares 1 —
    nothing to rewrite, and guessing is exactly what is forbidden."""
    work, sha, reconcile = _three_way_repo(tmp_path)
    # base==branch==merge-base ⇒ expected == declared(1) ⇒ real must be 1 to
    # pass the arithmetic; 1 != 7 on the line ⇒ 0 hits.
    ok, note = reconcile(work, sha, sha,
                         "EXPORT_CLASSIFICATION.txt:2: `ship 7  src/base*.py` actually wins 1 file(s).")
    assert not ok and "matched 0 line(s)" in note
    assert "ship   1  src/base*.py" in (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")


def test_reconcile_refuses_without_a_merge_base(tmp_path):
    work, sha, reconcile = _three_way_repo(tmp_path)
    ok, note = reconcile(work, sha, "0" * 40,
                         "EXPORT_CLASSIFICATION.txt:2: `ship 1  src/base*.py` actually wins 2 file(s).")
    assert not ok and "no merge base" in note


# --------------------------------------------------------------------------- #
# COMMIT-time reconciliation: an attempt's own diff moved a declared count.   #
# `reconcile_commit_count_drift` shares its refusal parser, rewrite-only-the- #
# number writer and "refuse with arithmetic" reporting with the merge-time    #
# `reconcile_merge_count_drift` above -- only the arithmetic differs (base    #
# diff adds/removes vs. base/branch/merge-base declared counts).              #
# --------------------------------------------------------------------------- #


def _commit_time_repo(tmp_path: Path) -> Path:
    """`_repo` plus a REAL git pre-commit hook wired via `_HOOK_CHECK_SRC`
    (the same stub `tests/test_vcs.py` uses for its realistic manifest gate),
    so committing a pinned-file change without a matching manifest re-pin
    takes the REACTIVE path in `commit_with_manifest_repair`: hook refusal ->
    parse -> re-approve subprocess -> `_try_reconcile_count_drift` ->
    `reconcile_commit_count_drift(repo, "HEAD", ...)` -> retry -> commit."""
    work = _repo(tmp_path)
    hook_py = work / ".git" / "hooks" / "_check_manifest.py"
    hook_py.write_text(_HOOK_CHECK_SRC, encoding="utf-8")
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.write_text('#!/bin/sh\nexec python3 "$(dirname "$0")/_check_manifest.py"\n', encoding="utf-8")
    hook.chmod(0o755)
    return work


def test_commit_time_count_drift_reconciles_and_the_attempt_proceeds(tmp_path):
    """AC1 (positive repro): the attempt's diff adds exactly one drop-
    classified `tests/*.py` file and leaves the declared count untouched.
    Committing a pinned-file change triggers the real pre-commit hook's
    refusal (staged content != pin), which forces the reactive path; that
    path must reconcile the incidental `tests/**` count drift instead of
    failing the whole attempt. RED on unfixed code (reconcile wiring removed
    or stub schema mismatched): `commit_with_manifest_repair` raises
    `GitError` with 'actually wins' instead of returning a commit sha."""
    work = _commit_time_repo(tmp_path)
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y", never_push_to=[])
    repo.create_branch("no-human/commit-time-drift", base="main")

    # A normal coder edit to the already-pinned file (forces the hook to
    # refuse: staged content no longer matches RELEASE_MANIFEST.txt's pin).
    (work / "src" / "base.py").write_text("base\nchanged\n", encoding="utf-8")
    # The incidental new test file: bumps the REAL drop-tests/** count from
    # 1 to 2 without anyone touching EXPORT_CLASSIFICATION.txt.
    (work / "tests" / "test_y.py").write_text("test y\n", encoding="utf-8")

    repairs = []
    result = commit_with_manifest_repair(
        repo, ["src/base.py", "tests/test_y.py"], "fix: y",
        on_repair=lambda paths, note: repairs.append((paths, note)),
    )
    assert result.sha, result
    assert repairs, "on_repair was never called -- reconciliation did not run"
    assert "count drift reconciled" in repairs[-1][1], repairs[-1]

    cls_text = _git(work, "show", "HEAD:EXPORT_CLASSIFICATION.txt").stdout
    assert "drop   2  tests/**" in cls_text, cls_text
    changed = _git(work, "show", "--name-only", "--format=", "HEAD").stdout
    assert "EXPORT_CLASSIFICATION.txt" in changed, changed


def test_commit_time_unexplained_drift_fails_with_the_reconcilers_arithmetic(tmp_path):
    """Refusal path, end to end: the drift at the named rule is +2 but the
    attempt's own diff explains only one file of it, so the safety net must
    DECLINE -- and the failed attempt must show that it ran and why
    (the reconciler's arithmetic), not only the guard's stale number.
    RED on the first cut, which computed the arithmetic and threw it away."""
    work = _commit_time_repo(tmp_path)
    # Pre-existing drift the attempt did not cause: a second tests/** file
    # already on main with the declared count left at 1.
    (work / "tests" / "test_pre.py").write_text("pre\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "pre-existing drift, not this attempt")
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y", never_push_to=[])
    repo.create_branch("no-human/commit-time-unexplained", base="main")

    (work / "src" / "base.py").write_text("base\nchanged\n", encoding="utf-8")
    (work / "tests" / "test_y.py").write_text("test y\n", encoding="utf-8")
    before = (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")

    with pytest.raises(GitError) as err:
        commit_with_manifest_repair(repo, ["src/base.py", "tests/test_y.py"], "fix: y")
    msg = str(err.value)
    assert "manifest re-approve failed" in msg, msg
    assert "count-drift reconciliation declined" in msg, msg
    assert "not explained by this attempt's own changes" in msg, msg
    # Nothing was rewritten: the number is a hand decision here.
    assert (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8") == before


def test_reconcile_commit_count_drift_explains_an_added_file(tmp_path):
    """AC (unit): a single added file the attempt's own diff introduced
    fully explains a ship rule's 1 -> 2 drift."""
    work = _repo(tmp_path)
    base_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    (work / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(work, "add", "src/base_two.py")
    _git(work, "commit", "-qm", "add base_two.py, count not bumped")

    ok, note = reconcile_commit_count_drift(
        work, base_sha,
        "EXPORT_CLASSIFICATION.txt:2: `ship 1  src/base*.py` actually wins 2 file(s).",
    )
    assert ok and "1 -> 2" in note, note
    assert "ship   2  src/base*.py" in (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")


def test_reconcile_commit_count_drift_explains_a_removed_file(tmp_path):
    """AC (negative-shaped positive): a removed file reconciles to N-1."""
    work = _repo(tmp_path)
    base_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "rm", "-q", "tests/test_x.py")
    _git(work, "commit", "-qm", "remove tests/test_x.py, count not bumped")

    ok, note = reconcile_commit_count_drift(
        work, base_sha,
        "EXPORT_CLASSIFICATION.txt:3: `drop 1  tests/**` actually wins 0 file(s).",
    )
    assert ok and "1 -> 0" in note, note
    assert "drop   0  tests/**" in (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")


def test_reconcile_commit_count_drift_refuses_unexplained_drift(tmp_path):
    """AC (negative): drift is +2 at the named rule, but the attempt's own
    diff (base_sha..worktree) only added ONE matching file -- one file of
    drift predates the attempt and is not this reconciler's to explain.
    Refuses exactly as today, with the unexplained amount named; no rewrite."""
    work = _repo(tmp_path)
    # Pre-existing drift the attempt did not cause.
    (work / "tests" / "test_pre.py").write_text("pre\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "pre-existing drift, not this attempt")
    base_sha = _git(work, "rev-parse", "HEAD").stdout.strip()

    (work / "tests" / "test_new.py").write_text("new\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "attempt adds one more drop file, count not bumped")

    before = (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    ok, note = reconcile_commit_count_drift(
        work, base_sha,
        "EXPORT_CLASSIFICATION.txt:3: `drop 1  tests/**` actually wins 3 file(s).",
    )
    assert not ok and "not explained by this attempt's own changes" in note, note
    assert (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8") == before


def test_reconcile_commit_count_drift_ignores_a_non_matching_added_file(tmp_path):
    """AC (negative): a drop-classified added file must not explain a SHIP
    rule's drift -- the winner lookup (`cls.wins`), not mere presence in the
    diff, decides which rule a path counts against."""
    work = _repo(tmp_path)
    (work / "src" / "base_extra.py").write_text("extra\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "pre-existing ship drift, not this attempt")
    base_sha = _git(work, "rev-parse", "HEAD").stdout.strip()

    (work / "tests" / "test_new2.py").write_text("new2\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "attempt adds only a drop-classified file")

    before = (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    ok, note = reconcile_commit_count_drift(
        work, base_sha,
        "EXPORT_CLASSIFICATION.txt:2: `ship 1  src/base*.py` actually wins 2 file(s).",
    )
    assert not ok and "not explained by this attempt's own changes" in note, note
    assert (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8") == before


def test_reconcile_commit_count_drift_reuses_the_merge_reconcilers_shared_code():
    """The parser, number-writer and refusal reporting are the SAME code as
    `reconcile_merge_count_drift` -- imported, not re-implemented -- and the
    only win-count source is `cls.wins` (no second glob matcher)."""
    src = inspect.getsource(approve_merge)
    assert src.count("def _rewrite_declared_count(") == 1
    assert src.count("def _write_classification_lines(") == 1
    assert src.count("COUNT_DRIFT_RE = ") == 1

    fn_src = inspect.getsource(reconcile_commit_count_drift)
    assert "_rewrite_declared_count(" in fn_src
    assert "_write_classification_lines(" in fn_src
    assert "COUNT_DRIFT_RE" in fn_src
    assert "fnmatch" not in fn_src, "a second glob matcher would defeat cls.wins as the single win-count source"


# --------------------------------------------------------------------------- #
# PROACTIVE-time reconciliation: `approve_pending_pins` (run before every     #
# commit attempt) must reconcile a count drift the SAME way the reactive     #
# path does, not just log a refusal -- an attempt that adds a brand-new ship #
# file + a brand-new drop-classified test file, touching no already-pinned   #
# file, never triggers the reactive path at all (no pinned content changed,  #
# so no hook refusal to react to).                                           #
# --------------------------------------------------------------------------- #


def test_proactive_pin_maintenance_reconciles_a_count_drift_and_commits(tmp_path):
    """AC1: an attempt that adds a brand-new ship file AND a brand-new
    drop-classified test file -- touching NO already-pinned file -- never
    forces the REAL pre-commit hook to refuse (nothing pinned changed), so
    only the PROACTIVE `approve --all --prune` step ever sees the stale
    declared counts. That step must reconcile via the SAME
    `_try_reconcile_count_drift` helper the reactive path uses, not just log
    the refusal and leave the attempt to commit unpinned with stale counts.
    RED on unfixed code: the proactive approve refuses rc 2 on the count
    drift, EXPORT_CLASSIFICATION.txt keeps its stale counts, and
    src/base2.py is never pinned -- yet the commit itself still "succeeds"
    (there is no hook here to catch it), which is exactly the silent-failure
    the followup ticket describes."""
    work = _repo(tmp_path)
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y", never_push_to=[])
    repo.create_branch("no-human/proactive-drift", base="main")

    # Brand-new ship file (bumps `ship 1 src/base*.py` -> 2) and brand-new
    # drop-classified test file (bumps `drop 1 tests/**` -> 2). Neither
    # touches src/base.py (the already-pinned file), so this is isolated to
    # the PROACTIVE path exclusively -- no hook refusal is even possible.
    (work / "src" / "base2.py").write_text("base2\n", encoding="utf-8")
    (work / "tests" / "test_y.py").write_text("test y\n", encoding="utf-8")

    repairs = []
    result = commit_with_manifest_repair(
        repo, ["src/base2.py", "tests/test_y.py"], "feat: base2 + y",
        on_repair=lambda paths, note: repairs.append((paths, note)),
    )
    assert result.sha, result
    assert repairs, "on_repair was never called -- the proactive step did not reconcile"
    last_note = repairs[-1][1]
    assert "count drift reconciled" in last_note, last_note

    cls_text = _git(work, "show", "HEAD:EXPORT_CLASSIFICATION.txt").stdout
    assert "ship   2  src/base*.py" in cls_text, cls_text
    assert "drop   2  tests/**" in cls_text, cls_text

    changed = _git(work, "show", "--name-only", "--format=", "HEAD").stdout
    assert "EXPORT_CLASSIFICATION.txt" in changed, changed
    assert "RELEASE_MANIFEST.txt" in changed, changed

    pins = (work / "RELEASE_MANIFEST.txt").read_text(encoding="utf-8")
    assert "src/base2.py" in pins, pins


def test_proactive_unexplained_count_drift_is_logged_with_the_reconcilers_arithmetic(
        tmp_path, caplog):
    """AC2: a drift the attempt did NOT cause (already on `main` before the
    branch existed) must NOT be silently reconciled by the proactive step --
    only drift this attempt's own diff fully explains is a mechanical fix,
    anything else stays a hand decision. The commit still succeeds (the
    proactive path is advisory-only by contract), but the warning must carry
    the reconciler's own declined arithmetic, not just the guard's stale
    number, so a human reading the log sees the safety net ran and why it
    declined."""
    work = _repo(tmp_path)
    # Pre-existing drift the attempt did not cause: a second tests/** file
    # already on `main`, declared count left at 1.
    (work / "tests" / "test_pre.py").write_text("pre\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "pre-existing drift, not this attempt")
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y", never_push_to=[])
    repo.create_branch("no-human/proactive-unexplained", base="main")

    (work / "tests" / "test_y.py").write_text("test y\n", encoding="utf-8")
    before = (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")

    repairs = []
    with caplog.at_level("WARNING"):
        result = commit_with_manifest_repair(
            repo, ["tests/test_y.py"], "feat: y",
            on_repair=lambda paths, note: repairs.append((paths, note)),
        )
    assert result.sha, result
    assert not repairs, f"on_repair fired on an unexplained drift: {repairs}"
    assert (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8") == before

    assert any("count-drift reconciliation declined" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]
    assert any("not explained by this attempt's own changes" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]


def test_proactive_drop_only_count_drift_still_reports_the_ledger_rewrite(tmp_path):
    """Attempt-1 regression: a DROP-only drift (no new/changed ship file)
    leaves the post-reconciliation retry's `approve --all --prune` with
    nothing new to (re-)pin or prune -- `r_approved` and `r_pruned` are BOTH
    empty even though `_try_reconcile_count_drift` already rewrote and
    staged EXPORT_CLASSIFICATION.txt before the retry ran. The independent
    review of attempt 1 found this exact gap (manifest_repair.py:259): the
    retry branch only called `on_repair` when `r_approved or r_pruned` was
    non-empty, so the ledger rewrite rode into the commit unreported. RED on
    that code: `on_repair` never fires even though
    EXPORT_CLASSIFICATION.txt changed and shipped in the commit."""
    work = _repo(tmp_path)
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y", never_push_to=[])
    repo.create_branch("no-human/proactive-drop-only-drift", base="main")

    # Only a new drop-classified file -- no new/changed ship file, so the
    # retry's `--all --prune` finds nothing to (re-)pin or prune.
    (work / "tests" / "test_y.py").write_text("test y\n", encoding="utf-8")

    repairs = []
    result = commit_with_manifest_repair(
        repo, ["tests/test_y.py"], "feat: y",
        on_repair=lambda paths, note: repairs.append((paths, note)),
    )
    assert result.sha, result
    assert repairs, "the ledger rewrite (EXPORT_CLASSIFICATION.txt) was never reported to on_repair"
    assert "count drift reconciled" in repairs[-1][1], repairs[-1]

    cls_text = _git(work, "show", "HEAD:EXPORT_CLASSIFICATION.txt").stdout
    assert "drop   2  tests/**" in cls_text, cls_text
    changed = _git(work, "show", "--name-only", "--format=", "HEAD").stdout
    assert "EXPORT_CLASSIFICATION.txt" in changed, changed


def test_approve_pending_pins_reuses_the_shared_count_drift_reconciler():
    """AC3 (reuse-by-identity): the proactive path's count-drift handling
    must call the SAME `_try_reconcile_count_drift` helper the reactive path
    uses -- no second refusal parser, no second drift regex."""
    fn_src = inspect.getsource(manifest_repair.approve_pending_pins)
    assert "_try_reconcile_count_drift(" in fn_src
    assert "COUNT_DRIFT_RE" in fn_src
    assert "re.compile(" not in fn_src, "a second drift regex would defeat the shared parser"


# --------------------------------------------------------------------------- #
# A drift that never reaches `approve` is still stopped by `verify` (step 7)  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_drift_that_skips_approve_is_caught_by_verify_and_escalates(store, tmp_path, monkeypatch):
    """The branch touches ONLY its manifest (a coder's re-approve comment), so
    `base..branch` names nothing ship-classified; main meanwhile added
    tests/test_z.py under the COUNTED drop rule without bumping it — a real
    human bug (the extra file is NOT explained by merge arithmetic: real 2 !=
    base 1 + (branch 1 - merge-base 1) = 1), not a mechanical count-merge.
    Step 5 now always runs an `approve --prune` pass (even with nothing
    ship-classified to pin) so it catches this drift itself and attempts
    `reconcile_merge_count_drift`, which correctly refuses (fails closed) and
    reports it at step 'regenerate' — one step earlier than before this
    module always ran a prune pass, but the outcome is identical: the task
    must still escalate and nothing may be pushed. RED when `check_counts` is
    removed from the fixture guard's `verify` (the mutant pushes the drifted
    tree) — the proof that gate is live. Also the reconcile's known limit: a
    drift that is not merge arithmetic always fails closed, wherever it is
    first observed."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    pins = wt_a / "RELEASE_MANIFEST.txt"
    pins.write_text(pins.read_text(encoding="utf-8") + "# re-approved on the branch\n", encoding="utf-8")
    _git(wt_a, "add", "-A")
    _git(wt_a, "commit", "-qm", "manifest-only touch")
    _push_branch(work, wt_a, "branch-a")

    (work / "tests" / "test_z.py").write_text("test z\n", encoding="utf-8")   # drop count left at 1
    pins = work / "RELEASE_MANIFEST.txt"
    pins.write_text(pins.read_text(encoding="utf-8") + "# main re-approved\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "add tests/test_z.py, count NOT bumped")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    before = _git(work, "rev-parse", "origin/branch-a").stdout.strip()

    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert paths == {"RELEASE_MANIFEST.txt"}
    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")
    assert result != "resolved_pr_conflict"
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    evidence = (stored.blocker.get("evidence") or "") + (stored.blocker.get("question") or "")
    assert "step 'regenerate'" in evidence and "actually wins" in evidence, stored.blocker
    assert _git(work, "rev-parse", "origin/branch-a").stdout.strip() == before, "verify refused but something was pushed"


def test_reconcile_refuses_when_the_classification_is_absent_on_a_side(tmp_path):
    work, sha, reconcile = _three_way_repo(tmp_path)
    _git(work, "rm", "-q", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "no classification here")
    gone = _git(work, "rev-parse", "HEAD").stdout.strip()
    ok, note = reconcile(work, gone, sha,
                         "EXPORT_CLASSIFICATION.txt:2: `ship 1  src/base*.py` actually wins 2 file(s).")
    assert not ok and "missing on one side" in note


@pytest.mark.asyncio
async def test_reconcile_repins_a_shipped_classification_file(store, tmp_path, monkeypatch):
    """A repo that SHIPS its classification file pins it, and the count rewrite
    stales that pin. The resolver must re-approve it on the retry or step-7
    `verify` refuses the tree — this is the derived_conflict copy of that
    logic (the land fixture covers approve_merge's copy). RED when the
    `_ship_classified_paths(…, [CLASSIFICATION_NAME])` element is removed
    from the retry targets."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    # reclassify the classification file as SHIPPED and pin it (a different
    # repo's choice; this repo drops it)
    cls = work / "EXPORT_CLASSIFICATION.txt"
    cls.write_text(cls.read_text(encoding="utf-8").replace(
        "drop   1  EXPORT_CLASSIFICATION.txt", "ship   1  EXPORT_CLASSIFICATION.txt"), encoding="utf-8")
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "this repo ships its classification")
    _approve(work, ["EXPORT_CLASSIFICATION.txt"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin the classification")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "-A")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump 1 -> 2")
    _approve(wt_a, ["src/base_two.py", "EXPORT_CLASSIFICATION.txt"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin")
    _push_branch(work, wt_a, "branch-a")

    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    _bump_count(work, "src/base*.py", 2)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "add base_three.py, bump 1 -> 2")
    _approve(work, ["src/base_three.py", "EXPORT_CLASSIFICATION.txt"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    assert await dc.conflicting_paths(str(work), "main", "branch-a") == {"RELEASE_MANIFEST.txt"}
    base_tip = _git(work, "rev-parse", "origin/main").stdout.strip()
    res = dc.resolve_derived_conflict(str(work), "branch-a", base_tip, remote="origin")
    assert res.ok, f"{res.step}: {res.detail}"
    assert "1 -> 3" not in res.reconciled and "2 -> 3" in res.reconciled
    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "branch-a")
    assert "ship   3  src/base*.py" in (wt_check / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr   # the re-pinned classification verifies


# ---------------------------------------------------------------------------
# 855f1263: the forge says CONFLICTING, the local merge finds NO conflicting
# path. An empty set is a contradiction, not a conflict — it must never open
# a paid coder round.
# ---------------------------------------------------------------------------

def _clean_feature(tmp_path):
    """A branch that merges cleanly into main (no overlap): the local
    enumeration is EMPTY while the fixture's forge keeps saying DIRTY."""
    work = _repo(tmp_path)
    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "src" / "feature_only.py").write_text("feature\n", encoding="utf-8")
    _git(wt, "add", "src/feature_only.py")
    _git(wt, "commit", "-qm", "feature adds its own file")
    _push_branch(work, wt, "feature")
    (work / "src" / "base.py").write_text("base\nmain change\n", encoding="utf-8")
    _git(work, "commit", "-qam", "main edits base.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    return work


async def test_a_forge_conflict_with_a_locally_clean_merge_trusts_the_local_merge(
        store, tmp_path, monkeypatch):
    """2026-09-01: 4/4 review-passed deliveries in one day (99e67f5e,
    a9c4f0f4, a5753a8a, 11d5ff46) needed a human takeover because every
    landing moved RELEASE_MANIFEST.txt, the forge flipped every OTHER open
    PR to CONFLICTING, and the watcher's local merge (against refs fetched
    THIS tick) found nothing — yet the old behavior deferred and then
    escalated once the disagreement persisted past a bound. A *definite*
    empty local conflict-path set is now trusted outright: no escalation,
    no coder round, the tick falls through exactly like MERGEABLE and the
    task stays reachable by `nh approve`."""
    _use_stub_export_guard(monkeypatch)
    work = _clean_feature(tmp_path)
    assert await dc.conflicting_paths(str(work), "main", "feature") == set()

    fetches = []
    real_fetch = dc.fetch_conflict_refs

    async def spy_fetch(repo_path, base, branch):
        fetches.append((base, branch))
        return await real_fetch(repo_path, base, branch)

    monkeypatch.setattr(dc, "fetch_conflict_refs", spy_fetch)
    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events,
                 derived_resolver=lambda *a, **k: resolver_calls.append((a, k)))

    result = await w._check_pr_conflict(
        t, "https://code.example.com/dev/x/pull/40", "DIRTY", branch="feature")

    assert result is None, result
    assert fetches == [("main", "feature")]      # it asked again on fresh refs
    assert resolver_calls == []
    kinds = [k for k, _ in events]
    assert "pr_conflict_local_clean" in kinds
    assert "escalated_pr_conflict" not in kinds
    assert "resumed" not in kinds and "pr_conflict" not in kinds
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL   # untouched, reachable by nh approve
    assert stored.blocker is None
    ctx = stored.context or {}
    assert ctx.get("pr_conflict_rounds", 0) == 0, "trusting the local merge is not a rebase round"
    assert ctx.get("pr_conflict_stale_flags") in (0, None)
    assert ctx.get("pr_conflict_local_clean_checks") == 1


async def test_a_conflict_that_appears_after_the_fetch_opens_the_coder_round_as_today(
        store, tmp_path, monkeypatch):
    """The stale side was the watcher's refs: the first enumeration is empty,
    the one after `git fetch` names a source path — exactly today's coder
    round, with the recovery recorded."""
    _use_stub_export_guard(monkeypatch)
    work = _clean_feature(tmp_path)
    real_paths = dc.conflicting_paths
    calls = {"n": 0}

    async def stale_then_fresh(repo_path, base, branch):
        calls["n"] += 1
        if calls["n"] == 1:
            return set()
        return {"src/base.py"}

    monkeypatch.setattr(dc, "conflicting_paths", stale_then_fresh)
    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events,
                 derived_resolver=lambda *a, **k: None)

    result = await w._check_pr_conflict(
        t, "https://code.example.com/dev/x/pull/41", "DIRTY", branch="feature")

    assert result == "resumed", result
    kinds = [k for k, _ in events]
    assert "pr_conflict" in kinds and "pr_conflict_deferred" not in kinds
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.IMPLEMENTING
    assert "names 1 path(s)" in (stored.context or {}).get("pr_conflict_enumerate_error", "")
    assert calls["n"] == 2
    del real_paths


async def test_persistent_forge_local_disagreement_never_escalates_no_matter_how_many_checks_disagree(
        store, tmp_path, monkeypatch):
    """Mirror of the repro above, run past the old bound:
    `max_pr_conflict_rounds + 2` consecutive ticks where the forge keeps
    saying CONFLICTING and the local merge keeps finding nothing must
    NEVER escalate — the old code stopped deferring and escalated once the
    disagreement persisted; the fix removes that bound for this class
    entirely, since a definite empty local set can never be a real
    conflict a coder round could act on."""
    _use_stub_export_guard(monkeypatch)
    work = _clean_feature(tmp_path)
    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events,
                 derived_resolver=lambda *a, **k: None)
    url = "https://code.example.com/dev/x/pull/42"

    results = []
    for _ in range(w.max_pr_conflict_rounds + 2):
        results.append(await w._check_pr_conflict(t, url, "DIRTY", branch="feature"))
        t = await store.get_task(t.id)
        assert t.status == TaskStatus.AWAITING_APPROVAL, "must never escalate mid-loop"

    assert results == [None] * (w.max_pr_conflict_rounds + 2), results
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL
    assert stored.blocker is None
    kinds = [k for k, _ in events]
    assert "escalated_pr_conflict" not in kinds
    assert "resumed" not in kinds
    clean_texts = [txt for k, txt in events if k == "pr_conflict_local_clean"]
    assert len(clean_texts) == w.max_pr_conflict_rounds + 2
    assert all("CONFLICTING" in txt and "no conflicting path" in txt for txt in clean_texts)
    ctx = stored.context or {}
    assert ctx.get("pr_conflict_local_clean_checks") == w.max_pr_conflict_rounds + 2
    assert ctx.get("pr_conflict_stale_flags") in (0, None)


async def test_a_definite_mergeable_clears_the_disagreement_streak(
        store, tmp_path, monkeypatch):
    _use_stub_export_guard(monkeypatch)
    work = _clean_feature(tmp_path)
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=[], derived_resolver=lambda *a, **k: None)
    await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/43", "DIRTY", branch="feature")
    t = await store.get_task(t.id)
    # Trusting the local merge zeroes the streak immediately (there is no
    # bound left to feed) instead of counting up toward one.
    assert (t.context or {}).get("pr_conflict_stale_flags") in (0, None)
    assert (t.context or {}).get("pr_conflict_local_clean_checks") == 1

    w_ok = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN", events=[])
    assert await w_ok._check_pr_conflict(t, "https://code.example.com/dev/x/pull/43", "CLEAN", branch="feature") is None
    t = await store.get_task(t.id)
    assert (t.context or {}).get("pr_conflict_stale_flags") == 0


async def test_a_real_source_conflict_past_the_bound_still_escalates(
        store, tmp_path, monkeypatch):
    """Mirror of the trust-the-local-merge fix: a GENUINE source conflict
    (both sides edit the same line of src/base.py) must still open a coder
    round each tick and escalate once it survives `max_pr_conflict_rounds`
    rebase-round send-backs — trusting the local merge on the honest
    empty-set class must never leak into a class where the local
    enumeration names a real path."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "src" / "base.py").write_text("base\nfeature change\n", encoding="utf-8")
    _git(wt, "commit", "-qam", "feature edits base.py")
    _push_branch(work, wt, "feature")

    (work / "src" / "base.py").write_text("base\nmain change\n", encoding="utf-8")
    _git(work, "commit", "-qam", "main also edits base.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"src/base.py"}

    events = []
    url = "https://code.example.com/dev/x/pull/44"
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events, derived_resolver=lambda *a, **k: None)

    for n in range(1, w.max_pr_conflict_rounds + 1):
        t = await store.get_task(t.id)
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
        result = await w._check_pr_conflict(t, url, "DIRTY", branch="feature")
        assert result == "resumed", f"round {n}: {result}"

    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    final = await w._check_pr_conflict(t, url, "DIRTY", branch="feature")
    assert final == "escalated_pr_conflict", final
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    kinds = [k for k, _ in events]
    assert "escalated_pr_conflict" in kinds
    assert "pr_conflict_local_clean" not in kinds


async def test_the_generated_pair_reuses_the_landed_derived_resolver(
        store, tmp_path, monkeypatch):
    """AC3: the generated-pair-only conflict (RELEASE_MANIFEST.txt) must
    route through the EXISTING resolver landed in commit 26dc16248
    (`derived_conflict.resolve_derived_conflict`), reached via the same
    `derived_resolver` seam the rung has always called — this bugfix adds
    no new resolver, it only stops the empty-set branch from escalating."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    _git(wt_f, "add", "src/on_feature.py")
    _git(wt_f, "commit", "-qm", "add on_feature.py")
    _approve(wt_f, ["src/on_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    _git(work, "commit", "-qm", "add on_main.py")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"RELEASE_MANIFEST.txt"}

    resolver_calls = []

    def spying_resolver(repo_path, branch, base_tip_sha, remote="origin",
                         eligible=dc.DERIVED_ARTEFACTS):
        resolver_calls.append((repo_path, branch, base_tip_sha))
        return dc.resolve_derived_conflict(repo_path, branch, base_tip_sha,
                                            remote=remote, eligible=eligible)

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events, derived_resolver=spying_resolver)
    result = await w._check_pr_conflict(
        t, "https://code.example.com/dev/x/pull/45", "DIRTY", branch="feature")

    assert result == "resolved_pr_conflict"
    assert len(resolver_calls) == 1, "the landed resolver, not a new one, must be invoked"

    # no new resolver-shaped symbol was added to wake.py by this bugfix.
    import no_human.blockers.wake as wake_module
    pre_existing_resolver_names = {
        "resolve_derived_conflict", "resolve_base_tip",
        "resolve_task_pr",  # unrelated: `vcs.task_pr.resolve_task_pr` finds a task's PR URL
    }
    resolver_like = {
        name for name in dir(wake_module)
        if (name.startswith("resolve_") or name.startswith("_resolve"))
        and name not in pre_existing_resolver_names
    }
    assert resolver_like == set(), f"new resolver symbol(s) added to wake.py: {resolver_like}"

    # membership is still exactly the landed rule -- no new eligible artefact.
    assert dc.DERIVED_ARTEFACTS | {dc.CLASSIFICATION_NAME} == {
        "RELEASE_MANIFEST.txt", dc.CLASSIFICATION_NAME,
    }


async def test_the_local_clean_path_touches_neither_the_review_gate_nor_push_to_main(
        store):
    """AC4 (scope): the rung this bugfix changes must not gain a push-to-main
    call or a review-gate call. `push`/`review`/`land_task` appear as plain
    English in the docstring and comments (the rung's own commentary talks
    ABOUT the approve path and about GitHub recomputing `mergeable` after a
    push) -- that prose is fine and expected. What must never appear is an
    actual CALL SHAPE that performs one: a literal ``git push`` argv, a
    push targeting ``refs/heads/main``/``refs/heads/master``, or a direct
    invocation of the review-gate/approve-path functions."""
    import inspect
    from no_human.blockers.wake import WakeWatcher

    src = inspect.getsource(WakeWatcher._check_pr_conflict)
    banned_call_shapes = (
        '"git", "push"',
        "'git', 'push'",
        "refs/heads/main",
        "refs/heads/master",
        "_review_pass_evidence(",
        "land_task(",
        "never_push_to",
    )
    for banned in banned_call_shapes:
        assert banned not in src, f"{banned!r} must not appear in _check_pr_conflict"


async def test_a_count_only_conflict_still_resolves_when_main_gained_a_rule_line(
    store, tmp_path, monkeypatch,
):
    """LIVE REGRESSION (2026-08-22, task 63928824 / PR #592). Branch-a's own
    edit to EXPORT_CLASSIFICATION.txt is nothing but the digit (1 -> 2 for
    the one file it adds), but MAIN — besides bumping the same rule 1 -> 3 —
    also gained a rule line from an unrelated landing. `git merge` conflicts
    ONLY on the count line; the added rule merges cleanly. The eligibility
    check used to compare main's TIP against the branch wholesale, so main's
    clean addition made it say "not count-only" and a paid coder round was
    opened for a number no coder can author. It must judge the BRANCH'S OWN
    edit against the MERGE-BASE instead, and resolve this mechanically.
    """
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(wt_a, "add", "src/base_two.py")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump counted rule 1 -> 2")
    _approve(wt_a, ["src/base_two.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py")
    _push_branch(work, wt_a, "branch-a")

    # main: the same count bump 1 -> 3 for the two files it adds, PLUS a rule
    # line (with the doc file it classifies) that landed from elsewhere and
    # merges cleanly — appended at the end, far from the conflicting line.
    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    (work / "src" / "base_four.py").write_text("base four\n", encoding="utf-8")
    (work / "docs").mkdir()
    (work / "docs" / "note.md").write_text("note\n", encoding="utf-8")
    _bump_count(work, "src/base*.py", 3)
    cls = work / "EXPORT_CLASSIFICATION.txt"
    cls.write_text(cls.read_text(encoding="utf-8") + "drop   1  docs/**\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm",
         "add base_three.py + base_four.py (1 -> 3) and a docs/ rule from another landing")
    _approve(work, ["src/base_three.py", "src/base_four.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin base_three.py + base_four.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert dc.CLASSIFICATION_NAME in paths
    assert paths <= (dc.DERIVED_ARTEFACTS | {dc.CLASSIFICATION_NAME}), paths
    base_tip = await dc.resolve_base_tip(str(work), "main")
    eligible = await dc.mechanically_resolvable(str(work), paths, base_tip, "branch-a")
    assert eligible == dc.DERIVED_ARTEFACTS | {dc.CLASSIFICATION_NAME}

    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(
        t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resolved_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" in kinds and "resumed" not in kinds
    assert "pr_conflict" not in kinds
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "resolved mechanically" in text, text
    # The NOTE names the arithmetic, not just "reconciled": declared 2 (the
    # branch side taken by `--ours`) -> 4, from base 3 + branch 2 - base 1.
    assert "EXPORT_CLASSIFICATION.txt count reconciled" in text, text
    assert "2 -> 4" in text and "(base 3 + branch 2 - merge-base 1)" in text, text
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL

    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "branch-a")
    merged = (wt_check / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    assert "ship   4  src/base*.py" in merged, merged
    assert "drop   1  docs/**" in merged, merged
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr


async def test_a_main_side_rule_added_inside_the_conflicting_hunk_opens_a_coder_round(
    store, tmp_path, monkeypatch,
):
    """Negative control for the case above: main's new rule line lands
    DIRECTLY BESIDE the count line, so git folds both into ONE conflicting
    hunk. The branch's own edit is still count-only, but the hunk carries a
    decision — so eligibility must refuse and a coder round opens."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(wt_a, "add", "src/base_two.py")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump counted rule 1 -> 2")
    _approve(wt_a, ["src/base_two.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py")
    _push_branch(work, wt_a, "branch-a")

    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    (work / "src" / "base_four.py").write_text("base four\n", encoding="utf-8")
    (work / "docs").mkdir()
    (work / "docs" / "note.md").write_text("note\n", encoding="utf-8")
    cls = work / "EXPORT_CLASSIFICATION.txt"
    cls.write_text(
        cls.read_text(encoding="utf-8").replace(
            "ship   1  src/base*.py", "ship   3  src/base*.py\ndrop   1  docs/**"),
        encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "main bumps the rule AND adds a docs/ rule on the next line")
    _approve(work, ["src/base_three.py", "src/base_four.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin base_three.py + base_four.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert dc.CLASSIFICATION_NAME in paths
    base_tip = await dc.resolve_base_tip(str(work), "main")
    assert await dc.mechanically_resolvable(str(work), paths, base_tip, "branch-a") is None

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(
        t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resumed"
    assert resolver_calls == []
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.IMPLEMENTING


def test_reconcile_computes_the_live_321_323_322_arithmetic(tmp_path):
    """The exact numbers from the live incident: merge-base declares 321,
    main declares 323, the branch declares 322 — the merged tree carries the
    files behind both bumps, so the only correct count is 324. Proves the
    reconciler already computes merge-base + (main - base) + (branch - base)
    and needs no second implementation."""
    from no_human.vcs.approve_merge import reconcile_merge_count_drift

    work = _repo(tmp_path)
    _bump_count(work, "src/base*.py", 321)
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "merge-base declares 321")

    _git(work, "checkout", "-qb", "branch-322")
    _bump_count(work, "src/base*.py", 322)
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "branch declares 322")
    branch_sha = _git(work, "rev-parse", "HEAD").stdout.strip()

    _git(work, "checkout", "-q", "main")
    _bump_count(work, "src/base*.py", 323)
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "main declares 323")
    main_sha = _git(work, "rev-parse", "HEAD").stdout.strip()

    # The resolver's worktree is the BRANCH side (`git checkout --ours`), so
    # the file on disk declares 322 and the guard refuses with real 324.
    _bump_count(work, "src/base*.py", 322)
    ok, note = reconcile_merge_count_drift(
        work, main_sha, branch_sha,
        "EXPORT_CLASSIFICATION.txt:2: `ship 322  src/base*.py` actually wins 324 file(s).")

    assert ok, note
    assert "322 -> 324" in note and "(base 323 + branch 322 - merge-base 321)" in note, note
    assert "ship   324  src/base*.py" in (
        work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
