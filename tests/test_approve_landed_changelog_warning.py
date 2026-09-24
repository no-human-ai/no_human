"""`nh approve --landed` prints a non-blocking pointer to `nh changelog-check`
when the landed commit changes a user-visible surface and touches no
CHANGELOG.md. Never blocks: the landing must complete (task -> DONE) whether
or not the warning fires, and even if the check itself blows up.

Idiom and fixtures copied from tests/test_landed_override.py /
tests/test_approve.py (real temp git repos via subprocess, `_bootstrap`
patched the way tests/test_approve.py does) — reused directly rather than
duplicated, per this repo's established cross-test-file import convention.
"""

from __future__ import annotations

import asyncio
import unittest.mock as mock

from click.testing import CliRunner

from no_human.cli.commands import approve
from no_human.core.db import Store

from tests.test_landed_override import _git, _git_out, _make_repo, _seed  # noqa: F401 — fixtures re-exported on purpose

# --------------------------------------------------------------------------- #
# CLI plumbing (copied from tests/test_approve.py)                            #
# --------------------------------------------------------------------------- #

class _Cfg:
    db_path = None
    data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)


def _cfg(db_path):
    c = _Cfg()
    c.db_path = db_path
    return c


def _invoke(cmd, db, args):
    import no_human.cli.commands as cmd_mod
    with mock.patch.object(cmd_mod, "_bootstrap",
                           lambda require_auth=False: (_cfg(db), None)):
        return CliRunner().invoke(cmd, args)


def _task_state(db, task_id):
    async def _go():
        async with Store(db) as store:
            t = await store.get_task(task_id)
            return t
    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# fixtures                                                                    #
# --------------------------------------------------------------------------- #

def _commit(repo, touches, message):
    for path, content in touches:
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        _git(repo, "add", path)
    _git(repo, "commit", "-m", message)
    return _git_out(repo, "rev-parse", "HEAD")


def _seed_awaiting(db, repo):
    async def _go():
        async with Store(db) as store:
            t = await _seed(store, repo, base_branch="main", pr_branch="")
            return t.id
    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# tests                                                                       #
# --------------------------------------------------------------------------- #

def test_warning_when_landed_commit_has_no_changelog_entry(tmp_path):
    db = tmp_path / "nh.db"
    repo = _make_repo(tmp_path)
    sha = _commit(repo, [("src/no_human/feature.py", "x\n")],
                  "feat: ship the thing")
    tid = _seed_awaiting(db, repo)

    result = _invoke(approve, db,
                     [tid, "--landed", sha, "--because", "hand-verified landing"])

    assert result.exit_code == 0, result.output
    assert "override recorded" in result.output
    assert "note:" in result.output
    assert sha[:12] in result.output
    assert "nh changelog-check" in result.output
    assert _task_state(db, tid).status.value == "done"


def test_no_warning_when_the_commit_touches_the_changelog(tmp_path):
    db = tmp_path / "nh.db"
    repo = _make_repo(tmp_path)
    sha = _commit(repo, [("src/no_human/feature.py", "x\n"),
                          ("CHANGELOG.md", "- shipped the thing\n")],
                  "feat: ship the thing, documented")
    tid = _seed_awaiting(db, repo)

    result = _invoke(approve, db,
                     [tid, "--landed", sha, "--because", "hand-verified landing"])

    assert result.exit_code == 0, result.output
    assert "override recorded" in result.output
    assert "note:" not in result.output
    assert "changelog-check" not in result.output
    assert _task_state(db, tid).status.value == "done"


def test_no_warning_for_a_test_only_commit(tmp_path):
    db = tmp_path / "nh.db"
    repo = _make_repo(tmp_path)
    sha = _commit(repo, [("tests/test_feature.py", "x\n")],
                  "test: cover the thing")
    tid = _seed_awaiting(db, repo)

    result = _invoke(approve, db,
                     [tid, "--landed", sha, "--because", "hand-verified landing"])

    assert result.exit_code == 0, result.output
    assert "override recorded" in result.output
    assert "note:" not in result.output
    assert "changelog-check" not in result.output
    assert _task_state(db, tid).status.value == "done"


def test_warning_never_blocks_the_landing(tmp_path):
    """Even if the changelog check itself blows up, the landing already
    recorded above it must still stand — this is a courtesy note, not a
    gate (see the task's OUT OF SCOPE: no blocking gate)."""
    db = tmp_path / "nh.db"
    repo = _make_repo(tmp_path)
    sha = _commit(repo, [("src/no_human/feature.py", "x\n")],
                  "feat: ship the thing")
    tid = _seed_awaiting(db, repo)

    import no_human.vcs.changelog_gap as changelog_gap

    def _boom(*a, **k):
        raise RuntimeError("boom")

    with mock.patch.object(changelog_gap, "commit_needs_changelog", _boom):
        result = _invoke(approve, db,
                         [tid, "--landed", sha, "--because", "hand-verified landing"])

    assert result.exit_code == 0, result.output
    assert "override recorded" in result.output
    assert _task_state(db, tid).status.value == "done"
