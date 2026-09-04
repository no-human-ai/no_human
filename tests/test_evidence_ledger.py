"""The proof ledger (#23, second half): every claim in the PR body links to a
file no_human committed on the task's `nh-evidence/<task-id>` side branch,
pinned by commit SHA so the link can never be rewritten under the body."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from no_human.core import evidence_ledger
from no_human.core.pr_evidence import PrEvidence
from no_human.vcs.git import GitRepo


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A working clone with a bare origin, on branch `task`, one commit in."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hello\n")
    _git(work, "add", "."); _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "-b", "task")
    return GitRepo(work)


def _ls_tree(repo: GitRepo, sha: str) -> set[str]:
    return set(_git(Path(repo.path), "ls-tree", "-r", "--name-only", sha).splitlines())


def test_deliver_commits_files_on_the_side_branch_and_returns_its_sha(repo):
    sha = evidence_ledger.deliver(
        repo, "deadbeefcafe", {"verification.md": b"# log\n", "tests.md": b"# t\n"},
        "evidence ledger for deadbeef")
    assert len(sha) == 40
    assert _git(Path(repo.path), "rev-parse", "nh-evidence/deadbeefcafe") == sha
    assert _git(Path(repo.path), "rev-parse", "origin/nh-evidence/deadbeefcafe") == sha, (
        "the side branch must be pushed: a link into it is dead otherwise")
    assert {".nh-evidence/deadbeefcafe/verification.md",
            ".nh-evidence/deadbeefcafe/tests.md"} <= _ls_tree(repo, sha)
    assert repo.current_branch() == "task", "the working tree must be left on the task branch"
    assert ".nh-evidence/deadbeefcafe/tests.md" not in _ls_tree(repo, repo.head_sha()), (
        "the ledger must never be committed on the task branch itself")


def test_a_second_delivery_stacks_on_the_existing_side_branch(repo):
    """UI evidence opens the branch first; the ledger commit must land ON TOP
    of it, not recreate the branch from the task tip and drop the shots."""
    first = evidence_ledger.deliver(repo, "t1", {"shot.png": b"png"}, "UI evidence")
    second = evidence_ledger.deliver(repo, "t1", {"tests.md": b"# t\n"}, "ledger")
    assert first != second
    assert _git(Path(repo.path), "rev-parse", f"{second}^") == first
    assert {".nh-evidence/t1/shot.png", ".nh-evidence/t1/tests.md"} <= _ls_tree(repo, second)


def test_deliver_with_nothing_to_commit_returns_empty_and_touches_no_branch(repo):
    assert evidence_ledger.deliver(repo, "t2", {}, "nothing") == ""
    assert repo.current_branch() == "task"
    assert "nh-evidence/t2" not in _git(Path(repo.path), "branch", "--list", "nh-evidence/*")


def test_blob_url_pins_the_commit_not_the_branch():
    url = evidence_ledger.blob_url("acme", "widget", "a" * 40, "t1", "tests.md")
    assert url == f"https://github.com/acme/widget/blob/{'a' * 40}/.nh-evidence/t1/tests.md"
    assert "nh-evidence/t1/" in url and "/blob/nh-evidence" not in url
    line = evidence_ledger.blob_url("acme", "widget", "a" * 40, "t1", "verification.md", line=12)
    assert line.endswith("/verification.md?plain=1#L12")
    assert evidence_ledger.blob_url("acme", "widget", "a" * 40, "t1", "a b/é.md").endswith(
        "/.nh-evidence/t1/a%20b/%C3%A9.md")


def _evidence(**kw) -> PrEvidence:
    base = {
        "repro": {"receipts": [], "observable": True},
        "tests": {"ran": True, "ok": True, "passed": 5, "failed": 0, "errors": 0},
        "review_verdict": {"rounds": 2, "verdict": "PASSED", "addressed": ["x"], "unmatched": False},
        "verifiers": [{"verifier_id": "v1", "passed": True, "files": 2}],
        "tamper": [{"verdict": "LEGITIMATE", "reason": "renamed test"}],
        "merge_policy": {"ready": True, "summary": "ready — 1 of 1 rules satisfied",
                         "source": "default", "problems": [], "policy_changed_in_diff": False,
                         "rules": [{"name": "review_passed", "passed": True, "detail": "d"}]},
    }
    base.update(kw)
    return PrEvidence(**base)


def test_render_files_writes_one_file_per_claim_source_each_saying_what_it_is():
    files = evidence_ledger.render_files(
        _evidence(), task_id="deadbeefcafe", head_sha="b" * 40,
        verification_md="## How I verified this\n- `pytest`\n",
        review_md="<!-- m -->\n## Independent review — PASSED\n| a |\n",
        assumptions_md="<details><summary>1 assumption</summary>\n- q\n</details>\n")
    assert set(files) == {"README.md", "verification.md", "review.md", "tests.md",
                          "verifiers.md", "tamper.md", "merge-policy.md", "assumptions.md"}
    for name, text in files.items():
        assert text.startswith("# "), name
        assert "deadbeef" in text and "b" * 40 in text, name
        assert "not model-authored" in text.lower(), (
            f"{name} must say what it is: a harness-captured record")
    assert "- `pytest`" in files["verification.md"]
    assert "## Independent review — PASSED" in files["review.md"]
    assert '"passed": 5' in files["tests.md"]
    assert '"verifier_id": "v1"' in files["verifiers.md"]
    assert "renamed test" in files["tamper.md"]
    assert "review_passed" in files["merge-policy.md"]
    assert "1 assumption" in files["assumptions.md"]
    assert "tests.md" in files["README.md"] and "verification.md" in files["README.md"]


def test_render_files_omits_a_file_for_a_gate_that_produced_nothing():
    files = evidence_ledger.render_files(
        _evidence(tests=None, verifiers=None, tamper=None, merge_policy=None,
                  review_verdict=None),
        task_id="t", head_sha="c" * 40, verification_md="log", review_md="",
        assumptions_md="")
    assert set(files) == {"README.md", "verification.md"}, (
        "a file for a gate that never ran would be a claim with nothing behind it")


def test_proof_urls_render_as_a_link_on_the_matching_row_only():
    ev = _evidence(proof_urls={"review": "https://x/review.md", "tests": "https://x/tests.md"})
    assert ev.proof("review") == " · [proof](https://x/review.md)"
    assert ev.proof("tests") == " · [proof](https://x/tests.md)"
    assert ev.proof("verifiers") == ""
    assert PrEvidence(repro=None).proof("review") == ""


def test_proof_url_keys_match_the_files_the_ledger_writes():
    files = evidence_ledger.render_files(
        _evidence(), task_id="t", head_sha="d" * 40, verification_md="l",
        review_md="r", assumptions_md="a")
    urls = evidence_ledger.proof_urls("acme", "widget", "e" * 40, "t", files)
    assert set(urls) == {"review", "tests", "verifiers", "tamper", "merge_policy",
                         "verification", "assumptions", "readme"}
    assert urls["merge_policy"].endswith("/.nh-evidence/t/merge-policy.md")
    assert all(("e" * 40) in u for u in urls.values())



def test_deliver_refuses_paths_that_escape_the_ledger_directory(repo):
    with pytest.raises(ValueError):
        evidence_ledger.deliver(repo, "t3", {"../x.md": b"x"}, "m")
    with pytest.raises(ValueError):
        evidence_ledger.deliver(repo, "t3", {"/etc/x.md": b"x"}, "m")


def test_deliver_refuses_a_dirty_tree_rather_than_sweeping_it_onto_the_side_branch(repo):
    """`commit_paths` stages modified tracked and untracked source files too;
    a stray edit would be published on the side branch and then dropped
    from the tree. The ledger stays out rather than doing that."""
    (Path(repo.path) / "README.md").write_text("changed\n")
    from no_human.vcs.git import GitError
    with pytest.raises(GitError):
        evidence_ledger.deliver(repo, "t4", {"tests.md": b"# t"}, "m")
    assert repo.current_branch() == "task"
    assert "nh-evidence/t4" not in _git(Path(repo.path), "branch", "--list", "nh-evidence/*")


def test_a_failed_push_leaves_the_tree_on_the_task_branch_and_raises(repo, monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("remote refused")
    monkeypatch.setattr(GitRepo, "push", boom)
    with pytest.raises(RuntimeError):
        evidence_ledger.deliver(repo, "t5", {"tests.md": b"# t"}, "m")
    assert repo.current_branch() == "task"
    assert ".nh-evidence" not in _git(Path(repo.path), "ls-tree", "-r", "--name-only", "HEAD")


def test_render_files_writes_no_review_file_when_the_rounds_judged_another_commit():
    files = evidence_ledger.render_files(
        _evidence(review_verdict={"rounds": 1, "verdict": "PASSED", "unmatched": True}),
        task_id="t", head_sha="a" * 40, verification_md="l", review_md="r", assumptions_md="")
    assert "review.md" not in files, "the body says no review judged this commit; a file would say otherwise"


def test_command_lines_skips_a_kind_whose_last_command_the_entry_cap_left_off_the_page():
    from no_human.core.orchestrator import Orchestrator
    rows = [{"command": "uv run ruff check src", "output_excerpt": "ok", "kind": "lint",
             "truncated": False, "output_bytes": 2}] + [
        {"command": f"uv run pytest -q -k t{i}", "output_excerpt": "1 passed", "kind": "test",
         "truncated": False, "output_bytes": 8} for i in range(45)]
    log = Orchestrator._verification_appendix(rows)
    assert "- `uv run ruff check src`" not in log, "premise: the 40-entry cap dropped the lint run"
    lines = evidence_ledger.command_lines(log, rows)
    assert "lint" not in lines, "no dead anchor for a command that is not on the page"
    assert log.split("\n")[lines["test"] - 1] == "- `uv run pytest -q -k t44`"


def test_command_lines_counts_newlines_the_way_github_does():
    log = "# h\n\n- `x`\noutput a\x85b\n- `uv run ruff check src`\n"
    rows = [{"command": "uv run ruff check src", "kind": "lint"}]
    assert evidence_ledger.command_lines(log, rows) == {"lint": 5}


# ══════ the body: every row links to its file; fold summaries link into the log ══ #

import sys as _sys

_sys.path.insert(0, str(Path(__file__).parent))
import test_pr_evidence as _P  # the body fixtures live there


def _urls() -> dict[str, str]:
    base = "https://github.com/acme/widget/blob/" + "f" * 40 + "/.nh-evidence/t1/"
    return {"review": base + "review.md", "tests": base + "tests.md",
            "verifiers": base + "verifiers.md", "merge_policy": base + "merge-policy.md",
            "verification": base + "verification.md",
            "verification:test": base + "verification.md?plain=1#L9",
            "verification:lint": base + "verification.md?plain=1#L12"}


def test_every_evidence_row_links_to_its_ledger_file(store, tmp_path):
    orch = _P._orch(store, tmp_path)
    task = _P._task()
    task.context["verifier_results"] = {"": [{"verifier_id": "v1", "passed": True, "files": 1}]}
    te = {"ran": True, "ok": True, "passed": 5, "failed": 0, "errors": 0}
    ev = orch._gather_evidence(task, test_evidence=te, receipts=_P._receipts(), head_sha="",
                               merge_policy={"ready": True, "summary": "ready — 1 of 1",
                                             "source": "default", "problems": [],
                                             "policy_changed_in_diff": False,
                                             "rules": [{"name": "r", "passed": True, "detail": "d"}]})
    from dataclasses import replace
    ev = replace(ev, proof_urls=_urls())
    body = orch._pr_body(task, _P._Commit(), _P._Result(), test_evidence=te,
                         receipts=_P._receipts(), evidence=ev)
    u = _urls()
    assert f"| Independent review | ✅ **PASSED** — 2 rounds · [proof]({u['review']}) |" in body
    assert f"0 errors · [proof]({u['tests']}) |" in body
    assert f"1 of 1 satisfied · [proof]({u['verifiers']}) |" in body
    assert f"ready — 1 of 1 · [proof]({u['merge_policy']}) |" in body
    assert body.count("[proof](") == 4, body
    assert body.count("| Tests | ✅ PASS") == 1


def test_without_a_ledger_the_body_is_byte_identical_to_before(store, tmp_path):
    orch = _P._orch(store, tmp_path)
    task = _P._task()
    te = {"ran": True, "ok": True, "passed": 5, "failed": 0, "errors": 0}
    body = orch._pr_body(task, _P._Commit(), _P._Result(), test_evidence=te,
                         receipts=_P._receipts())
    assert "[proof](" not in body and "full log</a>" not in body


def test_fold_summaries_link_into_the_log_on_the_commands_line():
    from no_human.core.orchestrator import Orchestrator
    ev = PrEvidence(repro=None, proof_urls=_urls())
    assert ev.log_anchors() == {"test": _urls()["verification:test"],
                                "lint": _urls()["verification:lint"]}
    section = Orchestrator._verification_section(
        _P._receipts(), task_id="deadbeef", anchors=ev.log_anchors())
    assert ('<code>uv run pytest tests/test_webhook_retry.py -q</code> · '
            f'<a href="{_urls()["verification:test"]}">full log</a></summary>') in section
    assert f'<a href="{_urls()["verification:lint"]}">full log</a>' in section
    plain = Orchestrator._verification_section(_P._receipts(), task_id="deadbeef")
    assert "full log</a>" not in plain


def test_command_lines_point_at_the_last_command_of_each_kind_in_the_rendered_log():
    from no_human.core.orchestrator import Orchestrator
    rows = [_P._MID_WORK_RECEIPT, _P._FINAL_RECEIPT, {
        "command": "uv run ruff check src", "output_excerpt": "ok", "kind": "lint",
        "truncated": False, "output_bytes": 2}]
    log = evidence_ledger.render_files(
        PrEvidence(repro={"receipts": rows, "observable": True}), task_id="t", head_sha="a" * 40,
        verification_md=Orchestrator._verification_appendix(rows), review_md="",
        assumptions_md="")["verification.md"]
    lines = evidence_ledger.command_lines(log, rows)
    assert set(lines) == {"test", "lint"}
    page = log.splitlines()
    assert page[lines["test"] - 1] == "- `uv run pytest tests/test_webhook_retry.py -q`"
    assert page[lines["lint"] - 1] == "- `uv run ruff check src`"
    # two identical pytest lines are on the page; the anchor is the LAST one
    assert lines["test"] == max(i + 1 for i, l in enumerate(page)
                                if l == "- `uv run pytest tests/test_webhook_retry.py -q`")


# ══════ end to end: the real `_finalize` delivers the ledger and the body links to it ══ #

from tests import test_ui_evidence_attempt_hook as _H

repo_env = _H.repo_env


async def test_finalize_delivers_the_ledger_and_the_body_links_to_it_at_the_commit(
        repo_env, tmp_path, store, monkeypatch):
    """A non-UI task through `run_task`: the side branch carries the ledger,
    every Evidence row links to a blob at the ledger COMMIT (never the
    branch), the fold summary links into the log, the task branch stays
    clean, and the working tree is left on the task branch."""
    from no_human.core import orchestrator as orch_mod
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.task import Task, TaskStatus
    from no_human.notify.slack import SlackNotifier

    def mutate(cwd):
        (Path(cwd) / "calc.py").write_text("def add(a, b):\n    return a + b  # ok\n")

    monkeypatch.setattr(GitRepo, "remote_url",
                        lambda self, remote="origin": "https://github.com/acme/widget.git")
    fake_open_pr, opens = _H._fake_open_pr()
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    cfg = _H._config(tmp_path)
    events: list = []
    orch = Orchestrator(store, cfg.data, _H.FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("comment the add", repo_path=str(repo_env["work"]))
    t.acceptance_criteria = ["add still adds"]
    await store.create_task(t)
    outcome = await orch.run_task(t)
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    branch = f"nh-evidence/{t.id}"
    ledger_events = [e for e in events if "ledger" in str(e).lower() or "advisory" in str(getattr(e, "kind", e)).lower()]
    sha = _H._git(repo_env["origin"], "rev-parse", branch, check=False).stdout.strip()
    assert len(sha) == 40, f"the ledger branch must be pushed to origin; events: {ledger_events}"
    names = _H._git(repo_env["origin"], "ls-tree", "-r", "--name-only", branch).stdout
    assert f".nh-evidence/{t.id}/README.md" in names, names
    assert f".nh-evidence/{t.id}/verification.md" in names, names
    body = opens[-1]["body"]
    prefix = f"https://github.com/acme/widget/blob/{sha}/.nh-evidence/{t.id}/"
    assert f"[proof]({prefix}tests.md)" in body, body
    assert f"[proof]({prefix}merge-policy.md)" in body, body
    assert "review.md" not in body and f".nh-evidence/{t.id}/review.md" not in names, (
        "no reviewer ran here (advisory gate): a review file would be a claim with nothing behind it")
    assert f"/blob/{branch}/" not in body, "a link must pin the commit, never the branch"
    assert "[proof](" in body.split("## Acceptance criteria")[0], "links live in the Evidence table"
    for line in body.splitlines():
        if line.startswith("| ") and "[proof](" in line:
            assert line.count("[proof](") == 1, line
    task_branch = opens[-1]["branch"]
    task_names = _H._git(repo_env["work"], "ls-tree", "-r", "--name-only", task_branch).stdout
    assert ".nh-evidence" not in task_names, task_names
    assert _H._git(repo_env["work"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == task_branch
    readme = _H._git(repo_env["origin"], "show", f"{branch}:.nh-evidence/{t.id}/README.md").stdout
    assert "not model-authored" in readme


def test_a_non_github_remote_leaves_the_evidence_unchanged(store, tmp_path, monkeypatch):
    orch = _P._orch(store, tmp_path)
    task = _P._task()
    ev = orch._gather_evidence(task, test_evidence={"ran": True, "ok": True, "passed": 1,
                                                   "failed": 0, "errors": 0})

    class _Repo:
        def remote_url(self, remote="origin"):
            return "https://gitlab.example.com/acme/widget.git"

        def has_changes(self):
            raise AssertionError("must not get as far as git")

    out = orch._deliver_evidence_ledger(_Repo(), task, ev, head_sha="a" * 40)
    assert out is ev and out.proof_urls == {}
    assert orch._deliver_evidence_ledger(None, task, ev, head_sha="a" * 40) is ev
