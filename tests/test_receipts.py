"""Ground-truth delivery receipts (D2 #1, watchtower-inspired).

"The adapter said ok" is not ground truth — the commit_paths incident shipped
PRs with coder-created files silently missing past EVERY gate. A receipt
verifies the side effect against the forge itself: the PR's file list must
cover the coder-touched files and its head must be the pushed SHA. Only a
VERIFIED mismatch is `lost` (fails the attempt); a flaky forge read is
`unverifiable` (advisory — network trouble must never fail good work).
"""

from __future__ import annotations

import json

from no_human.vcs.receipts import Receipt, verify_pr_receipt


def _gh(files, head, rc=0):
    payload = json.dumps({
        "files": [{"path": p} for p in files],
        "headRefOid": head,
    })

    def run(args, **kw):
        class P:
            returncode = rc
            stdout = payload
            stderr = "" if rc == 0 else "boom"
        return P()
    return run


def test_landed_when_files_covered_and_sha_matches(tmp_path):
    r = verify_pr_receipt(
        tmp_path, "https://x/pr/1",
        expected_files={"src/a.py", "tests/test_a.py"},
        local_sha="abc123",
        runner=_gh(["src/a.py", "tests/test_a.py", "docs/extra.md"], "abc123"))
    assert r.status == "landed"


def test_lost_when_a_coder_file_is_missing_from_the_pr(tmp_path):
    """The commit_paths incident class: coder created it, the PR lacks it."""
    r = verify_pr_receipt(
        tmp_path, "https://x/pr/1",
        expected_files={"src/a.py", "web/favicon.js"},
        local_sha="abc123",
        runner=_gh(["src/a.py"], "abc123"))
    assert r.status == "lost"
    assert "web/favicon.js" in r.detail


def test_lost_when_head_sha_diverges(tmp_path):
    r = verify_pr_receipt(
        tmp_path, "https://x/pr/1",
        expected_files={"src/a.py"}, local_sha="abc123",
        runner=_gh(["src/a.py"], "OTHER"))
    assert r.status == "lost"
    assert "head" in r.detail.lower()


def test_unverifiable_on_forge_error_is_advisory_not_lost(tmp_path):
    r = verify_pr_receipt(
        tmp_path, "https://x/pr/1",
        expected_files={"src/a.py"}, local_sha="abc123",
        runner=_gh([], "", rc=1))
    assert r.status == "unverifiable"


def test_expected_files_outside_repo_scope_are_ignored(tmp_path):
    """The coder edits scratch/absolute paths too; only repo-relative files
    can appear in a PR file list."""
    r = verify_pr_receipt(
        tmp_path, "https://x/pr/1",
        expected_files={"src/a.py", "/tmp/scratch/notes.md"},
        local_sha="abc123",
        runner=_gh(["src/a.py"], "abc123"))
    assert r.status == "landed"


def test_capped_file_list_is_unverifiable_not_lost(tmp_path):
    """gh caps the files array — absence from a capped list proves nothing."""
    many = [f"src/f{i}.py" for i in range(100)]
    r = verify_pr_receipt(
        tmp_path, "https://x/pr/1",
        expected_files={"src/zz_not_in_first_page.py"},
        local_sha="abc", runner=_gh(many, "abc"))
    assert r.status == "unverifiable"


# --------------------------- orchestrator wiring --------------------------- #

from no_human.core.task import Task, TaskStatus  # noqa: E402
from no_human.core.orchestrator import Orchestrator  # noqa: E402
from no_human.notify.slack import SlackNotifier  # noqa: E402

from .test_e2e_orchestrator import (  # noqa: F401,E402
    FakeBackend,
    _config,
    bare_repo,
)


async def test_lost_receipt_escalates_instead_of_reporting_success(
    bare_repo, tmp_path, store, monkeypatch
):
    """A PR the forge disagrees with must never reach AWAITING_APPROVAL."""
    import no_human.core.orchestrator as orch_mod

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\ndef test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    from no_human.vcs.receipts import Receipt
    monkeypatch.setattr(
        orch_mod, "verify_pr_receipt",
        lambda *a, **kw: Receipt("pr_open", "https://x/pr/9", "lost",
                                 "PR is missing calc.py"))

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert "forge" in (outcome.detail or "").lower() or "missing" in (outcome.detail or "")
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"
    assert "receipt" in (attempts[-1]["failure_reason"] or "").lower()


async def test_landed_receipt_proceeds_to_gate(
    bare_repo, tmp_path, store, monkeypatch
):
    import no_human.core.orchestrator as orch_mod
    from no_human.vcs.receipts import Receipt

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\ndef test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    monkeypatch.setattr(
        orch_mod, "verify_pr_receipt",
        lambda *a, **kw: Receipt("pr_open", "https://x/pr/9", "landed", "ok"))

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL


# ------------------------- B2 #5 plan-usage capture ------------------------ #


async def test_plan_columns_exist_on_attempts(store):
    cur = await store.db.execute("PRAGMA table_info(attempts)")
    cols = {row["name"] for row in await cur.fetchall()}
    assert {"plan_tokens_used", "plan_cache_read_tokens",
            "plan_cache_creation_tokens"} <= cols



async def test_utility_columns_exist_on_attempts(store):
    cur = await store.db.execute("PRAGMA table_info(attempts)")
    cols = {row["name"] for row in await cur.fetchall()}
    assert {"utility_tokens_used", "utility_cache_read_tokens",
            "utility_cache_creation_tokens"} <= cols


def test_reverted_edit_is_not_lost_when_absent_from_the_pr(tmp_path):
    """Review #1: a file the coder edited then reverted has no net diff, so it
    is legitimately absent from the PR — requiring it would escalate a good,
    review-passed PR. With committed_files given, only real changes are
    required."""
    r = verify_pr_receipt(
        tmp_path, "https://x/pr/1",
        expected_files={"src/a.py", "src/scratch_reverted.py"},
        committed_files={"src/a.py"},   # only a.py actually changed
        local_sha="abc", runner=_gh(["src/a.py"], "abc"))
    assert r.status == "landed"


def test_a_genuinely_missing_committed_file_is_still_lost(tmp_path):
    """The fix must not blind the guard: a file that DID change but is absent
    from the PR (the commit_paths incident) is still lost."""
    r = verify_pr_receipt(
        tmp_path, "https://x/pr/1",
        expected_files={"src/a.py", "web/favicon.js"},
        committed_files={"src/a.py", "web/favicon.js"},
        local_sha="abc", runner=_gh(["src/a.py"], "abc"))
    assert r.status == "lost"
    assert "web/favicon.js" in r.detail


async def test_plan_usage_lands_on_the_attempt_row_end_to_end(bare_repo, tmp_path, store):
    """Review #4: the pop-once tests were tautologies (they popped a dict and
    asserted the pop returned it). This drives a REAL attempt: planning burn
    noted during the run must land in the attempt's plan_* columns exactly
    once."""
    import no_human.core.orchestrator as orch_mod
    from types import SimpleNamespace

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\ndef test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    # Simulate a planner having run before the coder session.
    orch._note_plan_usage(SimpleNamespace(tokens_used=111, cache_read_tokens=999,
                                          cache_creation_tokens=3))
    monkey_receipt = SimpleNamespace  # noqa: F841
    import unittest.mock as mock
    from no_human.vcs.receipts import Receipt
    with mock.patch.object(orch_mod, "verify_pr_receipt",
                           return_value=Receipt("pr_open", "u", "landed", "ok")):
        t = Task.new("add mul()", repo_path=str(bare_repo))
        await store.create_task(t)
        await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["plan_tokens_used"] == 111
    assert attempts[-1]["plan_cache_read_tokens"] == 999
    # popped: a hypothetical retry would not re-book it
    assert getattr(orch, "_plan_usage", None) is None
