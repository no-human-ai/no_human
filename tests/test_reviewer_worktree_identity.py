"""What counts as "the reviewer changed something" in the `.git` subtree.

THE INCIDENT (2026-08-25 onward). Completed review verdicts were repeatedly
discarded, each reported as `the reviewer wrote to the worktree it was judging
(0 added, 1 modified, 0 deleted)` with a `.git/common/` path — git's own
bookkeeping, never a source file — and at least one task was lost to it
without the coder ever seeing a verdict it had already earned.

The count is deliberately NOT given here. An earlier version of this docstring
named three tasks and a commit message named seven events across six different
tasks; both were true when written and neither is checkable from the shipped
tree, so a reader could only find them contradicting each other. What matters
and is reproducible is the MECHANISM below. Whether each of those
writes changed content is NOT established — the `.git` inventory stores
hashes, not bytes, and `revert()` never touches `.git`-subtree paths, so
the before/after content of those incident writes was never captured
anywhere. What IS established by reproduction is that
an identical-bytes rewrite is SUFFICIENT to produce exactly that report,
because `st_mtime_ns` was part of the entry identity tuple.

Every task worktree is a linked worktree whose `--git-common-dir` IS the
primary checkout's `.git`, so those files are shared with every other writer
in the repo. Measurement after the mtime fix found THREE more paths tripping
the same guard from ordinary primary-checkout work — `COMMIT_EDITMSG` on a
commit, `HEAD` on a checkout, `info/refs` on a gc.

The shipped fix is a SKIP LIST of exactly TWO names (`COMMIT_EDITMSG`,
`info/refs`), SCOPED to the shared `common` dir where the concurrent writer
is, plus ONE content-shape rule: `common/HEAD` stays inventoried and
`compare()` excuses only a symref -> symref rewrite of it (an ordinary
branch switch); a raw sha or garbage is a violation. It is NOT an inversion.
An earlier revision did invert this into an allowlist — watch only what git
can execute — and that opened four execution bypasses; the decisive one
planted a hook, ran `git checkout`, and the hook EXECUTED while `compare()`
reported an empty delta. An allowlist cannot be made safe by lengthening it:
the safe set is the one nobody has thought of yet. Everything the inventory
WALKS that is not excused by the two names or the one shape rule stays
watched (the walk itself prunes `objects/`, `refs/`, `worktrees/`, and the
volatile exact names plus `logs/` are excused after the walk — see
`_SKIPPED_GIT_DIR_PREFIXES` and `_is_volatile_git_path`), and the tests
below pin that.

The property these tests pin:

    An entry's identity is exactly what determines what git will DO with it —
    its content and its mode. A write that changes neither is not a change.

Both directions matter and both are tested here. Only asserting the
false-positive half would be satisfied by a `compare()` that never reports
anything, which is the fail-open this guard exists to prevent.
"""

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from no_human.core import reviewer_worktree as rw

_TIMEOUT = 30.0


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True,
    )


@pytest.fixture
def linked_worktree(tmp_path):
    """A LINKED worktree, so `--absolute-git-dir` and `--git-common-dir`
    differ and the inventory walks both — the production shape, and the one
    the incident occurred in."""
    up = tmp_path / "up"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    (up / "f.txt").write_text("v1\n")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "base")
    wt = tmp_path / "wt"
    _git(up, "worktree", "add", "-q", str(wt), "-b", "feature")
    admin = rw._resolve_git_root(wt, "--absolute-git-dir", timeout=_TIMEOUT)
    common = rw._resolve_git_root(wt, "--git-common-dir", timeout=_TIMEOUT)
    assert admin != common, "fixture must produce a LINKED worktree"
    return wt, common


def _same_length(*scripts: bytes) -> tuple[bytes, ...]:
    """Pad each shell script with trailing spaces to a common byte length.

    Trailing whitespace before the newline is inert in `sh`, so the scripts
    keep their behaviour while becoming indistinguishable by size alone.
    """
    width = max(len(s) for s in scripts)
    out = []
    for s in scripts:
        assert s.endswith(b"\n")
        out.append(s[:-1] + b" " * (width - len(s)) + b"\n")
    return tuple(out)


def _delta_after(wt, mutate):
    before = rw.snapshot(wt, timeout=_TIMEOUT)
    # Real filesystems have coarse mtime granularity; without this a same-second
    # rewrite could match the old mtime by luck and pass for the wrong reason.
    time.sleep(1.1)
    mutate()
    return rw.compare(wt, before, timeout=_TIMEOUT)


# --------------------------------------------------------- the regression ---


def test_a_byte_identical_rewrite_of_a_git_file_is_not_a_change(linked_worktree):
    """The incident, reproduced. `config` is rewritten with the exact bytes it
    already held — the mtime moves, nothing git reads changes."""
    wt, common = linked_worktree
    cfg = common / "config"
    original = cfg.read_bytes()

    delta = _delta_after(wt, lambda: cfg.write_bytes(original))

    assert cfg.read_bytes() == original, "the fixture must not alter content"
    assert delta.is_empty(), delta


def test_touching_a_git_file_without_writing_it_is_not_a_change(linked_worktree):
    """The weaker sibling: mtime moves with no write at all."""
    wt, common = linked_worktree
    cfg = common / "config"

    delta = _delta_after(wt, lambda: os.utime(cfg, None))

    assert delta.is_empty(), delta


# ------------------------------------- the other direction: still detected ---


def test_a_content_change_with_an_unchanged_mode_is_still_detected(linked_worktree):
    wt, common = linked_worktree
    cfg = common / "config"
    mode_before = cfg.stat().st_mode

    delta = _delta_after(
        wt, lambda: cfg.write_text(cfg.read_text() + "\n[alias]\n\tx = !sh -c 'id'\n"))

    assert cfg.stat().st_mode == mode_before, "this case must vary ONLY content"
    assert any(p.startswith(".git/common/config") for p in delta.modified), delta


def test_a_SAME_SIZE_content_change_is_still_detected(linked_worktree):
    """The case that separates a real content check from a cheap proxy.

    Appending to a file changes its SIZE, so a size field alone catches it and
    a test that only appends cannot tell whether the sha256 is load-bearing —
    verified: blanking `_content_hash` out of the entry tuple left the
    append-based test green. Overwriting a hook with a payload of exactly the
    same length is the shape that gets past everything but the hash, and it is
    the shape an attacker would choose.
    """
    wt, common = linked_worktree
    hook = common / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    # Padded to equal length BY CONSTRUCTION. Hand-counting two literals is
    # how this test first failed: 37 vs 38 bytes.
    benign, evil = _same_length(b"#!/bin/sh\n: # harmless\n",
                                b"#!/bin/sh\ntouch /tmp/pwned\n")
    assert len(benign) == len(evil) and benign != evil
    hook.write_bytes(benign)
    hook.chmod(0o755)
    mode_before = hook.stat().st_mode

    delta = _delta_after(wt, lambda: hook.write_bytes(evil))

    assert hook.stat().st_size == len(benign), "this case must vary ONLY content"
    assert hook.stat().st_mode == mode_before, "this case must vary ONLY content"
    assert ".git/common/hooks/post-checkout" in delta.modified, delta


def test_a_mode_change_with_identical_content_is_still_detected(linked_worktree):
    """A mode change on a byte-identical file — the case the mode field exists
    for, and the reason mode cannot follow mtime out of the tuple.

    The direction is whatever the fixture is NOT: `git init` ships its sample
    hooks 0o755, so this flips them to 0o644, i.e. chmod MINUS x. Calling it
    "chmod +x" would describe an operation the test does not perform. And git
    never executes `*.sample` under any mode — the file is a stand-in chosen
    because it pre-exists the baseline, which is what makes this a MODE change
    rather than an addition."""
    wt, common = linked_worktree
    sample = common / "hooks" / "pre-commit.sample"
    sample.parent.mkdir(parents=True, exist_ok=True)
    if not sample.exists():
        sample.write_text("#!/bin/sh\nexit 0\n")
    # The file must pre-exist the baseline (else this is an ADDITION), and the
    # new mode must actually differ from the old one. `git init` ships its
    # sample hooks 0o755 already, so a hardcoded chmod(0o755) is a mutation
    # that mutates nothing and the test passes for the wrong reason.
    mode_before = stat.S_IMODE(sample.stat().st_mode)
    mode_after = mode_before ^ 0o111
    assert mode_after != mode_before
    content_before = sample.read_bytes()

    delta = _delta_after(wt, lambda: sample.chmod(mode_after))

    assert stat.S_IMODE(sample.stat().st_mode) == mode_after, "chmod did not take"
    assert sample.read_bytes() == content_before, "this case must vary ONLY mode"
    assert ".git/common/hooks/pre-commit.sample" in delta.modified, delta


def test_a_newly_planted_hook_is_still_detected(linked_worktree):
    wt, common = linked_worktree
    hook = common / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)

    def plant():
        hook.write_text("#!/bin/sh\ntouch /tmp/pwned\n")
        hook.chmod(0o755)

    delta = _delta_after(wt, plant)

    assert ".git/common/hooks/post-checkout" in delta.added, delta


def test_retargeting_a_symlinked_hook_is_still_detected(linked_worktree):
    """A symlinked hook is executed exactly like a regular file at that path,
    and `_symlink_entry` never dereferences — so the TARGET STRING is the
    content. Repointing it with the mode untouched must still be a delta."""
    wt, common = linked_worktree
    hooks = common / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    # SAME-LENGTH names, on purpose. With `benign.sh`/`evil.sh` the two link
    # targets differ in LENGTH, so `len(target)` alone killed the mutant and
    # the sha256 was never load-bearing — blanking the digest left this test
    # green while a same-length retarget went undetected. An attacker picks
    # both names, so equal length is free.
    (hooks / "aaaaaa.sh").write_text("#!/bin/sh\nexit 0\n")
    (hooks / "bbbbbb.sh").write_text("#!/bin/sh\ntouch /tmp/pwned\n")
    link = hooks / "post-checkout"
    link.symlink_to(hooks / "aaaaaa.sh")

    def repoint():
        link.unlink()
        link.symlink_to(hooks / "bbbbbb.sh")

    delta = _delta_after(wt, repoint)

    assert ".git/common/hooks/post-checkout" in delta.modified, delta


def test_deleting_a_git_file_is_still_detected(linked_worktree):
    wt, common = linked_worktree
    hook = common / "hooks" / "victim"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    rw.snapshot(wt, timeout=_TIMEOUT)  # ensure it exists before the baseline

    delta = _delta_after(wt, hook.unlink)

    assert ".git/common/hooks/victim" in delta.deleted, delta


# ---------------------------------------------------------------------------
# THE CLASS: who wrote it, not which filename.
# ---------------------------------------------------------------------------


def test_the_three_measured_primary_checkout_writers_do_not_discard_a_verdict(
    linked_worktree,
):
    """The defect one level up from the mtime one.

    Every task worktree is a LINKED worktree whose `--git-common-dir` IS the
    primary checkout's `.git`. Removing `st_mtime_ns` stopped `config` from
    false-positiving on identical-bytes rewrites; measurement then found
    THREE more paths written by ordinary work in the primary checkout:

        primary `git commit`       -> common/COMMIT_EDITMSG  (path-excused)
        primary `git gc`           -> common/info/refs       (path-excused)
        primary `git checkout <b>` -> common/HEAD  (excused by CONTENT SHAPE:
                                      symref -> symref only, see `compare`)

    This test exercises exactly those three commands and asserts no more
    than that: OTHER ordinary primary-checkout actions that CHANGE shared
    content — `git config user.email`, `git remote add`, a leftover
    `config.lock` — still discard a verdict, by design (fail-closed on
    unattributable content changes; an earlier name claimed 'ordinary work
    never discards', which a review falsified with `git config`). The
    reverted-allowlist lesson stands separately: what is EXCUSED must be a
    closed, measured list plus one content-shape rule — everything else
    under `.git` stays watched, because data there (config, attributes,
    hooksPath targets) can direct execution.
    """
    wt, common = linked_worktree
    up = common.parent          # `<tmp>/up/.git` -> the primary checkout

    for label, action in (
        ("commit", lambda: (_git(up, "commit", "--allow-empty", "-qm", "concurrent"),)),
        ("checkout", lambda: (_git(up, "checkout", "-q", "-b", "another-branch"),)),
        ("gc", lambda: (_git(up, "gc", "--quiet"),)),
    ):
        before = rw.snapshot(wt, timeout=_TIMEOUT)
        action()
        delta = rw.compare(wt, before, timeout=_TIMEOUT)
        assert delta.is_empty(), (
            f"{label} in the PRIMARY checkout discarded the verdict: {delta}")


def test_a_non_symref_write_to_the_shared_HEAD_discards_the_verdict(
    linked_worktree,
):
    """A round-10 review measured that a path-level skip of `common/HEAD`
    hid a write repointing the PRIMARY checkout — with `refs/` pruned, the
    hijack's last visible step. The excuse is therefore CONTENT SHAPE, not
    the path: only symref -> symref (an ordinary branch switch) is clean.
    A raw sha (detach) or garbage stays a violation. The stated residual:
    a symref -> symref rewrite by the reviewer itself is indistinguishable
    from the operator switching branches and is NOT detected — that case is
    asserted clean below so the residual is pinned as a choice, not drift.
    """
    wt, common = linked_worktree
    head_file = common / "HEAD"
    original = head_file.read_text()
    assert original.startswith("ref: refs/heads/"), original

    # Raw sha (detached) — a violation.
    before = rw.snapshot(wt, timeout=_TIMEOUT)
    head_file.write_text("0" * 40 + "\n")
    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    head_file.write_text(original)
    assert ".git/common/HEAD" in delta.modified, (
        "a detach write to the shared HEAD went undetected", delta)

    # Garbage — a violation.
    before = rw.snapshot(wt, timeout=_TIMEOUT)
    head_file.write_text("gitdir: /somewhere/else\n")
    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    head_file.write_text(original)
    assert ".git/common/HEAD" in delta.modified, (
        "a garbage write to the shared HEAD went undetected", delta)

    # Symref -> symref — clean BY DESIGN (the residual, pinned).
    before = rw.snapshot(wt, timeout=_TIMEOUT)
    head_file.write_text("ref: refs/heads/some-existing-branch\n")
    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    head_file.write_text(original)
    assert delta.is_empty(), (
        "a symref->symref HEAD rewrite is the excused ordinary-branch-switch "
        "shape; reporting it would reintroduce the measured false-positive "
        "class", delta)


# ---------------------------------------------------------------------------
# EXECUTION BYPASSES AN EARLIER REVISION OF THIS COMMIT OPENED.
#
# That revision inverted the rule: instead of watching everything under `.git`
# and excluding what is provably inert, it watched only an ALLOWLIST of
# "execution surfaces" — `hooks/**`, `*config*`, `info/attributes`. An
# independent review broke it by EXECUTION in the three ways below, each of
# which main's denylist caught. They are regression tests, not hypotheticals:
# every one was demonstrated with a hook that actually ran or an alias that
# actually printed.
#
# The property, stated so the SHAPE is pinned and not these three instances:
#
#     The set of paths under `.git` that can get code executed is NOT CLOSED
#     under any finite list of names — a WATCHED file can NAME an UNWATCHED
#     one (`include.path`). So the guard must watch everything and exclude
#     only what is provably inert. Unknown must mean WATCHED.
# ---------------------------------------------------------------------------


def test_a_hook_dir_more_than_one_level_deep_is_still_watched(linked_worktree):
    """`core.hooksPath` can point anywhere. The allowlist matched the FIRST
    path component, so `<admin>/a/b` was unwatched and a hook planted there
    executed with the verdict kept."""
    wt, common = linked_worktree
    admin = rw._resolve_git_root(wt, "--absolute-git-dir", timeout=_TIMEOUT)
    deep = admin / "a" / "b"
    deep.mkdir(parents=True)
    _git(wt, "config", "core.hooksPath", str(deep))
    hook = deep / "post-checkout"

    def plant():
        hook.write_text("#!/bin/sh\ntouch /tmp/nh-test-pwned\n")
        hook.chmod(0o755)

    delta = _delta_after(wt, plant)

    assert not delta.is_empty(), delta
    assert any("a/b/post-checkout" in p for p in delta.added + delta.modified), delta


def test_rewriting_a_file_the_config_INCLUDES_is_still_watched(linked_worktree):
    """The case that settles the design. `config` is watched, but it can
    `include.path` another file — and rewriting THAT file defines aliases git
    runs. A watched file naming an unwatched one is why an allowlist of names
    cannot be made safe by lengthening it."""
    wt, common = linked_worktree
    inc = common / "extra.inc"
    inc.write_text("[alias]\n\tok = !true\n")
    _git(wt, "config", "include.path", str(inc))

    delta = _delta_after(
        wt, lambda: inc.write_text("[alias]\n\tpwn = !echo EXECUTED\n"))

    assert not delta.is_empty(), delta
    assert any("extra.inc" in p for p in delta.modified + delta.added), delta


def test_rewriting_this_worktrees_own_HEAD_is_still_watched(linked_worktree):
    """A `HEAD` rewrite that PRESERVES the resolved sha. `git rev-parse HEAD`
    is byte-identical afterwards, so the dedicated HEAD comparison in
    `compare()` sees nothing — while the worktree is now detached and the
    branch the pipeline pushes stops advancing. The SHARED `common/HEAD` is
    inventoried too but excused only for symref -> symref rewrites; this
    worktree's OWN copy has no excuse shape at all and must stay watched."""
    wt, common = linked_worktree
    admin = rw._resolve_git_root(wt, "--absolute-git-dir", timeout=_TIMEOUT)
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    delta = _delta_after(wt, lambda: (admin / "HEAD").write_text(sha + "\n"))

    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == sha, (
        "the fixture must leave the RESOLVED sha unchanged, else this passes "
        "via the HEAD comparison rather than the inventory")
    assert not delta.is_empty(), delta
    assert any(p.endswith("admin/HEAD") for p in delta.modified), delta


# --------------------------------------------------------------------------- #
# The failure text the NEXT ATTEMPT reads.                                     #
#                                                                              #
# It carried counts only — "0 added, 1 modified, 0 deleted" — and nothing      #
# asserted on it at all, so every diagnosis had to be made by querying         #
# `reviewer_wrote` events out of the database to learn the "modification" was  #
# `.git/common/config`, git's own bookkeeping, and not a source file. A        #
# message that cannot be acted on is a defect in the guard, not a cosmetic     #
# detail. NO TASK COUNT IS GIVEN HERE — see the module docstring for why a     #
# census that is not checkable from the shipped tree does not belong in it.    #
# --------------------------------------------------------------------------- #

from no_human.core.orchestrator import (  # noqa: E402
    _INTEGRITY_PATHS_SHOWN,
    _integrity_failure_decision,
    _integrity_failure_detail,
)


class _Delta:
    def __init__(self, added=(), modified=(), deleted=()):
        self.added, self.modified, self.deleted = (
            list(added), list(modified), list(deleted))


def test_the_integrity_failure_names_the_path_it_objected_to():
    """The exact production case: one git bookkeeping file, and the reader
    must be able to SEE that is what it was."""
    detail = _integrity_failure_detail(_Delta(modified=[".git/common/config"]))
    assert ".git/common/config" in detail, detail
    assert "0 added, 1 modified, 0 deleted" in detail, detail


def test_the_integrity_failure_marks_which_bucket_each_path_came_from():
    detail = _integrity_failure_detail(
        _Delta(added=["a.py"], modified=["b.py"], deleted=["c.py"]))
    assert "+a.py" in detail and "~b.py" in detail and "-c.py" in detail, detail


def test_a_large_delta_is_truncated_but_never_misreported():
    """Truncation must not be mistakable for the whole list: the COUNTS stay
    exact and the overflow is stated."""
    n = _INTEGRITY_PATHS_SHOWN + 4
    detail = _integrity_failure_detail(
        _Delta(modified=[f"f{i}.py" for i in range(n)]))
    assert f"{n} modified" in detail, detail
    assert "and 4 more" in detail, detail
    assert f"f{_INTEGRITY_PATHS_SHOWN}.py" not in detail, (
        "a path beyond the cap was listed", detail)


def test_the_detail_still_reports_the_revert_and_the_discard():
    """Positive control: the message must not lose what it always said —
    that the tree was restored and the verdict thrown away."""
    detail = _integrity_failure_detail(_Delta(modified=["x"]))
    assert "reverted to the reviewed baseline" in detail, detail
    assert "verdict discarded" in detail, detail


def test_the_detail_never_claims_a_git_path_was_reverted():
    """`revert` deliberately skips `.git`-subtree paths (hash-only inventory,
    nothing to restore from), so persisted operator-read text must not say
    "reverted" about a delta that contains one — every path in the incident
    data was exactly that class, and a round-10 review found the old tail
    welding "reverted to the reviewed baseline" onto them."""
    detail = _integrity_failure_detail(_Delta(modified=[".git/common/config"]))
    assert "flagged, not reverted" in detail, detail
    assert "the verdict is discarded" in detail, detail
    assert "reverted to the reviewed baseline and the verdict discarded" \
        not in detail, detail
    # Mixed delta: the honest tail must win whenever ANY .git path is present.
    mixed = _integrity_failure_detail(
        _Delta(modified=["x.py", ".git/common/config"]))
    assert "flagged, not reverted" in mixed, mixed


# --------------------------------------------------------------------------- #
# The three volatile-path skips are scoped to "common". Neither direction was  #
# pinned: a review measured the unscoped version as a detection REGRESSION     #
# against main for `admin/COMMIT_EDITMSG` and `admin/info/refs`, and measured  #
# that scoping them costs nothing. Both halves are asserted here, because a    #
# skip is only safe while it stays where the concurrent writer is.             #
# --------------------------------------------------------------------------- #

def test_a_write_to_the_ADMIN_copy_of_a_volatile_name_is_still_detected(
    linked_worktree,
):
    """The admin dir is THIS worktree's own git dir. No other process writes
    it, so a write there is the reviewer's and must be seen — even for a name
    that is legitimately noisy in the shared dir."""
    wt, _ = linked_worktree
    admin = rw._resolve_git_root(wt, "--absolute-git-dir", timeout=_TIMEOUT)

    def plant():
        (admin / "COMMIT_EDITMSG").write_text("reviewer was here\n")
        (admin / "info").mkdir(parents=True, exist_ok=True)
        (admin / "info" / "refs").write_text("deadbeef\trefs/heads/x\n")

    delta = _delta_after(wt, plant)
    touched = set(delta.added) | set(delta.modified)
    assert ".git/admin/COMMIT_EDITMSG" in touched, delta
    assert ".git/admin/info/refs" in touched, delta


def test_the_same_names_under_COMMON_are_still_excused(linked_worktree):
    """The other half. Without this the scoping above could be 'fixed' by
    watching everything again, which reinstates the false positives that
    discarded completed verdicts in the first place."""
    wt, common = linked_worktree

    def churn():
        (common / "COMMIT_EDITMSG").write_text("primary checkout commit msg\n")
        (common / "info").mkdir(parents=True, exist_ok=True)
        (common / "info" / "refs").write_text("cafebabe\trefs/heads/main\n")

    delta = _delta_after(wt, churn)
    touched = set(delta.added) | set(delta.modified)
    assert ".git/common/COMMIT_EDITMSG" not in touched, delta
    assert ".git/common/info/refs" not in touched, delta


def test_a_symlink_retargeted_to_a_SAME_LENGTH_path_is_detected_without_mtime(
    linked_worktree,
):
    """Pins `_symlink_entry`'s mtime removal, which nothing covered.

    `_stat_entry` losing mtime is pinned; the symlink half was not, so
    re-adding `st_mtime_ns` there left the suite green. This retargets a
    symlink to a target of IDENTICAL length, so neither size nor mtime can
    carry the detection — only the hashed link target can.
    """
    wt, common = linked_worktree
    hooks = common / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    a, b = hooks / "aaaa", hooks / "bbbb"
    a.write_text("#!/bin/sh\ntrue\n")
    b.write_text("#!/bin/sh\nfalse\n")
    link = hooks / "post-checkout"
    link.symlink_to(a)

    def retarget():
        link.unlink()
        link.symlink_to(b)

    delta = _delta_after(wt, retarget)
    assert len(str(a)) == len(str(b)), "fixture must keep the targets same-length"
    assert ".git/common/hooks/post-checkout" in delta.modified, delta


def test_rewriting_the_linked_worktrees_git_POINTER_FILE_is_detected(
    linked_worktree,
):
    """Pins the `pointer` entry, which had no test in either file.

    A linked worktree's `.git` is a text file (`gitdir: <admin>/...`) that
    lives in the WORKTREE, so neither root walk reaches it — yet rewriting it
    repoints the entire admin dir. The commit message argues this entry is
    what makes the surviving `_git_dir_inventory` the correct one; that
    argument rested on untested code until now.
    """
    wt, _ = linked_worktree
    pointer = wt / ".git"
    assert pointer.is_file(), "fixture must be a LINKED worktree"
    # The pointer IS inventoried (that is the mechanism under test).
    before = rw.snapshot(wt, timeout=_TIMEOUT)
    assert any(k == "pointer" for k in before.git_entries), (
        "the .git pointer file is not inventoried at all")

    pointer.write_text(pointer.read_text().rstrip("\n") + "-tampered\n")

    # Tampering must not be SILENTLY ignored. Either the delta names it, or
    # the guard fails closed because git can no longer resolve the worktree —
    # both refuse the verdict. What must never happen is an empty delta.
    try:
        delta = rw.compare(wt, before, timeout=_TIMEOUT)
    except rw.WorktreeCheckFailed:
        return  # fail-closed: acceptable, and what git actually does here
    assert "pointer" in delta.modified, delta


def test_recreating_a_symlink_with_the_SAME_target_is_not_a_change(
    linked_worktree,
):
    """Pins `_symlink_entry`'s mtime REMOVAL, which nothing covered.

    The retarget test above still passes with `st_mtime_ns` re-added — extra
    sensitivity does not break a detection test. Only the FALSE-POSITIVE
    direction pins the removal: deleting a symlink and recreating it pointing
    at the same place changes its mtime and nothing else, and must not read as
    a modification. This is the symlink twin of
    `test_a_byte_identical_rewrite_of_a_git_file_is_not_a_change`.
    """
    wt, common = linked_worktree
    hooks = common / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "real-hook"
    target.write_text("#!/bin/sh\ntrue\n")
    link = hooks / "post-checkout"
    link.symlink_to(target)

    def recreate():
        time.sleep(1.1)  # ensure a DIFFERENT mtime, which is the whole point
        link.unlink()
        link.symlink_to(target)

    delta = _delta_after(wt, recreate)
    assert ".git/common/hooks/post-checkout" not in delta.modified, (
        "recreating a symlink with an identical target read as a "
        "modification — mtime is back in the symlink identity")


def test_the_cap_applies_to_every_bucket_not_just_modified():
    """The cap was pinned for `modified` only. Removing it from `added` or
    `deleted` left the suite green — the same mis-aimed-mutation trap that
    produced a false GREEN earlier on this branch, here as a coverage gap
    rather than a measurement error."""
    n = _INTEGRITY_PATHS_SHOWN + 3
    for bucket in ("added", "deleted"):
        detail = _integrity_failure_detail(
            _Delta(**{bucket: [f"{bucket}{i}.py" for i in range(n)]}))
        assert f"{n} {bucket}" in detail, (bucket, detail)
        assert "and 3 more" in detail, (bucket, detail)
        assert f"{bucket}{_INTEGRITY_PATHS_SHOWN}.py" not in detail, (
            f"a {bucket} path beyond the cap was listed", detail)


def test_the_cap_value_itself_is_asserted_not_derived():
    """`n = _INTEGRITY_PATHS_SHOWN + 4` derives from the constant under test,
    so raising the cap to 200 keeps that test green while making a persisted
    message 200 paths long. This pins the VALUE against a literal."""
    assert _INTEGRITY_PATHS_SHOWN == 5, (
        "the per-bucket cap changed; a persisted failure message is read by "
        "the next attempt and by an operator, so a larger value needs a "
        "deliberate decision, not a silent edit")


def test_the_identity_tuple_components_are_the_real_values(linked_worktree):
    """Pins EVERY component of `_stat_entry`'s tuple BY VALUE.

    The previous version asserted only that a content change is detected (the
    hash carries that) and described itself as "a cheap tuple-shape guard" —
    but it contained no shape assertion at all, and an independent review
    measured `st.st_size -> 0` as GREEN. A length check would not have helped
    either: zeroing a component keeps the length. So each component is compared
    against a value this TEST recomputes from the filesystem, never read back
    out of the code under test. (An earlier name said "size-only change";
    the grow below changes size AND content — it is a detection positive
    control, not a size-only probe. The by-value tuple assertion is what
    pins `size` separately.)
    """
    import hashlib as _hashlib
    import os as _os
    import stat as _stat

    wt, common = linked_worktree
    hook = common / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\ntrue\n")

    before = rw.snapshot(wt, timeout=_TIMEOUT)
    key = "common/hooks/post-checkout"
    assert key in before.git_entries, (
        "fixture did not inventory the hook", sorted(before.git_entries)[:8])

    raw = hook.read_bytes()
    st = _os.stat(hook)
    expected = (_stat.S_IMODE(st.st_mode), st.st_size,
                _hashlib.sha256(raw).hexdigest())
    assert before.git_entries[key] == expected, (
        "a component of the file identity tuple is not the real value — a "
        "zeroed or dropped component makes that change undetectable",
        before.git_entries[key], expected)

    def grow():
        hook.write_text("#!/bin/sh\ntrue\necho longer\n")

    delta = _delta_after(wt, grow)
    assert ".git/common/hooks/post-checkout" in delta.modified, delta


def test_a_symlink_mode_change_is_still_detected(linked_worktree):
    """Pins `mode` in `_symlink_entry`'s tuple, which was unpinned in the
    other direction: dropping it left the suite green."""
    wt, common = linked_worktree
    hooks = common / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "real"
    target.write_text("#!/bin/sh\ntrue\n")
    link = hooks / "post-checkout"
    link.symlink_to(target)
    before = rw.snapshot(wt, timeout=_TIMEOUT)
    # NOTE the namespaces differ: `Delta` paths carry a `.git/` prefix,
    # `Snapshot.git_entries` keys do not. Reading one with the other's key
    # silently finds nothing and the assertion reads as a real failure.
    key = "common/hooks/post-checkout"
    assert key in before.git_entries, (
        "fixture did not inventory the symlink", sorted(before.git_entries)[:8])
    # A symlink's own mode is not portably chmod-able, so the tuple's
    # components are compared BY VALUE against what this test reads from the
    # filesystem itself. The previous assertion was `len(...) == 3`, which a
    # zeroed component satisfies — an independent review measured BOTH
    # `S_IMODE(st.st_mode) -> 0` and `len(target) -> 0` as GREEN under it.
    import hashlib as _hashlib
    import os as _os
    import stat as _stat

    lst = _os.lstat(link)
    tgt = _os.readlink(link)
    expected = (_stat.S_IMODE(lst.st_mode), len(tgt),
                _hashlib.sha256(tgt.encode("utf-8", "surrogateescape")).hexdigest())
    assert before.git_entries[key] == expected, (
        "a component of the symlink identity tuple is not the real value — a "
        "zeroed component makes that class of change undetectable",
        before.git_entries[key], expected)


def test_the_integrity_verdict_carries_the_named_paths_to_the_checklist():
    """The WIRING, which nothing asserted.

    `grep "reviewer worktree integrity" tests/` found nothing before this:
    the label, the failed flag and the detail could drift apart — or the
    detail could be dropped entirely — with the suite green. This asserts the
    whole verdict a reviewer-worktree integrity failure produces, not just the
    string it is built from.
    """
    decision = _integrity_failure_decision(
        _Delta(modified=[".git/common/config"]))

    assert decision.passed is False
    assert len(decision.checklist) == 1
    item = decision.checklist[0]
    assert item.label == "reviewer worktree integrity"
    assert item.passed is False
    assert ".git/common/config" in item.evidence, (
        "the verdict reached the checklist without the path it objected to",
        item.evidence)
    assert "0 added, 1 modified, 0 deleted" in item.evidence
