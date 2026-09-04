"""SCRUM-68 follow-up: every successfully shipped task escalated, because the
CLOSED-PR rung trusted GitHub's merged flag. The operator's hard rule is a
LOCAL, identity-normalized squash merge (never `gh pr merge`), so a squash
commit lands on the base branch with a fresh SHA that has no commit-graph
lineage back to the source branch — `git merge-base --is-ancestor` is FALSE
for every one of our merges even when the change is fully landed. These tests
pin the content-based fix: `default_branch_shipped` (git-backed) and its
wiring into WakeWatcher._check_open_pr's CLOSED rung."""

from __future__ import annotations

import subprocess
import time

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus
from no_human.vcs import pr_watcher
from no_human.vcs.pr_watcher import default_branch_shipped


def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                    capture_output=True)


def _git_rc(repo_path, *args):
    """git's exit code, failure allowed. Sync on purpose: an inline
    ``subprocess.run`` inside an async test blocks the event loop."""
    return subprocess.run(["git", "-C", str(repo_path), *args],
                          capture_output=True, check=False).returncode


def _git_out(repo_path, *args):
    """git's stdout, failure allowed. Sync for the same reason as ``_git_rc``."""
    return subprocess.run(["git", "-C", str(repo_path), *args], text=True,
                          capture_output=True, check=False).stdout


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _squash_merge_repo(tmp_path):
    """Branch commits a real change; main gets the SAME content via a fresh
    (squash-shaped) commit — branch head is NOT an ancestor of main, but the
    touched file's content matches."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature: change a.txt")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "squash: change a.txt (fresh sha, no lineage)")
    return repo


def _unshipped_repo(tmp_path):
    """Branch commits a real change that never made it to main at all."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature: change a.txt")
    _git(repo, "checkout", "main")
    return repo


def _with_upstream(repo):
    """Give ``main`` a real upstream, as any working checkout has."""
    bare = repo.parent / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _stale_local_base_repo(tmp_path, *, land=True):
    """The live shape that escalated two shipped tasks on 2026-08-10.

    The identity-normalized squash is committed and PUSHED from a throwaway
    worktree, so ``refs/remotes/origin/main`` carries the landing while the
    long-lived checkout the watcher inspects keeps its own ``main`` exactly
    where it was — here, and in the real repo, 20+ commits behind.
    """
    repo = _with_upstream(_make_repo(tmp_path))
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature: change a.txt")
    if land:
        _git(repo, "checkout", "-b", "landing", "main")
        (repo / "a.txt").write_text("changed\n")
        _git(repo, "commit", "-am", "squash: land the feature content")
        _git(repo, "push", "origin", "landing:main")
        _git(repo, "checkout", "main")
        _git(repo, "branch", "-D", "landing")
    _git(repo, "checkout", "main")
    _git(repo, "fetch", "origin")
    return repo


async def _approval_task(store, repo_path, url="https://github.com/o/r/pull/86"):
    t = Task.new("shipped-check", repo_path=str(repo_path))
    t.context = {"pr_watch": url, "pr_branch": "feature", "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


async def test_closed_pr_with_squash_merged_content_ships_not_escalates(tmp_path, store):
    repo = _squash_merge_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    out = await w._check_open_pr(t)
    assert out == "shipped_pr_closed"
    assert (await store.get_task(t.id)).status is TaskStatus.DONE


async def test_closed_pr_with_content_genuinely_absent_still_escalates(tmp_path, store):
    repo = _unshipped_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_closed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert "closed without merging" in fresh.blocker["question"]


async def test_closed_pr_with_no_pr_shipped_hook_falls_back_to_escalation(tmp_path, store):
    """Backward compatibility: hosts that don't wire pr_shipped keep the old
    (safe, if noisy) behavior rather than crashing."""
    repo = _unshipped_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_closed"


async def test_a_half_landed_rename_is_not_shipped(tmp_path):
    """A branch that MOVES a file must not report shipped when only the
    destination landed on base.

    `git diff --name-only` has rename detection ON by default, so a `git mv`
    is reported as the destination path alone — the source path never enters
    the touched set and its deletion is never compared. Without
    `--no-renames` this returns True while `old.py` is still sitting on base,
    and the task is marked DONE with half its deliverable missing.
    """
    repo = _make_repo(tmp_path)
    (repo / "old.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add old")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-m", "move old -> new")
    # base gets the destination but NOT the removal — a half-landed move.
    _git(repo, "checkout", "main")
    (repo / "new.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add new, forget to delete old")

    assert (repo / "old.py").exists(), "fixture: base must still carry old.py"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_fully_landed_rename_is_shipped(tmp_path):
    """The companion: once BOTH halves of the move are on base, it ships.

    Without this, `--no-renames` could be 'fixed' by always returning False
    for any branch that renames anything.
    """
    repo = _make_repo(tmp_path)
    (repo / "old.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add old")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-m", "move old -> new")
    _git(repo, "checkout", "main")
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-m", "same move, squash-landed")

    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_branch_name_that_is_also_a_path_does_not_crash(tmp_path):
    """Without a trailing `--`, git bails with 'ambiguous argument' when a
    branch name is also a path in the tree. That failed safe (escalate), but
    it meant a genuinely shipped task kept escalating forever."""
    repo = _make_repo(tmp_path)
    (repo / "dup").write_text("i am a file named like a branch\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add dup file")
    _git(repo, "checkout", "-b", "dup")
    (repo / "feat.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature work")
    _git(repo, "checkout", "main")
    (repo / "feat.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "squash-land the same content")

    assert await default_branch_shipped(str(repo), "dup", "main") is True


async def test_a_deleted_branch_fails_safe(tmp_path):
    """The branch ref may be gone by the time the watcher looks. That must
    escalate (the old behaviour), never silently report shipped."""
    repo = _make_repo(tmp_path)
    assert await default_branch_shipped(str(repo), "no-such-branch", "main") is False


async def test_a_non_ascii_path_that_never_landed_is_not_shipped(tmp_path):
    """A branch whose touched paths need C-quoting must not report shipped.

    `core.quotePath` is on by default, so `git diff --name-only` returns
    `café.py` as the literal 7-character string `"caf\303\251.py"`. Feeding
    that back as a pathspec matches NOTHING, `git diff --quiet` then reports
    "no differences", and a branch whose entire deliverable is unmerged is
    marked DONE. `-z` emits raw NUL-separated names instead.

    No other test in this file uses a path outside [a-z0-9_.], which is why
    this survived two rounds of review.
    """
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "café.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add a non-ascii path")
    _git(repo, "checkout", "main")

    assert not (repo / "café.py").exists(), "fixture: base must NOT have the file"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_non_ascii_path_that_did_land_is_shipped(tmp_path):
    """Companion, so the fix cannot be 'return False for anything unusual'."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "café.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add a non-ascii path")
    _git(repo, "checkout", "main")
    (repo / "café.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "squash-land the same content")

    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_stale_local_base_does_not_hide_a_landed_pr(tmp_path):
    """The defect: the probe compared against the checkout's own ``main``.

    That ref is routinely days behind the branch the PR actually merged into,
    so a fully landed PR read as "content absent" and the watcher escalated
    "closed without merging" minutes after the content was on main.
    """
    repo = _stale_local_base_repo(tmp_path)
    rc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", "feature", "main", "--", "a.txt"]
    ).returncode
    assert rc != 0, "fixture: the LOCAL base must still be missing the landing"
    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_stale_local_base_does_not_invent_a_landing(tmp_path):
    """Control: an upstream that never received the content still says no."""
    repo = _stale_local_base_repo(tmp_path, land=False)
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_closed_pr_ships_when_only_the_upstream_base_has_the_landing(tmp_path, store):
    """End to end through the CLOSED rung, on the shape that misfired live."""
    repo = _stale_local_base_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    assert await w._check_open_pr(t) == "shipped_pr_closed"
    assert (await store.get_task(t.id)).status is TaskStatus.DONE


async def test_a_shared_generated_file_base_extended_further_is_still_shipped(tmp_path):
    """RELEASE_MANIFEST.txt's shape, and the second half of the live defect.

    Every PR here edits the same generated checksum manifest, so by the time
    the watcher looks, base carries this PR's line PLUS every other landing's.
    A per-file BYTE-EQUALITY test can therefore never recognise such a PR
    again — task 2f29209f's content was on main and its manifest still
    differed. A three-way merge that changes nothing is the honest question:
    does the branch still have anything to contribute?
    """
    repo = _make_repo(tmp_path)
    (repo / "manifest.txt").write_text("aaa  one\nbbb  two\nccc  three\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add the generated manifest")
    _git(repo, "checkout", "-b", "feature")
    (repo / "manifest.txt").write_text("aaa  one\nBBB  two\nccc  three\n")
    _git(repo, "commit", "-am", "feature: re-hash two")
    _git(repo, "checkout", "main")
    # base got this branch's line AND another landing's new entry.
    (repo / "manifest.txt").write_text("aaa  one\nBBB  two\nccc  three\nddd  four\n")
    _git(repo, "commit", "-am", "squash-land, plus another task's entry")

    rc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", "feature", "main", "--",
         "manifest.txt"]).returncode
    assert rc != 0, "fixture: the file must NOT be byte-identical on base"
    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_deletion_that_landed_is_shipped(tmp_path):
    """A PR whose deliverable is a REMOVAL ships once base has removed it too."""
    repo = _make_repo(tmp_path)
    (repo / "gone.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add the file the PR will delete")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "rm", "-q", "gone.py")
    _git(repo, "commit", "-m", "feature: delete gone.py")
    _git(repo, "checkout", "main")
    _git(repo, "rm", "-q", "gone.py")
    _git(repo, "commit", "-m", "squash-land the deletion")

    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_deletion_still_absent_from_base_is_not_shipped(tmp_path):
    """Companion, so "shipped" cannot be reached by ignoring removals."""
    repo = _make_repo(tmp_path)
    (repo / "gone.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add the file the PR will delete")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "rm", "-q", "gone.py")
    _git(repo, "commit", "-m", "feature: delete gone.py")
    _git(repo, "checkout", "main")

    assert (repo / "gone.py").exists(), "fixture: base must still carry the file"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


def _f_py(first, last="epsilon"):
    """The driver-owned file. Multi-line so a later task's edit to the LAST
    line and the branch's edit to the FIRST are separate hunks — without a
    driver they merge cleanly, which is what makes the no-driver control a
    control rather than a conflict."""
    return f"{first}\nbeta\ngamma\ndelta\n{last}\n"


def _driver_ran(tmp_path):
    """Whether the fixture's merge driver actually fired.

    git invokes a merge driver ONLY when BOTH sides changed the path AND the
    two blobs differ; anything else resolves trivially and the driver is never
    consulted. A driver test without this sentinel therefore proves nothing —
    which is exactly how the round-3 defect hid.
    """
    return (tmp_path / "drvran").exists()


def _merge_driver_repo(tmp_path, *, land, extend=False, attrs="both"):
    """A repo whose `.gitattributes` routes a file to a merge driver that
    DISCARDS the incoming side (it only touches the sentinel, leaving %A, i.e.
    ours, untouched — `true` with a receipt).

    Nothing here is exotic: `merge=<name>` in `.gitattributes` plus
    `merge.<name>.driver` in config is the documented way to own a generated
    or lockfile-shaped path, and `pr_watcher` runs against the USER'S repo, so
    any customer repo may carry one.

    ``attrs``: ``"both"`` commits the attributes before branching (both sides
    carry them), ``"base"`` commits them on ``main`` only, ``None`` builds the
    same history with NO driver at all — the control.
    ``extend``: a later task edits the same path after the landing, which is
    what makes both sides differ and so is what makes the driver fire.
    """
    repo = _make_repo(tmp_path)
    rule = "f.py merge=keepours\n"
    if attrs:
        _git(repo, "config", "merge.keepours.driver",
             f'touch "{tmp_path / "drvran"}"')
    if attrs == "both":
        (repo / ".gitattributes").write_text(rule)
    (repo / "f.py").write_text(_f_py("alpha"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add f.py behind a custom merge driver")
    _git(repo, "checkout", "-b", "feature")
    (repo / "f.py").write_text(_f_py("feature content"))
    _git(repo, "commit", "-am", "feature: rewrite f.py")
    _git(repo, "checkout", "main")
    if attrs == "base":
        (repo / ".gitattributes").write_text(rule)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "base only: adopt the driver")
    (repo / "f.py").write_text(_f_py("feature content" if land else "something else"))
    _git(repo, "commit", "-am", "land it" if land else "unrelated change to f.py")
    if extend:
        (repo / "f.py").write_text(_f_py("feature content", "a later task's line"))
        _git(repo, "commit", "-am", "a later task extends the same path")
    return repo


async def test_a_custom_merge_driver_cannot_manufacture_a_landing(tmp_path):
    """A merge driver that discards "theirs" must not be able to report
    shipped for content that never landed.

    `git merge-tree` honours `.gitattributes`, so a driver like `true` (keep
    ours) resolves every contested path to the BASE TIP's own side: exit 0,
    and the tree written is byte-for-byte the tip's tree. Trusting that pair
    alone reports shipped for a branch whose whole deliverable is still
    missing, and the CLOSED rung turns shipped into `TaskStatus.DONE` with no
    human in the loop -- a silent completion on undelivered work. The old
    per-file blob comparison was immune (attributes never enter a `git diff`),
    so this is a regression the merge-tree switch would introduce.
    """
    repo = _merge_driver_repo(tmp_path, land=False)
    assert "feature content" not in (repo / "f.py").read_text(), \
        "fixture: base must NOT carry the feature content"
    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert _driver_ran(tmp_path), "vacuous: the driver was never consulted"


async def test_a_genuine_landing_under_a_position_driver_is_shipped_via_history_anchor(tmp_path):
    """🔴 FORMERLY A PINNED COST (pre-2026-08-12): this used to assert False
    for content that DID land, documenting a spurious escalation the
    both-directions rule could not avoid — a position-resolving driver on a
    generated/lockfile-shaped path, genuinely landed, with a LATER task
    editing the same path afterwards. Both sides then carry an edit, the
    driver fires, and the TIP-anchored check is direction-asymmetric (True
    forward, False reverse) — see ``_contained_at`` at the tip, still exactly
    this shape.

    ``branch_landed_commit``'s history scan closes this specific instance
    without touching the merge-driver limitation at all: at the LANDING
    commit — before the later edit ever happened — the branch's blob and the
    landing commit's blob are byte-IDENTICAL, so git resolves trivially and
    never invokes the driver there. The driver only ever fires at the tip
    (proven below by the sentinel), where the check still fails exactly as
    documented; the scan then walks back and finds the commit where the
    question can be answered without the driver in the loop. This is the same
    "ask at the landing commit, not the tip" fix as the ordinary same-file
    case — the driver was never the deciding factor, only what made the
    tip-level symptom visible enough to pin a test on.

    Its predecessor asserted True here and was VACUOUS: both sides made the
    IDENTICAL edit, so git resolved trivially and never ran the driver at
    ALL, which is how that version went unnoticed. Hence the sentinel: the
    driver DOES run (at the tip) in this fixture, and the answer is still
    correct.
    """
    repo = _merge_driver_repo(tmp_path, land=True, extend=True)
    assert "feature content" in (repo / "f.py").read_text(), \
        "fixture: base really DID receive the branch's content"
    assert await default_branch_shipped(str(repo), "feature", "main") is True
    assert _driver_ran(tmp_path), \
        "vacuous: the driver was never consulted (the tip-level attempt must " \
        "still run it before the scan falls back to the landing commit)"


async def test_the_same_landing_without_a_driver_is_still_shipped(tmp_path):
    """The control for the test above: identical history, no merge driver. It
    ships. So the driver is the sole cause of that False, not the fact that
    base moved on -- and the cure was not "return False whenever the path was
    touched twice"."""
    repo = _merge_driver_repo(tmp_path, land=True, extend=True, attrs=None)
    assert await default_branch_shipped(str(repo), "feature", "main") is True
    assert not _driver_ran(tmp_path), "fixture: the control must have no driver"


async def test_a_merge_driver_in_info_attributes_cannot_manufacture_a_landing(tmp_path):
    """The same attack through `$GIT_DIR/info/attributes`, which outranks the
    in-tree file and which no `--attr-source` / `core.attributesFile` override
    can switch off. Pinned so the cure cannot be narrowed to `.gitattributes`.
    """
    repo = _merge_driver_repo(tmp_path, land=False)
    (repo / ".git" / "info" / "attributes").write_text("f.py merge=keepours\n")
    _git(repo, "rm", "-q", "--cached", ".gitattributes")
    (repo / ".gitattributes").unlink()
    _git(repo, "commit", "-m", "drop the in-tree attributes file")

    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert _driver_ran(tmp_path), "vacuous: the driver was never consulted"


async def test_a_merge_driver_from_core_attributesfile_cannot_manufacture_a_landing(tmp_path):
    """The same attack through `core.attributesFile`, i.e. attributes that live
    entirely outside the repo."""
    repo = _merge_driver_repo(tmp_path, land=False)
    external = tmp_path / "attributes"
    external.write_text("f.py merge=keepours\n")
    _git(repo, "config", "core.attributesFile", str(external))
    _git(repo, "rm", "-q", "--cached", ".gitattributes")
    (repo / ".gitattributes").unlink()
    _git(repo, "commit", "-m", "drop the in-tree attributes file")

    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert _driver_ran(tmp_path), "vacuous: the driver was never consulted"


async def test_a_wildcard_merge_driver_cannot_manufacture_a_landing(tmp_path):
    """The same attack routed by `*` rather than by an exact path, so the rule
    is not one the probe could dodge by naming the file."""
    repo = _merge_driver_repo(tmp_path, land=False)
    (repo / ".gitattributes").write_text("* merge=keepours\n")
    _git(repo, "commit", "-am", "route EVERY path to the driver")

    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert _driver_ran(tmp_path), "vacuous: the driver was never consulted"


async def test_a_merge_driver_attached_on_the_base_side_only_cannot_manufacture_a_landing(tmp_path):
    """The same attack with the attributes committed on ONE side only -- base,
    which is the side the checkout is usually on: `merge-tree` reads the
    CHECKOUT's attributes, not either commit's, so a rule living only on base
    fires exactly as if both sides carried it, which is what this pins. The
    mirror case is conditional rather than absolute: a rule living only on the
    branch goes unconsulted while the checkout sits on base (a weaker no, not a
    defeated driver), and IS consulted once the branch itself is checked out --
    verified both ways, False either way."""
    repo = _merge_driver_repo(tmp_path, land=False, attrs="base")
    assert _git_rc(repo, "cat-file", "-e", "feature:.gitattributes") != 0, \
        "fixture: the branch side must NOT carry the attributes"

    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert _driver_ran(tmp_path), "vacuous: the driver was never consulted"


async def test_a_deleted_local_base_fails_closed_instead_of_guessing_a_remote(tmp_path):
    """A worktree or fresh checkout need not carry a LOCAL branch named
    ``main``, and ``<base>@{upstream}`` is only defined for a local branch. So
    with no such branch NEITHER candidate tip resolves and a fully landed PR
    reads as absent.

    That is a deliberate choice, not an oversight. A ``refs/remotes/*/<base>``
    fallback would answer this case, and was tried -- but a glob over every
    remote cannot tell the remote the PR TARGETS from any other remote that
    happens to carry a branch of the same name, and the two tests below show it
    manufacturing a landing out of a fork's ``main``. The two outcomes are not
    symmetric: a False costs one spurious escalation with a human on the other
    end of it, while a True writes ``TaskStatus.DONE`` on undelivered work with
    no human at all. Fail closed.
    """
    repo = _stale_local_base_repo(tmp_path)
    _git(repo, "checkout", "feature")
    _git(repo, "branch", "-D", "main")
    assert _git_rc(repo, "rev-parse", "--verify", "--quiet", "main") != 0, \
        "fixture: the local base branch must be gone"
    assert _git_out(repo, "show", "origin/main:a.txt") == "changed\n", \
        "fixture: the remote-tracking ref really does carry the landing"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_deleted_local_base_does_not_invent_a_landing(tmp_path):
    """Companion: nothing on any ref carries the content either, so the same
    False here is the honest answer rather than an artefact of failing closed.
    """
    repo = _stale_local_base_repo(tmp_path, land=False)
    _git(repo, "checkout", "feature")
    _git(repo, "branch", "-D", "main")
    assert await default_branch_shipped(str(repo), "feature", "main") is False


def _fork_layout_repo(tmp_path, *, local_base):
    """The standard OSS fork layout: ``origin`` is the developer's OWN fork and
    ``upstream`` is the canonical repo the PR actually targets.

    The developer merged the branch into their fork's ``main``. The PR against
    canonical is still open, or was closed unmerged -- so the content is on
    ``origin/main`` and is NOT on ``upstream/main``, which is the tip the
    question is about.

    ``local_base`` picks which of the two routes into a glob fallback this
    exercises: with a local ``main`` created ``--no-track`` (or from a
    customised ``remote.origin.fetch``, or a locally-created base branch),
    ``main@{upstream}`` is unset even though the branch exists; without one,
    there is no local branch for ``@{upstream}`` to be defined on at all.
    """
    canonical = tmp_path / "canonical.git"
    fork = tmp_path / "fork.git"
    for bare in (canonical, fork):
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                       check=True, capture_output=True)
    seed = _make_repo(tmp_path)
    _git(seed, "remote", "add", "canonical", str(canonical))
    _git(seed, "remote", "add", "fork", str(fork))
    _git(seed, "push", "canonical", "main")
    _git(seed, "push", "fork", "main")
    _git(seed, "checkout", "-b", "feature")
    (seed / "a.txt").write_text("changed\n")
    _git(seed, "commit", "-am", "feature: change a.txt")
    _git(seed, "checkout", "main")
    (seed / "a.txt").write_text("changed\n")
    _git(seed, "commit", "-am", "squash-land onto MY OWN fork, not canonical")
    _git(seed, "push", "fork", "main")

    work = tmp_path / "work"
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True,
                   capture_output=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "add", "origin", str(fork))
    _git(work, "remote", "add", "upstream", str(canonical))
    _git(work, "fetch", "origin")
    _git(work, "fetch", "upstream")
    _git(work, "fetch", str(seed), "feature:feature")
    _git(work, "checkout", "feature")
    if local_base:
        _git(work, "branch", "--no-track", "main", "upstream/main")
    return work


def _assert_fork_layout(work):
    """Pin the shape the two tests below depend on, so neither can pass by
    accident: the PR's target tip lacks the content and the fork's has it."""
    assert _git_out(work, "show", "upstream/main:a.txt") == "orig\n", \
        "fixture: canonical (what the PR targets) must NOT have the content"
    assert _git_out(work, "show", "origin/main:a.txt") == "changed\n", \
        "fixture: the developer's fork MUST have the content"


async def test_a_fork_remote_cannot_vouch_for_a_landing_on_canonical(tmp_path):
    """A local base branch with no upstream must not let ANY remote answer.

    ``--no-track`` (and a customised ``remote.origin.fetch``, and a
    locally-created base branch) leaves ``main@{upstream}`` unset while ``main``
    itself exists. A ``refs/remotes/*/main`` fallback then offers
    ``origin/main`` -- the developer's own fork -- as evidence about a PR that
    targeted ``upstream``, and the CLOSED rung turns that True into
    ``TaskStatus.DONE`` with no human in the loop.
    """
    work = _fork_layout_repo(tmp_path, local_base=True)
    _assert_fork_layout(work)
    assert _git_rc(work, "rev-parse", "--abbrev-ref", "main@{upstream}") != 0, \
        "fixture: the local base must have no configured upstream"
    assert _git_rc(work, "rev-parse", "--verify", "--quiet", "main") == 0, \
        "fixture: but the local base branch itself must exist"
    assert await default_branch_shipped(str(work), "feature", "main") is False


async def test_a_fork_remote_cannot_vouch_when_there_is_no_local_base(tmp_path):
    """The same attack through the other route into the fallback: no local
    ``main`` at all, so ``@{upstream}`` cannot be defined."""
    work = _fork_layout_repo(tmp_path, local_base=False)
    _assert_fork_layout(work)
    assert _git_rc(work, "rev-parse", "--verify", "--quiet", "main") != 0, \
        "fixture: there must be no local base branch"
    assert await default_branch_shipped(str(work), "feature", "main") is False


async def test_a_content_resolving_merge_driver_is_a_known_residual(tmp_path):
    """🔴 THIS TEST PINS A HOLE, NOT A GUARANTEE. It asserts True for content
    that never landed -- i.e. it documents a live false "shipped".

    The both-directions rule defeats drivers that resolve by POSITION (which
    side git handed them as %A). It does NOT defeat drivers that resolve by
    CONTENT: a constant emitter, a regenerator, or the rule-picker below all
    write the same bytes whichever side is %A, so they satisfy BOTH passes and
    the probe returns True. That is a silent ``TaskStatus.DONE`` on undelivered
    work.

    It is PRE-EXISTING, not a regression: the single-direction predecessor
    returns True here too. Closing it needs a merge that ignores merge drivers
    entirely, which `merge-tree` offers no flag for.

    Kept as a test rather than a comment so the residual cannot rot silently:
    if a later change makes this False, that is a WIN -- delete the test and
    the "NOT COVERED" paragraph in ``default_branch_shipped``'s docstring
    together, so the claim and the mechanism move as one.
    """
    driver = tmp_path / "pick.sh"
    driver.write_text(
        "#!/bin/bash\n"
        # %A=ours %B=theirs -- keep whichever side lacks the branch marker,
        # by CONTENT, so the answer does not depend on the argument order.
        'if grep -q "feature content" "$1"; then cp "$2" "$1"; fi\nexit 0\n'
    )
    driver.chmod(0o755)
    repo = _merge_driver_repo(tmp_path, land=False)
    _git(repo, "config", "merge.keepours.driver", f"{driver} %A %B")

    assert "feature content" not in (repo / "f.py").read_text(), \
        "fixture: base must NOT carry the feature content"
    assert await default_branch_shipped(str(repo), "feature", "main") is True, \
        "if this is now False the hole is closed -- update the docstring too"


async def test_a_hung_merge_driver_cannot_wedge_the_watcher(tmp_path, monkeypatch):
    """``merge-tree`` EXECUTES the repo's custom merge drivers, so a driver
    that blocks blocks the watcher's event loop for as long as it likes. The
    probe runs up to two of them per candidate tip, against the USER'S repo.

    A bounded wait turns that into the same fail-closed False every other git
    failure produces.
    """
    repo = _merge_driver_repo(tmp_path, land=False)
    _git(repo, "config", "merge.keepours.driver", "sleep 20")
    monkeypatch.setattr(pr_watcher, "_GIT_TIMEOUT", 1.0)

    started = time.monotonic()
    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert time.monotonic() - started < 15, "the hung driver was not bounded"


async def test_squash_merge_shape_is_shipped_despite_no_ancestry(tmp_path):
    repo = _squash_merge_repo(tmp_path)
    rc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", "feature", "main"]
    ).returncode
    assert rc != 0, "fixture must NOT have branch-head ancestry into main"
    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_genuinely_absent_branch_is_not_shipped(tmp_path):
    repo = _unshipped_repo(tmp_path)
    assert await default_branch_shipped(str(repo), "feature", "main") is False


# --------------------------------------------------------------------------- #
# Landed-commit anchoring (2026-08-12 incident): containment used to be asked
# ONLY at the current base tip, so a LATER commit touching the same file as an
# already-landed branch made the probe read "not shipped" again — the sibling
# of the re-derived-ledger blindness, for ordinary source files. train15 (a
# squash landing orchestrator.py) + train16 (a second, later edit to the same
# file) is the exact live shape: train15's task re-escalated "closed without
# merging" the moment train16 landed.
# --------------------------------------------------------------------------- #


def _stacked_trains_repo(tmp_path, *, filler_commits=0):
    """train15 lands a branch's edit to shared.py as a fresh squash commit;
    train16 (also from its own branch) later lands a SECOND edit to the same
    file. Returns (repo, train15_branch, train16_branch, train15_landed_sha).

    ``filler_commits`` inserts unrelated commits (touching a DIFFERENT file)
    between the two trains, so a scan that is not path-filtered would have to
    wade through them — the bound the depth/path-filter test pins.
    """
    repo = _make_repo(tmp_path)
    (repo / "shared.py").write_text("line one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add shared.py")

    _git(repo, "checkout", "-b", "train15")
    (repo / "shared.py").write_text("line one\ntrain15 edit\n")
    _git(repo, "commit", "-am", "train15: edit shared.py")
    _git(repo, "checkout", "main")
    (repo / "shared.py").write_text("line one\ntrain15 edit\n")
    _git(repo, "commit", "-am", "squash-land train15")
    train15_sha = _git_out(repo, "rev-parse", "HEAD").strip()

    for i in range(filler_commits):
        (repo / "unrelated.py").write_text(f"filler {i}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"unrelated filler commit {i}")

    _git(repo, "checkout", "-b", "train16", "main")
    (repo / "shared.py").write_text("line one\ntrain15 edit\ntrain16 edit\n")
    _git(repo, "commit", "-am", "train16: edit shared.py again")
    _git(repo, "checkout", "main")
    (repo / "shared.py").write_text("line one\ntrain15 edit\ntrain16 edit\n")
    _git(repo, "commit", "-am", "squash-land train16 (rewrites the same file)")

    return repo, "train15", "train16", train15_sha


async def test_a_landed_pr_is_shipped_after_a_later_commit_rewrites_the_same_file(tmp_path):
    """The `b53b9e13`/`6d525dd5` shape: train15 lands cleanly, train16 later
    rewrites the same file — train15 must still read shipped, anchored at
    the commit where IT landed, not at the (now-diverged) tip."""
    repo, train15, _train16, train15_sha = _stacked_trains_repo(tmp_path)
    rc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", train15, "main", "--",
         "shared.py"]
    ).returncode
    assert rc != 0, "fixture: the tip must no longer equal train15's own tree"
    assert await default_branch_shipped(str(repo), train15, "main") is True
    assert await pr_watcher.branch_landed_commit(
        str(repo), train15, "main") == train15_sha


async def test_two_stacked_squash_trains_sharing_a_file_both_read_shipped(tmp_path):
    """Both halves of the stack ship: train16 (the current tip's own content)
    via the ordinary tip check, train15 (superseded at the tip) via the
    history anchor — the honest '+2' when both flip to done."""
    repo, train15, train16, _sha = _stacked_trains_repo(tmp_path)
    assert await default_branch_shipped(str(repo), train15, "main") is True
    assert await default_branch_shipped(str(repo), train16, "main") is True


async def test_genuinely_unlanded_work_is_still_not_shipped_after_the_history_scan(tmp_path):
    """Control: a branch whose content never reached main at any historical
    commit must not become shipped just because the scan now looks further
    back — the scan finds nothing because there is nothing to find."""
    repo = _make_repo(tmp_path)
    (repo / "shared.py").write_text("line one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add shared.py")
    _git(repo, "checkout", "-b", "feature")
    (repo / "shared.py").write_text("line one\nfeature edit, never landed\n")
    _git(repo, "commit", "-am", "feature: edit shared.py")
    _git(repo, "checkout", "main")
    for i in range(5):
        (repo / "unrelated.py").write_text(f"filler {i}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"unrelated filler commit {i}")

    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert await pr_watcher.branch_landed_commit(str(repo), "feature", "main") is None


async def test_the_history_scan_is_bounded_and_path_filtered(tmp_path, monkeypatch):
    """60 unrelated commits sit between train16's landing (which re-touches
    the shared file, forcing the scan) and train15's own landing. Without
    path-filtering, a depth-50 scan would examine up to 60 candidates; with
    it, only commits that touched `shared.py` are even considered — pinned
    by counting calls to `_contained_at` rather than trusting the result
    alone."""
    repo, train15, _train16, train15_sha = _stacked_trains_repo(
        tmp_path, filler_commits=60)

    calls = []
    real = pr_watcher._contained_at

    async def counting(repo_path, commit, branch):
        calls.append(commit)
        return await real(repo_path, commit, branch)

    monkeypatch.setattr(pr_watcher, "_contained_at", counting)

    # ONE call — `default_branch_shipped` is a thin `bool()` wrapper over the
    # same function, so calling both here would double the count for no
    # extra coverage.
    result = await pr_watcher.branch_landed_commit(str(repo), train15, "main")
    assert result == train15_sha
    assert len(calls) <= 5, (
        f"expected a small, path-filtered candidate set, got {len(calls)} "
        f"calls — the 60 unrelated filler commits must never be examined"
    )


async def test_a_recorded_landed_sha_short_circuits_the_probe(tmp_path, monkeypatch):
    """A previously recorded `landed_sha` that is still an ancestor of the
    base tip must short-circuit straight to `commit_is_ancestor` — no
    merge-tree at all, so a broken/raising `_unsettled_paths` must not even
    be reached on this path."""
    repo, train15, _train16, train15_sha = _stacked_trains_repo(tmp_path)

    async def boom(*a, **kw):
        raise AssertionError("_unsettled_paths must not run on the fast path")

    monkeypatch.setattr(pr_watcher, "_unsettled_paths", boom)

    result = await pr_watcher.branch_landed_commit(
        str(repo), train15, "main", landed_sha=train15_sha)
    assert result == train15_sha


async def test_closed_pr_whose_landing_predates_a_same_file_commit_completes_the_task(
        tmp_path, store):
    """End to end through `WakeWatcher._check_open_pr`: train15's task, with
    its PR closed unmerged (the operator's local-squash workflow never uses
    GitHub's merge button) and train16 having since rewritten the same file,
    must still complete — DONE, a `shipped` event, and `landed_sha` recorded
    in context so a restart never has to re-run the scan."""
    repo, train15, _train16, train15_sha = _stacked_trains_repo(tmp_path)
    t = await _approval_task(store, repo)
    t.context = await store.merge_context(t.id, {"pr_branch": train15})

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state,
                    pr_shipped=pr_watcher.branch_landed_commit)
    out = await w._check_open_pr(t)
    assert out == "shipped_pr_closed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert fresh.context.get("landed_sha") == train15_sha
    events = await store.list_events(t.id)
    assert any(e.get("kind") == "shipped" for e in events)


# --------------------------------------------------------------------------- #
# The same content check, one rung earlier: the CONFLICTING rung (2026-08-11).
# --------------------------------------------------------------------------- #


async def _conflicting_watcher(store, *, pr_shipped=default_branch_shipped):
    async def pr_state(url):
        return "OPEN"

    async def pr_mergeable(url):
        return {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}

    return WakeWatcher(store, {}, pr_state=pr_state, pr_mergeable=pr_mergeable,
                       pr_shipped=pr_shipped)


async def test_open_conflicting_pr_whose_content_landed_ships_instead_of_rebasing(
        tmp_path, store):
    """End to end on real git, on the shape measured live on 2026-08-11: the
    supervised local squash pushed the content to origin/main while the PR was
    still OPEN and still carrying GitHub's cached CONFLICTING verdict. A full
    rebase round — an entire coder attempt — used to start here."""
    repo = _stale_local_base_repo(tmp_path)
    t = await _approval_task(store, repo)
    w = await _conflicting_watcher(store)

    assert await w._check_open_pr(t) == "shipped_pr_conflict"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert "pr_conflict_rounds" not in (fresh.context or {})
    assert not (fresh.context or {}).get("send_back_feedback")


async def test_open_conflicting_pr_with_content_absent_still_rebases(tmp_path, store):
    """The control on the SAME real-git harness: nothing landed, so the round
    starts exactly as before. Without this, the guard could be 'fixed' by
    always reporting shipped."""
    repo = _stale_local_base_repo(tmp_path, land=False)
    # A forge CONFLICTING means main and the branch really do disagree: give
    # main its own edit to a.txt so the local merge names a conflicting path
    # (855f1263: an EMPTY local enumeration under a forge CONFLICTING is a
    # stale-flag contradiction that now defers instead of opening a round).
    (repo / "a.txt").write_text("main's own change\n")
    _git(repo, "commit", "-am", "main: also change a.txt")
    _git(repo, "push", "origin", "main:main")
    t = await _approval_task(store, repo)
    w = await _conflicting_watcher(store)

    assert await w._check_open_pr(t) == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert fresh.context["pr_conflict_rounds"] == 1


async def test_a_half_landed_branch_on_a_conflicting_pr_still_rebases(tmp_path, store):
    """Devil's advocate (b): a PARTIALLY landed branch must never complete.
    `default_branch_shipped` is all-or-nothing by construction — it asks
    whether merging the branch into the base tip changes the tip's TREE, so a
    branch with anything left to contribute fails it. Here the destination of
    a rename landed and the source deletion did not."""
    repo = _make_repo(tmp_path)
    (repo / "old.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add old")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "mv", "old.py", "new.py")
    # The real conflict lives ELSEWHERE (a.txt, edited on both sides) so the
    # forge's CONFLICTING is backed by a locally conflicting path (855f1263)
    # while the rename stays exactly half-landed: destination identical on
    # both sides, source deletion forgotten on main.
    (repo / "a.txt").write_text("feature's a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "move old -> new; edit a.txt")
    _git(repo, "checkout", "main")
    (repo / "new.py").write_text("value = 1\n")
    (repo / "a.txt").write_text("main's a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add new, forget to delete old; edit a.txt")

    t = await _approval_task(store, repo)
    w = await _conflicting_watcher(store)
    assert await w._check_open_pr(t) == "resumed"
    assert (await store.get_task(t.id)).context["pr_conflict_rounds"] == 1


@pytest.mark.parametrize("answer", [False, True, RuntimeError("git exploded")],
                         ids=["absent", "landed", "probe-raised"])
async def test_going_terminal_during_the_probe_aborts_the_closed_rung(
        tmp_path, store, answer):
    """Review finding D, on the rung that has had this guard since SCRUM-68:
    the abort must fire on EVERY probe answer, not only the positive one.
    Before the fix, a probe that said 'absent' (or raised) let the rung write
    a phantom blocker, a `pr_closed` event and an outcome row onto a task that
    had gone DONE while the probe ran — the store's CAS refuses only the
    status flip, and `cli/commands.py`'s restore-approval path documents that
    exact shape as already-ruled-wrong."""
    repo = _unshipped_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    async def pr_shipped(repo_path, branch, base):
        fresh = await store.get_task(t.id)
        await store.set_status(fresh, TaskStatus.DONE, validate=False,
                               event={"source": "test", "kind": "test_seed"})
        if isinstance(answer, BaseException):
            raise answer
        return answer

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=pr_shipped)
    assert await w._check_open_pr(t) is None

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert not fresh.blocker, "no phantom blocker on a finished task"
    assert await store.list_pr_outcomes(task_id=t.id) == []
    kinds = {e.get("kind") for e in await store.list_events(t.id)}
    assert not (kinds & {"pr_closed", "shipped"}), kinds


# --------------------------------------------------------------------------- #
# The generated-ledger exclusion (live incident, 2026-08-11). Task 5ef97879's
# PR #183 was fully landed on main as squash 5433835d8, and the CLOSED rung's
# probe still said NOT shipped, so the task escalated "abandon or rework?".
# Measured on the real refs at the time:
#
#   git merge-tree --write-tree origin/main no-human/5ef97879-2   -> rc=1
#     CONFLICT (content): Merge conflict in RELEASE_MANIFEST.txt
#   git diff --name-only <origin/main^{tree}> <merged>  ->  RELEASE_MANIFEST.txt
#
# …in BOTH directions, with RELEASE_MANIFEST.txt the ONLY differing path — the
# classification ledger merged cleanly and was never implicated. The repo's
# merge-result rule RE-DERIVES that manifest at landing instead of taking the
# branch's rows, so it diverges on every landing — and nearly every branch
# touches it, because adding or changing any shipped file re-pins. Without the
# exclusion the completion path can essentially never fire on this repo's own
# PRs. The exclusion is that ONE path: see `_GENERATED_LEDGERS` for why
# EXPORT_CLASSIFICATION.txt was tried and removed.
# --------------------------------------------------------------------------- #


def _ledger_repo(tmp_path, *, extra_branch_file=None, extra_ledger=None,
                 diverge_classification=False,
                 classification_branch=None, classification_main=None):
    """The 5ef97879 shape: the code landed, the manifest was RE-DERIVED.

    Base, branch and main each carry a different manifest row for the same
    path, so the two sides changed the same line differently — a genuine
    merge CONFLICT confined to the ledger, which is exactly what the live
    refs produced. `EXPORT_CLASSIFICATION.txt` stays IDENTICAL across the two
    sides unless `diverge_classification` (a ship<->drop flip) or the more
    precise `classification_branch`/`classification_main` pair, which each
    write the EXACT text their side needs — used by the count-only-divergence
    cases, where the two sides must differ in a rule's count integer alone.
    """
    repo = _make_repo(tmp_path)
    (repo / "RELEASE_MANIFEST.txt").write_text("AAA  a.txt\n")
    (repo / "EXPORT_CLASSIFICATION.txt").write_text("ship     1  a.txt\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add the generated ledgers")

    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    (repo / "RELEASE_MANIFEST.txt").write_text("BBB  a.txt\n")
    if diverge_classification:
        (repo / "EXPORT_CLASSIFICATION.txt").write_text("drop     1  a.txt\n")
    if classification_branch is not None:
        (repo / "EXPORT_CLASSIFICATION.txt").write_text(classification_branch)
    if extra_branch_file:
        (repo / extra_branch_file).write_text("only on the branch\n")
    if extra_ledger:
        (repo / extra_ledger).parent.mkdir(parents=True, exist_ok=True)
        (repo / extra_ledger).write_text("BBB  a.txt\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature: change a.txt and re-pin")

    # The supervised local squash: the CODE lands verbatim, the ledgers are
    # re-derived from the landed tree and land with DIFFERENT rows.
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("changed\n")
    (repo / "RELEASE_MANIFEST.txt").write_text("DDD  a.txt\n")
    if classification_main is not None:
        (repo / "EXPORT_CLASSIFICATION.txt").write_text(classification_main)
    if extra_ledger:
        (repo / extra_ledger).parent.mkdir(parents=True, exist_ok=True)
        (repo / extra_ledger).write_text("DDD  a.txt\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "squash: land a.txt, re-derive the manifest")
    return repo


#: The base text for the count-only-divergence cases below: two rules, so a
#: pattern change, an added/removed rule, and a header edit are all separately
#: expressible against it.
_CLASSIFICATION_BASE_TEXT = "# header\nship     2  a.txt\ndrop     1  secrets/\n"


async def test_a_branch_diverging_only_in_the_generated_manifest_is_shipped(tmp_path):
    """THE LIVE DEFECT. Every real change the branch carries is on base; the
    only residue is the manifest the landing re-derived."""
    repo = _ledger_repo(tmp_path)
    assert (repo / "a.txt").read_text() == "changed\n", "fixture: code landed"
    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_classification_only_divergence_is_not_shipped(tmp_path):
    """THE NARROWING (review, 2026-08-11). `EXPORT_CLASSIFICATION.txt` was
    briefly excluded too, by analogy rather than measurement. A row there is a
    DECISION, not a derivation, so excluding it hid an unlanded `drop` — a file
    somebody decided to stop publishing that goes on publishing — inside this
    repo's primary privacy mechanism. Here the branch flips `ship` to `drop`
    and the landing never took it: NOT shipped, so the task gets a rebase round
    or a human rather than a silent DONE."""
    repo = _ledger_repo(tmp_path, diverge_classification=True)
    assert (repo / "EXPORT_CLASSIFICATION.txt").read_text() == "ship     1  a.txt\n", \
        "fixture: base still carries the OLD decision"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_classification_count_only_divergence_is_shipped(tmp_path):
    """THE FIX (live class, 2026-08-14). A supervising-session squash train
    UNION-edits every rule's win-count across the branches it folds in, so a
    non-first car's classification differs from its own landing candidate in
    the count integer alone — forever, since the count is a re-derived tally,
    never taken from the branch. Here the branch carries `ship 2 a.txt`; the
    landing re-derived it to `ship 3 a.txt`. Everything else (the header, the
    `drop` rule, the code) is identical, apart from the manifest re-pin every
    landing does anyway. This must read as shipped, and `branch_landed_commit`
    must resolve to a real SHA, not just a bool."""
    repo = _ledger_repo(
        tmp_path,
        classification_branch=_CLASSIFICATION_BASE_TEXT,
        classification_main="# header\nship     3  a.txt\ndrop     1  secrets/\n",
    )
    assert (repo / "a.txt").read_text() == "changed\n", "fixture: code landed"
    assert await default_branch_shipped(str(repo), "feature", "main") is True
    landed = await pr_watcher.branch_landed_commit(str(repo), "feature", "main")
    assert landed is not None


async def test_a_classification_pattern_change_is_still_not_shipped(tmp_path):
    """CONTROL. The `drop` rule's PATTERN changes (`secrets/` ->
    `secrets/keys/`), not just a count — the exact shape that once hid an
    unlanded drop. Still not shipped."""
    repo = _ledger_repo(
        tmp_path,
        classification_branch=_CLASSIFICATION_BASE_TEXT,
        classification_main="# header\nship     2  a.txt\ndrop     1  secrets/keys/\n",
    )
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_classification_added_or_removed_rule_is_still_not_shipped(tmp_path):
    """CONTROL. The branch adds a THIRD rule the landing never took — a real
    decision, not a count re-derivation. Still not shipped."""
    repo = _ledger_repo(
        tmp_path,
        classification_branch=_CLASSIFICATION_BASE_TEXT + "ship     5  extra/\n",
        classification_main=_CLASSIFICATION_BASE_TEXT,
    )
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_classification_comment_change_is_still_not_shipped(tmp_path):
    """CONTROL, fail-closed: only the `#` header differs (no rule line
    changes at all). Only count lines are ever forgiven — a comment edit is
    not."""
    repo = _ledger_repo(
        tmp_path,
        classification_branch=_CLASSIFICATION_BASE_TEXT,
        classification_main="# updated header\nship     2  a.txt\ndrop     1  secrets/\n",
    )
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_classification_lookalike_path_is_not_forgiven(tmp_path):
    """CONTROL. `docs/EXPORT_CLASSIFICATION.txt` — a lookalike BASENAME, not
    the exact repo-root path — diverges between the two sides. The exact-path
    doctrine means this is never eligible for the count-only forgiveness, so
    it must still block, same as the real ledger's own lookalike-path test."""
    repo = _ledger_repo(tmp_path, extra_ledger="docs/EXPORT_CLASSIFICATION.txt")
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_count_only_classification_never_excuses_a_real_unlanded_path(tmp_path):
    """CONTROL. The same count-only classification divergence as the red-first
    case, PLUS one real file (`b.txt`) that never landed. The classification
    forgiveness only ever fires when it is the SOLE residue; a real unlanded
    path must still block regardless."""
    repo = _ledger_repo(
        tmp_path,
        classification_branch=_CLASSIFICATION_BASE_TEXT,
        classification_main="# header\nship     3  a.txt\ndrop     1  secrets/\n",
        extra_branch_file="b.txt",
    )
    assert not (repo / "b.txt").exists(), "fixture: b.txt must not be on base"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_branch_that_deletes_the_classification_is_not_shipped(tmp_path):
    """CONTROL. The branch removes `EXPORT_CLASSIFICATION.txt` outright — a
    real decision (stop classifying at all), not a count re-derivation.
    `git show <branch>:EXPORT_CLASSIFICATION.txt` fails on that side, so
    `_classification_decisions` returns ``None`` and the divergence is never
    settled."""
    repo = _make_repo(tmp_path)
    (repo / "RELEASE_MANIFEST.txt").write_text("AAA  a.txt\n")
    (repo / "EXPORT_CLASSIFICATION.txt").write_text(_CLASSIFICATION_BASE_TEXT)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add the generated ledgers")

    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    (repo / "RELEASE_MANIFEST.txt").write_text("BBB  a.txt\n")
    _git(repo, "rm", "-q", "EXPORT_CLASSIFICATION.txt")
    _git(repo, "commit", "-m", "feature: change a.txt, drop the classification")

    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("changed\n")
    (repo / "RELEASE_MANIFEST.txt").write_text("DDD  a.txt\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "squash: land a.txt, re-derive the manifest")

    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_ledger_noise_never_excuses_a_real_unlanded_path(tmp_path):
    """The control that stops the exclusion from becoming 'ignore the diff':
    the same ledger divergence, plus ONE real file that never landed."""
    repo = _ledger_repo(tmp_path, extra_branch_file="b.txt")
    assert not (repo / "b.txt").exists(), "fixture: b.txt must not be on base"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_the_exclusion_is_one_exact_path_not_a_name_pattern(tmp_path):
    """A file that merely LOOKS like the ledger is not it. The exclusion is a
    frozenset of ONE exact repo-root path; anything matched by basename or by
    glob would let real content through under a lookalike name."""
    repo = _ledger_repo(tmp_path, extra_ledger="docs/RELEASE_MANIFEST.txt")
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_real_file_moved_onto_the_ledger_name_is_not_shipped(tmp_path):
    """The exclusion must not become a laundering channel through git's RENAME
    DETECTION — the reason `_unsettled_paths` passes `--no-renames`.

    `git diff --name-only` pairs a deletion with an ADDITION and prints the
    DESTINATION ALONE. A branch that moves a real file onto the ledger's name
    (possible whenever the ledger is absent on base) therefore presents as a
    diff touching only the excluded path, and the deletion of the real file —
    work that never landed — disappears from the comparison. Measured both
    ways on the same trees:

        git diff --name-only        -> RELEASE_MANIFEST.txt
        git diff --name-only --no-renames
                                    -> RELEASE_MANIFEST.txt, keep.py

    Only the second is the question this function is asking.
    """
    repo = _make_repo(tmp_path)
    (repo / "keep.py").write_text("def keep():\n    return 42\n# distinctive\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add a real file (no RELEASE_MANIFEST yet)")
    _git(repo, "checkout", "-b", "feature")
    # Destination is an ADD, which is what lets rename detection pair them.
    _git(repo, "mv", "keep.py", "RELEASE_MANIFEST.txt")
    _git(repo, "commit", "-m", "move a real file onto the ledger name")
    _git(repo, "checkout", "main")

    assert (repo / "keep.py").exists(), "fixture: base still carries the real file"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


# --------------------------------------------------------------------------- #
# The modify/delete channel (review finding, 2026-08-11). `merge-tree` resolves
# a modify/delete conflict by KEEPING THE MODIFIED SIDE, so for a branch whose
# outstanding work is a DELETION of a file base has since modified, the merged
# tree IS the tip's tree — in both directions — and the residual diff is empty.
# Measured on the shape below:
#
#   rc=1
#   100644 3367afd… 1  dead.py
#   100644 89710fb… 2  dead.py
#   CONFLICT (modify/delete): dead.py deleted in feature and modified in main.
#     Version main of dead.py left in tree.
#   git diff --name-only <tip_tree> <merged>  ->  (empty)
#
# The tree difference cannot see it; the CONFLICTED PATH section can. This is
# why containment gates on both. `merge-tree` reports a conflict for every
# modify/delete, so GitHub's CONFLICTING actively SELECTS for this shape, and
# "remove the deprecated module" is an ordinary task.
# --------------------------------------------------------------------------- #


def _modify_delete_repo(tmp_path, *, land_the_addition=False):
    """Base modifies a file the branch DELETES — merge-tree keeps base's copy."""
    repo = _make_repo(tmp_path)
    (repo / "dead.py").write_text("old\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add the module the branch will remove")

    _git(repo, "checkout", "-b", "feature")
    _git(repo, "rm", "-q", "dead.py")
    if land_the_addition:
        (repo / "live.py").write_text("the replacement\n")
        _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "remove the deprecated module")

    _git(repo, "checkout", "main")
    (repo / "dead.py").write_text("old\n# main touched this\n")
    if land_the_addition:
        # HALF landed: the addition squashed onto main, the deletion did not.
        (repo / "live.py").write_text("the replacement\n")
        _git(repo, "add", "-A")
    _git(repo, "commit", "-am", "main modifies dead.py (and takes the addition)")
    return repo


async def test_an_unlanded_deletion_of_a_file_base_modified_is_not_shipped(tmp_path):
    """(a) The attack shape, at the probe. The branch's whole deliverable is
    the removal of `dead.py`; it is still on base; containment must say no."""
    repo = _modify_delete_repo(tmp_path)
    assert (repo / "dead.py").exists(), "fixture: base still carries the file"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_half_landed_delete_takes_a_rebase_round_not_a_completion(
        tmp_path, store):
    """(b) End to end through the CONFLICTING rung: the addition landed, the
    deletion did not. Completing here marks the task DONE with `dead.py` still
    on main — the all-or-nothing property, falsified."""
    repo = _modify_delete_repo(tmp_path, land_the_addition=True)
    t = await _approval_task(store, repo)
    w = await _conflicting_watcher(store)

    assert await w._check_open_pr(t) == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert fresh.context["pr_conflict_rounds"] == 1
    assert (repo / "dead.py").exists(), "the deletion really is outstanding"


async def test_an_unparseable_conflict_section_is_cannot_tell(tmp_path, monkeypatch):
    """Fail CLOSED on an output shape this code does not understand.

    The conflicted-path section is parsed positionally
    (`<mode> <object> <stage>\\t<path>`), so a future git that reshapes it, or
    any output the parser cannot read, must yield "cannot tell" — never
    "nothing left to contribute". The repo underneath genuinely IS shipped, so
    a `False` here can only come from the guard.

    The substituted tree is the tip's REAL tree, not a made-up OID. With a
    bogus one the subsequent `git diff` fails and returns None by itself, which
    masks the guard entirely — a mutant that skips the malformed field instead
    of failing closed survived that version of this test.
    """
    repo = _squash_merge_repo(tmp_path)
    assert await default_branch_shipped(str(repo), "feature", "main") is True
    tip_tree = _git_out(repo, "rev-parse", "main^{tree}").strip()
    assert tip_tree, "fixture: need a real tree so the diff below succeeds"

    real = pr_watcher._git_rc

    async def fake_git(repo_path, *args):
        if args[:2] == ("merge-tree", "--write-tree"):
            # A valid tree (residue would be EMPTY) beside a field the parser
            # cannot read: the malformed section alone decides the answer.
            return 1, f"{tip_tree}\0no-tab-here\0"
        return await real(repo_path, *args)

    monkeypatch.setattr(pr_watcher, "_git_rc", fake_git)
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_wake_delegates_to_shared_shipped_helper(tmp_path, store, monkeypatch):
    """One check, both callers (the resume/restart dispatch gate in
    `core/scheduler.py` is the other). `_complete_if_content_landed` must be a
    thin delegate to `blockers.shipped.complete_if_content_landed` — never a
    re-implementation the scheduler's copy can drift from — so driving the
    existing CLOSED rung here must call through the SAME module-level
    function `wake.py` binds as `_complete_landed`."""
    from no_human.blockers import wake as wake_module

    repo = _squash_merge_repo(tmp_path)
    t = await _approval_task(store, repo)
    calls = []
    real = wake_module._complete_landed

    async def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return await real(*args, **kwargs)

    monkeypatch.setattr(wake_module, "_complete_landed", spy)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    out = await w._check_open_pr(t)

    assert out == "shipped_pr_closed"
    assert len(calls) == 1, "the CLOSED rung must call the shared helper, not a copy"
