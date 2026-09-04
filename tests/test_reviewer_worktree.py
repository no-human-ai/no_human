"""Tests for the reviewer-worktree integrity guard (task d115e22f).

`reviewer_worktree.snapshot`/`compare` originally drove entirely off `git
status --porcelain`, which is structurally blind to the `.git` subtree — a
Bash-enabled reviewer session could plant an executable hook there invisibly
to that instrument, and `revert()`'s own git calls would then EXECUTE it.
These tests cover, against REAL git (no mocking: the whole claim is about
what git itself does with hooks in a linked worktree, which a mock would
assume away):

  - detection: a planted/chmod-flipped `.git`-subtree file is reported
    through the same added/modified/deleted delta as a worktree change
  - non-vacuity: the `.git` inventory is not silently empty for the kind of
    linked worktree the product actually hands a reviewer
  - hook-safety: `revert()`'s own `git checkout`/`git reset` calls cannot be
    made to execute a hook planted in the (real, already-installed)
    per-worktree hooks path
  - no false positives: read-only git plumbing and the documented volatile
    paths do not themselves trip the guard
  - the pre-existing worktree-file snapshot/compare/revert cycle and
    fail-closed semantics, unrelated to the `.git`-subtree work above
"""

from __future__ import annotations

import ast
import stat
import subprocess
from pathlib import Path

import pytest

from no_human.core import reviewer_worktree as rw
from no_human.core.orchestrator import _integrity_failure_decision
from no_human.vcs.git import GitRepo

PROTECTED = ["main", "master", "release/*"]
_TIMEOUT = 30.0


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=check,
    )


def _seeded_repo(tmp_path) -> Path:
    """A repo with a real commit, so `snapshot` succeeds on the unpatched
    path. A bare `git init` has no resolvable HEAD and makes `snapshot` raise
    for the WRONG reason."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("v1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


@pytest.fixture
def worktree_env(tmp_path):
    """A reviewer worktree built through the product's own `add_worktree`,
    so `no_human-hooks` (the hooks path already installed and effective —
    see `push_hook.py`) is present exactly as it is for a real task."""
    remote = tmp_path / "remote.git"
    up = tmp_path / "upstream"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(tmp_path, "init", "-q", "-b", "main", str(up))
    (up / "src").mkdir()
    (up / "src" / "main.py").write_text("v1\n")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "init")
    _git(up, "remote", "add", "origin", str(remote))
    _git(up, "push", "-q", "origin", "main")

    wt_path = tmp_path / "reviewer-wt"
    main_repo = GitRepo(up, never_push_to=list(PROTECTED))
    main_repo.add_worktree(wt_path, base="main", detach=True)
    _git(wt_path, "checkout", "-q", "-b", "no-human/task-1")

    admin_dir = rw._resolve_git_root(wt_path, "--absolute-git-dir", timeout=_TIMEOUT)
    common_dir = rw._resolve_git_root(wt_path, "--git-common-dir", timeout=_TIMEOUT)
    return {
        "remote": remote, "up": up, "wt": wt_path,
        "admin_dir": admin_dir, "common_dir": common_dir,
        "hooks_dir": admin_dir / "no_human-hooks",
    }


def _plant_exec_hook(path: Path, canary: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch {canary}\n")
    path.chmod(0o755)


def _plant_symlinked_hook(path: Path, canary: Path, target_dir: Path) -> Path:
    """Plant an executable OUTSIDE the repo and symlink `path` at it — the
    exact evasion a prior review round refuted this guard on: the old
    `_walk_git_root` unconditionally skipped `path.is_symlink()` entries, so
    `ln -s` past the inventory instead of writing a regular file defeated
    detection outright."""
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "payload.sh"
    target.write_text(f"#!/bin/sh\ntouch {canary}\n")
    target.chmod(0o755)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    return target


# --------------------------------------------------------------------------- #
# Part A: `.git`-subtree detection
# --------------------------------------------------------------------------- #

def test_git_inventory_is_nonempty_for_a_linked_worktree(worktree_env):
    """The inventory covers the EXECUTION SURFACES of a real linked worktree.

    `admin/HEAD` IS present and IS watched — it is this worktree's own and
    no other process writes it; rewriting it to the raw sha leaves
    `git rev-parse HEAD` byte-identical, so only the inventory sees the
    detach. The SHARED copy (`common/HEAD`) is ALSO inventoried: an earlier
    revision path-skipped it because ordinary `git checkout` in the primary
    checkout writes it and that discarded completed verdicts, but a round-10
    review measured the skip hiding a write that repointed the primary
    checkout. It is now content-adjudicated in `compare()` instead —
    symref -> symref is clean, anything else is a violation — so the
    inventory must carry it for `compare()` to see the change at all.
    """
    inv = rw._git_dir_inventory(worktree_env["wt"], timeout=_TIMEOUT)
    assert inv, "the .git inventory is empty for a real linked worktree"
    hooks = [k for k in inv if "hooks/" in k]
    assert hooks, (
        "no hook path in the inventory — the guard is watching nothing that "
        f"git can execute: {sorted(inv)[:20]}")
    assert "admin/HEAD" in inv, sorted(inv)[:20]
    assert "common/HEAD" in inv, (
        "the SHARED HEAD left the inventory — compare()'s content-shape "
        f"adjudication has nothing to adjudicate: {sorted(inv)[:20]}")


def test_a_head_change_is_reported_via_the_dedicated_HEAD_comparison(
        worktree_env):
    """The other half of the test above: whatever the inventory carries,
    a reviewer's commit must never be invisible. `compare()` compares the
    resolved HEAD separately and reports it as a synthetic entry."""
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)
    (wt / "moved.txt").write_text("x\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-qm", "the reviewer committed")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)

    assert not delta.is_empty(), delta
    assert any(m.startswith("HEAD:") for m in delta.modified), delta.modified


def test_planted_git_hook_between_snapshot_and_compare_is_detected(worktree_env):
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    hook = worktree_env["hooks_dir"] / "post-checkout"
    _plant_exec_hook(hook, worktree_env["wt"] / "canary")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert not delta.is_empty(), "planted .git hook was not detected"
    offending = [*delta.added, *delta.modified, *delta.deleted]
    assert any(p.startswith(".git/") and "post-checkout" in p for p in offending), offending


def test_planted_symlinked_git_hook_between_snapshot_and_compare_is_detected(worktree_env):
    """A prior revision of `_walk_git_root` unconditionally skipped any path
    where `path.is_symlink()` was true, so `ln -s /tmp/payload post-checkout`
    was invisible to the `.git` inventory even though git executes a
    symlinked hook exactly like a regular one. This is the exact evasion an
    independent review used to refute the previous attempt."""
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    hook = worktree_env["hooks_dir"] / "post-checkout"
    canary = worktree_env["wt"] / "canary"
    outside = worktree_env["wt"].parent / "outside-payload"
    _plant_symlinked_hook(hook, canary, outside)

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert not delta.is_empty(), "planted symlinked .git hook was not detected"
    offending = [*delta.added, *delta.modified, *delta.deleted]
    assert any(p.startswith(".git/") and "post-checkout" in p for p in offending), offending


def test_chmod_of_existing_git_file_is_detected(worktree_env):
    wt = worktree_env["wt"]
    sample = worktree_env["common_dir"] / "hooks" / "pre-commit.sample"
    assert sample.is_file(), "fixture assumption: git ships hooks/*.sample files"
    before_mode = stat.S_IMODE(sample.stat().st_mode)

    before = rw.snapshot(wt, timeout=_TIMEOUT)
    # XOR, not OR: git's shipped *.sample hooks are already mode 0o755 on some
    # git versions, so OR-ing in the exec bits would be a no-op. XOR-ing a
    # single bit is guaranteed to change the mode regardless of the starting
    # value.
    sample.chmod(before_mode ^ stat.S_IXOTH)

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert not delta.is_empty(), (
        "an exec-bit-only change on byte-identical content was missed"
    )
    offending = [*delta.added, *delta.modified, *delta.deleted]
    assert any("pre-commit.sample" in p for p in offending), offending


def test_benign_git_status_does_not_trigger_reviewer_wrote(worktree_env):
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    # Real, read-only git plumbing rewrites `.git/index`'s stat cache — this
    # is the exact false positive the exclusion list exists to prevent.
    _git(wt, "status", "--porcelain")
    _git(wt, "log", "--oneline", "-1")
    _git(wt, "diff", "--stat")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.is_empty(), (
        f"read-only git activity was reported as a write: added={delta.added} "
        f"modified={delta.modified} deleted={delta.deleted}"
    )


def test_excluded_volatile_paths_do_not_trigger_reviewer_wrote(worktree_env):
    """FETCH_HEAD/ORIG_HEAD/reflog churn constantly under ordinary git use
    (fetch, reset, merge) and carries no execution risk. Writing them
    directly isolates the exclusion list itself from git's own, less
    predictable, internals (e.g. whether a given git version elides a
    same-value ref rewrite)."""
    wt = worktree_env["wt"]
    common = worktree_env["common_dir"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    (common / "FETCH_HEAD").write_text("deadbeef\t\tbranch 'main' of origin\n")
    (common / "ORIG_HEAD").write_text(_git(wt, "rev-parse", "HEAD").stdout)
    log_head = common / "logs" / "HEAD"
    log_head.parent.mkdir(parents=True, exist_ok=True)
    with log_head.open("a") as fh:
        fh.write("0" * 40 + " " + "1" * 40 + " t <t@t.t> 0 +0000\tcommit: x\n")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.is_empty(), (
        f"a volatile-list path was reported as a write: added={delta.added} "
        f"modified={delta.modified} deleted={delta.deleted}"
    )


def test_config_reserialization_excused_but_key_change_and_source_edit_caught(
    worktree_env,
):
    """Root-cause regression for the `.git/common/config` verdict-discard.

    git and concurrent writers of the SHARED common dir re-serialize `config`
    with the SAME effective keys — different whitespace, section order,
    comments — on ordinary bookkeeping. Byte-hashing that (the guard's prior
    behaviour) reported it as a reviewer write and discarded completed
    verdicts, each named with a `.git/common/config` path. `compare` now
    adjudicates config by EFFECTIVE key set, the same content-shape treatment
    `common/HEAD` gets, so:

      - a byte-different, effective-EQUAL rewrite is NOT reported (green), while
      - a real execution-relevant key (`include.path`) AND a real source-file
        edit — the guard's core protection — are BOTH still caught (red).
    """
    wt = worktree_env["wt"]
    cfg = worktree_env["common_dir"] / "config"

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    # Benign: change config's BYTES (leading comment + retabbed) while leaving
    # every effective key identical — exactly the shared-dir bookkeeping churn
    # that used to discard verdicts.
    original = cfg.read_text()
    cfg.write_text("# a concurrent tool rewrote me\n"
                   + original.replace("\t", "    ") + "\n")
    assert cfg.read_text() != original, "the reformat did not change the bytes"

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.is_empty(), (
        "an effective-equal config re-serialization was reported as a write: "
        f"added={delta.added} modified={delta.modified} deleted={delta.deleted}"
    )

    # Real: an include.path key (an exec-on-checkout surface) added to config
    # AND a reviewer edit of a tracked source file. Both must fire against the
    # SAME baseline the benign churn was measured against.
    _git(wt, "config", "--file", str(cfg), "include.path", "/tmp/evil")
    (wt / "src" / "main.py").write_text("v2 -- reviewer edit\n")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert "src/main.py" in delta.modified, (
        f"reviewer source edit was not caught: modified={delta.modified}")
    assert any("/config" in p for p in delta.modified), (
        "an include.path addition to config was not caught: "
        f"modified={delta.modified}")


def test_a_bookkeeping_branch_key_written_to_the_shared_config_is_excused_and_disclosed(
    worktree_env,
):
    """Root-cause regression for the 31 recorded `.git/common/config`
    verdict-discards (task: reviewer-worktree-integrity-benign-config-write).

    Every one of those attempts shared the identical shape: zero tracked-path
    changes, and the sole diff a key added to the WORKTREE-SHARED
    `.git/config` by a CONCURRENT process (another worktree's `git branch`/
    `checkout -b`/`fetch`) during the review window — never the reviewer
    under test. This plants exactly that shape: a `branch.<name>.rebase` key,
    the same key git itself writes on `git branch --set-upstream-to`/
    `checkout -b`, added directly to the shared common config as a stand-in
    for the concurrent worktree that really writes it.

    `delta.is_empty()` must stay True (the verdict is accepted) while the
    path and key are still surfaced through `delta.benign`/`benign_keys` (the
    disclosure survives even though the discard does not).
    """
    wt = worktree_env["wt"]
    common_dir = worktree_env["common_dir"]
    cfg = common_dir / "config"

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    _git(wt, "config", "--file", str(cfg), "branch.some-other-task.rebase", "true")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.is_empty(), (
        "a bookkeeping branch key written to the shared config discarded the "
        f"verdict: added={delta.added} modified={delta.modified} "
        f"deleted={delta.deleted}"
    )
    assert any(p.endswith("common/config") or p == ".git/common/config"
               for p in delta.benign), (
        f"the excused config write was not disclosed: benign={delta.benign}")
    assert "branch.some-other-task.rebase" in delta.benign_keys, (
        f"the changed key was not disclosed: benign_keys={delta.benign_keys}")


def test_a_non_bookkeeping_config_key_and_a_tracked_edit_still_discard(
    worktree_env,
):
    """Pin: the benign-key allowlist must not swallow a real violation.

    Same baseline as the bookkeeping-key test above, but this time the
    changed config key (`include.path`) is NOT on the allowlist, and a
    tracked source file is ALSO edited. Both must still land in `modified`
    and the delta must stay non-empty — this is the exact protection the
    module exists for, and the allowlist added by this change must not widen
    to cover it.
    """
    wt = worktree_env["wt"]
    common_dir = worktree_env["common_dir"]
    cfg = common_dir / "config"

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    _git(wt, "config", "--file", str(cfg), "include.path", "/tmp/evil")
    (wt / "src" / "main.py").write_text("v2 -- reviewer edit\n")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert any("/config" in p for p in delta.modified), (
        f"a non-allowlisted config key was excused instead of caught: "
        f"modified={delta.modified} benign={delta.benign}")
    assert not any(p.endswith("/config") for p in delta.benign), (
        f"a non-allowlisted config key landed in benign: benign={delta.benign}")
    assert "src/main.py" in delta.modified, (
        f"the tracked edit alongside a config change was not caught: "
        f"modified={delta.modified}")
    assert not delta.is_empty()

    # Mixed case: a benign key added ALONGSIDE the tracked edit must still
    # yield a non-empty delta — the tracked edit alone is enough to keep the
    # gate from ever excusing the whole review.
    before2 = rw.snapshot(wt, timeout=_TIMEOUT)
    _git(wt, "config", "--file", str(cfg), "branch.some-other-task.rebase", "true")
    (wt / "src" / "main.py").write_text("v3 -- another reviewer edit\n")

    mixed = rw.compare(wt, before2, timeout=_TIMEOUT)
    assert "src/main.py" in mixed.modified
    assert not mixed.is_empty(), (
        "a tracked edit alongside a benign-only config key was excused: "
        f"added={mixed.added} modified={mixed.modified} deleted={mixed.deleted}"
    )


def test_a_non_benign_config_key_is_named_in_the_discard_alongside_a_benign_one(
    worktree_env,
):
    """Task reviewer-worktree-integrity-name-nonbenign-keys.

    A discard used to name only the FILE (`.git/common/config`), never WHICH
    key kept it a violation — so a real execution-surface write (`alias.*`,
    `include.path`, `core.hooksPath`) was indistinguishable from a new
    bookkeeping key that SHOULD be allowlisted, and the allowlist could never
    be safely closed. This plants ONE benign key and ONE non-benign key in
    the same write and asserts the discard names the non-benign one and does
    NOT falsely name the benign one.
    """
    wt = worktree_env["wt"]
    common_dir = worktree_env["common_dir"]
    cfg = common_dir / "config"

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    _git(wt, "config", "--file", str(cfg), "branch.x.rebase", "true")
    _git(wt, "config", "--file", str(cfg), "alias.pwn", "!sh -c id")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert not delta.is_empty(), (
        "a mixed benign/non-benign config change was excused: "
        f"added={delta.added} modified={delta.modified} deleted={delta.deleted}")

    entries = [p for p in delta.modified
               if p.startswith(".git/") and "/config" in p]
    assert entries, f"no config entry in modified: {delta.modified}"
    entry = entries[0]
    assert "(non-benign keys: alias.pwn)" in entry, entry
    assert "branch.x.rebase" not in entry, (
        f"the benign key was falsely named as non-benign: {entry}")
    assert delta.nonbenign_keys == ["alias.pwn"], delta.nonbenign_keys
    assert "branch.x.rebase" not in delta.benign_keys, (
        "the benign key was disclosed as excused even though the whole "
        f"file stayed a violation: benign_keys={delta.benign_keys}")


def test_a_benign_only_config_change_names_nothing_as_non_benign(worktree_env):
    """The unchanged half: a benign-only change stays excused, and the
    naming added by this task must not start naming anything for it."""
    wt = worktree_env["wt"]
    common_dir = worktree_env["common_dir"]
    cfg = common_dir / "config"

    before = rw.snapshot(wt, timeout=_TIMEOUT)
    _git(wt, "config", "--file", str(cfg), "branch.some-other-task.rebase", "true")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.is_empty(), (
        f"added={delta.added} modified={delta.modified} deleted={delta.deleted}")
    assert "branch.some-other-task.rebase" in delta.benign_keys, delta.benign_keys
    assert delta.nonbenign_keys == [], delta.nonbenign_keys
    assert not any("non-benign keys" in p
                   for p in (*delta.added, *delta.modified, *delta.deleted)), (
        "a benign-only change was named as non-benign: "
        f"added={delta.added} modified={delta.modified} deleted={delta.deleted}")


def test_the_non_benign_key_list_is_capped_and_reports_the_remainder(worktree_env):
    """A pathological diff must not blow the persisted verdict message up —
    same shape as `orchestrator._INTEGRITY_PATHS_SHOWN`'s per-bucket cap."""
    wt = worktree_env["wt"]
    common_dir = worktree_env["common_dir"]
    cfg = common_dir / "config"

    before = rw.snapshot(wt, timeout=_TIMEOUT)
    n = rw._MAX_NONBENIGN_KEYS_SHOWN + 3
    for i in range(n):
        _git(wt, "config", "--file", str(cfg), f"alias.pwn{i}", "!true")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    entries = [p for p in delta.modified
               if p.startswith(".git/") and "/config" in p]
    assert entries, f"no config entry in modified: {delta.modified}"
    entry = entries[0]

    assert "and 3 more" in entry, entry
    shown = [f"alias.pwn{i}" for i in range(rw._MAX_NONBENIGN_KEYS_SHOWN)]
    for key in shown:
        assert key in entry, entry
    beyond = [f"alias.pwn{i}" for i in range(rw._MAX_NONBENIGN_KEYS_SHOWN, n)]
    for key in beyond:
        assert key not in entry, f"a key beyond the cap was named: {key} in {entry}"
    assert len(delta.nonbenign_keys) == rw._MAX_NONBENIGN_KEYS_SHOWN, delta.nonbenign_keys


def test_the_non_benign_key_cap_value_itself_is_asserted_not_derived():
    """`n = _MAX_NONBENIGN_KEYS_SHOWN + 3` in the cap test above derives
    from the constant under test, so raising the cap to 200 would keep that
    test green while making a persisted discard message 200 keys long. This
    pins the VALUE against a literal."""
    assert rw._MAX_NONBENIGN_KEYS_SHOWN == 5, (
        "the non-benign key display cap changed; a persisted discard message "
        "is read by the next attempt and by an operator, so a larger value "
        "needs a deliberate decision, not a silent edit")


def test_the_benign_allowlist_is_unchanged_by_this_naming_change():
    """Naming a non-benign key (this task) is disclosure only — it is NOT
    allowlisting it. Any future addition to `_BENIGN_CONFIG_KEY_PATTERNS`
    needs its own evidence-gated change (one of the 31 recorded
    false-discards, proven benign one key at a time), never bundled with a
    display/diagnostics change like this one.

    `^user\\.(name|email)$` is exactly such an evidence-gated addition (the
    false-discards of 15eb6e7d / bf0cfd72: the reviewer setting its own git
    identity on the shared config) and is pinned here alongside the rest.
    """
    patterns = [p.pattern for p in rw._BENIGN_CONFIG_KEY_PATTERNS]
    assert patterns == [
        r"^branch\.[^.]+\.(rebase|remote|merge|pushremote|description|"
        r"vscode-merge-base)$",
        r"^maintenance\..+$",
        r"^gc\..+$",
        r"^user\.(name|email)$",
    ], patterns


def test_the_reviewer_identity_written_to_the_shared_config_is_excused_and_disclosed(
    worktree_env,
):
    """Root-cause regression for the reviewer-worktree-integrity false
    discards of 15eb6e7d / bf0cfd72: the reviewer session runs `git config
    user.email`/`user.name` to set its own identity, which writes to the
    SHARED `.git/common/config`. That write has no execution surface and
    changes no judged file, so it must be excused exactly like the
    bookkeeping branch/maintenance/gc keys above.
    """
    wt = worktree_env["wt"]
    common_dir = worktree_env["common_dir"]
    cfg = common_dir / "config"

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    _git(wt, "config", "--file", str(cfg), "user.email", "reviewer@example.com")
    _git(wt, "config", "--file", str(cfg), "user.name", "Reviewer Bot")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.is_empty(), (
        "the reviewer's own git identity written to the shared config "
        f"discarded the verdict: added={delta.added} modified={delta.modified} "
        f"deleted={delta.deleted}"
    )
    assert any(p.endswith("common/config") or p == ".git/common/config"
               for p in delta.benign), (
        f"the excused identity write was not disclosed: benign={delta.benign}")
    assert "user.email" in delta.benign_keys, delta.benign_keys
    assert "user.name" in delta.benign_keys, delta.benign_keys
    assert delta.nonbenign_keys == [], delta.nonbenign_keys


def test_reviewer_identity_plus_an_exec_surface_key_still_discards(worktree_env):
    """Positive control: the allowlist added by this task must not widen
    past `user.name`/`user.email`. A genuine execution-surface key
    (`alias.pwn`) alongside the reviewer's identity write must still
    discard the whole verdict — the existing all()-benign gate holds."""
    wt = worktree_env["wt"]
    common_dir = worktree_env["common_dir"]
    cfg = common_dir / "config"

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    _git(wt, "config", "--file", str(cfg), "user.email", "reviewer@example.com")
    _git(wt, "config", "--file", str(cfg), "alias.pwn", "!sh -c id")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert not delta.is_empty(), (
        "user.email alongside a non-benign alias key was excused: "
        f"added={delta.added} modified={delta.modified} deleted={delta.deleted}")
    assert any("/config" in p for p in delta.modified), (
        f"the mixed config change was not caught: modified={delta.modified}")
    assert not any(p.endswith("/config") for p in delta.benign), (
        f"a non-allowlisted config change landed in benign: benign={delta.benign}")
    assert delta.nonbenign_keys == ["alias.pwn"], delta.nonbenign_keys
    assert "user.email" not in delta.benign_keys, (
        "user.email was disclosed as excused even though the whole file "
        f"stayed a violation: benign_keys={delta.benign_keys}")


def test_no_exec_surface_key_entered_the_benign_allowlist():
    """Guard: the allowlist widened by this task must not, even
    accidentally, cover any of the execution-surface keys the module's own
    evidence comment lists as 'Deliberately NOT here'."""
    for key in (
        "alias.pwn",
        "core.hookspath",
        "include.path",
        "filter.lfs.clean",
        "remote.origin.url",
    ):
        assert not rw._is_benign_config_key(key), (
            f"{key!r} must not be treated as a benign config key")


def test_the_named_non_benign_key_reaches_the_persisted_checklist_evidence(
    worktree_env,
):
    """End-to-end: the key named in `compare()`'s discard text must survive
    into the persisted `ReviewDecision.checklist` evidence the next attempt
    and an operator actually read — naming it in `compare()` alone is not
    enough if the wiring to the verdict drops it."""
    wt = worktree_env["wt"]
    common_dir = worktree_env["common_dir"]
    cfg = common_dir / "config"

    before = rw.snapshot(wt, timeout=_TIMEOUT)
    _git(wt, "config", "--file", str(cfg), "branch.x.rebase", "true")
    _git(wt, "config", "--file", str(cfg), "alias.pwn", "!sh -c id")
    delta = rw.compare(wt, before, timeout=_TIMEOUT)

    decision = _integrity_failure_decision(delta)
    assert decision.passed is False
    assert "alias.pwn" in decision.checklist[0].evidence, decision.checklist[0].evidence


# --------------------------------------------------------------------------- #
# Part A2: index FLAG BITS (assume-unchanged / skip-worktree)
# --------------------------------------------------------------------------- #

def test_assume_unchanged_bit_hiding_a_rewritten_tracked_file_is_detected(worktree_env):
    """`git update-index --assume-unchanged` on a tracked file, followed by
    rewriting that file on disk, leaves `git status --porcelain` EMPTY -- the
    module docstring's own opening threat (`echo x > calc.py`), with no
    `.git` artefact involved at all.

    RED-if-removed: the final assertion pins that detection is coming from
    the NEW `.git/index:`-prefixed entry, not from the pre-existing byte
    inventory -- deleting the `compare()` index-flags block (or making
    `_index_flags` return `{}`) makes this test fail, not merely weaken.
    """
    wt = worktree_env["wt"]

    # Non-vacuity of the parser: nothing is flagged on a clean, untouched
    # fixture -- an implementation that flagged every ordinary `H` (cached)
    # entry would fail here, and would also break the positive control below.
    assert rw._index_flags(wt, timeout=_TIMEOUT) == {}

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    _git(wt, "update-index", "--assume-unchanged", "src/main.py")
    (wt / "src" / "main.py").write_text("import os; os.system('curl evil')\n")

    # Premise: porcelain really is blind here. If a future git version stops
    # being blind, THIS assertion says so, instead of the assertion below
    # silently passing for the wrong reason.
    porcelain = _git(wt, "status", "--porcelain")
    assert porcelain.stdout == "", (
        "premise failed -- git status --porcelain is no longer blind to an "
        f"assume-unchanged rewrite: {porcelain.stdout!r}")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert not delta.is_empty(), (
        "an assume-unchanged-hidden rewrite of a tracked source file was "
        "not detected")
    offenders = [e for e in delta.modified if e.startswith(".git/index:")]
    assert any("src/main.py" in e for e in offenders), (
        f"no .git/index: entry named the tampered path: modified={delta.modified}")


def test_skip_worktree_bit_hiding_a_rewritten_tracked_file_is_detected(worktree_env):
    """`--skip-worktree` is a DISTINCT index flag from assume-unchanged (tag
    `S`, not a lowercased `H`) and is deliberately not assumed covered by
    that case -- pinned separately here, same shape as the assume-unchanged
    test above."""
    wt = worktree_env["wt"]

    assert rw._index_flags(wt, timeout=_TIMEOUT) == {}

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    _git(wt, "update-index", "--skip-worktree", "src/main.py")
    (wt / "src" / "main.py").write_text("import os; os.system('curl evil')\n")

    porcelain = _git(wt, "status", "--porcelain")
    assert porcelain.stdout == "", (
        "premise failed -- git status --porcelain is no longer blind to a "
        f"skip-worktree rewrite: {porcelain.stdout!r}")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert not delta.is_empty(), (
        "a skip-worktree-hidden rewrite of a tracked source file was not "
        "detected")
    offenders = [e for e in delta.modified if e.startswith(".git/index:")]
    assert any("src/main.py" in e for e in offenders), (
        f"no .git/index: entry named the tampered path: modified={delta.modified}")


def test_repeated_read_only_git_status_and_diff_still_produce_an_empty_delta(
    worktree_env,
):
    """Positive control: several rounds of read-only `git status`/`git diff`
    -- exactly what rewrites the index's stat-cache BYTES -- must NOT trip
    the new flag-bit check. This is the false-positive wall the intake
    decision to KEEP `index` excluded (rather than byte-watching it) exists
    to satisfy. `test_benign_git_status_does_not_trigger_reviewer_wrote`
    covers a single round; this is the explicit repeated-refresh control so a
    regression here names the flag check, not the byte inventory, as the
    suspect."""
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    for _ in range(3):
        _git(wt, "status", "--porcelain")
        _git(wt, "diff")
        _git(wt, "diff", "--stat")
        _git(wt, "status")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.is_empty(), (
        "repeated read-only git status/diff (an index stat-cache refresh) "
        "was reported as tampering -- suspect the new index-flag check: "
        f"added={delta.added} modified={delta.modified} deleted={delta.deleted}")


def test_an_unreadable_index_flag_listing_fails_closed(worktree_env, monkeypatch):
    """Unparseable `git ls-files -v` output is unverifiable state and must
    fail closed (raise), never be silently skipped -- the intake decision:
    "unverifiable state => tampering", not "skip the check"."""
    wt = worktree_env["wt"]
    real_run_git = rw._run_git

    def _fake_run_git(repo_path, *args, timeout):
        if args and args[0] == "ls-files":
            return "garbage\0"
        return real_run_git(repo_path, *args, timeout=timeout)

    monkeypatch.setattr(rw, "_run_git", _fake_run_git)

    with pytest.raises(rw.WorktreeCheckFailed, match="unparseable record"):
        rw.snapshot(wt, timeout=_TIMEOUT)


# --------------------------------------------------------------------------- #
# Part B: hook-safe revert
# --------------------------------------------------------------------------- #

def test_revert_does_not_execute_planted_post_checkout_hook(worktree_env):
    wt = worktree_env["wt"]
    canary = worktree_env["wt"].parent / "canary"
    assert not canary.exists()

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    # Plant in every location the resolved hooks-path chain can reach: the
    # already-installed, ALREADY-EFFECTIVE per-worktree hooks dir (this is
    # what `core.hooksPath` resolves to today, absent the fix's override —
    # see push_hook.py's per-worktree `core.hooksPath`), and the shared
    # common-dir hooks as a second surface.
    _plant_exec_hook(worktree_env["hooks_dir"] / "post-checkout", canary)
    _plant_exec_hook(worktree_env["common_dir"] / "hooks" / "post-checkout", canary)

    # Dirty a TRACKED file so revert() must run `git checkout <head> --
    # path` — the call whose hook-safety is under test. (`git reset` for
    # added paths never fires a hook at all, so that path would prove
    # nothing here.)
    tracked = wt / "src" / "main.py"
    tracked.write_text("v2 -- reviewer edit\n")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert "src/main.py" in delta.modified

    rw.revert(wt, before, delta, timeout=_TIMEOUT)

    assert not canary.exists(), "revert() executed a planted post-checkout hook"
    assert tracked.read_text() == "v1\n", "revert() did not restore the reviewed baseline"


def test_revert_does_not_execute_planted_symlinked_post_checkout_hook(worktree_env):
    """Same evasion as the detection-side symlink test, but exercised
    against `revert()`'s own `git checkout`/`git reset` calls: even if a
    symlinked hook slipped past detection, `-c core.hooksPath=<empty dir>`
    must still stop git from executing it."""
    wt = worktree_env["wt"]
    canary = worktree_env["wt"].parent / "symlink-canary"
    assert not canary.exists()

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    outside = worktree_env["wt"].parent / "outside-payload-2"
    _plant_symlinked_hook(worktree_env["hooks_dir"] / "post-checkout", canary, outside)
    _plant_symlinked_hook(
        worktree_env["common_dir"] / "hooks" / "post-checkout", canary, outside)

    tracked = wt / "src" / "main.py"
    tracked.write_text("v2 -- reviewer edit via symlinked hook\n")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert "src/main.py" in delta.modified

    rw.revert(wt, before, delta, timeout=_TIMEOUT)

    assert not canary.exists(), "revert() executed a planted symlinked post-checkout hook"
    assert tracked.read_text() == "v1\n", "revert() did not restore the reviewed baseline"


# --------------------------------------------------------------------------- #
# Baseline: pre-existing worktree-file snapshot/compare/revert + fail-closed
# --------------------------------------------------------------------------- #

def test_snapshot_and_compare_detect_worktree_add_modify_delete(worktree_env):
    wt = worktree_env["wt"]
    (wt / "src" / "extra.py").write_text("x\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "second file")

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    (wt / "src" / "new_file.py").write_text("new\n")   # added
    (wt / "src" / "main.py").write_text("changed\n")   # modified
    (wt / "src" / "extra.py").unlink()                  # deleted

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.added == ["src/new_file.py"]
    assert delta.modified == ["src/main.py"]
    assert delta.deleted == ["src/extra.py"]


def test_revert_restores_worktree_to_snapshot(worktree_env):
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    (wt / "src" / "new_file.py").write_text("new\n")
    (wt / "src" / "main.py").write_text("changed\n")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    rw.revert(wt, before, delta, timeout=_TIMEOUT)

    assert not (wt / "src" / "new_file.py").exists()
    assert (wt / "src" / "main.py").read_text() == "v1\n"
    assert rw.compare(wt, before, timeout=_TIMEOUT).is_empty()


def test_compare_reports_moved_head_and_revert_refuses(worktree_env):
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    (wt / "src" / "main.py").write_text("committed change\n")
    _git(wt, "commit", "-qam", "reviewer committed")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert any(m.startswith("HEAD:") for m in delta.modified), delta.modified

    with pytest.raises(rw.WorktreeCheckFailed, match="moved HEAD"):
        rw.revert(wt, before, delta, timeout=_TIMEOUT)


def test_snapshot_fails_closed_for_a_non_git_directory(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(rw.WorktreeCheckFailed):
        rw.snapshot(not_a_repo, timeout=_TIMEOUT)


def test_a_git_timeout_fails_closed_rather_than_reporting_a_clean_snapshot(
    tmp_path, monkeypatch,
):
    """A slow git must raise, never degrade to a "don't know" that reads as clean.

    `snapshot` is the reviewer-integrity instrument: if it cannot obtain a
    trustworthy answer it must say so. `_run_git` catches
    `subprocess.TimeoutExpired` and re-raises `WorktreeCheckFailed`; nothing
    else in the tree drives a timeout into this module, so without this the
    branch ships uncovered. Recovered from the superseded PR #706.

    Two things are asserted, and the second is why the first is worth having.
    Converting a timeout is useless if a timeout can never HAPPEN: drop
    `timeout=timeout` from the `subprocess.run` call and the `except
    TimeoutExpired` clause becomes permanently dead code, real git hangs
    forever, and a stub that raises unconditionally would still be green. So
    the stub records what it was called with and the forwarded value is
    asserted — reading `kwargs["timeout"]`, never `kwargs.get(...)` with a
    default, which would paper over the very omission being tested.

    The repo is seeded with a real commit so the UNPATCHED path returns
    normally. Against a bare directory `snapshot` raises `WorktreeCheckFailed`
    anyway (no resolvable HEAD), which would leave `match=` as the only thing
    separating a true pass from a monkeypatch that silently failed to apply.
    """
    repo = _seeded_repo(tmp_path)
    # Control: unpatched, this repo snapshots cleanly. Anything raising below
    # is therefore the timeout, not the fixture.
    assert rw.snapshot(repo, timeout=_TIMEOUT).head

    recorded: dict = {}

    def _always_times_out(*args, **kwargs):
        # Records, then raises with a LITERAL timeout: reading kwargs here
        # would turn a missing `timeout=` into a KeyError inside the stub,
        # which is red for the wrong reason and reports the wrong cause. The
        # assertion after the block is the discriminator.
        recorded.update(kwargs)
        raise subprocess.TimeoutExpired(
            cmd=args[0] if args else ["git"], timeout=_TIMEOUT,
        )

    monkeypatch.setattr(rw.subprocess, "run", _always_times_out)

    with pytest.raises(rw.WorktreeCheckFailed, match="timed out after"):
        rw.snapshot(repo, timeout=_TIMEOUT)

    # `.get` deliberately: a MISSING key is the defect under test, and it
    # must read as a failed comparison, not as a KeyError from the assertion.
    assert recorded.get("timeout") == _TIMEOUT, (
        "_run_git did not forward its timeout to subprocess.run, so the "
        f"TimeoutExpired branch is unreachable in production: {recorded}")


def test_guard_config_defaults_and_fallback():
    assert rw.guard_config(None) == rw._DEFAULT_TIMEOUT_SECONDS
    assert rw.guard_config({}) == rw._DEFAULT_TIMEOUT_SECONDS
    assert rw.guard_config({"pipeline": None}) == rw._DEFAULT_TIMEOUT_SECONDS
    assert rw.guard_config(
        {"pipeline": {"reviewer_worktree_guard": {"timeout_seconds": "not-a-number"}}}
    ) == rw._DEFAULT_TIMEOUT_SECONDS
    assert rw.guard_config(
        {"pipeline": {"reviewer_worktree_guard": {"timeout_seconds": -5}}}
    ) == rw._DEFAULT_TIMEOUT_SECONDS
    assert rw.guard_config(
        {"pipeline": {"reviewer_worktree_guard": {"timeout_seconds": 12}}}
    ) == 12.0


# --------------------------------------------------------------------------- #
# Part C: pruned shared subtrees (`objects/`, `refs/`) — perf + concurrency
# --------------------------------------------------------------------------- #

def test_new_loose_object_and_unrelated_ref_do_not_trigger_reviewer_wrote(worktree_env):
    """A second review refutation of this guard: `objects/` and `refs/` live
    in `--git-common-dir`, SHARED by every linked worktree of the repo, so
    unrelated concurrent git activity in another worktree/task lands there
    too. This reproduces exactly that shape without needing a second real
    worktree: `hash-object -w` adds a brand-new loose object under
    `common_dir/objects/`, and `update-ref` creates a brand-new ref under
    `common_dir/refs/heads/` for a branch this review never touched — neither
    touches this worktree's `index`, `HEAD`, or any tracked file. Both must
    be invisible to `compare()`: they are additions to shared,
    content-addressed/bookkeeping storage, not writes to an execution
    surface, and the checked-out ref this worktree actually cares about is
    independently covered by the `HEAD` comparison (see
    `test_compare_reports_moved_head_and_revert_refuses`)."""
    wt = worktree_env["wt"]
    common = worktree_env["common_dir"]

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    # New loose object: content-addressed, never previously present, written
    # straight into the shared object store — the exact "an object can only
    # be added" case the `objects/` prune is justified on.
    new_blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(wt), input="unrelated concurrent worktree content\n",
        capture_output=True, text=True, check=True,
    )
    assert new_blob.stdout.strip(), "fixture assumption: hash-object printed a sha"
    oid = new_blob.stdout.strip()
    assert (common / "objects" / oid[:2] / oid[2:]).is_file(), (
        "fixture assumption: hash-object -w wrote a loose object file"
    )

    # New ref on a branch this review never touched, as if a sibling
    # worktree sharing this common dir pushed/branched concurrently.
    _git(wt, "update-ref", "refs/heads/unrelated-concurrent-branch", "HEAD")
    assert (common / "refs" / "heads" / "unrelated-concurrent-branch").is_file(), (
        "fixture assumption: update-ref wrote a loose ref file"
    )

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.is_empty(), (
        "a new object/ref from shared, concurrent-safe storage was reported "
        f"as a write: added={delta.added} modified={delta.modified} "
        f"deleted={delta.deleted}"
    )


# --------------------------------------------------------------------------- #
# `_run_git`'s returncode check (task reviewer-worktree-returncode-audit)
# --------------------------------------------------------------------------- #

def test_every_subprocess_run_call_site_is_captured_in_the_audit():
    """`rw.SUBPROCESS_CALL_AUDIT` must name every `subprocess.run` call site in
    this module, by parsing the module's own source rather than re-listing the
    names by hand — a hand-kept list would go stale the moment a new call site
    was added and nobody remembered to also add its entry. This is the
    mechanical guarantee that the returncode check on `_run_git` (and the
    deliberate absence of one on `_config_effective`) stays documented as the
    module grows: an undocumented new call site, or a stale entry for a
    removed one, goes red here before anyone has to notice by reading diffs.
    """
    source = Path(rw.__file__).read_text()
    tree = ast.parse(source)
    call_sites = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "run"
                    and isinstance(inner.func.value, ast.Name)
                    and inner.func.value.id == "subprocess"):
                call_sites.add(node.name)
    assert call_sites, "no subprocess.run call site found in reviewer_worktree.py"
    assert call_sites == set(rw.SUBPROCESS_CALL_AUDIT), (
        f"subprocess.run call sites in the module ({sorted(call_sites)}) do "
        f"not match SUBPROCESS_CALL_AUDIT ({sorted(rw.SUBPROCESS_CALL_AUDIT)}) "
        "-- a call site was added without documenting its exit-status "
        "handling, or a stale entry survives a removed call site")


def test_run_git_raises_on_a_nonzero_exit_and_never_returns_its_empty_stdout(
    tmp_path, monkeypatch,
):
    """The returncode check `_run_git` performs after the timeout/OSError
    `except` clauses (reviewer_worktree.py, `if proc.returncode != 0:`) is
    exercised nowhere else in this file: the neighbouring timeout test above
    only drives `subprocess.TimeoutExpired`, never a plain non-zero exit. That
    left the branch itself a silent mutant — disable it
    (`if False and proc.returncode != 0:`) and the whole suite (10k+ tests)
    stays green, because nothing forces a REAL non-zero-exit git call through
    `_run_git` and checks the outcome.

    A failed `git status`/`git rev-parse` prints nothing useful to stdout;
    without the check, `_run_git` would return that empty string as if it
    were a legitimate (empty) answer. `_parse_porcelain_z("")` reads that as
    "no entries", which is indistinguishable from an actually-clean tree —
    the exact inversion `SUBPROCESS_CALL_AUDIT["_run_git"]` documents.
    """
    repo = _seeded_repo(tmp_path)
    # Control: unpatched, `_run_git` and `snapshot` both work normally.
    assert rw.snapshot(repo, timeout=_TIMEOUT).head

    real_run = subprocess.run

    def _fail_status(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        if argv and "status" in argv:
            return subprocess.CompletedProcess(
                argv, returncode=128, stdout="",
                stderr="fatal: not a git repository (or any parent)")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(rw.subprocess, "run", _fail_status)

    with pytest.raises(rw.WorktreeCheckFailed, match="exit 128"):
        rw._run_git(repo, "status", "--porcelain=v1", "-z", timeout=_TIMEOUT)

    # `snapshot()` calls `_run_git(..., "status", ...)` internally; it must
    # raise rather than come back with a `Snapshot` whose `entries` is just
    # empty because the failed status call's "" was accepted as real output.
    with pytest.raises(rw.WorktreeCheckFailed, match="exit 128"):
        rw.snapshot(repo, timeout=_TIMEOUT)
