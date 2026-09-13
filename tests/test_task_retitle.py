"""A filed task's title can now be corrected (`nh task retitle`) — until now
there was no rename/retitle/edit verb, and a title measured wrong at filing
time propagated as-is to the board, `nh task show`, the PR title, and the
commit subject, with no way to withdraw the false claim short of
`nh task cancel` (which throws away attempt history and the open PR).

Fix (in `no_human.cli.commands`): a new `@task.command("retitle")`.
  - State gate: refused outside `_RETITLE_SAFE_STATES` — a running attempt's
    coder/reviewer prompts and its eventual commit subject both read
    `task.title` live (`Orchestrator._commit_message` / `commit_subject`),
    so a mid-attempt retitle would desynchronise the prompt an attempt was
    given from the subject it commits under.
  - PR gate: an open PR's title must not silently disagree with the task's.
    Refused unless `--update-pr` is passed, in which case the PR's title is
    updated FIRST (via the existing `vcs.comment_poster.set_pr_title`) and
    only written to the DB once that succeeds — a forge failure leaves both
    titles agreeing on the OLD value.
  - `--description`/`--criteria` are declared-and-refused: description and
    acceptance criteria are graded artifacts, so retitle must stay title-only
    and must not become a path to re-scope a task underneath a running
    attempt.
  - Every successful retitle writes a `human_retitle` event carrying the
    prior title, so the correction is auditable.

Idiom copied verbatim from `tests/test_task_cancel_relabel.py`: sync tests,
each owning its own `asyncio.run`, `_bootstrap` patched via
`mock.patch.object`, real `CliRunner` invocations through the `task` click
group. The `set_pr_title` monkeypatch follows the pattern already used in
`tests/test_abandoned_draft_closed.py`.
"""

from __future__ import annotations

import asyncio
import unittest.mock as mock

from click.testing import CliRunner

from no_human.api.models import TaskSummaryOut
from no_human.api.title_short import title_short
from no_human.cli.commands import task
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus, commit_subject

# --------------------------------------------------------------------------- #
# CLI plumbing (copied from tests/test_task_cancel_relabel.py)              #
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
            events = await store.list_events(task_id)
            return t, events
    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Task fixtures                                                              #
# --------------------------------------------------------------------------- #

async def _seed(db, *, status, title="old title", pr_watch=None,
                 external_id=None, description="do the thing",
                 acceptance_criteria=None):
    async with Store(db) as store:
        t = Task.new(title, repo_path="/tmp/does-not-matter",
                      description=description, external_id=external_id)
        t.status = status
        t.acceptance_criteria = (
            acceptance_criteria if acceptance_criteria is not None
            else ["it does the thing"])
        t.context = {}
        if pr_watch:
            t.context["pr_watch"] = pr_watch
        await store.create_task(t)
        return t.id


def _recorder():
    calls = []

    def fake_set_pr_title(url, title):
        calls.append((url, title))
        return {"ok": True, "error": ""}

    return calls, fake_set_pr_title


def _patch_pr_state(monkeypatch, state):
    """Stub the forge-state check the retitle command runs on any resolved
    PR. Tests that only care about the resolve/refuse plumbing pin this to
    "OPEN" so they exercise the pre-existing open-PR behaviour deterministically
    (no real `gh`/`glab` subprocess call, whether or not those CLIs happen to
    be installed on the machine running the suite)."""
    import no_human.vcs.pr_watcher as pr_watcher

    async def fake_default_pr_state(ref):
        return state

    monkeypatch.setattr(pr_watcher, "default_pr_state", fake_default_pr_state)


# --------------------------------------------------------------------------- #
# AC1 — retitle works; board + `task show` read it back                      #
# --------------------------------------------------------------------------- #

def test_retitle_shows_through_task_show(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            title="old title"))

    result = _invoke(task, db, ["retitle", tid, "new title"])
    assert result.exit_code == 0, result.output

    shown = _invoke(task, db, ["show", tid])
    assert "title: new title" in shown.output, shown.output
    assert "title: old title" not in shown.output, shown.output


def test_retitle_shows_on_board_summary(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            title="old title"))

    result = _invoke(task, db, ["retitle", tid, "new title"])
    assert result.exit_code == 0, result.output

    t, _ = _task_state(db, tid)
    summary = TaskSummaryOut.from_task(t)
    assert summary.title == "new title"
    assert summary.title_short == title_short("new title")


# --------------------------------------------------------------------------- #
# AC2 — PR branch: refuse without --update-pr, update both with it           #
# --------------------------------------------------------------------------- #

def test_open_pr_without_flag_refuses_and_names_pr(tmp_path, monkeypatch):
    import no_human.vcs.comment_poster as cp
    calls, fake = _recorder()
    monkeypatch.setattr(cp, "set_pr_title", fake)
    _patch_pr_state(monkeypatch, "OPEN")

    db = tmp_path / "nh.db"
    url = "https://github.com/o/r/pull/7"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            title="old title", pr_watch=url))

    result = _invoke(task, db, ["retitle", tid, "new title"])

    assert result.exit_code != 0, result.output
    assert url in result.output, result.output
    assert calls == []

    t, events = _task_state(db, tid)
    assert t.title == "old title"
    assert not any(e.get("kind") == "human_retitle" for e in events), events


def test_open_pr_with_update_pr_retitles_both(tmp_path, monkeypatch):
    import no_human.vcs.comment_poster as cp
    calls, fake = _recorder()
    monkeypatch.setattr(cp, "set_pr_title", fake)
    _patch_pr_state(monkeypatch, "OPEN")

    db = tmp_path / "nh.db"
    url = "https://github.com/o/r/pull/7"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            title="old title", pr_watch=url,
                            external_id="JIRA-1"))

    result = _invoke(task, db, ["retitle", tid, "new title", "--update-pr"])

    assert result.exit_code == 0, result.output
    assert calls == [(url, commit_subject("new title", "JIRA-1", ""))], calls

    t, _ = _task_state(db, tid)
    assert t.title == "new title"


# --------------------------------------------------------------------------- #
# AC2 positive controls                                                      #
# --------------------------------------------------------------------------- #

def test_no_pr_is_allowed_without_flag(tmp_path, monkeypatch):
    import no_human.vcs.comment_poster as cp
    calls, fake = _recorder()
    monkeypatch.setattr(cp, "set_pr_title", fake)

    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            title="old title"))

    result = _invoke(task, db, ["retitle", tid, "new title"])

    assert result.exit_code == 0, result.output
    t, _ = _task_state(db, tid)
    assert t.title == "new title"
    assert calls == []


def test_update_pr_flag_is_a_noop_when_there_is_no_pr(tmp_path, monkeypatch):
    import no_human.vcs.comment_poster as cp
    calls, fake = _recorder()
    monkeypatch.setattr(cp, "set_pr_title", fake)

    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            title="old title"))

    result = _invoke(task, db, ["retitle", tid, "new title", "--update-pr"])

    assert result.exit_code == 0, result.output
    assert calls == []
    t, _ = _task_state(db, tid)
    assert t.title == "new title"


def test_forge_failure_refuses_and_leaves_titles_agreeing(tmp_path, monkeypatch):
    import no_human.vcs.comment_poster as cp

    def failing_set_pr_title(url, title):
        return {"ok": False, "error": "gh: 403"}

    monkeypatch.setattr(cp, "set_pr_title", failing_set_pr_title)
    _patch_pr_state(monkeypatch, "OPEN")

    db = tmp_path / "nh.db"
    url = "https://github.com/o/r/pull/7"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            title="old title", pr_watch=url))

    result = _invoke(task, db, ["retitle", tid, "new title", "--update-pr"])

    assert result.exit_code != 0, result.output
    assert url in result.output, result.output
    t, _ = _task_state(db, tid)
    assert t.title == "old title"


def test_db_write_failure_after_forge_success_names_both_and_disagreement(
    tmp_path, monkeypatch,
):
    """The forge write (`set_pr_title`) runs before the DB write
    (`update_task_title`). If the DB write then raises, the PR and the task
    row are left disagreeing — silence there would be worse than a crash;
    the command must say so by name (PR URL, new PR title, old task title)."""
    import no_human.vcs.comment_poster as cp
    calls, fake = _recorder()
    monkeypatch.setattr(cp, "set_pr_title", fake)
    _patch_pr_state(monkeypatch, "OPEN")

    import no_human.core.db as db_mod

    async def failing_update_task_title(self, task_id, title):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db_mod.Store, "update_task_title",
                         failing_update_task_title)

    db = tmp_path / "nh.db"
    url = "https://github.com/o/r/pull/7"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            title="old title", pr_watch=url,
                            external_id="JIRA-1"))

    result = _invoke(task, db, ["retitle", tid, "new title", "--update-pr"])

    assert result.exit_code != 0, result.output
    # The forge write DID happen — that's the whole problem.
    assert calls == [(url, commit_subject("new title", "JIRA-1", ""))], calls
    assert url in result.output, result.output
    assert "old title" in result.output, result.output
    assert commit_subject("new title", "JIRA-1", "") in result.output, (
        result.output)
    assert "disagree" in result.output, result.output


# --------------------------------------------------------------------------- #
# A merged/closed PR's title is history — the task's own title may still be  #
# corrected, but the forge must never be touched once its commits landed.    #
# --------------------------------------------------------------------------- #

def test_done_task_merged_pr_retitles_task_without_touching_forge(
    tmp_path, monkeypatch,
):
    import no_human.vcs.comment_poster as cp
    calls, fake = _recorder()
    monkeypatch.setattr(cp, "set_pr_title", fake)
    _patch_pr_state(monkeypatch, "MERGED")

    db = tmp_path / "nh.db"
    url = "https://github.com/o/r/pull/7"
    tid = asyncio.run(_seed(db, status=TaskStatus.DONE,
                            title="old title", pr_watch=url))

    result = _invoke(task, db, ["retitle", tid, "new title"])

    assert result.exit_code == 0, result.output
    assert calls == [], calls  # forge never touched

    t, _ = _task_state(db, tid)
    assert t.title == "new title"


def test_done_task_merged_pr_with_update_pr_refuses_and_names_merged(
    tmp_path, monkeypatch,
):
    import no_human.vcs.comment_poster as cp
    calls, fake = _recorder()
    monkeypatch.setattr(cp, "set_pr_title", fake)
    _patch_pr_state(monkeypatch, "MERGED")

    db = tmp_path / "nh.db"
    url = "https://github.com/o/r/pull/7"
    tid = asyncio.run(_seed(db, status=TaskStatus.DONE,
                            title="old title", pr_watch=url))

    result = _invoke(task, db, ["retitle", tid, "new title", "--update-pr"])

    assert result.exit_code != 0, result.output
    assert url in result.output, result.output
    assert "merged" in result.output.lower(), result.output
    assert "open" not in result.output.lower(), result.output
    assert calls == [], calls  # forge never touched

    t, _ = _task_state(db, tid)
    assert t.title == "old title"


def test_failed_task_closed_pr_with_update_pr_refuses_and_names_closed(
    tmp_path, monkeypatch,
):
    import no_human.vcs.comment_poster as cp
    calls, fake = _recorder()
    monkeypatch.setattr(cp, "set_pr_title", fake)
    _patch_pr_state(monkeypatch, "CLOSED")

    db = tmp_path / "nh.db"
    url = "https://github.com/o/r/pull/9"
    tid = asyncio.run(_seed(db, status=TaskStatus.FAILED,
                            title="old title", pr_watch=url))

    result = _invoke(task, db, ["retitle", tid, "new title", "--update-pr"])

    assert result.exit_code != 0, result.output
    assert url in result.output, result.output
    assert "closed" in result.output.lower(), result.output
    assert calls == [], calls

    t, _ = _task_state(db, tid)
    assert t.title == "old title"


def test_failed_task_closed_pr_without_flag_retitles_task(tmp_path, monkeypatch):
    """Positive control paired with the refusal above: without --update-pr
    the same closed-PR task's own title is still correctable."""
    import no_human.vcs.comment_poster as cp
    calls, fake = _recorder()
    monkeypatch.setattr(cp, "set_pr_title", fake)
    _patch_pr_state(monkeypatch, "CLOSED")

    db = tmp_path / "nh.db"
    url = "https://github.com/o/r/pull/9"
    tid = asyncio.run(_seed(db, status=TaskStatus.FAILED,
                            title="old title", pr_watch=url))

    result = _invoke(task, db, ["retitle", tid, "new title"])

    assert result.exit_code == 0, result.output
    assert calls == [], calls

    t, _ = _task_state(db, tid)
    assert t.title == "new title"


# --------------------------------------------------------------------------- #
# F1 regression — a PR recorded ONLY via task_events (no pr_watch, no        #
# attempts.pr_url) must still be found by the PR gate. `resolve_task_pr`     #
# alone misses this shape (the 8c8b36b5 incident `task_pr.py` documents);    #
# `task_has_pr_evidence` is the belt-and-braces check every sibling site     #
# already uses, and is what the retitle command must use too.               #
# --------------------------------------------------------------------------- #

def test_pr_recorded_only_via_event_still_refuses_without_flag(
    tmp_path, monkeypatch,
):
    import no_human.vcs.comment_poster as cp
    calls, fake = _recorder()
    monkeypatch.setattr(cp, "set_pr_title", fake)
    _patch_pr_state(monkeypatch, "OPEN")

    db = tmp_path / "nh.db"
    url = "https://github.com/o/r/pull/42"

    async def _seed_event_only():
        async with Store(db) as store:
            t = Task.new("old title", repo_path="/tmp/does-not-matter",
                          description="do the thing")
            t.status = TaskStatus.AWAITING_APPROVAL
            t.acceptance_criteria = ["it does the thing"]
            t.context = {}
            await store.create_task(t)
            await store.save_events(t.id, [{
                "kind": "pr_draft", "pr_url": url, "text": "opened draft PR",
            }])
            return t.id

    tid = asyncio.run(_seed_event_only())

    result = _invoke(task, db, ["retitle", tid, "new title"])

    assert result.exit_code != 0, result.output
    assert url in result.output, result.output
    assert calls == []

    t, _ = _task_state(db, tid)
    assert t.title == "old title"


# --------------------------------------------------------------------------- #
# F2 regression — a stale in-memory Task handle must not revert a retitle    #
# that landed while the handle was in flight. `update_task`/                #
# `update_task_columns` both key their `title` write off `updated_at`: the  #
# row's own title wins whenever the row moved on since this handle last     #
# synced (see `core/db.py`'s `update_task` docstring).                      #
# --------------------------------------------------------------------------- #

def test_stale_handle_update_task_does_not_revert_a_landed_retitle(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            title="old title"))

    async def _load():
        async with Store(db) as store:
            return await store.get_task(tid)  # snapshot BEFORE the retitle
    stale = asyncio.run(_load())

    result = _invoke(task, db, ["retitle", tid, "corrected title"])
    assert result.exit_code == 0, result.output

    async def _stale_write():
        async with Store(db) as store:
            await store.update_task(stale)

    asyncio.run(_stale_write())

    t, _ = _task_state(db, tid)
    assert t.title == "corrected title", t.title


def test_stale_handle_update_task_columns_does_not_revert_a_landed_retitle(
    tmp_path,
):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            title="old title"))

    async def _load():
        async with Store(db) as store:
            return await store.get_task(tid)
    stale = asyncio.run(_load())

    result = _invoke(task, db, ["retitle", tid, "corrected title"])
    assert result.exit_code == 0, result.output

    async def _stale_write():
        async with Store(db) as store:
            await store.update_task_columns(stale)

    asyncio.run(_stale_write())

    t, _ = _task_state(db, tid)
    assert t.title == "corrected title", t.title


# --------------------------------------------------------------------------- #
# AC3 — description/acceptance criteria are NOT editable by this path        #
# --------------------------------------------------------------------------- #

def test_description_edit_refused_names_graded_artifacts(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            description="original description"))

    result = _invoke(task, db,
                      ["retitle", tid, "new title", "--description", "hacked"])

    assert result.exit_code != 0, result.output
    assert "graded" in result.output, result.output
    assert ("re-scope" in result.output
            or "acceptance criteria" in result.output), result.output

    t, _ = _task_state(db, tid)
    assert t.description == "original description"
    assert t.title == "old title"


def test_criteria_edit_refused_names_graded_artifacts(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            acceptance_criteria=["original criterion"]))

    result = _invoke(task, db,
                      ["retitle", tid, "new title", "--criteria", "hacked"])

    assert result.exit_code != 0, result.output
    assert "graded" in result.output, result.output
    assert ("re-scope" in result.output
            or "acceptance criteria" in result.output), result.output

    t, _ = _task_state(db, tid)
    assert t.acceptance_criteria == ["original criterion"]
    assert t.title == "old title"


# --------------------------------------------------------------------------- #
# AC4 — the correction is recorded as an auditable event                     #
# --------------------------------------------------------------------------- #

def test_retitle_records_event_with_prior_title(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_APPROVAL,
                            title="old"))

    result = _invoke(task, db, ["retitle", tid, "new"])
    assert result.exit_code == 0, result.output

    _, events = _task_state(db, tid)
    retitle_events = [e for e in events if e.get("kind") == "human_retitle"]
    assert len(retitle_events) == 1, events
    ev = retitle_events[0]
    assert ev["prior_title"] == "old"
    assert ev["new_title"] == "new"
    assert ev["prior_status"] == "awaiting_approval"


# --------------------------------------------------------------------------- #
# State gate — refused while an attempt is running, allowed while parked     #
# --------------------------------------------------------------------------- #

def test_retitle_refused_while_implementing_names_prompt_and_commit_subject(
    tmp_path,
):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.IMPLEMENTING,
                            title="old title"))

    result = _invoke(task, db, ["retitle", tid, "new title"])

    assert result.exit_code != 0, result.output
    assert "prompt" in result.output, result.output
    assert "commit subject" in result.output, result.output

    t, _ = _task_state(db, tid)
    assert t.title == "old title"


def test_retitle_refused_for_compound_parent_with_a_true_reason(tmp_path):
    """`compound_parent` is a legacy, nothing-creates-it-anymore status
    (`core/task.py`) — no attempt runs and no commit subject is ever built
    against a parent row, so the generic "running attempt" wording used for
    CONTEXT/PLANNING/IMPLEMENTING/REVIEWING/TESTING would be false here.
    The refusal must say something true of what was actually checked, not
    borrow that reason."""
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.COMPOUND_PARENT,
                            title="old title"))

    result = _invoke(task, db, ["retitle", tid, "new title"])

    assert result.exit_code != 0, result.output
    assert "compound_parent" in result.output, result.output
    # Must NOT claim an attempt is running against a parent row.
    assert "running attempt" not in result.output, result.output

    t, _ = _task_state(db, tid)
    assert t.title == "old title"


def test_retitle_allowed_when_paused(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.AWAITING_INPUT,
                            title="old title"))

    result = _invoke(task, db, ["retitle", tid, "new title"])

    assert result.exit_code == 0, result.output
    t, _ = _task_state(db, tid)
    assert t.title == "new title"


# --------------------------------------------------------------------------- #
# `commit_subject` pins the three `_commit_message` shapes                   #
# --------------------------------------------------------------------------- #

def test_commit_subject_helper_matches_orchestrator():
    # No external_id, no prefix: title passes through unchanged.
    assert commit_subject("fix the bug", None, "") == "fix the bug"

    # external_id not already in the title: prepended.
    assert (commit_subject("fix the bug", "JIRA-1", "")
            == "JIRA-1: fix the bug")

    # external_id already carried by the title: not doubled.
    assert (commit_subject("JIRA-1: fix the bug", "JIRA-1", "")
            == "JIRA-1: fix the bug")

    # A configured commit prefix goes in front of everything else.
    assert (commit_subject("fix the bug", "JIRA-1", "[nh] ")
            == "[nh] JIRA-1: fix the bug")
