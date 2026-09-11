"""`nh approve` merges the PR: local squash as the operator identity
(operator directive 2026-08-12, refile of 74bf7dec).

Drives REAL git against REAL temp repos — an origin bare repo plus a clone
that stands in for a task's `repo_path` — with a STUB `scripts/export_guard.py`
and a minimal double of `scripts/build_public_export.py`'s classification
grammar (the real files require the source repo's private term inventory —
`build_public_export.py` loads `src/no_human/eval/vendor_terms.py` at IMPORT
TIME, and `export_guard.py`'s `_terms_or_die` refuses outright without it —
neither of which this fixture repo carries). The double reimplements exactly
`parse_classification`/`classify`/`Rule` (same glob-to-regex translation,
same last-match-wins semantics); the REAL grammar is exercised against the
REAL file by tests/test_export_manifest.py, so this is a documented, narrow
double of a heavy dependency, not a second copy of a gate. A `gh` stub on
PATH records every argv it was called with.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import pytest_asyncio
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app
from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs.approve_merge import LandResult, land_task
from no_human.vcs.git import GitError, GitRepo, ProtectedBranch
from no_human.vcs.pr_watcher import default_branch_shipped

pytestmark = pytest.mark.usefixtures("isolated_env_file")

# --------------------------------------------------------------------------- #
# git / fixture plumbing                                                      #
# --------------------------------------------------------------------------- #


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True, check=check,
    )


_CLASSIFICATION = """\
ship 2 src/*.py
ship 2 scripts/*.py
ship 1 EXPORT_CLASSIFICATION.txt
ship 1 RELEASE_MANIFEST.txt
drop 1 README.md
drop 1 tests/*.py
"""

# A minimal double of scripts/build_public_export.py's classification
# grammar — see the module docstring for why the real file cannot be used
# unmodified here. `parse_classification`/`classify`/`Rule` are a verbatim
# port of the real ones (same glob-to-regex translation, same last-match-wins
# semantics); everything else (the term scanner, the build, the manifest
# writer) is intentionally absent — nothing here calls it.
_MINI_BUILD_PUBLIC_EXPORT = '''\
import re
from dataclasses import dataclass, field

CLASSIFICATION_NAME = "EXPORT_CLASSIFICATION.txt"
RELEASE_MANIFEST_NAME = "RELEASE_MANIFEST.txt"


class ExportError(RuntimeError):
    pass


def _entry_to_regex(entry):
    is_dir = entry.endswith("/")
    body = entry.rstrip("/")
    out = []
    i = 0
    while i < len(body):
        if body.startswith("**/", i):
            out.append(r"(?:[^/]+/)*")
            i += 3
        elif body.startswith("**", i):
            out.append(r".*")
            i += 2
        elif body[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif body[i] == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(body[i]))
            i += 1
    return re.compile("".join(out) + (r"/.*" if is_dir else "") + r"\\Z")


@dataclass
class Rule:
    verb: str
    declared: int
    pattern: str
    lineno: int

    def matcher(self):
        return _entry_to_regex(self.pattern)


def parse_classification(text):
    rules = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) != 3 or parts[0] not in ("ship", "drop") or not parts[1].isdigit():
            raise ExportError(f"{CLASSIFICATION_NAME}:{lineno}: cannot parse {line!r}")
        rules.append(Rule(parts[0], int(parts[1]), parts[2], lineno))
    return rules


@dataclass
class Classification:
    wins: dict
    shipped: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    unclassified: list = field(default_factory=list)


def classify(rules, paths):
    matchers = [(r, r.matcher()) for r in rules]
    out = Classification(wins={r.lineno: 0 for r in rules})
    for path in paths:
        winner = None
        for rule, matcher in matchers:
            if matcher.match(path):
                winner = rule
        if winner is None:
            out.unclassified.append(path)
            continue
        out.wins[winner.lineno] += 1
        (out.shipped if winner.verb == "ship" else out.dropped).append(path)
    return out
'''

# A stub of scripts/export_guard.py: same CLI surface (approve <paths...>,
# verify) and the same exit-code contract (0/1/2 for approve, 0/1 for
# verify), over the mini classification grammar above — but with no term
# scan (the real `_terms_or_die` refuses without the source repo's private
# term inventory). `REFUSE_SCAN` / `REFUSE_BEFORE_WRITE` / `FORCE_VERIFY_FAIL`
# marker files (committed by a test's branch) drive the failure paths
# deterministically.
_EXPORT_GUARD_STUB = '''\
"""Test stub of scripts/export_guard.py — see tests/test_approve_merge.py."""
import argparse
import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MANIFEST = "RELEASE_MANIFEST.txt"


def _builder():
    spec = importlib.util.spec_from_file_location(
        "_stub_build_public_export", HERE / "scripts" / "build_public_export.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _pins(root):
    path = root / MANIFEST
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, rel = line.partition("  ")
        out[rel] = digest
    return out


def _write_pins(root, pins):
    rows = "".join(f"{d}  {p}\\n" for p, d in sorted(pins.items()))
    (root / MANIFEST).write_text("# RELEASE_MANIFEST.txt\\n" + rows, encoding="utf-8")


def _shipped(root):
    b = _builder()
    rules = b.parse_classification(
        (root / b.CLASSIFICATION_NAME).read_text(encoding="utf-8"))
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                         capture_output=True, text=True).stdout
    tracked = [p for p in out.split("\\0") if p]
    cls = b.classify(rules, tracked)
    return set(cls.shipped)


def _count_drift(root):
    """The real guard's classification_errors, count half: every rule's real
    win-count must equal its declared one. Same phrasing, same stream."""
    b = _builder()
    text = (root / b.CLASSIFICATION_NAME).read_text()
    rules = b.parse_classification(text)
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True).stdout
    tracked = [p for p in out.split("\\0") if p]
    cls = b.classify(rules, tracked)
    return [f"{b.CLASSIFICATION_NAME}:{r.lineno}: `{r.verb} {r.declared}  {r.pattern}` "
            f"actually wins {cls.wins[r.lineno]} file(s)."
            for r in rules if cls.wins[r.lineno] != r.declared]


def cmd_approve(args, root):
    if (root / "REFUSE_SCAN").exists():
        print("approve: REFUSED (stub) - 1 scan hit(s)", file=sys.stderr)
        return 1
    if (root / "REFUSE_BEFORE_WRITE").exists():
        print("approve: REFUSED (stub) - the advisory scan cannot run", file=sys.stderr)
        return 2
    drift = _count_drift(root)
    if drift:
        print("approve: REFUSED (stub) - fix EXPORT_CLASSIFICATION.txt first:\\n  "
              + "\\n  ".join(drift), file=sys.stderr)
        return 2
    shipped = _shipped(root)
    pins = _pins(root)
    targets = list(dict.fromkeys(args.paths)) if args.paths else \\
        sorted(r for r in shipped if r != MANIFEST)
    bad = [t for t in targets if t not in shipped and t != MANIFEST]
    if bad:
        print("approve: REFUSED (stub) - not ship-classified:\\n  "
              + "\\n  ".join(bad), file=sys.stderr)
        return 2
    for rel in targets:
        if rel == MANIFEST:
            continue
        pins[rel] = _sha(root / rel)
        print(f"approved  {pins[rel][:12]}  {rel} (stub)")
    _write_pins(root, pins)
    print(f"{MANIFEST}: {len(pins)} pinned file(s) total")
    return 0


def cmd_verify(args, root):
    if (root / "FORCE_VERIFY_FAIL").exists():
        print("verify: FAILED (stub forced)", file=sys.stderr)
        return 1
    pins = _pins(root)
    if not pins:
        print("verify: FAILED (stub) - no manifest", file=sys.stderr)
        return 1
    shipped = _shipped(root)
    for rel in shipped:
        if rel == MANIFEST:
            continue
        if rel not in pins:
            print(f"verify: FAILED (stub) - {rel} unpinned", file=sys.stderr)
            return 1
        if pins[rel] != _sha(root / rel):
            print(f"verify: FAILED (stub) - {rel} hash mismatch", file=sys.stderr)
            return 1
    print(f"verify: OK (stub) - {len(shipped)} shipped file(s)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_a = sub.add_parser("approve")
    ap_a.add_argument("paths", nargs="*")
    ap_a.add_argument("--all", action="store_true")
    ap_a.add_argument("--prune", action="store_true")
    ap_a.add_argument("--acknowledge", action="store_true")
    sub.add_parser("verify")
    args = ap.parse_args(argv)
    root = Path.cwd()
    if args.cmd == "approve":
        return cmd_approve(args, root)
    return cmd_verify(args, root)


if __name__ == "__main__":
    sys.exit(main())
'''

# A `gh` stub: records every argv (one JSON array per line) to
# $GH_STUB_LOG, and answers `pr view --json state` / `pr close` from a
# one-line state file at $GH_STUB_STATE_FILE ("OPEN" by default).
_GH_STUB = '''\
#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
log = Path(os.environ["GH_STUB_LOG"])
with log.open("a") as f:
    f.write(json.dumps(argv) + "\\n")

state_file = Path(os.environ["GH_STUB_STATE_FILE"])

if argv[:2] == ["pr", "view"]:
    state = state_file.read_text().strip() if state_file.exists() else "OPEN"
    print(json.dumps({"state": state}))
    sys.exit(0)
if argv[:2] == ["pr", "close"]:
    state_file.write_text("CLOSED")
    sys.exit(0)
sys.exit(0)
'''


class LandEnv:
    def __init__(self, tmp_path, origin, clone, config, gh_log, gh_state):
        self.tmp_path = tmp_path
        self.origin = origin
        self.clone = clone
        self.config = config
        self.gh_log = gh_log
        self.gh_state = gh_state
        self.pr_url = "https://github.com/acme/widget/pull/42"

    def tip_sha(self) -> str:
        return _git(self.origin, "rev-parse", "main").stdout.strip()

    def remote_main_sha(self) -> str:
        return _git(self.origin, "rev-parse", "main").stdout.strip()

    def cut_branch(self, name: str, *, extra_files: dict[str, str] | None = None,
                   corrupt_manifest: bool = True) -> tuple[str, str]:
        """Branch `name` off the CURRENT origin/main tip, adding
        `src/feature.py` (a new ship-classified file) and bumping the
        classification's win-count for it — the shape a real coder attempt
        produces. `corrupt_manifest` wipes RELEASE_MANIFEST.txt on the
        branch (proving land re-derives it from the tip, never carries the
        branch's own copy forward)."""
        _git(self.clone, "fetch", "-q", "origin", "main")
        _git(self.clone, "checkout", "-q", "-B", name, "origin/main")
        (self.clone / "src" / "feature.py").write_text("def feature():\n    return 3\n")
        cls_path = self.clone / "EXPORT_CLASSIFICATION.txt"
        text = cls_path.read_text(encoding="utf-8").replace("ship 2 src/*.py", "ship 3 src/*.py")
        # Every added `tests/*.py` extra bumps its counted drop rule, the way a
        # real attempt's commit must (the stub guard, like the real one,
        # refuses `approve` on a drifted count).
        added_tests = sum(1 for rel in (extra_files or {})
                          if rel.startswith("tests/") and rel.endswith(".py") and rel.count("/") == 1)
        if added_tests:
            text = text.replace("drop 1 tests/*.py", f"drop {1 + added_tests} tests/*.py")
        cls_path.write_text(text)
        if corrupt_manifest:
            (self.clone / "RELEASE_MANIFEST.txt").write_text("# RELEASE_MANIFEST.txt\n")
        for rel, content in (extra_files or {}).items():
            p = self.clone / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", f"feature: add feature.py ({name})")
        _git(self.clone, "push", "-q", "-u", "origin", name)
        head_sha = _git(self.clone, "rev-parse", "HEAD").stdout.strip()
        return name, head_sha

    def advance_origin(self, note: str) -> str:
        """Push a trivial commit onto origin/main from a THIRD clone —
        simulates a concurrent human/other-task push landing on main."""
        race = self.tmp_path / f"race-{note}"
        _git(self.tmp_path, "clone", "-q", str(self.origin), str(race))
        (race / "RACE.md").write_text(note)
        _git(race, "add", "RACE.md")
        _git(race, "commit", "-qm", f"race: {note}")
        _git(race, "push", "-q", "origin", "HEAD:main")
        return _git(race, "rev-parse", "HEAD").stdout.strip()


def _push_conflicting_change(land_env, path: str, content: str) -> str:
    """Push a change to *path* on origin/main from a THIRD clone, off the
    SAME tip a branch was cut from — a real conflicting edit (not just a
    ledger-file difference) so `git merge --squash` hits an actual conflict
    on *path* rather than a clean/empty merge."""
    race = land_env.tmp_path / f"conflict-{len(list(land_env.tmp_path.glob('conflict-*')))}"
    _git(land_env.tmp_path, "clone", "-q", str(land_env.origin), str(race))
    (race / path).write_text(content)
    _git(race, "add", path)
    _git(race, "commit", "-qm", f"conflict: {path}")
    _git(race, "push", "-q", "origin", "HEAD:main")
    return _git(race, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def land_env(tmp_path, monkeypatch) -> LandEnv:
    # Hermetic under `-n 4`: none of `land_task`'s many `git`/`gh` subprocess
    # calls pass an explicit `env=`, so they inherit whatever this process's
    # ambient environment is. `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE`/
    # `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` are process-
    # global and other test modules in this repo (test_guard.py,
    # test_repro_gate.py, test_repo_discovery.py,
    # test_doctor_editable_install.py) mutate them; a real `~/.gitconfig`
    # with a credential helper or an interactive prompt would also reach
    # every `git`/`gh` call below. Scrub and redirect so this fixture's git
    # never depends on the worker's ambient state or on test load order.
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    global_gitconfig = tmp_path / "gitconfig"
    # `land_task`'s squash step (`git merge --squash`) needs SOME resolvable
    # identity for its internal virtual-merge machinery even though the
    # squash itself makes no commit — the real commit later overrides this
    # via `-c user.name=/-c user.email=` (operator identity, unaffected).
    # Deleting the GIT_AUTHOR_*/GIT_COMMITTER_* env vars above and pointing
    # HOME at an empty tmp_path leaves git with no identity to resolve at
    # all, so this redirected global config supplies an innocuous one.
    global_gitconfig.write_text(
        "[user]\n\tname = Hermetic Test\n\temail = hermetic-test@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))

    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "src").mkdir()
    (seed / "scripts").mkdir()
    (seed / "tests").mkdir()
    (seed / "README.md").write_text("hello\n")
    (seed / "src" / "app.py").write_text("def app():\n    return 1\n")
    (seed / "src" / "lib.py").write_text("def lib():\n    return 2\n")
    (seed / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n")
    (seed / "scripts" / "build_public_export.py").write_text(_MINI_BUILD_PUBLIC_EXPORT)
    (seed / "scripts" / "export_guard.py").write_text(_EXPORT_GUARD_STUB)
    (seed / "EXPORT_CLASSIFICATION.txt").write_text(_CLASSIFICATION)
    # The classification declares `ship 1 RELEASE_MANIFEST.txt`, and the stub
    # guard — like the real one — refuses `approve` while a declared count is
    # wrong, so the manifest must exist (and be staged) before the first run.
    (seed / "RELEASE_MANIFEST.txt").write_text("# RELEASE_MANIFEST.txt\n")
    # `git ls-files` (what the guard classifies from) only sees the INDEX, so
    # everything must be staged before `approve` runs.
    _git(seed, "add", "-A")
    subprocess.run(
        [sys.executable, "scripts/export_guard.py", "approve", "--all"],
        cwd=seed, check=True, capture_output=True, text=True,
    )
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    # Repo-local identity for the clone — this is what the default
    # `approve_identity` resolution path (git.approve_identity.name/.email
    # left unset in `config` below) resolves the squash-commit identity
    # from. Distinct from the hermetic GLOBAL identity above ("Hermetic
    # Test") so repo-local-overrides-global is actually exercised by every
    # land test, not just the ones that test it explicitly.
    _git(clone, "config", "user.name", "clone-user")
    _git(clone, "config", "user.email", "clone@example.invalid")

    # `gh` stub on PATH.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_path = bin_dir / "gh"
    gh_path.write_text(_GH_STUB)
    gh_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    gh_log = tmp_path / "gh_log.jsonl"
    gh_log.write_text("")
    monkeypatch.setenv("GH_STUB_LOG", str(gh_log))
    gh_state = tmp_path / "gh_state.txt"
    gh_state.write_text("OPEN")
    monkeypatch.setenv("GH_STUB_STATE_FILE", str(gh_state))

    config = {
        "git": {
            "agent_identity_name": "no_human",
            "agent_identity_email": "no-human@acme.com",
            "never_push_to": ["main", "master", "release/*"],
            # No `approve_identity` here on purpose: the default path — resolved
            # from the clone's own git config (repo-local `clone-user` /
            # `clone@example.invalid`, set above) — is what every land test in
            # this module exercises unless it opts into an explicit override.
        },
        "approve_merge": {"enabled": True, "test_timeout_seconds": 120},
    }
    return LandEnv(tmp_path, origin, clone, config, gh_log, gh_state)


# --------------------------------------------------------------------------- #
# land_task — unit level                                                      #
# --------------------------------------------------------------------------- #

def test_lands_on_remote_tracking_tip_not_stale_local_branch(land_env):
    """Regression pin, deterministic: `land_task` resolves the land base from
    the REMOTE-TRACKING ref (`origin/main`), never a same-named LOCAL branch
    — exactly the shape a task's `repo_path` clone carries from clone time
    (a checked-out `main` that a plain `git fetch` never fast-forwards). The
    harness fetches here, not `land_task` — so this pins the actual
    base-selection behavior with zero dependence on `land_task`'s own
    best-effort `repo.fetch()` succeeding under load."""
    branch, head_sha = land_env.cut_branch("no-human/t-tip-remote")
    new_tip = land_env.advance_origin("advance-before-land")
    _git(land_env.clone, "fetch", "-q", "origin", "main")
    local_main = _git(land_env.clone, "rev-parse", "main").stdout.strip()
    assert local_main != new_tip, (
        "premise: the clone's local `main` must still be stale after a "
        "plain fetch, or this test proves nothing")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature",
        review_evidence="review PASS on abc123 after 1 round(s)",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    parent = _git(land_env.origin, "rev-parse", f"{result.landed_sha}^1").stdout.strip()
    assert parent == new_tip


def test_lands_on_a_tip_pushed_after_the_branch(land_env):
    """Keeps the ORIGINAL test's shape — no harness pre-fetch, so
    `land_task`'s own internal `repo.fetch()` call (step 2, at the very
    start) stays covered. This is the test that flaked under `-n 4` in
    train28 (as `test_lands_on_current_tip_not_stale_base`): `GitRepo.fetch`
    is best-effort and silently swallows `TimeoutExpired`/`OSError`
    (vcs/git.py:419-425 — out of scope to make fatal, that is a documented
    product behavior), so under fd/process pressure the fetch can silently
    no-op, leaving the clone's `origin/main` stale — `land_task` then
    correctly (by its own contract) bases the land on the pre-advance tip.
    The original test read that as "wrong parent", indistinguishable from an
    actual base-selection bug.

    This version checks the PREMISE first and loudly, using the
    `_before_push` test seam (already used by
    `test_aborts_when_tip_moved_during_land`) to sample `origin/main`
    immediately after step 2's fetch but before step 7's pre-push
    RE-fetch/re-check — the `git push` at the end of a successful land
    opportunistically advances the local `origin/main` tracking ref to the
    just-landed sha, so sampling it AFTER `land_task` returns (rather than
    via this seam) would always show forward movement and prove nothing
    about step 2's fetch specifically."""
    branch, head_sha = land_env.cut_branch("no-human/t-tip-ownfetch")
    new_tip = land_env.advance_origin("advance-before-land")
    sampled: dict[str, str] = {}

    def _sample_pre_push():
        sampled["origin_main"] = _git(
            land_env.clone, "rev-parse", "origin/main").stdout.strip()

    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature",
        review_evidence="review PASS on abc123 after 1 round(s)",
        config=land_env.config, _before_push=_sample_pre_push,
    )
    assert result.ok, result.stderr
    observed = sampled.get("origin_main")
    if observed != new_tip:
        diag = _git(land_env.clone, "fetch", "origin", "main", check=False)
        pytest.fail(
            "premise failed: land_task's own step-2 repo.fetch() did not "
            f"observe the advanced tip (origin/main was {observed} "
            f"immediately before the pre-push re-check, expected {new_tip}); "
            f"a diagnostic re-fetch here returned rc={diag.returncode} "
            f"stderr={diag.stderr.strip()!r} — see GitRepo.fetch's "
            "best-effort TimeoutExpired/OSError swallow (vcs/git.py)")
    parent = _git(land_env.origin, "rev-parse", f"{result.landed_sha}^1").stdout.strip()
    assert parent == new_tip


def test_squash_produces_single_commit(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-squash")
    tip = land_env.tip_sha()
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    count = _git(land_env.origin, "rev-list", "--count",
                 f"{tip}..{result.landed_sha}").stdout.strip()
    assert count == "1"


def test_manifest_reset_classification_preserved(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-manifest")
    branch_classification = (land_env.clone / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    sha = result.landed_sha
    names = _git(land_env.origin, "show", "--name-only", "--format=", sha).stdout.split()
    assert "RELEASE_MANIFEST.txt" in names
    assert "src/feature.py" in names
    manifest = _git(land_env.origin, "show", f"{sha}:RELEASE_MANIFEST.txt").stdout
    assert "src/feature.py" in manifest
    classification = _git(land_env.origin, "show", f"{sha}:EXPORT_CLASSIFICATION.txt").stdout
    assert classification == branch_classification


def test_squash_lands_an_nh_evidence_directory_committed_on_the_branch(land_env):
    """D1.2 DECISION GATE (step 1, planted red case): does a `.nh-evidence/
    <task-id>/` directory committed ON THE TASK BRANCH survive `land_task`'s
    squash onto main?

    Read the code first: step 3 (`squash`, `git merge --squash <branch>`)
    stages the branch's FULL diff into the worktree's index BEFORE step 4
    (`manifest`) ever runs `export_guard.py`. Step 4 only ADDS
    RELEASE_MANIFEST.txt and the re-approved ship-classified paths on top
    (`git add -- RELEASE_MANIFEST.txt` and `git add -A -- shipped_changed`)
    — it never unstages or filters what the squash already staged. Step 5
    then runs a plain `git commit` with no pathspec, committing whatever is
    currently staged. So an UNCLASSIFIED directory added on the branch (one
    that matches no `ship`/`drop` rule in EXPORT_CLASSIFICATION.txt, exactly
    the shape a `.nh-evidence/` directory would have) rides the squash
    straight into the landed commit — nothing in the procedure ever
    filters it back out.

    This test proves that reading empirically, with a real git squash
    against a real fixture repo (not just a code-reading argument): plant a
    `.nh-evidence/<task-id>/x` file on a branch, land it, and check whether
    it appears in the landed commit's tree.

    RULING (recorded here, not just in the D1.2 report, so a future change
    that alters this behavior fails a real test rather than going unnoticed):
    the directory DOES leak into main. Committing evidence directly on the
    task branch is therefore not a safe delivery mechanism — D1.2 uses a
    side branch (`nh-evidence/<task-id>`, pushed alongside the PR, deleted
    on close) instead of committing evidence on the task branch itself.
    """
    branch, head_sha = land_env.cut_branch(
        "no-human/t-nh-evidence-leak",
        extra_files={".nh-evidence/deadbeef/final.png": "not a real png, just a probe"},
    )
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    names = _git(land_env.origin, "show", "--name-only", "--format=",
                 result.landed_sha).stdout.split()
    assert ".nh-evidence/deadbeef/final.png" in names, (
        "premise check FAILED: `.nh-evidence/` did NOT survive the squash "
        "in this run. If this genuinely reproduces, the D1.2 ruling above "
        "is stale and the delivery mechanism should be reconsidered — do "
        "not silently accept this branch flipping without re-reading "
        "vcs/approve_merge.py's squash/manifest steps.")


def _commit_identity(land_env, sha) -> tuple[str, str]:
    out = _git(land_env.origin, "show", "-s", "--format=%an%x09%ae", sha).stdout.strip()
    name, _, email = out.partition("\t")
    return name, email


def _clear_repo_local_identity(clone) -> None:
    _git(clone, "config", "--unset", "user.name")
    _git(clone, "config", "--unset", "user.email")


def test_identity_defaults_to_repo_git_config(land_env):
    """AC: with no `git.approve_identity` in config (the fixture default),
    the squash identity is resolved from the clone's own repo-local git
    config, not from a hard-coded person."""
    branch, head_sha = land_env.cut_branch("no-human/t-default-identity")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    name, email = _commit_identity(land_env, result.landed_sha)
    assert name == "clone-user"
    assert email == "clone@example.invalid"


def test_repo_local_git_identity_wins_over_global(land_env, monkeypatch):
    """AC: repo-local git config overrides global, matching plain `git
    commit` precedence — `git config --get` already applies this, this test
    pins it end to end through `land_task`."""
    other_global = land_env.tmp_path / "other-global-gitconfig"
    other_global.write_text(
        "[user]\n\tname = Global Person\n\temail = global@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(other_global))
    branch, head_sha = land_env.cut_branch("no-human/t-local-wins")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    name, email = _commit_identity(land_env, result.landed_sha)
    assert name == "clone-user"
    assert email == "clone@example.invalid"


def test_explicit_approve_identity_overrides_git_config(land_env):
    """AC: an explicit `git.approve_identity` in config still wins over the
    resolved git-config default."""
    land_env.config["git"]["approve_identity"] = {
        "name": "Ada L.", "email": "ada@example.invalid",
    }
    branch, head_sha = land_env.cut_branch("no-human/t-explicit-wins")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    name, email = _commit_identity(land_env, result.landed_sha)
    assert name == "Ada L."
    assert email == "ada@example.invalid"


def test_identity_is_never_the_agent_identity_when_git_config_empty(land_env, monkeypatch):
    """CONSTRAINT #2: when git config yields no usable identity at all, land
    refuses at `preconditions` — it must NEVER fall through to the agent
    identity (`git.agent_identity_name`/`_email`), and nothing is landed."""
    empty_global = land_env.tmp_path / "empty-global-gitconfig"
    empty_global.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_global))
    _clear_repo_local_identity(land_env.clone)
    tip_before = land_env.tip_sha()
    branch, head_sha = land_env.cut_branch("no-human/t-no-identity")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok is False
    assert result.step == "preconditions"
    assert "no_human" not in result.stderr
    assert "no-human@acme.com" not in result.stderr
    # `origin/main`'s tip is unmoved: land_task never reached fetch/worktree/
    # commit/push, so the branch it would have squashed never landed.
    assert land_env.tip_sha() == tip_before
    log = _git(land_env.origin, "log", "--format=%s", "main").stdout
    assert "Add feature" not in log


def test_refusal_message_names_the_fix(land_env, monkeypatch):
    """Same setup as the constraint-#2 test: the refusal must be actionable,
    naming both ways to fix it."""
    empty_global = land_env.tmp_path / "empty-global-gitconfig"
    empty_global.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_global))
    _clear_repo_local_identity(land_env.clone)
    branch, head_sha = land_env.cut_branch("no-human/t-refusal-message")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok is False
    assert "user.email" in result.stderr
    assert "git.approve_identity" in result.stderr


def test_partial_git_identity_refuses(land_env, monkeypatch):
    """AC: only one of user.name/user.email set is not a usable identity —
    same refusal shape as the fully-empty case."""
    empty_global = land_env.tmp_path / "empty-global-gitconfig-partial"
    empty_global.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_global))
    _git(land_env.clone, "config", "--unset", "user.email")
    branch, head_sha = land_env.cut_branch("no-human/t-partial-identity")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok is False
    assert result.step == "preconditions"
    assert "no_human" not in result.stderr
    assert "no-human@acme.com" not in result.stderr
    assert "user.email" in result.stderr
    assert "git.approve_identity" in result.stderr


def test_commit_uses_operator_identity(land_env):
    """REGRESSION CONTROL: this deployment's own config does not set
    `git.approve_identity`, and its git identity resolves to exactly the
    values the old hard-coded literal used — proving byte-identical
    behaviour for the current deployment now that the default is resolved
    from git config instead of a literal."""
    _git(land_env.clone, "config", "user.name", "eyalgolan")
    _git(land_env.clone, "config", "user.email",
         "5146175+eyalgolan@users.noreply.github.com")
    branch, head_sha = land_env.cut_branch("no-human/t-ident")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    name, email = _commit_identity(land_env, result.landed_sha)
    assert name == "eyalgolan"
    assert email == "5146175+eyalgolan@users.noreply.github.com"


def test_commit_message_shape(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-msg")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="task-deadbeef", task_title="Add the feature everyone wants",
        review_evidence="review PASS on abc123 after 2 round(s)",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    msg = _git(land_env.origin, "show", "-s", "--format=%B", result.landed_sha).stdout
    assert "Add the feature everyone wants" in msg
    assert "task-deadbeef" in msg
    assert "review PASS on abc123 after 2 round(s)" in msg


def test_commit_message_scrubs_flagged_vendor_term_from_title(land_env):
    """A task title (or review-evidence line) carrying a flagged vendor term
    — task titles have carried one before, the PR #334/#339 incident — must
    never land verbatim in a commit message pushed to the forge's default
    branch. Both go through the same outbound scrub `nh bench publish` uses
    before free text reaches a tracked, published artifact."""
    from no_human.eval.vendor_terms import BANNED_TERMS, find_banned_terms

    term = BANNED_TERMS[0]
    branch, head_sha = land_env.cut_branch("no-human/t-vendor-term")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="task-deadbeef", task_title=f"Fix the {term} integration bug",
        review_evidence=f"review PASS, compared favorably to {term}",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    msg = _git(land_env.origin, "show", "-s", "--format=%B", result.landed_sha).stdout
    assert not find_banned_terms(msg), msg
    assert term not in msg.lower()
    assert "<redacted>" in msg


def test_commit_message_leaves_ordinary_title_and_evidence_unchanged(land_env):
    """Control: text carrying no flagged term must pass through the scrub
    byte-for-byte, so the fix does not corrupt an ordinary commit message."""
    branch, head_sha = land_env.cut_branch("no-human/t-vendor-term-control")
    title = "Add the feature everyone wants"
    evidence = "review PASS on abc123 after 2 round(s)"
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="task-deadbeef", task_title=title, review_evidence=evidence,
        config=land_env.config,
    )
    assert result.ok, result.stderr
    msg = _git(land_env.origin, "show", "-s", "--format=%B", result.landed_sha).stdout
    assert title in msg
    assert evidence in msg


def test_aborts_when_export_guard_verify_fails(land_env):
    branch, head_sha = land_env.cut_branch(
        "no-human/t-verifyfail", extra_files={"FORCE_VERIFY_FAIL": "x"})
    before = land_env.remote_main_sha()
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert not result.ok
    assert result.step == "verify"
    assert result.stderr
    assert land_env.remote_main_sha() == before


def test_change_scoped_tests_run_against_worktree(land_env, tmp_path, monkeypatch):
    marker = tmp_path / "test_marker.txt"
    monkeypatch.setenv("TEST_MARKER_FILE", str(marker))
    marker_test = (
        "import os, pathlib\n"
        "def test_records_env():\n"
        "    p = pathlib.Path(os.environ['TEST_MARKER_FILE'])\n"
        "    p.write_text(os.getcwd() + '\\n' + os.environ.get('PYTHONPATH', ''))\n"
    )
    branch, head_sha = land_env.cut_branch(
        "no-human/t-scopedtests", extra_files={"tests/test_feature.py": marker_test})
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    assert marker.exists(), "the change-scoped test never ran"
    recorded_cwd, _, recorded_pythonpath = marker.read_text(encoding="utf-8").partition("\n")
    assert recorded_cwd != str(land_env.clone), \
        "must run in the temp WORKTREE, not the task's own repo checkout"
    assert recorded_pythonpath.endswith(f"{os.sep}src")


# --------------------------------------------------------------------------- #
# merge-time gate: focused vs. full, decided by tree comparison               #
# --------------------------------------------------------------------------- #
#
# Every test below monkeypatches `approve_merge._run_pytest` — the sole seam
# `land_task`'s step 6b shells out to pytest through — with a recorder, so no
# real suite is ever invoked here. `test_change_scoped_tests_run_against_
# worktree` above deliberately does NOT patch it (proving the full gate's
# real subprocess still discovers and runs a change-scoped test file when it
# falls back to "full"); everything below is about the GATE DECISION itself.


def _patch_run_pytest(monkeypatch, *, returncode=0):
    calls = []

    def _fake(argv, *, cwd, timeout, env):
        calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        return subprocess.CompletedProcess(argv, returncode, "", "")

    monkeypatch.setattr("no_human.vcs.approve_merge._run_pytest", _fake)
    return calls


def test_diverged_tree_runs_the_full_gate_before_push(land_env, monkeypatch):
    """A conflict-round-style divergence: the recorded tested commit is the
    tip the branch was cut from, but the squash result's tree (branch tip
    + the squash-derived RELEASE_MANIFEST.txt) is not byte-identical to it
    — this is exactly what happened on 201baa8: the focused gate ran on a
    tree the full suite never actually saw."""
    calls = _patch_run_pytest(monkeypatch, returncode=0)
    base_sha = land_env.remote_main_sha()
    branch, head_sha = land_env.cut_branch("no-human/t-diverged")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config, tested_commit_sha=base_sha,
    )
    assert result.ok, result.stderr
    assert result.gate == "full"
    assert "diverged" in result.gate_reason
    assert base_sha[:12] in result.gate_reason
    assert len(calls) == 1
    assert calls[0]["argv"][:3] == [sys.executable, "-m", "pytest"]
    # no explicit test paths -- pytest discovers the whole worktree itself
    assert "-q" in calls[0]["argv"]
    assert not any(a.endswith(".py") for a in calls[0]["argv"])


def test_full_gate_failure_blocks_the_push(land_env, monkeypatch):
    calls = _patch_run_pytest(monkeypatch, returncode=1)
    before = land_env.remote_main_sha()
    branch, head_sha = land_env.cut_branch("no-human/t-fullfail")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config, tested_commit_sha="",
    )
    assert not result.ok
    assert result.step == "tests"
    assert result.gate == "full"
    assert len(calls) == 1
    assert land_env.remote_main_sha() == before, \
        "a failed full gate must never push"


def _pin_branch_head(land_env) -> str:
    """Re-pin RELEASE_MANIFEST.txt at the branch's own HEAD and amend — what
    a real coder attempt is required to do before pushing a new/changed
    ship-classified file (the EXPORT GATE: re-pin via `export_guard.py
    approve` in the same commit). `cut_branch` deliberately leaves this
    UNDONE (most gate tests below want a divergent tree, exactly like a real
    attempt that forgot to re-pin); the two "matching tree" tests need a
    branch tip whose manifest is byte-identical to what the squash step's
    own re-approve of the changed ship file would independently derive from
    the SAME tip — running `approve` with no path args re-pins every shipped
    file from its current on-disk content, so it reproduces that derivation
    exactly regardless of whether `corrupt_manifest` wiped the manifest."""
    clone = land_env.clone
    subprocess.run([sys.executable, "scripts/export_guard.py", "approve"],
                    cwd=clone, check=True, capture_output=True, text=True)
    _git(clone, "add", "-A")
    _git(clone, "commit", "--amend", "--no-edit", "-q")
    _git(clone, "push", "-q", "-f", "origin", "HEAD")
    return _git(clone, "rev-parse", "HEAD").stdout.strip()


def test_matching_tree_keeps_the_focused_gate(land_env, monkeypatch):
    calls = _patch_run_pytest(monkeypatch, returncode=0)
    branch, head_sha = land_env.cut_branch(
        "no-human/t-matching",
        extra_files={"tests/test_feature.py": "def test_x():\n    assert True\n"},
    )
    head_sha = _pin_branch_head(land_env)
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config, tested_commit_sha=head_sha,
    )
    assert result.ok, result.stderr
    assert result.gate == "focused"
    assert "matches tested attempt" in result.gate_reason
    assert head_sha[:12] in result.gate_reason
    assert len(calls) == 1
    assert calls[0]["argv"] == [sys.executable, "-m", "pytest", "-q", "tests/test_feature.py"]


def test_moved_base_counts_as_divergence(land_env, monkeypatch):
    """The attempt tested its branch against ONE base tip; a concurrent
    push (another task landing, a human commit) moves the default branch
    before this task's `nh approve` runs — the squash-result tree can no
    longer match the tested one even with an otherwise-clean squash, so
    this must fall back to the full gate exactly like an in-band conflict
    round would."""
    calls = _patch_run_pytest(monkeypatch, returncode=0)
    branch, head_sha = land_env.cut_branch("no-human/t-movedbase")
    land_env.advance_origin("moved-base-before-land")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config, tested_commit_sha=head_sha,
    )
    assert result.ok, result.stderr
    assert result.gate == "full"
    assert "diverged" in result.gate_reason
    assert len(calls) == 1


def test_unknown_tested_commit_runs_the_full_gate(land_env, monkeypatch):
    calls = _patch_run_pytest(monkeypatch, returncode=0)
    branch, head_sha = land_env.cut_branch("no-human/t-notested")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config, tested_commit_sha="",
    )
    assert result.ok, result.stderr
    assert result.gate == "full"
    assert "no recorded tested-attempt tree" in result.gate_reason
    assert len(calls) == 1


def test_unresolvable_tested_commit_runs_the_full_gate(land_env, monkeypatch):
    calls = _patch_run_pytest(monkeypatch, returncode=0)
    branch, head_sha = land_env.cut_branch("no-human/t-badsha")
    bogus = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config, tested_commit_sha=bogus,
    )
    assert result.ok, result.stderr
    assert result.gate == "full"
    assert "does not resolve here" in result.gate_reason
    assert bogus[:12] in result.gate_reason
    assert len(calls) == 1


def test_full_gate_exit_code_5_is_not_a_landing_failure(land_env, monkeypatch):
    """rc=5 is pytest's own "no tests were collected" — a hermetic fixture
    repo with nothing under `tests/` for the full run to discover must not
    block a landing that would otherwise succeed; it's annotated onto the
    gate reason instead."""
    calls = _patch_run_pytest(monkeypatch, returncode=5)
    branch, head_sha = land_env.cut_branch("no-human/t-nocollect")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config, tested_commit_sha="",
    )
    assert result.ok, result.stderr
    assert result.gate == "full"
    assert "no tests collected" in result.gate_reason
    assert len(calls) == 1


def test_land_result_message_names_the_gate(land_env, monkeypatch):
    _patch_run_pytest(monkeypatch, returncode=0)
    branch, head_sha = land_env.cut_branch("no-human/t-message")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config, tested_commit_sha=head_sha,
    )
    assert result.ok, result.stderr
    assert result.gate_reason in result.message
    assert "gate:" in result.message


def test_cli_approve_prints_the_gate_that_ran(land_env, tmp_path, monkeypatch):
    _patch_run_pytest(monkeypatch, returncode=0)
    branch, head_sha = land_env.cut_branch("no-human/t-cligate")
    head_sha = _pin_branch_head(land_env)
    db = tmp_path / "t.db"
    task_id = _seed_land_task(
        db, TaskStatus.AWAITING_APPROVAL, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": head_sha, "passed": True}],
        attempt_commit_sha=head_sha,
    )
    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])
    assert result.exit_code == 0, result.output
    assert "gate: focused" in result.output, result.output
    assert head_sha[:12] in result.output


def test_cli_approve_passes_the_recorded_attempt_commit(land_env, tmp_path, monkeypatch):
    branch, head_sha = land_env.cut_branch("no-human/t-clitested")
    db = tmp_path / "t.db"
    task_id = _seed_land_task(
        db, TaskStatus.AWAITING_APPROVAL, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": head_sha, "passed": True}],
        attempt_commit_sha=head_sha,
    )
    captured = {}

    def _fake_land_task(*, repo_path, branch, pr_url, task_id, task_title,
                          review_evidence, config, on_step=None, **kwargs):
        captured.update(kwargs)
        return LandResult(ok=True, step="close_pr", landed_sha="a" * 40,
                           pr_url=pr_url, branch=branch, message="landed")

    monkeypatch.setattr("no_human.vcs.approve_merge.land_task", _fake_land_task)
    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])
    assert result.exit_code == 0, result.output
    assert captured.get("tested_commit_sha") == head_sha


def test_push_advances_remote_ref(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-push")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    assert land_env.remote_main_sha() == result.landed_sha


def test_aborts_when_tip_moved_during_land(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-race")
    before = land_env.remote_main_sha()
    raced: dict[str, str] = {}

    def _race():
        raced["sha"] = land_env.advance_origin("mid-land-race")

    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config, _before_push=_race,
    )
    assert not result.ok
    assert result.step == "push"
    assert raced.get("sha")
    assert land_env.remote_main_sha() == raced["sha"]
    assert land_env.remote_main_sha() != before


def test_closes_pr_without_comment(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-close")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    argvs = [json.loads(l) for l in land_env.gh_log.read_text(encoding="utf-8").splitlines() if l.strip()]
    close_calls = [a for a in argvs if a[:2] == ["pr", "close"]]
    assert close_calls, f"no `pr close` call recorded: {argvs}"
    for a in argvs:
        assert "--comment" not in a
        assert a[:2] != ["pr", "comment"]


def test_already_closed_pr_is_not_a_failure(land_env):
    land_env.gh_state.write_text("MERGED")
    branch, head_sha = land_env.cut_branch("no-human/t-alreadyclosed")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    argvs = [json.loads(l) for l in land_env.gh_log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert not any(a[:2] == ["pr", "close"] for a in argvs)


def test_failure_removes_temp_worktree_and_keeps_awaiting_approval(land_env):
    branch, head_sha = land_env.cut_branch(
        "no-human/t-abort", extra_files={"REFUSE_BEFORE_WRITE": "x"})
    repo = GitRepo(land_env.clone, never_push_to=["main", "master", "release/*"])
    before_worktrees = repo.list_worktrees()
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert not result.ok
    assert result.step == "manifest"
    assert result.stderr
    after_worktrees = repo.list_worktrees()
    assert after_worktrees == before_worktrees
    assert len(after_worktrees) == 1


def test_squash_conflict_leaves_no_worktree(land_env, monkeypatch):
    """A REAL `git merge --squash` conflict (not the manifest-step failure
    the test above covers) — the branch and a concurrent push both edit
    README.md, so the squash step itself fails and aborts. Pins that this
    failure path (like every other) leaves neither a registered worktree
    nor its temp dir behind."""
    branch, head_sha = land_env.cut_branch(
        "no-human/t-squashconflict",
        extra_files={"README.md": "branch changed this line\n"})
    _push_conflicting_change(land_env, "README.md", "main changed this line too\n")

    created_dirs: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def _track_mkdtemp(*a, **kw):
        p = real_mkdtemp(*a, **kw)
        created_dirs.append(p)
        return p

    monkeypatch.setattr(tempfile, "mkdtemp", _track_mkdtemp)

    repo = GitRepo(land_env.clone, never_push_to=["main", "master", "release/*"])
    before_worktrees = repo.list_worktrees()
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert not result.ok
    assert result.step == "squash"
    assert result.stderr

    after_worktrees = repo.list_worktrees()
    assert after_worktrees == before_worktrees
    assert len(after_worktrees) == 1
    assert created_dirs, "expected land_task to have created exactly one temp worktree dir"
    assert not Path(created_dirs[-1]).exists()


def test_squash_conflict_confined_to_the_manifest_lands(land_env):
    """RELEASE_MANIFEST.txt moves under every landing, so a branch cut before
    the previous landing conflicts on it at the squash step — and step 4
    then throws the squash's manifest away and re-derives it from the tip
    anyway. Refusing at step 3 therefore made every PR older than the last
    landing unlandable by `nh approve` (live: #582 on 2026-08-22). A conflict
    confined to the manifest is resolved the way step 4 resolves it: the
    tip's copy wins, the branch's pins are re-derived on top."""
    branch, _ = land_env.cut_branch("no-human/t-manifestconflict")
    tip_manifest = _git(land_env.origin, "show", "main:RELEASE_MANIFEST.txt").stdout
    _push_conflicting_change(land_env, "RELEASE_MANIFEST.txt",
                             tip_manifest + "# re-pinned by a concurrent landing\n")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, f"step={result.step}: {result.stderr}"
    landed = _git(land_env.origin, "show",
                  f"{result.landed_sha}:RELEASE_MANIFEST.txt").stdout
    # The stub guard rewrites the ledger wholesale on `approve`, so the tip's
    # comment line does not survive here the way it does under the real
    # guard; what is pinned is the shape: a clean, re-derived manifest that
    # carries the branch's new pin and no conflict residue. Before the fix
    # this test fails one step earlier, at `squash`.
    assert "src/feature.py" in landed                         # branch pin re-derived
    assert "<<<<<<<" not in landed and "=======" not in landed
    assert "EXPORT_CLASSIFICATION.txt" in landed               # tip's pins kept


def test_squash_conflict_beyond_the_manifest_still_refuses(land_env):
    """The tolerance is for the ledger file ONLY: a conflict that also
    touches an authored file is a real conflict and still fails at
    `squash`, with the worktree cleaned up as before."""
    branch, _ = land_env.cut_branch(
        "no-human/t-mixedconflict",
        extra_files={"README.md": "branch changed this line\n"})
    tip_manifest = _git(land_env.origin, "show", "main:RELEASE_MANIFEST.txt").stdout
    _push_conflicting_change(land_env, "README.md", "main changed this line too\n")
    _push_conflicting_change(land_env, "RELEASE_MANIFEST.txt",
                             tip_manifest + "# re-pinned by a concurrent landing\n")
    repo = GitRepo(land_env.clone, never_push_to=["main", "master", "release/*"])
    before = repo.list_worktrees()
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert not result.ok
    assert result.step == "squash"
    assert "README.md" in result.stderr
    assert repo.list_worktrees() == before


def test_manifest_conflict_without_the_export_guard_still_refuses(land_env):
    """The tolerance exists because step 4 re-derives the manifest — and
    step 4 runs only where scripts/export_guard.py exists. In a repo without
    it a manifest conflict is a real conflict: taking the tip's copy would
    silently discard the branch's edit and push that. Refuse at `squash`."""
    # Remove the guard on origin/main from a third clone, the way the
    # conflicting-change helper does, so the landing worktree has no guard.
    race = land_env.tmp_path / "noguard"
    _git(land_env.tmp_path, "clone", "-q", str(land_env.origin), str(race))
    _git(race, "rm", "-q", "scripts/export_guard.py")
    _git(race, "commit", "-qm", "drop the export guard")
    _git(race, "push", "-q", "origin", "HEAD:main")
    _git(land_env.clone, "pull", "-q", "--ff-only", "origin", "main")
    branch, _ = land_env.cut_branch("no-human/t-noguard")
    tip_manifest = _git(land_env.origin, "show", "main:RELEASE_MANIFEST.txt").stdout
    _push_conflicting_change(land_env, "RELEASE_MANIFEST.txt",
                             tip_manifest + "# re-pinned by a concurrent landing\n")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert not result.ok
    assert result.step == "squash"
    assert "RELEASE_MANIFEST.txt" in result.stderr


def test_worktree_add_failure_leaves_no_worktree(land_env, monkeypatch):
    """`add_worktree` succeeding at the git level (a REAL worktree gets
    registered) and then the wrapper raising afterward is the one failure
    shape `land_task`'s old `except (GitError, ProtectedBranch)` handler at
    the `worktree` step did NOT clean up — every other step's failure goes
    through the `finally` below it, but this one returned straight away.
    Without the fix, `after_worktrees` would show the leaked entry."""
    branch, head_sha = land_env.cut_branch("no-human/t-worktreefail")
    real_add_worktree = GitRepo.add_worktree

    def _add_then_fail(self, *a, **kw):
        real_add_worktree(self, *a, **kw)  # really create it, git-level
        raise GitError("simulated failure after worktree creation")

    monkeypatch.setattr(GitRepo, "add_worktree", _add_then_fail)

    created_dirs: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def _track_mkdtemp(*a, **kw):
        p = real_mkdtemp(*a, **kw)
        created_dirs.append(p)
        return p

    monkeypatch.setattr(tempfile, "mkdtemp", _track_mkdtemp)

    repo = GitRepo(land_env.clone, never_push_to=["main", "master", "release/*"])
    before_worktrees = repo.list_worktrees()
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert not result.ok
    assert result.step == "worktree"

    after_worktrees = repo.list_worktrees()
    assert after_worktrees == before_worktrees
    assert len(after_worktrees) == 1
    assert created_dirs, "expected land_task to have created exactly one temp worktree dir"
    assert not Path(created_dirs[-1]).exists()


def test_disabled_config_records_approval_only(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-disabled")
    before = land_env.remote_main_sha()
    cfg = json.loads(json.dumps(land_env.config))
    cfg["approve_merge"]["enabled"] = False
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=cfg,
    )
    assert result.ok
    assert result.skipped
    assert land_env.remote_main_sha() == before


def test_never_push_to_still_blocks_agent_pushes(land_env):
    repo = GitRepo(land_env.clone, never_push_to=["main", "master", "release/*"])
    with pytest.raises(ProtectedBranch):
        repo.push("main")


# --------------------------------------------------------------------------- #
# landed-completion — validates (never rebuilds) blockers/shipped.py's       #
# containment probe: this is the coupling that completes a closed-PR task.  #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_landed_squash_reads_as_shipped_to_the_containment_probe(land_env):
    """`blockers/shipped.py::complete_if_content_landed` calls
    `pr_watcher.default_branch_shipped`, which asks — by TREE CONTENT, not
    commit ancestry — whether a branch still has anything left to contribute
    against the tip. `land_task` produces exactly one squash commit whose
    tree differs from the branch's only in `RELEASE_MANIFEST.txt`
    (`_GENERATED_LEDGERS`), so the probe must read the branch as shipped
    once `land_task` has landed it — this is the mechanism a closed-PR task
    (b53b9e13, 424f14a2) relies on to actually complete."""
    branch, head_sha = land_env.cut_branch("no-human/t-shipped-probe")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    shipped = await default_branch_shipped(str(land_env.origin), branch, base="main")
    assert shipped, (
        "default_branch_shipped must read the approve-merge squash as "
        "containing the branch's content (ledger-only diff excluded) — "
        "this is the coupling that completes a closed-PR task")


# --------------------------------------------------------------------------- #
# CLI: `nh approve`                                                           #
# --------------------------------------------------------------------------- #

def _make_cli_runner(db_path, config_data, monkeypatch) -> CliRunner:
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"

        def __init__(self, data, db_path):
            self.data = data
            self._db_path = db_path

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

        @property
        def db_path(self):
            return self._db_path

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg(config_data, db_path))
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(
        cmd_mod, "_probe_pool",
        lambda _cfg: cmd_mod.PoolProbe(None, cmd_mod.POOL_REFUSED))
    return CliRunner()


def _seed_land_task(db_path, status, *, repo_path, branch=None, pr_url=None,
                    review_history=None, title="Fix the thing",
                    task_id=None, base_branch=None,
                    attempt_branch=None, attempt_commit_sha=None) -> str:
    """`attempt_branch`/`attempt_commit_sha` seed one `attempts` row (branch
    defaults to `branch` when omitted) so `Store.latest_attempt_branch` — the
    source `nh approve` reads `tested_commit_sha` from — has something to
    return. Neither existed before this task; every existing caller omits
    them, so `latest_attempt_branch` keeps returning `{"branch": "",
    "commit_sha": ""}` for them exactly as it did before this pair of
    keywords was added."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new(title, repo_path=repo_path)
            if task_id is not None:
                t.id = task_id
            t.acceptance_criteria = ["Should work"]
            await s.create_task(t)
            ctx = {}
            if pr_url:
                ctx["pr_watch"] = pr_url
            if branch:
                ctx["pr_branch"] = branch
            if review_history is not None:
                ctx["review_history"] = review_history
            if base_branch:
                ctx["base_branch"] = base_branch
            if ctx:
                await s.merge_context(t.id, ctx)
            if attempt_commit_sha is not None:
                attempt_id = await s.create_attempt(t.id, 1)
                await s.update_attempt(
                    attempt_id, branch_name=attempt_branch or branch or "",
                    commit_sha=attempt_commit_sha,
                )
            if status is not TaskStatus.PENDING:
                await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


def _fetch_task_and_events(db_path, task_id):
    async def _go():
        async with Store(db_path) as s:
            t = await s.find_task(task_id)
            events = await s.list_events(task_id)
            return t, events
    return asyncio.run(_go())


def test_refuses_when_not_awaiting_approval(land_env, tmp_path, monkeypatch):
    branch, head_sha = land_env.cut_branch("no-human/t-notawaiting")
    before = land_env.remote_main_sha()
    db = tmp_path / "t.db"
    task_id = _seed_land_task(
        db, TaskStatus.IMPLEMENTING, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": head_sha, "passed": True}],
    )
    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])
    assert result.exit_code != 0
    assert land_env.remote_main_sha() == before


def test_refuses_when_no_review_pass_for_head_sha(land_env, tmp_path, monkeypatch):
    branch, head_sha = land_env.cut_branch("no-human/t-noreview")
    before = land_env.remote_main_sha()
    db = tmp_path / "t.db"
    off_branch_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    task_id = _seed_land_task(
        db, TaskStatus.AWAITING_APPROVAL, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": off_branch_sha, "passed": True}],
    )
    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])
    assert result.exit_code != 0
    assert "preconditions" in result.output.lower(), result.output
    assert land_env.remote_main_sha() == before


def test_cli_approve_marks_done_with_landed_sha(land_env, tmp_path, monkeypatch):
    branch, head_sha = land_env.cut_branch("no-human/t-clidone")
    db = tmp_path / "t.db"
    task_id = _seed_land_task(
        db, TaskStatus.AWAITING_APPROVAL, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": head_sha, "passed": True}],
    )
    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])
    assert result.exit_code == 0, result.output

    t, events = _fetch_task_and_events(db, task_id)
    assert t.status is TaskStatus.DONE
    merged = [e for e in events if e.get("kind") == "human_merged"]
    assert len(merged) == 1
    sha = merged[0]["sha"]
    assert sha
    assert sha[:12] in result.output


def test_cli_approve_completes_when_content_already_landed(land_env, tmp_path, monkeypatch):
    """The live-repro shape (task e58a81d6): a supervising-session squash
    train already landed this branch's content — via a FIRST task's
    `land_task` here, standing in for that train — and this SECOND task's
    (closed) PR still points at the same branch. Approving it must complete
    via content-equivalence, never attempt a conflicting re-merge. Red-first
    on the code before this change: the old `nh approve` re-squashed the
    same branch onto the now-different tip and hit a real conflict on
    RELEASE_MANIFEST.txt (the branch wipes it; the first land regenerates
    it), leaving the task awaiting_approval — see
    `test_squash_conflict_leaves_no_worktree` for that failure shape."""
    branch, head_sha = land_env.cut_branch("no-human/t-clilanded")
    first = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="firsttask", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert first.ok, first.stderr
    # Belt-and-braces: `land_task`'s own push already opportunistically
    # updates `origin/main` locally, but an explicit fetch removes any doubt
    # the second approve's containment probe reads a stale tracking ref.
    _git(land_env.clone, "fetch", "-q", "origin", "main")

    db = tmp_path / "t.db"
    task_id = _seed_land_task(
        db, TaskStatus.AWAITING_APPROVAL, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": head_sha, "passed": True}],
        base_branch="main",
    )
    repo = GitRepo(land_env.clone, never_push_to=["main", "master", "release/*"])
    before_worktrees = repo.list_worktrees()
    before_main = land_env.remote_main_sha()

    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])
    assert result.exit_code == 0, result.output

    t, events = _fetch_task_and_events(db, task_id)
    assert t.status is TaskStatus.DONE
    approved_landed = [e for e in events if e.get("kind") == "approved_landed"]
    assert len(approved_landed) == 1
    assert not [e for e in events if e.get("kind") == "human_merged"]
    assert land_env.remote_main_sha() == before_main
    assert repo.list_worktrees() == before_worktrees


def test_approve_unlanded_pr_still_merges_and_probe_says_no(land_env, tmp_path, monkeypatch):
    """Control for the landed-completion path above: an ordinary, still-open
    PR's content is NOT on the default branch yet, so the new content check
    must say "no" and `nh approve` must still land it via `land_task` exactly
    as before this change."""
    branch, head_sha = land_env.cut_branch("no-human/t-unlanded")
    before = land_env.remote_main_sha()
    db = tmp_path / "t.db"
    task_id = _seed_land_task(
        db, TaskStatus.AWAITING_APPROVAL, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": head_sha, "passed": True}],
        base_branch="main",
    )
    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])
    assert result.exit_code == 0, result.output

    t, events = _fetch_task_and_events(db, task_id)
    assert t.status is TaskStatus.DONE
    merged = [e for e in events if e.get("kind") == "human_merged"]
    assert len(merged) == 1
    assert merged[0]["sha"]
    assert not [e for e in events if e.get("kind") == "approved_landed"]
    assert land_env.remote_main_sha() != before


# --------------------------------------------------------------------------- #
# API: POST /api/tasks/{id}/approve                                           #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def api_store_client(tmp_path):
    from no_human.config import load_config

    # `app.state` is the module-global FastAPI singleton, shared with ~every
    # other test module that drives the API — save/restore instead of
    # clobbering, so nothing here leaks into a worker-mate under `-n 4`.
    had_store = hasattr(app.state, "store")
    prior_store = getattr(app.state, "store", None)
    had_config = hasattr(app.state, "config")
    prior_config = getattr(app.state, "config", None)

    store = await Store(tmp_path / "api.db").connect()
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c, store
    finally:
        await store.close()
        if had_store:
            app.state.store = prior_store
        else:
            del app.state.store
        if had_config:
            app.state.config = prior_config
        else:
            del app.state.config


@pytest.mark.asyncio
async def test_api_approve_marks_done_with_landed_sha(land_env, api_store_client):
    client, store = api_store_client
    branch, head_sha = land_env.cut_branch("no-human/t-apidone")

    t = Task.new("Fix the thing", repo_path=str(land_env.clone))
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    await store.merge_context(t.id, {
        "pr_watch": land_env.pr_url, "pr_branch": branch,
        "review_history": [{"sha": head_sha, "passed": True}],
    })
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["landed_sha"]

    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    merged = [e for e in events if e.get("kind") == "human_merged"]
    assert len(merged) == 1
    assert merged[0]["sha"] == data["landed_sha"]


@pytest.mark.asyncio
async def test_api_approve_completes_when_content_already_landed(land_env, api_store_client):
    """API counterpart of `test_cli_approve_completes_when_content_already_landed`
    — same closed-PR-but-landed shape, via `POST /api/tasks/{id}/approve`."""
    client, store = api_store_client
    branch, head_sha = land_env.cut_branch("no-human/t-apilanded")
    first = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="firsttask", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert first.ok, first.stderr
    _git(land_env.clone, "fetch", "-q", "origin", "main")

    t = Task.new("Fix the thing", repo_path=str(land_env.clone))
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    await store.merge_context(t.id, {
        "pr_watch": land_env.pr_url, "pr_branch": branch,
        "review_history": [{"sha": head_sha, "passed": True}],
        "base_branch": "main",
    })
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    before_main = land_env.remote_main_sha()
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["landed_sha"] == ""

    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    approved_landed = [e for e in events if e.get("kind") == "approved_landed"]
    assert len(approved_landed) == 1
    assert not [e for e in events if e.get("kind") == "human_merged"]
    assert land_env.remote_main_sha() == before_main


@pytest.mark.asyncio
async def test_api_approve_surfaces_land_failure(land_env, api_store_client, monkeypatch):
    """A genuine `land_task` failure (export_guard verify fails here) must
    come back as an HTTPException the caller can see — not a silent 200
    wearing the record-only "merge it yourself" message, which would read
    a real failure as success. The task stays awaiting_approval either way
    (plan §8's clean-abort contract, mirrored by the CLI's own exit(1))."""
    from no_human.api.app import _mgr

    client, store = api_store_client
    branch, head_sha = land_env.cut_branch(
        "no-human/t-apiverifyfail", extra_files={"FORCE_VERIFY_FAIL": "x"})
    before = land_env.remote_main_sha()

    t = Task.new("Fix the thing", repo_path=str(land_env.clone))
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    await store.merge_context(t.id, {
        "pr_watch": land_env.pr_url, "pr_branch": branch,
        "review_history": [{"sha": head_sha, "passed": True}],
    })
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    broadcasts = []
    orig_broadcast = _mgr.broadcast

    async def spy_broadcast(payload):
        broadcasts.append(payload)
        return await orig_broadcast(payload)

    monkeypatch.setattr(_mgr, "broadcast", spy_broadcast)

    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["step"] == "verify"
    assert detail["stderr"]

    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL
    events = await store.list_events(t.id)
    assert not [e for e in events if e.get("kind") == "human_merged"]
    assert land_env.remote_main_sha() == before

    # A land failure must still surface a `merge_failed` progress frame — a
    # second observer/tab watching the WS sees the failure, not just the
    # caller of this one POST.
    task_events = [b for b in broadcasts if b.get("type") == "task_event"]
    failed = [b for b in task_events if b["event"].get("kind") == "merge_failed"]
    assert len(failed) == 1, broadcasts
    assert "verify" in failed[0]["event"]["text"]

    # The merge lock must be released even on a genuine land failure — a
    # retried approve for the same task must not itself 409.
    assert not (refreshed.context or {}).get("merge_in_progress")


# --------------------------------------------------------------------------- #
# Live merge progress: idempotency guard + streamed task_events               #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_second_approve_during_merge_returns_409(land_env, api_store_client, monkeypatch):
    """The idempotency guard (operator finding: a 2-4 minute synchronous land
    with zero feedback read as a dead button, and a raced double-click must
    never reach a second `land_task`). `land_task` is patched with a stub that
    blocks on a real thread barrier, so the second request deterministically
    observes "still landing" rather than depending on timing."""
    client, store = api_store_client
    branch, head_sha = land_env.cut_branch("no-human/t-409")

    t = Task.new("Fix the thing", repo_path=str(land_env.clone))
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    await store.merge_context(t.id, {
        "pr_watch": land_env.pr_url, "pr_branch": branch,
        "review_history": [{"sha": head_sha, "passed": True}],
    })
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    entered = threading.Event()
    release = threading.Event()

    def slow_land_task(*, repo_path, branch, pr_url, task_id, task_title,
                        review_evidence, config, on_step=None, **kwargs):
        entered.set()
        assert release.wait(timeout=5), "test barrier never released"
        return LandResult(ok=True, step="close_pr", landed_sha="a" * 40,
                           pr_url=pr_url, branch=branch, message="landed")

    monkeypatch.setattr("no_human.vcs.approve_merge.land_task", slow_land_task)

    first = asyncio.create_task(client.post(f"/api/tasks/{t.id}/approve"))
    # Block the TEST coroutine on a real OS thread wait (not an asyncio
    # primitive) — `slow_land_task` runs inside `asyncio.to_thread`, a real
    # worker thread, and only `entered.set()` proves the first request has
    # actually claimed the merge lock and started landing.
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)

    r2 = await client.post(f"/api/tasks/{t.id}/approve")
    release.set()
    r1 = await first

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 409, r2.text
    assert r2.json()["detail"] == "Merge already in progress"


@pytest.mark.asyncio
async def test_approve_emits_merge_progress_events_in_order(land_env, api_store_client, monkeypatch):
    """The server streams merge_started -> merge_step_<step> (one per real
    land step) -> human_merged over the SAME WS broadcast the SlideOver
    already renders other frames from — this is what turns the previously
    dead multi-minute window into visible progress."""
    from no_human.api.app import _mgr

    client, store = api_store_client
    branch, head_sha = land_env.cut_branch("no-human/t-progress")

    t = Task.new("Fix the thing", repo_path=str(land_env.clone))
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    await store.merge_context(t.id, {
        "pr_watch": land_env.pr_url, "pr_branch": branch,
        "review_history": [{"sha": head_sha, "passed": True}],
    })
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    broadcasts = []
    orig_broadcast = _mgr.broadcast

    async def spy_broadcast(payload):
        broadcasts.append(payload)
        return await orig_broadcast(payload)

    monkeypatch.setattr(_mgr, "broadcast", spy_broadcast)

    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200, r.text

    task_events = [b for b in broadcasts if b.get("type") == "task_event"]
    kinds = [b["event"]["kind"] for b in task_events]
    assert kinds and kinds[0] == "merge_started", kinds
    assert "merge_step_fetch" in kinds, kinds
    assert "merge_step_push" in kinds, kinds
    assert kinds[-1] == "human_merged", kinds

    ts_values = [b["event"]["ts"] for b in task_events]
    assert ts_values == sorted(ts_values), ts_values


@pytest.mark.asyncio
async def test_merge_lock_released_after_success_and_after_failure(
    land_env, api_store_client, monkeypatch,
):
    """`claim_merge`/`release_merge` must not wedge the button forever —
    the lock is released in `approve_task`'s `finally`, on both the happy
    path and a genuine land failure, so a retried approve is never stuck
    behind its own predecessor's lock."""
    client, store = api_store_client

    # -- success path -------------------------------------------------------
    branch, head_sha = land_env.cut_branch("no-human/t-lock-ok")
    t_ok = Task.new("Fix the thing", repo_path=str(land_env.clone))
    t_ok.acceptance_criteria = ["Should work"]
    await store.create_task(t_ok)
    await store.merge_context(t_ok.id, {
        "pr_watch": land_env.pr_url, "pr_branch": branch,
        "review_history": [{"sha": head_sha, "passed": True}],
    })
    await store.set_status(t_ok, TaskStatus.AWAITING_APPROVAL, validate=False)

    r_ok = await client.post(f"/api/tasks/{t_ok.id}/approve")
    assert r_ok.status_code == 200, r_ok.text
    refreshed_ok = await store.find_task(t_ok.id)
    assert not (refreshed_ok.context or {}).get("merge_in_progress")
    # The lock is genuinely reclaimable, not just falsy-looking.
    assert await store.claim_merge(t_ok.id) is True
    await store.release_merge(t_ok.id)

    # -- failure path -------------------------------------------------------
    branch2, head_sha2 = land_env.cut_branch(
        "no-human/t-lock-fail", extra_files={"FORCE_VERIFY_FAIL": "x"})
    t_fail = Task.new("Fix the other thing", repo_path=str(land_env.clone))
    t_fail.acceptance_criteria = ["Should work"]
    await store.create_task(t_fail)
    await store.merge_context(t_fail.id, {
        "pr_watch": land_env.pr_url, "pr_branch": branch2,
        "review_history": [{"sha": head_sha2, "passed": True}],
    })
    await store.set_status(t_fail, TaskStatus.AWAITING_APPROVAL, validate=False)

    r_fail = await client.post(f"/api/tasks/{t_fail.id}/approve")
    assert r_fail.status_code == 500, r_fail.text
    refreshed_fail = await store.find_task(t_fail.id)
    assert not (refreshed_fail.context or {}).get("merge_in_progress")

    # A retry after a failure must not be blocked by a stuck lock.
    def ok_land_task(*, repo_path, branch, pr_url, task_id, task_title,
                      review_evidence, config, on_step=None, **kwargs):
        return LandResult(ok=True, step="close_pr", landed_sha="b" * 40,
                           pr_url=pr_url, branch=branch, message="landed")

    monkeypatch.setattr("no_human.vcs.approve_merge.land_task", ok_land_task)
    r_retry = await client.post(f"/api/tasks/{t_fail.id}/approve")
    assert r_retry.status_code == 200, r_retry.text


@pytest.mark.asyncio
async def test_stale_merge_claim_is_reclaimable(tmp_path):
    """A crashed server must not wedge the button forever — a claim older
    than `Store._MERGE_CLAIM_STALE_S` is reclaimable by a fresh `approve`."""
    store = await Store(tmp_path / "stale.db").connect()
    try:
        t = Task.new("Fix the thing", repo_path="/tmp/does-not-matter")
        await store.create_task(t)
        stale_ts = time.time() - Store._MERGE_CLAIM_STALE_S - 60
        await store.merge_context(t.id, {"merge_in_progress": stale_ts})

        assert await store.claim_merge(t.id) is True, \
            "a claim older than the TTL must be reclaimable"
        refreshed = await store.find_task(t.id)
        claimed_ts = (refreshed.context or {}).get("merge_in_progress")
        assert claimed_ts and claimed_ts != stale_ts
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fresh_merge_claim_blocks_a_second_claim(tmp_path):
    """The mirror of the staleness test — a LIVE claim must refuse a second
    one, which is the whole point of the CAS."""
    store = await Store(tmp_path / "fresh.db").connect()
    try:
        t = Task.new("Fix the thing", repo_path="/tmp/does-not-matter")
        await store.create_task(t)
        assert await store.claim_merge(t.id) is True
        assert await store.claim_merge(t.id) is False, \
            "a live (non-stale) claim must refuse a second claim"
    finally:
        await store.close()


# --------------------------------------------------------------------------- #
# A squash onto a tip that also bumped a counted rule: merge arithmetic       #
# --------------------------------------------------------------------------- #


def _advance_origin_with_counted_file(land_env, name: str, *, bump: bool) -> str:
    """Land `src/<name>.py` on origin/main from a third clone, bumping (or,
    for the negative control, NOT bumping) `ship N src/*.py`."""
    race = land_env.tmp_path / f"race-{name}"
    _git(land_env.tmp_path, "clone", "-q", str(land_env.origin), str(race))
    (race / "src" / f"{name}.py").write_text(f"def {name}():\n    return 9\n")
    if bump:
        cls = race / "EXPORT_CLASSIFICATION.txt"
        cls.write_text(cls.read_text(encoding="utf-8").replace("ship 2 src/*.py", "ship 3 src/*.py"))
    _git(race, "add", "-A")
    if bump:
        # a real landed commit carries its own pin (the gate requires it)
        subprocess.run([sys.executable, "scripts/export_guard.py", "approve", f"src/{name}.py"],
                       cwd=race, check=True, capture_output=True, text=True)
        _git(race, "add", "-A")
    _git(race, "commit", "-qm", f"main: add {name}.py")
    _git(race, "push", "-q", "origin", "HEAD:main")
    return _git(race, "rev-parse", "HEAD").stdout.strip()


def test_land_reconciles_two_reviewed_count_bumps(land_env):
    """c309a6a3's shape at the `nh approve` site: the branch added feature.py
    and bumped 2->3; main then added another src file and bumped 2->3; the
    squash merges the classification cleanly at 3 while the tree holds 4.
    `approve` refuses; land does base + (branch - merge-base) == real and
    lands, and the result says so."""
    # corrupt_manifest=False: the watcher keeps a PR branch's manifest mergeable
    # before approval; what reaches land here is a tip that moved AFTER that,
    # with a manifest change that auto-merges and a count that does not.
    branch, _ = land_env.cut_branch("no-human/t-arith", corrupt_manifest=False)
    _advance_origin_with_counted_file(land_env, "other", bump=True)
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, f"{result.step}: {result.stderr}"
    assert "2 -> 4" not in result.reconciled and "3 -> 4" in result.reconciled, result.reconciled
    cls = _git(land_env.origin, "show", f"{result.landed_sha}:EXPORT_CLASSIFICATION.txt").stdout
    assert "ship 4 src/*.py" in cls
    manifest = _git(land_env.origin, "show", f"{result.landed_sha}:RELEASE_MANIFEST.txt").stdout
    assert "src/feature.py" in manifest and "src/other.py" in manifest


def test_land_refuses_a_count_drift_that_is_not_merge_arithmetic(land_env):
    """Negative control: main added a counted file WITHOUT bumping — a stale
    count on one side is a hand problem; land must refuse at 'manifest' with
    the arithmetic shown, and push nothing."""
    # corrupt_manifest=False: the watcher keeps a PR branch's manifest mergeable
    # before approval; what reaches land here is a tip that moved AFTER that,
    # with a manifest change that auto-merges and a count that does not.
    branch, _ = land_env.cut_branch("no-human/t-arith-neg", corrupt_manifest=False)
    tip = _advance_origin_with_counted_file(land_env, "sloppy", bump=False)
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert not result.ok and result.step == "manifest"
    assert "not a mechanical merge" in result.stderr, result.stderr
    assert land_env.remote_main_sha() == tip
