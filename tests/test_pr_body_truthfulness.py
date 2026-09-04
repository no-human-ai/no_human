"""The PR body is the artifact a human actually reads — it must not mislead.

Every test here comes from an audit of REAL shipped PRs, and each names the PR
that motivated it. The failure mode is always the same shape: the body states
something a reader takes as fact about THIS change, and it is about something
else — the coder's internal monologue, the last commit instead of the branch, a
suite that never ran, another attempt's review verdict, or a PR that was
abandoned three attempts ago.
"""

import html as htmlmod
import os  # noqa: F401 — the `skipif` condition strings below evaluate it
import re
import shutil
import subprocess

import pytest

from no_human.blockers import Blocker
from no_human.blockers.taxonomy import BlockerCategory
from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo

# R2 drives the real `run_task`, which needs a repo with a pushable remote.
# Only the fixture is borrowed — `store` below is this module's own.
from .test_e2e_orchestrator import bare_repo  # noqa: F401


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


class _Commit:
    files_changed = 1
    insertions = 0
    deletions = 1
    sha = ""


class _Result:
    final_text = "Implemented the change and added a regression test for it."
    num_turns = 5


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo_with_two_commits(tmp_path):
    """A repo whose BRANCH is much bigger than its last commit — the C2 shape."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "seed.txt").write_text("seed\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "seed")
    _git(work, "checkout", "-qb", "feat/x")
    (work / "big.txt").write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
    (work / "seed.txt").write_text("seed\nmore\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "the bulk of the change")
    (work / "tiny.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "one last line")
    return work


# ─────────────────────────── C1: the monologue ────────────────────────────── #

WAITING = "I'll just wait for that notification."


def test_a_waiting_note_is_never_pasted_as_an_implementation_summary(store, tmp_path):
    """PR #105 (review PASSED) shipped exactly this line under
    "## Changes". It is the coder talking to itself, not a
    report — and rendered under that heading a human reads it as a claim about
    the change."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = WAITING
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert WAITING not in body
    assert "**No implementation summary was produced.**" in body
    assert "was not a report of the work" in body


def test_a_background_run_note_is_not_a_summary(store, tmp_path):
    """PRs #110 and #102."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = "Waiting for the full-suite background run to finish."
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "Waiting for the full-suite" not in body
    assert "**No implementation summary was produced.**" in body


def test_the_absent_summary_block_invents_nothing(store, tmp_path):
    """The fix must NOT synthesize a summary from the diff — that is the same
    lie in better prose. It may only name the absence."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = WAITING
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    section = body.split("## Changes\n", 1)[1].split("\n## ", 1)[0]
    assert "read the commits and the diff" in section
    assert "Nothing was written in its place" in section


def test_a_real_summary_that_merely_mentions_waiting_still_ships(store, tmp_path):
    """The over-firing direction, which would COST evidence. A long report that
    happens to say "waiting for CI" is a report."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = (
        "Rewrote the retry path in fetcher.py so a 429 backs off instead of "
        "failing the batch, and added three tests for it. " * 6
        + "\n\nCI is still waiting for the nightly slot, so the remote run is "
          "not part of this evidence."
    )
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "Rewrote the retry path" in body
    assert "No implementation summary was produced" not in body


def test_a_short_but_real_summary_survives(store, tmp_path):
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = "Fixed the off-by-one in parse_date()."
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "Fixed the off-by-one in parse_date()." in body
    assert "No implementation summary was produced" not in body


# ───────────── C2: the Stats section is gone (files/diffstat/turns) ────────── #
#
# The `## Stats` line — "N files, +X/-Y, N turns" — was REMOVED at the operator's
# instruction: the forge shows the diffstat itself and the turn count is internal
# noise. These two tests used to pin the branch-vs-base diffstat (C2) and its
# best-effort fallback; both are now asserted the other way — the section, its
# heading, and the turn count must NOT appear in any body.

def test_the_stats_section_is_gone_even_on_a_multi_commit_branch(store, tmp_path):
    """The exact C2 scenario that used to render "3 files, +102/-0": with a real
    repo and base, no `## Stats` heading, no diffstat line, no turn count."""
    work = _repo_with_two_commits(tmp_path)
    orch = _orch(store, tmp_path)
    repo = GitRepo(work)
    body = orch._pr_body(Task.new("t", repo_path=str(work)), _Commit(), _Result(),
                         repo=repo, base="main")
    assert "## Stats" not in body
    assert "3 files, +102/-0" not in body
    assert "files, +" not in body
    assert "turns." not in body


def test_the_stats_section_is_gone_on_the_no_repo_path_too(store, tmp_path):
    """The old best-effort fallback path (no repo) must not resurrect the line."""
    orch = _orch(store, tmp_path)
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), _Result())
    assert "## Stats" not in body
    assert "1 files, +0/-1" not in body
    assert "turns." not in body


# ──────────────── C3: a suite that never ran said "0 failed" ──────────────── #

def test_an_invocation_error_reads_as_NOT_RUN_not_as_zero_failures():
    """"FAIL — 0 passed, 0 failed, 0 errors" is what the body printed when the
    runner never started. A reviewer reads that as a suite that ran."""
    section = Orchestrator._test_evidence_section({
        "ran": True, "ok": False, "passed": 0, "failed": 0, "errors": 0,
        "invocation_error": True, "reproduces_on_base": True,
    })
    assert "NOT RUN" in section
    assert "test invocation failed" in section
    assert "0 failed" not in section
    assert "reproduces on base: yes" in section
    assert "NO test evidence" in section


def test_invocation_error_says_when_the_base_tree_is_clean():
    section = Orchestrator._test_evidence_section({
        "ran": True, "ok": False, "passed": 0, "failed": 0, "errors": 0,
        "invocation_error": True, "reproduces_on_base": False,
    })
    assert "reproduces on base: no" in section


def test_invocation_error_says_when_the_base_could_not_be_checked():
    section = Orchestrator._test_evidence_section({
        "ran": True, "ok": False, "passed": 0, "failed": 0, "errors": 0,
        "invocation_error": True, "reproduces_on_base": None,
    })
    assert "could not be checked" in section


def test_a_real_failure_names_the_failing_tests():
    """The names are persisted on the attempt row and were invisible on the
    artifact — the human got a count and nothing to act on."""
    section = Orchestrator._test_evidence_section({
        "ran": True, "ok": False, "passed": 4, "failed": 2, "errors": 0,
        "failing_tests": ["tests/test_a.py::test_one", "tests/test_b.py::test_two"],
    })
    assert "FAIL — 4 passed, 2 failed" in section
    assert "tests/test_a.py::test_one" in section
    assert "tests/test_b.py::test_two" in section


def test_no_test_command_is_disclosed_not_silent():
    """`runner.py` returns `ran=False, ok=True` when it finds no command, and
    this section returned "" for it — the one evidence line a reviewer could
    not tell apart from "nothing to say". A repo with no tests is structural
    absence (the routing predicate `ok=True` stays), but the PR body must
    say so."""
    section = Orchestrator._test_evidence_section(
        {"ran": False, "ok": True, "passed": 0, "failed": 0, "errors": 0})
    assert "NOT RUN — no test command detected" in section
    assert "NO test evidence" in section
    assert "0 failed" not in section


def test_no_test_command_is_not_confused_with_an_invocation_error():
    """Negative control for the new branch: an invocation error keeps its own
    wording and its base-tree verdict, whatever `ran` says."""
    section = Orchestrator._test_evidence_section(
        {"ran": False, "ok": False, "passed": 0, "failed": 0, "errors": 0,
         "invocation_error": True, "reproduces_on_base": False})
    assert "test invocation failed" in section
    assert "no test command detected" not in section
    assert "reproduces on base: no" in section


def test_a_missing_evidence_dict_still_renders_nothing():
    """`None` means the caller had no evidence object at all (the draft-PR
    body before any test ran) — that is not the no-command case."""
    assert Orchestrator._test_evidence_section(None) == ""


def test_a_pass_still_reads_as_pass():
    section = Orchestrator._test_evidence_section(
        {"ran": True, "ok": True, "passed": 9, "failed": 0, "errors": 0})
    assert "| Tests | ✅ PASS — 9 passed, 0 failed, 0 errors |" in section
    assert "NOT RUN" not in section


def test_the_tamper_flag_is_rendered_when_set():
    """Constraint #4's signal: a net reduction in tests must reach the human."""
    section = Orchestrator._test_evidence_section(
        {"ran": True, "ok": True, "passed": 9, "failed": 0, "errors": 0,
         "tamper_flag": True})
    assert "tamper guard fired" in section


def test_the_tamper_flag_is_rendered_on_a_layered_run_too():
    section = Orchestrator._test_evidence_section(
        {"layers": ["unit: 9 passed"], "tamper_flag": True})
    assert "unit: 9 passed" in section
    assert "tamper guard fired" in section


def test_nothing_known_stays_silent_but_nothing_ran_is_said():
    """Used to pin `{"ran": False}` → "" — the silence that let a PR with no
    test command carry no test line at all. Only the ABSENT evidence object
    (the pre-review draft body) is silent now."""
    assert "NOT RUN — no test command detected" in Orchestrator._test_evidence_section({"ran": False})
    assert Orchestrator._test_evidence_section(None) == ""


# ───────── C4: a verdict from one attempt, rendered on another's diff ──────── #

async def test_review_history_records_the_commit_it_judged(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    await store.create_task(t)

    class _D:
        passed = False
        blocking_items = []
        advisory_items = []

    await orch._append_review_history(t, _D(), commit_sha="deadbeef")
    assert (t.context or {})["review_history"][-1]["sha"] == "deadbeef"


def test_a_round_that_judged_another_commit_is_not_shown(store, tmp_path):
    """PR #102: attempt 3, a 282-line diff, showing a finding about attempt 2's
    40-line diff. review_history is task-lifetime; the PR is not."""
    work = _repo_with_two_commits(tmp_path)
    repo = GitRepo(work)
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-qb", "other", "main")
    (work / "elsewhere.txt").write_text("nope\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "another attempt entirely")
    off_branch = _git(work, "rev-parse", "HEAD")

    t = Task.new("t", repo_path=str(work))
    t.context = {"review_history": [
        {"round": 1, "sha": off_branch, "passed": False,
         "blocking": ["a finding about a diff this PR does not contain"]},
    ]}
    section = Orchestrator._review_evidence_section(t, head_sha=head, repo=repo)
    assert "no review has run against this commit yet" in section
    assert "a finding about a diff this PR does not contain" not in section


def test_a_round_that_judged_this_head_is_shown(store, tmp_path):
    work = _repo_with_two_commits(tmp_path)
    repo = GitRepo(work)
    head = _git(work, "rev-parse", "HEAD")
    parent = _git(work, "rev-parse", "HEAD~1")
    t = Task.new("t", repo_path=str(work))
    t.context = {"review_history": [
        {"round": 1, "sha": parent, "passed": False, "blocking": ["fix the guard"]},
        {"round": 2, "sha": head, "passed": True, "blocking": []},
    ]}
    section = Orchestrator._review_evidence_section(t, head_sha=head, repo=repo)
    assert "**PASSED** — 2 rounds |" in section
    assert "fix the guard" in section


def test_the_pr_body_scopes_the_review_dossier_to_its_own_head(store, tmp_path):
    """End of the wire, not just the helper."""
    work = _repo_with_two_commits(tmp_path)
    repo = GitRepo(work)
    head = _git(work, "rev-parse", "HEAD")
    t = Task.new("t", repo_path=str(work))
    t.context = {"review_history": [
        {"round": 1, "sha": "0" * 40, "passed": False,
         "blocking": ["a verdict on some other attempt"]},
    ]}
    commit = _Commit()
    commit.sha = head
    body = _orch(store, tmp_path)._pr_body(t, commit, _Result(), repo=repo, base="main")
    assert "a verdict on some other attempt" not in body
    assert "no review has run against this commit yet" in body


def test_a_task_with_no_review_at_all_renders_no_section(store, tmp_path):
    assert Orchestrator._review_evidence_section(Task.new("t", repo_path="/r")) == ""


# ──────── constraint §6d: the PR-body reviewer-backend disclosure row ─────── #


def test_default_reviewer_adds_no_extra_row():
    from no_human.core.pr_evidence import PrEvidence
    evidence = PrEvidence(
        review_verdict={"rounds": 2, "verdict": "PASSED", "addressed": [], "unmatched": False},
        reviewer_attribution="",
    )
    section = Orchestrator._review_evidence_section(
        Task.new("t", repo_path="/r"), evidence=evidence,
    )
    assert "| Independent review | ✅ **PASSED** — 2 rounds |" in section
    assert "Reviewer model" not in section


def test_non_default_reviewer_adds_the_disclosure_row_after_the_verdict_row():
    from no_human.core.pr_evidence import PrEvidence
    evidence = PrEvidence(
        review_verdict={"rounds": 1, "verdict": "PASSED", "addressed": [], "unmatched": False},
        reviewer_attribution="codex `gpt-5-codex`",
    )
    section = Orchestrator._review_evidence_section(
        Task.new("t", repo_path="/r"), evidence=evidence,
    )
    verdict_row = "| Independent review | ✅ **PASSED** — 1 round |\n"
    assert verdict_row in section
    assert evidence.review_verdict_pin() in verdict_row  # untouched exact-substring pin
    disclosure_row = "| Reviewer model | codex `gpt-5-codex` — non-default, chosen in Settings |\n"
    assert disclosure_row in section
    assert section.index(verdict_row) < section.index(disclosure_row)


# ──────────────── C5: abandoned drafts look exactly like the live one ─────── #

class _Forge:
    """Records the forge writes instead of making them."""

    def __init__(self):
        self.titles = []
        self.comments = []
        self.closes = []

    def set_pr_title(self, url, title):
        self.titles.append((url, title))
        return {"ok": True, "error": ""}

    def post_to_pr(self, url, body, file=None, line=None):
        self.comments.append((url, body))
        return {"ok": True, "error": ""}

    def close_pr(self, url):
        self.closes.append(url)
        return {"ok": True, "error": ""}


@pytest.fixture
def forge(monkeypatch):
    f = _Forge()
    import no_human.vcs.comment_poster as cp
    monkeypatch.setattr(cp, "set_pr_title", f.set_pr_title)
    monkeypatch.setattr(cp, "post_to_pr", f.post_to_pr)
    monkeypatch.setattr(cp, "close_pr", f.close_pr)
    return f


async def test_an_abandoned_draft_says_so_in_its_title(store, tmp_path, forge):
    """One task left #106, #107 and #111 open at once — all draft, all CI-red,
    every body asserting its criteria met, none referencing the others."""
    orch = _orch(store, tmp_path)
    t = Task.new("Fix the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/106",
                 "pr_draft_branch": "nh/attempt-1"}
    await store.create_task(t)
    await orch._abandon_draft_pr(t, "the attempt did not pass review",
                                 reason_from_agent=False)

    # The prefix names only what every route establishes — that this draft was
    # not delivered. It used to read "[ABANDONED — attempt failed review]", and
    # THIS TEST PINNED THAT STRING, so the suite defended the false claim
    # instead of catching it. The reason no longer goes to a comment at all
    # (refile of 1dfed378: closing, not commenting, is how an abandoned draft
    # is marked) — it is closed instead.
    assert forge.titles == [("https://github.com/o/r/pull/106",
                             "[ABANDONED — not delivered] Fix the thing")]
    assert forge.comments == [], "the abandon path must never post a comment"
    assert forge.closes == ["https://github.com/o/r/pull/106"]


async def test_abandoning_moves_the_url_out_of_the_live_slot(store, tmp_path, forge):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/106",
                 "pr_draft_branch": "nh/attempt-1"}
    await store.create_task(t)
    await orch._abandon_draft_pr(t, "why", reason_from_agent=False)
    assert "pr_draft_created" not in t.context
    assert t.context["abandoned_pr_urls"] == ["https://github.com/o/r/pull/106"]


async def test_the_live_body_links_the_drafts_that_were_abandoned(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.context = {"abandoned_pr_urls": ["https://github.com/o/r/pull/106",
                                       "https://github.com/o/r/pull/107"]}
    body = orch._pr_body(t, _Commit(), _Result())
    assert "## Superseded PRs" in body
    assert "https://github.com/o/r/pull/106" in body
    assert "https://github.com/o/r/pull/107" in body


async def test_a_clean_task_has_no_superseded_section(store, tmp_path):
    body = _orch(store, tmp_path)._pr_body(
        Task.new("t", repo_path="/r"), _Commit(), _Result())
    assert "Superseded" not in body


async def test_escalating_abandons_the_draft_it_opened(store, tmp_path, forge):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/111",
                 "pr_draft_branch": "nh/attempt-3"}
    await store.create_task(t)
    blocker = Blocker(category=BlockerCategory.NOVEL_UNKNOWN, goal="g",
                      root_cause_hypothesis="max attempts reached", confidence=0.9)
    out = await orch._raise_blocker(t, blocker, escalate_now=True)
    assert out.status == TaskStatus.ESCALATED
    assert forge.titles and "[ABANDONED" in forge.titles[0][1]
    assert forge.closes == ["https://github.com/o/r/pull/111"]
    assert "pr_draft_created" not in (t.context or {})


async def test_a_PARKED_task_keeps_its_draft(store, tmp_path, forge):
    """A parked task resumes onto the SAME branch and `_finalize` refreshes
    exactly that draft's body. Retiring it here would mislabel a live PR and
    lose the refresh."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/120",
                 "pr_draft_branch": "nh/attempt-1"}
    await store.create_task(t)
    blocker = Blocker(category=BlockerCategory.DEPENDENCY_WAIT, goal="g",
                      question="waiting on the human-gated CI job", confidence=0.9)
    out = await orch._raise_blocker(t, blocker)
    assert out.status != TaskStatus.ESCALATED, "fixture no longer parks — retune it"
    assert forge.titles == []
    assert forge.closes == []
    assert t.context["pr_draft_created"] == "https://github.com/o/r/pull/120"


# ───────────────────────── H8: who merges this ────────────────────────────── #

def test_the_body_states_that_a_human_must_merge(store, tmp_path):
    """Ten real bodies contained the words merge, approve and draft exactly
    zero times. The boundary is enforced in code and stated nowhere a human
    reading the PR would see it."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _Result(), branch="nh/x", base="main",
                         attempt_n=2)
    assert "never merges" in body
    assert f"nh approve {t.id[:8]}" in body
    assert "attempt 2 of 3" in body
    assert "`nh/x` → `main`" in body


# ───────────────────────── H9: the ticket link ────────────────────────────── #

def test_the_body_names_the_ticket_first(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("Fix X", repo_path="/r", external_id="SCRUM-59")
    body = orch._pr_body(t, _Commit(), _Result())
    assert body.startswith("**Ticket:** SCRUM-59")


def test_the_ticket_is_linked_when_intake_recorded_a_url(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("Fix X", repo_path="/r", external_id="SCRUM-59")
    t.context = {"jira": {"url": "https://acme.atlassian.net/browse/SCRUM-59"}}
    body = orch._pr_body(t, _Commit(), _Result())
    assert body.startswith(
        "**Ticket:** [SCRUM-59](https://acme.atlassian.net/browse/SCRUM-59)")


@pytest.mark.parametrize("url", [
    "https://acme.atlassian.net/browse/SCRUM-59",
    "https://linear.app/acme/issue/NH-1/add-the-thing",
    "http://jira.internal:8080/browse/SCRUM-59?filter=1#tab",
    "https://acme.atlassian.net/browse/SCRUM-59%2Fsub",
])
def test_a_real_tracker_url_is_still_a_link(store, tmp_path, url):
    """The two-way control on `_SAFE_LINK_DEST`. Every assertion about the
    ticket URL elsewhere is `no intruder heading`, which a guard that rejected
    EVERYTHING would satisfy while quietly deleting the link this section
    exists for. These are the shapes Jira and Linear actually emit — ports,
    query strings, fragments and percent-escapes included."""
    orch = _orch(store, tmp_path)
    t = Task.new("Fix X", repo_path="/r", external_id="SCRUM-59")
    t.context = {"jira": {"url": url}}
    body = orch._pr_body(t, _Commit(), _Result())
    assert body.startswith(f"**Ticket:** [SCRUM-59]({url})"), body[:200]


def test_a_url_that_is_not_a_link_destination_is_neutralised_not_dropped(
    store, tmp_path,
):
    """A tracker URL carrying markdown is not linked — and not silently
    discarded either. Both halves matter: emitting it raw rendered a live
    `<h1>` (measured through pandoc), and dropping it would hide a malformed
    tracker record from the only human who reads this."""
    orch = _orch(store, tmp_path)
    t = Task.new("Fix X", repo_path="/r", external_id="SCRUM-59")
    t.context = {"jira": {"url": f"http://t/x)<h1>{_MARKER}</h1>("}}
    body = orch._pr_body(t, _Commit(), _Result())
    assert "](http://t/x)" not in body, body[:200]
    assert _MARKER in body, body[:200]
    assert _intruders(_live_headings(body)) == [], body[:400]
    assert _intruders(_pandoc_headings(body)) == [], body[:400]


def test_no_ticket_no_line(store, tmp_path):
    body = _orch(store, tmp_path)._pr_body(
        Task.new("t", repo_path="/r"), _Commit(), _Result())
    assert not body.startswith("**Ticket:**")
    assert body.startswith("## ")


# ─────────────────────────── H13: scannability ────────────────────────────── #

def test_consecutive_criterion_lines_become_a_list(store, tmp_path):
    """Markdown collapses consecutive lines into one paragraph, so a criteria
    walkthrough rendered as a single run-on line."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = (
        "Walked the criteria.\n\n"
        "CRITERION 1: the parser rejects a negative width — done, parser.py:88.\n"
        "CRITERION 2: the error names the field — done, parser.py:91.\n"
        "CRITERION 3: a test covers both — done, test_parser.py:40."
    )
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "\n- CRITERION 1:" in body
    assert "\n- CRITERION 2:" in body
    assert "\n- CRITERION 3:" in body


def test_fenced_code_is_left_exactly_alone(store, tmp_path):
    """`# comment` in a shell block is not a heading, and pytest output must
    not sprout bullets."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = (
        "Ran it.\n\n```\n# run the suite\nCRITERION 1: not a bullet\n```"
    )
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "\n# run the suite" in body
    assert "\nCRITERION 1: not a bullet" in body
    assert "- CRITERION 1" not in body


# ══════════════════════════════════════════════════════════════════════════ #
# Defects found by independent review of the fix above. Each of these is the  #
# truthfulness bug pointed BACK at the change that was meant to cure it.      #
# ══════════════════════════════════════════════════════════════════════════ #

# ───────── D1: the abandon hook could retitle a LIVE, delivered PR ───────── #
#
# `_finalize` never clears `pr_draft_created`/`pr_draft_branch` — it only adds
# `pr_watch`/`pr_branch` beside them, naming the SAME PR on the SAME branch. So
# a revision (`nh reject`, a PR comment) that exhausts max_attempts walked
# `_escalate_exhausted` -> `_raise_blocker` -> `_abandon_draft_pr` and stamped
# "[ABANDONED]" on a human-reviewed PR in AWAITING_APPROVAL, telling its reader
# it is not a delivered change — while it still held exactly the reviewed code,
# because the failed revision pushed nothing.

_DELIVERED = "https://github.com/o/r/pull/200"


async def test_a_delivered_pr_is_never_retitled_as_abandoned(store, tmp_path, forge):
    orch = _orch(store, tmp_path)
    t = Task.new("Fix the thing", repo_path="/r")
    t.context = {
        "pr_draft_created": _DELIVERED, "pr_draft_branch": "nh/attempt-1",
        # what `_finalize` wrote when it delivered this very PR
        "pr_watch": _DELIVERED, "pr_branch": "nh/attempt-1",
    }
    await store.create_task(t)
    out = await orch._abandon_draft_pr(t, "the revision did not pass review",
                                       reason_from_agent=False)

    assert out == ""
    assert forge.titles == [], "a delivered PR was retitled [ABANDONED]"
    assert forge.comments == [], "a delivered PR was told it is not a delivery"
    assert forge.closes == [], "a delivered, human-reviewed PR was closed"
    assert t.context["pr_draft_created"] == _DELIVERED
    assert "abandoned_pr_urls" not in t.context


async def test_the_delivered_pr_is_safe_even_when_the_forge_spells_it_differently(
    store, tmp_path, forge,
):
    """URL equality is the direct proof, the branch is the durable one: a forge
    that renders the same MR under two spellings must not defeat the guard."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.context = {
        "pr_draft_created": "https://gl/o/r/-/merge_requests/7",
        "pr_draft_branch": "nh/attempt-1",
        "pr_watch": "https://gl/o/r/merge_requests/7",   # same MR, other spelling
        "pr_branch": "nh/attempt-1",
    }
    await store.create_task(t)
    await orch._abandon_draft_pr(t, "why", reason_from_agent=False)
    assert forge.titles == []


async def test_a_revision_that_exhausts_its_attempts_leaves_the_reviewed_pr_alone(
    store, tmp_path, forge,
):
    """The end-to-end shape the reviewer drove: a delivered PR sitting in
    AWAITING_APPROVAL, a revision on its branch, max_attempts exhausted."""
    orch = _orch(store, tmp_path)
    t = Task.new("Fix the thing", repo_path="/r")
    t.context = {
        "pr_draft_created": _DELIVERED, "pr_draft_branch": "nh/attempt-1",
        "pr_watch": _DELIVERED, "pr_branch": "nh/attempt-1",
    }
    await store.create_task(t)
    blocker = Blocker(category=BlockerCategory.NOVEL_UNKNOWN, goal="g",
                      root_cause_hypothesis="max attempts reached", confidence=0.9)
    out = await orch._raise_blocker(t, blocker, escalate_now=True)

    assert out.status == TaskStatus.ESCALATED
    assert forge.titles == [], (
        "escalating a revision retitled the delivered PR — the human's "
        "reviewed artifact now claims it was abandoned")
    assert t.context["pr_watch"] == _DELIVERED


async def test_a_draft_from_an_attempt_that_never_delivered_is_still_abandoned(
    store, tmp_path, forge,
):
    """The guard must not swallow the case C5 exists for: no `pr_watch` means
    nothing was ever delivered, so the draft really is a corpse."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/111",
                 "pr_draft_branch": "nh/attempt-3"}
    await store.create_task(t)
    await orch._abandon_draft_pr(t, "attempt failed review",
                                 reason_from_agent=False)
    assert forge.titles and "[ABANDONED" in forge.titles[0][1]
    assert "pr_draft_created" not in t.context


async def test_a_draft_on_a_branch_that_was_never_delivered_is_still_abandoned(
    store, tmp_path, forge,
):
    """A delivered PR on ANOTHER branch must not shelter this branch's draft."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/112",
                 "pr_draft_branch": "nh/attempt-3",
                 "pr_watch": "https://github.com/o/r/pull/99",
                 "pr_branch": "nh/attempt-1"}
    await store.create_task(t)
    await orch._abandon_draft_pr(t, "attempt failed review",
                                 reason_from_agent=False)
    assert forge.titles and "[ABANDONED" in forge.titles[0][1]


# ────── D2: "NOT RUN" printed for a suite that ran and genuinely failed ───── #
#
# `_is_invocation_error` returns True on ImportError/"Cannot find module" even
# with real counts — deliberately, for a node worktree with no `node_modules`
# reading as "2335 passed, 1 failed". `_run_attempt` persists that flag NEXT TO
# the counts and proceeds to the PR. Checking the flag first therefore printed
# "NOT RUN — this change carries NO test evidence" for 2336 tests with one
# genuine failure, dropping the counts and the failing names with it.

def test_a_run_that_produced_results_is_never_reported_as_NOT_RUN():
    section = Orchestrator._test_evidence_section({
        "ran": True, "ok": False, "passed": 2335, "failed": 1, "errors": 0,
        "invocation_error": True, "reproduces_on_base": True,
        "failing_tests": ["tests/test_parser.py::test_leap_year"],
    })
    assert "NOT RUN" not in section, (
        "2336 tests ran and one genuinely failed — the body called it NOT RUN")
    assert "carries NO test evidence" not in section
    assert "FAIL — 2335 passed, 1 failed, 0 errors" in section
    assert "tests/test_parser.py::test_leap_year" in section, (
        "the failing test name was dropped along with the counts")


def test_a_partial_run_still_says_the_runner_also_stumbled():
    """Keeping the counts must not hide the anomaly — say both."""
    section = Orchestrator._test_evidence_section({
        "ran": True, "ok": False, "passed": 2335, "failed": 1, "errors": 0,
        "invocation_error": True, "reproduces_on_base": True,
    })
    assert "may be PARTIAL" in section
    assert "reproduces on base: yes" in section


def test_a_passing_run_with_an_invocation_flag_keeps_its_counts():
    section = Orchestrator._test_evidence_section({
        "ran": True, "ok": True, "passed": 812, "failed": 0, "errors": 0,
        "invocation_error": True, "reproduces_on_base": False,
    })
    assert "PASS — 812 passed" in section
    assert "NOT RUN" not in section


def test_the_genuine_no_results_case_is_untouched():
    """The C3 fix must survive intact: no counts really is no evidence."""
    section = Orchestrator._test_evidence_section({
        "ran": True, "ok": False, "passed": 0, "failed": 0, "errors": 0,
        "invocation_error": True, "reproduces_on_base": True,
    })
    assert "**NOT RUN — test invocation failed**" in section
    assert "reproduces on base: yes" in section
    assert "carries NO test evidence" in section


# ───────── D4: C1 over-fired and deleted real summaries wholesale ────────── #
#
# `_NON_REPORT_MAX_CHARS = 600` was documented "deliberately conservative", but
# the sub-600 band is exactly where real summaries live — the existing over-fire
# test only ever probed a ~1100-char one. Each of these is a genuine report
# naming files, line numbers, test node ids or a measurement, and each was
# replaced wholesale by "No implementation summary was produced." over ONE
# trailing clause. Fixing a truthfulness bug by deleting true content is the
# same failure pointed the other way.

_REAL_SUMMARIES_UNDER_600 = [
    pytest.param(
        "Added a retry with exponential backoff to fetcher.py:88 and a "
        "regression test in tests/test_fetcher.py:40. I'll update the module "
        "docstring in a follow-up.",
        "fetcher.py:88", id="follow-up-promise"),
    pytest.param(
        "Fixed the off-by-one in parse_date() and added "
        "tests/test_date.py::test_leap. Once the nightly CI runs I will report "
        "the timing numbers.",
        "tests/test_date.py::test_leap", id="nightly-numbers-later"),
    pytest.param(
        "Rewrote the CSV loader to stream rather than slurp; memory now flat "
        "at 40MB (measured). Still waiting for the perf harness to be "
        "merged...",
        "40MB", id="still-waiting-on-a-harness"),
    pytest.param(
        "Implemented the feature and added three tests. Nothing else to do "
        "here.",
        "added three tests", id="nothing-else-to-do"),
]


@pytest.mark.parametrize("summary,evidence", _REAL_SUMMARIES_UNDER_600)
def test_a_real_summary_under_600_chars_is_not_deleted(
    store, tmp_path, summary, evidence,
):
    assert len(summary) < Orchestrator._NON_REPORT_MAX_CHARS, "not the band under test"
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = summary
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)

    assert "No implementation summary was produced" not in body, (
        f"a real {len(summary)}-char report was discarded over a trailing "
        f"clause: {summary!r}")
    assert evidence in body, "the concrete evidence must reach the reader"


@pytest.mark.parametrize("summary,_evidence", _REAL_SUMMARIES_UNDER_600)
def test_the_classifier_calls_these_reports(summary, _evidence):
    """Directly, so a failure names the classifier rather than the body."""
    assert Orchestrator._is_non_report_summary(summary) is False


# The other direction must not slacken: a message that is ONLY deferral still
# has to be caught, or C1 stops doing its job.
@pytest.mark.parametrize("text", [
    "I'll just wait for that notification.",
    "Waiting for the full-suite background run to finish.",
    "I'm waiting for the background suite. Let me know if you need anything.",
    "Standing by for the CI result.",
    "I'll report back once the nightly run completes.",
    "I don't need to do anything else here.",
])
def test_a_message_that_is_only_deferral_is_still_not_a_summary(text):
    assert Orchestrator._is_non_report_summary(text) is True, (
        f"the over-fire fix went too far and let a deferral through: {text!r}")


# ───── R4: reader-addressed lines, STANDING ALONE ─────────────────────────── #
#
# These reached the body under "## Changes" as if they described
# the diff. Nothing else in `_NON_REPORT_PATTERNS` matched them, so the early
# return fired before the residue was ever judged — and the case above does NOT
# pin them, because its "I'm waiting…" prefix is caught by an older pattern all
# on its own. Each of these must be caught by its OWN pattern.
@pytest.mark.parametrize("text", [
    # no other pattern matches this at all — the whole message is courtesy
    "Let me know if you need anything.",
    "Let me know if you'd like anything else.",
    # here the deferral half is caught, and the residue "I already updated you
    # on this." was rescued by `updated` reading as a completed-work verb
    "I already updated you on this. I'll wait for CI.",
    "I have updated you on this. Standing by.",
])
def test_a_line_addressed_to_the_reader_is_not_an_implementation_summary(text):
    assert Orchestrator._is_non_report_summary(text) is True, (
        f"a courtesy line shipped as the implementation summary: {text!r}")


def test_a_courtesy_sign_off_does_not_cost_the_report_it_is_attached_to(
    store, tmp_path,
):
    """The over-fire direction of the same patterns: catching "let me know"
    must not delete the evidence in the sentence before it."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = (
        "Added the retry to fetcher.py:88 and covered it in "
        "tests/test_fetcher.py:40. Let me know if you want it configurable."
    )
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "fetcher.py:88" in body
    assert "No implementation summary was produced" not in body


def test_a_vague_but_genuine_report_still_ships(store, tmp_path):
    """The documented residual. "I changed my approach." names the work, badly.
    `_reads_as_a_report`'s docstring says this ships on purpose; deleting it
    would be the D4 over-fire again, so the contract is pinned here rather than
    left to drift."""
    assert Orchestrator._is_non_report_summary(
        "I changed my approach. Waiting on the run now.") is False


def test_dropping_a_deferral_clause_does_not_split_a_path_apart(store, tmp_path):
    """The residue is measured on sentences, and a naive split on "." would cut
    `fetcher.py` and `test_date.py::test_leap` in half — which is how the
    evidence that saves a summary goes missing."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = (
        "Fixed src/no_human/vcs/git.py:412 and covered it in "
        "tests/test_git.py::test_push_retry. I'll let you know when CI is green."
    )
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "src/no_human/vcs/git.py:412" in body
    assert "tests/test_git.py::test_push_retry" in body
    assert "No implementation summary was produced" not in body


# ══════════════════════════════════════════════════════════════════════════ #
# D3: the two PRODUCTION CALL SITES were untested.                            #
#                                                                             #
# Every C2/C4 test above calls the helpers DIRECTLY with hand-supplied         #
# arguments, so they prove the functions and nothing about the wiring. A       #
# reviewer removed BOTH call sites and the whole suite stayed green:           #
#                                                                             #
#   * `_finalize` no longer passing repo/base/branch to `_pr_body`  → every    #
#     body silently reverts to per-commit stats (the original C2 bug) and      #
#     loses the branch pair from the footer.                                   #
#   * `_run_review` no longer passing commit_sha to                            #
#     `_append_review_history` → unstamped rounds cannot prove they judged     #
#     this head, so every reviewed, PASSING PR renders "no review has run      #
#     against this commit yet".                                                #
#                                                                             #
# These drive the REAL methods and read the observable output. No assertion    #
# here reads source text — that anti-pattern has already bought this branch's  #
# siblings nine rounds of false confidence.                                    #
# ══════════════════════════════════════════════════════════════════════════ #

class _FakePR:
    kind = "github"

    def __init__(self, url, pushed_sha):
        self.url = url
        self.pushed_sha = pushed_sha


@pytest.fixture
def captured_pr_body(monkeypatch):
    """Capture the body `_finalize` actually hands to the forge."""
    import no_human.core.orchestrator as orch_mod
    from no_human.vcs.receipts import Receipt

    seen = {}

    def fake_open_pr(repo, branch, title, body, **kw):
        seen["body"] = body
        seen["branch"] = branch
        seen["base"] = kw.get("base")
        return _FakePR("https://github.com/o/r/pull/300", repo.head_sha())

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    monkeypatch.setattr(orch_mod, "verify_pr_receipt",
                        lambda *a, **k: Receipt("pr_open", a[1] if len(a) > 1
                                                else "", "landed", "ok"))
    return seen


async def test_finalize_hands_the_pr_body_the_branch_it_is_opening(
    store, tmp_path, captured_pr_body,
):
    """WIRING. Remove `base=base, branch=branch` from `_finalize`'s
    `_pr_body(...)` call and this goes red: the footer stops naming the branch
    pair. (The `## Stats` canary this test used for `repo`/`base` is gone with
    the Stats section; `repo` reaching `_pr_body` from `_finalize` is now proven
    by the sibling `test_finalize_scopes_the_review_dossier_to_the_head_it_is_
    shipping`, which reads the review-evidence output `repo` drives.)"""
    work = _repo_with_two_commits(tmp_path)
    repo = GitRepo(work)
    orch = _orch(store, tmp_path)
    head_sha = repo.head_sha()

    task = Task.new("t", repo_path=str(work))
    # Delivery must push the review-approved sha (test_delivery_pushes_
    # reviewed_sha.py): stamp a PASS for this exact head so `_finalize`'s
    # pre-push gate lets this test reach `open_pr` at all.
    task.context = {"review_history": [
        {"round": 1, "sha": head_sha, "passed": True, "blocking": [], "advisory": []},
    ]}
    await store.create_task(task)
    await store.set_status(task, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(task.id, 1)

    commit = _Commit()
    commit.sha = head_sha
    out = await orch._finalize(task, repo, "feat/x", "main", commit,
                               attempt_id, _Result())
    assert out.status == TaskStatus.AWAITING_APPROVAL
    body = captured_pr_body["body"]

    # The Stats section must not come back through the real `_finalize` path.
    assert "## Stats" not in body
    assert "turns." not in body

    # H8: the footer names the branch pair, which nothing else on the PR says.
    assert "`feat/x` → `main`" in body, (
        "`branch`/`base` are not reaching the merge-boundary footer")


async def test_finalize_scopes_the_review_dossier_to_the_head_it_is_shipping(
    store, tmp_path, captured_pr_body,
):
    """The same wiring, read through C4: without `repo`, `_rounds_for_head`
    has nothing to test ancestry against and every stale round renders.

    Also stamps a real PASS for the actual head — since
    test_delivery_pushes_reviewed_sha.py, `_finalize`'s pre-push gate refuses
    to push at all without one, so the off-branch round alone can no longer
    reach `open_pr` to exercise this. Two rounds prove BOTH halves at once:
    the off-branch round's finding must not leak (unchanged assertion), and
    the real round's PASSED verdict — the one `_finalize` actually delivered
    on — must be exactly what renders."""
    work = _repo_with_two_commits(tmp_path)
    repo = GitRepo(work)
    orch = _orch(store, tmp_path)
    head_sha = repo.head_sha()

    task = Task.new("t", repo_path=str(work))
    task.context = {"review_history": [
        # A round stamped with a commit that is NOT on this branch.
        {"round": 1, "sha": "0" * 40, "passed": False,
         "blocking": ["a finding about a diff that is not in front of you"],
         "advisory": []},
        # The real PASS for this head — required by `_finalize`'s delivery
        # gate before it will push at all.
        {"round": 2, "sha": head_sha, "passed": True,
         "blocking": [], "advisory": []},
    ]}
    await store.create_task(task)
    await store.set_status(task, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(task.id, 1)

    commit = _Commit()
    commit.sha = head_sha
    await orch._finalize(task, repo, "feat/x", "main", commit,
                         attempt_id, _Result())
    body = captured_pr_body["body"]

    assert "a finding about a diff that is not in front of you" not in body, (
        "a verdict on another commit rendered on this PR — `repo` is not "
        "reaching `_review_evidence_section` from `_finalize`")
    assert "| Independent review | ✅ **PASSED**" in body, (
        "the real round for this head did not render — `repo` is not "
        "reaching `_review_evidence_section` from `_finalize`")


async def test_the_review_gate_stamps_the_commit_it_judged(store, tmp_path):
    """WIRING. Remove `commit_sha=reviewed_sha` from `_run_review`'s
    `_append_review_history(...)` call and this goes red."""
    from .test_e2e_orchestrator import FakeBackend, _config
    from no_human.review.reviewer import ReviewDecision as RD
    from no_human.review.selfcheck import ChecklistItem as CI

    work = _repo_with_two_commits(tmp_path)
    repo = GitRepo(work)

    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    cfg.data.setdefault("tests", {})["command"] = "true"   # keep the gate cheap

    class StubReviewer:
        _on_event = None

        async def review(self, task, **kw):
            return RD(passed=True, checklist=[CI("it holds", True, "evidence")])

    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), reviewer=StubReviewer())
    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)

    decision = await orch._run_review(task, repo, "attempt-1", base="main")
    assert decision.passed is True

    (round_1,) = task.context["review_history"]
    assert round_1["sha"] == repo.head_sha(), (
        "the round was recorded without the commit it judged — every reviewed "
        "PR will now say no review has run against its head")


async def test_the_gate_prompt_names_the_same_sha_the_round_stamps(store, tmp_path):
    """WIRING. `_run_review` must hand the reviewer the SAME sha it later
    stamps onto `review_history` — otherwise the prompt and the record can
    (and did) cite different commits. Remove `reviewed_sha=reviewed_sha,
    reviewed_branch=reviewed_branch` from `_run_review`'s `_run_reviewer(...)`
    call and this goes red at the first assertion with a KeyError.

    The trailing `_build_review_prompt(...)` call below is a BUILDER-level
    check only — it calls the builder directly with a self-computed sha, so
    it cannot see whether `AdversarialReviewer.review()`'s own gate branch
    (`reviewer.py:2278-2297`) still forwards `reviewed_sha`/`reviewed_branch`
    to that builder. That link is guarded separately by
    `test_reviewer.py::test_gate_review_hands_the_backend_a_prompt_naming_the_reviewed_sha`,
    which drives `review()` through a fake backend and asserts on the prompt
    the backend actually receives."""
    from .test_e2e_orchestrator import FakeBackend, _config
    from no_human.review.reviewer import ReviewDecision as RD, _build_review_prompt
    from no_human.review.selfcheck import ChecklistItem as CI

    work = _repo_with_two_commits(tmp_path)
    repo = GitRepo(work)

    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    cfg.data.setdefault("tests", {})["command"] = "true"   # keep the gate cheap

    captured = {}

    class StubReviewer:
        _on_event = None

        async def review(self, task, **kw):
            captured.update(kw)
            return RD(passed=True, checklist=[CI("it holds", True, "evidence")])

    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), reviewer=StubReviewer())
    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)

    await orch._run_review(task, repo, "attempt-1", base="main")

    assert captured["reviewed_sha"] == repo.head_sha(), (
        "the reviewer was never told which commit it was judging")

    (round_1,) = task.context["review_history"]
    assert round_1["sha"] == captured["reviewed_sha"], (
        "the prompt and the stamped round cite different commits")

    prompt = _build_review_prompt(
        task, "diff", "", "",
        reviewed_sha=captured["reviewed_sha"],
        reviewed_branch=captured.get("reviewed_branch", ""),
    )
    head7 = repo.head_sha()[:7]
    assert len(repo.head_sha()) >= 7
    assert f"You are reviewing {head7}" in prompt


async def test_the_already_satisfied_gate_prompt_names_the_same_sha_the_round_stamps(
    store, tmp_path, bare_repo
):
    """WIRING — the already_satisfied twin of the test above.
    `_gate_already_satisfied` builds NO diff (the claim is that HEAD already
    satisfies every criterion), but it still resolves `reviewed_sha` once at
    review start and stamps `commit_sha=reviewed_sha` on the round it PASSES
    — the exact same contract `_run_review` upholds for a diff round. Before
    this fix `_build_already_satisfied_prompt` accepted no
    `reviewed_sha`/`reviewed_branch` params at all, so the round record cited
    a commit the prompt never named.

    This test pins the ORCHESTRATOR-level wiring: `_gate_already_satisfied`
    resolves a sha once, passes it to `review()` as `reviewed_sha`/
    `reviewed_branch`, and the SAME sha is stamped onto the round it passes.
    The trailing `_build_already_satisfied_prompt(...)` call below is a
    BUILDER-level check only — it calls the builder directly with a
    self-computed sha, so it cannot see whether `AdversarialReviewer.review()`'s
    own already_satisfied branch (`reviewer.py:2206-2213`) still forwards
    `reviewed_sha`/`reviewed_branch` to that builder. That link is guarded
    separately by
    `test_reviewer.py::test_already_satisfied_review_hands_the_backend_a_prompt_naming_the_reviewed_sha`,
    which drives `review()` through a fake backend and asserts on the prompt
    the backend actually receives."""
    from no_human.review.reviewer import ReviewDecision as RD
    from no_human.review.reviewer import _build_already_satisfied_prompt
    from no_human.review.selfcheck import ChecklistItem as CI

    from .test_e2e_orchestrator import _config

    repo = GitRepo(bare_repo)

    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False

    captured = {}

    class StubReviewer:
        _on_event = None

        async def review(self, task, **kw):
            captured.update(kw)
            return RD(passed=True, checklist=[CI("it holds", True, "evidence")])

    orch = Orchestrator(store, cfg.data, _Backend(),
                        SlackNotifier(None), reviewer=StubReviewer())
    task = Task.new("t", repo_path=str(bare_repo))
    task.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    claim = (
        "Verified every criterion against the existing code.\n"
        "ALREADY-SATISFIED\n"
        "CRITERION: mul(a,b) returns product — MET — evidence: calc.py:4\n"
    )

    outcome = await orch._gate_already_satisfied(
        task, repo, attempt_id, claim, branch="main",
    )

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    assert captured["reviewed_sha"] == repo.head_sha(), (
        "the reviewer was never told which commit it was judging")

    (round_1,) = task.context["review_history"]
    assert round_1["sha"] == captured["reviewed_sha"], (
        "the prompt and the stamped round cite different commits")

    prompt = _build_already_satisfied_prompt(
        task, claim,
        reviewed_sha=captured["reviewed_sha"],
        reviewed_branch=captured.get("reviewed_branch", ""),
    )
    head7 = repo.head_sha()[:7]
    assert len(repo.head_sha()) >= 7
    assert f"You are reviewing {head7}" in prompt, (
        "_build_already_satisfied_prompt is not rendering the reviewed sha "
        "even though the round it stamps cites one")


async def test_the_already_satisfied_body_names_a_real_verification_artifact(
    store, tmp_path, bare_repo, monkeypatch,
):
    """D1.1 review finding #1: `_gate_already_satisfied`'s body-refresh
    block is a THIRD `_pr_body` caller (besides `_finalize` and the
    pre-review draft) that rebuilds the body with REAL receipts. Before this
    fix it never wrote the artifact file and never passed
    `verification_artifact_path`, so a PR delivered ENTIRELY through the
    already-satisfied gate (which never reaches `_finalize` at all) said
    "Full verification log: (not written this run)" while genuinely never
    writing one — the one delivery path where that pointer was always a lie.
    """
    from types import SimpleNamespace

    import no_human.core.orchestrator as orch_mod
    from no_human.agent.verification_receipts import VerificationReceipt
    from no_human.review.reviewer import ReviewDecision as RD
    from no_human.review.selfcheck import ChecklistItem as CI
    from no_human.vcs.task_pr import ResolvedPR

    repo = GitRepo(bare_repo)

    class StubReviewer:
        _on_event = None

        async def review(self, task, **kw):
            return RD(passed=True, checklist=[CI("it holds", True, "evidence")])

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = False
    orch = Orchestrator(store, cfg.data, _Backend(),
                        SlackNotifier(None), reviewer=StubReviewer())
    task = Task.new("t", repo_path=str(bare_repo))
    task.acceptance_criteria = ["mul(a,b) returns product"]
    pr_url = "https://github.com/o/r/pull/9"
    task.context = {"pr_draft_created": pr_url, "pr_draft_branch": "main"}
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)
    await store.add_verification_receipt(attempt_id, VerificationReceipt(
        kind="test", command="uv run pytest -q",
        output_excerpt="9 passed in 1.1s", output_bytes=15,
        truncated=False, seq=1))

    async def fake_resolve_task_pr(store_, task_):
        return ResolvedPR(url=pr_url, branch="main", source="draft")
    monkeypatch.setattr(orch_mod, "resolve_task_pr", fake_resolve_task_pr)

    captured: dict = {}

    def fake_open_pr(repo_, branch_, title, body, **kw):
        captured["body"] = body
        return SimpleNamespace(url=pr_url, pushed_sha="", kind="github")
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    async def _noop_comment(*a, **k):
        return None
    monkeypatch.setattr(orch, "_post_review_checklist_comment", _noop_comment)

    claim = (
        "Verified every criterion against the existing code.\n"
        "ALREADY-SATISFIED\n"
        "CRITERION: mul(a,b) returns product — MET — evidence: calc.py:4\n"
    )
    outcome = await orch._gate_already_satisfied(
        task, repo, attempt_id, claim, branch="main", attempt_n=1,
    )
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    body = captured.get("body")
    assert body, "the body was never rebuilt through open_pr"
    assert "(not written this run)" not in body, (
        "the pointer fell back to the empty-artifact text even though "
        "receipts existed for this attempt")
    artifact_path = Orchestrator._verification_artifact_path(task.id, 1)
    assert Orchestrator._display_path(str(artifact_path)) in body, (
        "the pointer does not name the real (display-form) artifact path")
    assert artifact_path.exists(), (
        "the already-satisfied gate never wrote the verification artifact")
    assert "uv run pytest -q" in artifact_path.read_text()


async def test_a_reviewed_passing_pr_shows_its_review_evidence(store, tmp_path):
    """The consequence a human sees, driven through BOTH real call sites: the
    gate records the round, and the body renders it instead of disowning it."""
    from .test_e2e_orchestrator import FakeBackend, _config
    from no_human.review.reviewer import ReviewDecision as RD
    from no_human.review.selfcheck import ChecklistItem as CI

    work = _repo_with_two_commits(tmp_path)
    repo = GitRepo(work)

    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    cfg.data.setdefault("tests", {})["command"] = "true"

    class StubReviewer:
        _on_event = None

        async def review(self, task, **kw):
            return RD(passed=True, checklist=[CI("it holds", True, "evidence")])

    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), reviewer=StubReviewer())
    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    await orch._run_review(task, repo, "attempt-1", base="main")

    commit = _Commit()
    commit.sha = repo.head_sha()
    body = orch._pr_body(task, commit, _Result(), repo=repo, base="main")

    assert "| Independent review | ✅ **PASSED** — 1 round |" in body
    assert "no review has run against this commit yet" not in body, (
        "a reviewed, PASSING PR told its reader no review had run against it")


# ═══════════ Re-review round 2: what the first pass still left open ═══════════ #

# ───── R1: `_pr_body` has THREE production call sites, not two ────────────── #
#
# `_open_draft_pr_for_review` (live at `_run_attempt`) also passes
# repo/base/branch, and unwiring it left the FULL suite green — the same hole
# D3 was opened to close, one call site over. It matters MORE here than at
# `_finalize`: this is the only body an ESCALATED or ABANDONED attempt ever
# produces (`_finalize` never runs on that path), and it is the body the
# reviewer is told to read (reviewer.py:367-371). So the C5 corpse this branch
# labels "[ABANDONED]" would carry the original C2 lie forever.

@pytest.fixture
def captured_draft_body(monkeypatch):
    """Capture the body the PRE-GATE draft open actually sends to the forge."""
    import no_human.core.orchestrator as orch_mod
    import no_human.vcs.github as gh_mod

    seen = {}

    def fake_open_pr(repo, branch, title, body, **kw):
        seen["body"] = body
        return _FakePR("https://github.com/o/r/pull/400", repo.head_sha())

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    # No `gh` shell-out: this run is the author, so the body is its to write.
    monkeypatch.setattr(gh_mod, "_existing_pr_url", lambda *a, **k: "")
    return seen


async def test_the_pre_gate_draft_body_gets_the_branch_it_is_opening(
    store, tmp_path, captured_draft_body,
):
    """WIRING. Remove `base=base, branch=branch` from
    `_open_draft_pr_for_review`'s `_pr_body(...)` call and this goes red on the
    footer. (The `## Stats` canary is gone with the Stats section; the footer
    branch pair proves the args still reach `_pr_body`.)"""
    work = _repo_with_two_commits(tmp_path)
    # `_open_draft_pr_for_review` is GitHub-only by design (the only backend that
    # is both draft-by-default and already-exists idempotent).
    _git(work, "remote", "add", "origin", "https://github.com/o/r.git")
    repo = GitRepo(work)
    orch = _orch(store, tmp_path)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)

    commit = _Commit()
    commit.sha = repo.head_sha()
    url = await orch._open_draft_pr_for_review(
        task, repo, "feat/x", "main", "attempt-1", commit=commit, result=_Result())
    assert url == "https://github.com/o/r/pull/400", "the draft never opened"
    body = captured_draft_body["body"]

    assert "## Stats" not in body
    assert "turns." not in body
    assert "`feat/x` → `main`" in body, (
        "`branch`/`base` are not reaching the merge-boundary footer")


async def test_the_abandoned_draft_body_is_the_only_one_written(
    store, tmp_path, captured_draft_body, forge,
):
    """Why R1 is worse than the `_finalize` case: on the escalation path this
    body is the ONLY one ever written, and it is what "[ABANDONED]" labels.
    (Formerly pinned the C2 branch stats; the Stats section is gone, so this now
    asserts the abandoned body is written, labelled, and carries no Stats line.)"""
    work = _repo_with_two_commits(tmp_path)
    _git(work, "remote", "add", "origin", "https://github.com/o/r.git")
    repo = GitRepo(work)
    orch = _orch(store, tmp_path)

    task = Task.new("Fix the thing", repo_path=str(work))
    await store.create_task(task)
    commit = _Commit()
    commit.sha = repo.head_sha()
    await orch._open_draft_pr_for_review(
        task, repo, "feat/x", "main", "attempt-1", commit=commit, result=_Result())

    # the attempt now walks away — exactly the C5 shape
    blocker = Blocker(category=BlockerCategory.NOVEL_UNKNOWN, goal="g",
                      root_cause_hypothesis="max attempts reached", confidence=0.9)
    out = await orch._raise_blocker(task, blocker, escalate_now=True)
    assert out.status == TaskStatus.ESCALATED
    assert forge.titles and "[ABANDONED" in forge.titles[0][1], (
        "this draft was never delivered, so C5 must still label it")
    # …and the body it was labelled ON exists and carries no Stats line.
    assert captured_draft_body["body"], "no draft body was ever written"
    assert "## Stats" not in captured_draft_body["body"]
    assert "3 files, +102/-0" not in captured_draft_body["body"]


# ───── R3: pin the URL half of the D1 guard ───────────────────────────────── #
#
# Reducing the guard to its branch clause alone left the file green, so the URL
# clause was redundancy nothing asserted. It is the half that survives branch
# bookkeeping drifting apart — the delivered PR is still the delivered PR even
# when the two branch fields disagree — so it gets pinned rather than dropped.

async def test_the_delivered_url_alone_is_enough_to_spare_the_pr(
    store, tmp_path, forge,
):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.context = {
        "pr_draft_created": _DELIVERED, "pr_draft_branch": "nh/attempt-1",
        "pr_watch": _DELIVERED, "pr_branch": "nh/attempt-2",  # branches DISAGREE
    }
    await store.create_task(t)
    out = await orch._abandon_draft_pr(t, "why", reason_from_agent=False)

    assert out == ""
    assert forge.titles == [], (
        "the branch fields drifted apart and the delivered PR was retitled "
        "[ABANDONED] anyway — the URL clause is what stops that")
    assert forge.closes == [], "the URL clause is also what stops the close"
    assert t.context["pr_draft_created"] == _DELIVERED


# ───── R2: the "failing tests:" block had nothing to render ───────────────── #
#
# D2 kept the COUNTS on a partial run, and that half works in production. The
# NAMES did not: `update_attempt` REPLACES the `test_results` column rather than
# merging it (db.py: `test_results = :test_results`), and the only write that
# sets `invocation_error: True` omitted `failing_tests` — overwriting the
# earlier write that carried them. So the body's "- failing tests:" block was
# unreachable on the one path that reaches it, which is the same dead code as
# the `or counted` clause deleted from `_test_evidence_section`.
#
# This drives the REAL `run_task` and reads the REAL persisted dict, because a
# hand-supplied dict is what hid the hole in the first place.

async def test_a_partial_run_reaches_the_body_with_its_failing_test_names(
    bare_repo, tmp_path, store,
):
    import json as _json
    from .test_e2e_orchestrator import FakeBackend, _config

    # The SCRUM-33 shape: a suite that RUNS and produces real counts while the
    # output still carries a module-resolution error, which is what makes
    # `_is_invocation_error` fire despite the counts. The import is INSIDE a
    # test body on purpose — at module scope it aborts collection, and then
    # nothing runs and there are no counts to preserve, which is the other case
    # entirely. Both files live on the base tree, so the error reproduces there
    # and the attempt proceeds to the human gate instead of failing.
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    (bare_repo / "test_missing_dep.py").write_text(
        "def test_needs_a_dep_that_is_not_installed():\n"
        "    import nonexistent_dep_xyz  # noqa\n")
    (bare_repo / "test_genuinely_fails.py").write_text(
        "def test_this_one_really_fails():\n    assert 1 == 2\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-qm", "a partial-run base tree")

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    orch = Orchestrator(store, cfg.data,
                        FakeBackend(lambda cwd: (cwd / "calc.py").write_text(
                            "def add(a, b):\n    return a + b\n\n\n"
                            "def mul(a, b):\n    return a * b\n")),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)
    outcome = await orch.run_task(t)
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    (attempt,) = await store.list_attempts(t.id)
    persisted = attempt["test_results"]
    persisted = _json.loads(persisted) if isinstance(persisted, str) else persisted

    assert persisted.get("invocation_error") is True, "not the partial-run shape"
    assert persisted["passed"] + persisted["failed"] + persisted["errors"] > 0, (
        "not the partial-run shape — this run produced no counts at all")
    assert persisted.get("failing_tests"), (
        "the invocation-error write dropped the failing test names, so the PR "
        "body's '- failing tests:' block can never render")

    # …and the renderer, fed the REAL dict, actually names them.
    section = Orchestrator._test_evidence_section(persisted)
    assert "NOT RUN" not in section
    assert "<details><summary>2 failing tests</summary>" in section
    assert "test_this_one_really_fails" in section


# ═══ R5: the abandon TITLE asserted a reason it could not know ═══════════════ #
#
# Round 1's defect, in the one field a repo's PR list shows. The prefix was the
# constant "[ABANDONED — attempt failed review]" and `_raise_blocker` stamps it
# on EVERY escalated route. `_run_attempt` orders the stages
# draft -> review -> CI -> escalate -> finalize, so on the CI-infra route the
# review has already PASSED and `pr_watch` is not yet written (only `_finalize`
# writes it), which means the delivered-PR guard cannot fire either. The title
# therefore announced a review failure that never happened — on a repo about to
# go public, from the branch whose whole purpose is to stop untrue claims.
#
# The suite PINNED the false string, so it defended the bug; that assertion is
# corrected above and these hold the line from the other side.

def _blocker(**kw):
    kw.setdefault("goal", "Add the thing")
    kw.setdefault("confidence", 0.9)
    return Blocker(**kw)


# Every escalating route, including ones where the review demonstrably passed.
_ESCALATING_ROUTES = [
    pytest.param(
        _blocker(category=BlockerCategory.TRANSIENT_INFRA, transient=True,
                 root_cause_hypothesis="CI infra failure persisted after 2 retries"),
        id="ci-infra-after-a-PASSING-review"),
    pytest.param(
        _blocker(category=BlockerCategory.STAGNATION,
                 root_cause_hypothesis="Two consecutive attempts ended without "
                                       "editing any file."),
        id="zero-diff-no-review-ever-ran"),
    pytest.param(
        _blocker(category=BlockerCategory.NOVEL_UNKNOWN,
                 root_cause_hypothesis="max attempts reached"),
        id="attempts-exhausted"),
    pytest.param(
        _blocker(category=BlockerCategory.IMPOSSIBLE,
                 root_cause_hypothesis="the acceptance criteria contradict"),
        id="impossible"),
]


@pytest.mark.parametrize("blocker", _ESCALATING_ROUTES)
async def test_the_abandoned_title_never_asserts_a_reason_it_cannot_know(
    store, tmp_path, forge, blocker,
):
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/9",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    out = await orch._raise_blocker(t, blocker, escalate_now=True)
    assert out.status == TaskStatus.ESCALATED

    (_url, title), = forge.titles
    assert "failed review" not in title, (
        f"the PR title claims the review failed, on a route where that is not "
        f"established: {title!r}")
    assert title.startswith("[ABANDONED — not delivered] "), title


@pytest.mark.parametrize("blocker", _ESCALATING_ROUTES)
async def test_the_abandon_comment_claims_no_review_verdict_either(
    store, tmp_path, forge, blocker,
):
    """The old comment's first sentence — "nothing in it should be read as a
    REVIEWED or delivered change" — contradicted a review that passed on the
    CI-infra route. Refile of 1dfed378: the abandon path posts no comment at
    all any more, on ANY escalating route — the strongest form of "claims no
    review verdict" is claiming nothing, on the forge, whatsoever. The PR is
    retitled and closed instead; the reason still reaches the human off-forge
    (the `pr_draft_abandoned` event / board / `nh blocked`)."""
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/9",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    await orch._raise_blocker(t, blocker, escalate_now=True)

    assert forge.comments == [], (
        "the abandon path posted a comment — it must post none, regardless "
        "of the route or the reason it carries")
    assert forge.closes == ["https://github.com/o/r/pull/9"]


async def test_the_true_reason_is_still_carried_on_the_ci_infra_route(
    store, tmp_path, forge,
):
    """Neutralising the title must not cost the information — it moves, it does
    not vanish. Refile of 1dfed378: it moved off the forge entirely (no
    comment), onto the `pr_draft_abandoned` event / board / `nh blocked`; the
    draft itself is retitled and closed, never told a false verdict."""
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/9",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    await orch._raise_blocker(
        t, _blocker(category=BlockerCategory.TRANSIENT_INFRA, transient=True,
                    root_cause_hypothesis="CI infra failure persisted after 2 retries"),
        escalate_now=True)

    assert forge.comments == [], "no comment is posted on an abandoned draft"
    (_url, title), = forge.titles
    assert title.startswith("[ABANDONED — not delivered] "), title
    assert forge.closes == ["https://github.com/o/r/pull/9"]


# ═══ R6: the SECOND `_abandon_draft_pr` caller, and the `Reason:` channel ════ #
#
# R5 neutralised the title and the note's first sentence, then left the identical
# false claim hardcoded one call site over: `_open_draft_pr_for_review` retires a
# prior-branch draft with "this draft's attempt did not pass review". That call
# site establishes exactly one thing — the task's work moved to a different
# branch. Whether a review ran, and how it voted, it does not know: the route
# `review PASSED -> local tests fail -> FAILED -> retry on a new branch` reaches
# it with a passing verdict on record.
#
# And `Reason:` itself is a raw model-authored channel. On the ESCALATED route
# it is `blocker.root_cause_hypothesis or blocker.question`, which `parse_blocker`
# lifts verbatim out of the coder's own final text — it sanitises `options` and
# `category`, never the prose. Rendered unescaped it lets the coder author
# headings and verdicts on a PR no_human is simultaneously labelling
# [ABANDONED]. Everywhere else coder prose is confined under
# `## Changes`, cleaned and demoted; this channel had none of it.


def _outside_fences(text: str) -> list[str]:
    """The lines a markdown renderer treats as live — fenced blocks removed.

    Fence lengths vary here on purpose (the wrapper has to be longer than
    anything inside it), so an opener of N backticks is closed only by a run of
    N or more.
    """
    live, open_len = [], 0
    for line in text.split("\n"):
        m = re.match(r"^(`{3,})", line)
        if open_len:
            if m and len(m.group(1)) >= open_len:
                open_len = 0
            continue
        if m:
            open_len = len(m.group(1))
            continue
        live.append(line)
    return live


def _coder_final_text(root_cause: str, *, question: str | None = None) -> str:
    """A real coder emission: prose, then the fenced BLOCKER_JSON the prompt asks
    for (`blockers/report.py:220`). `parse_blocker` is what production runs on
    `result.final_text` at `orchestrator.py:3043`."""
    import json
    payload = {"category": "NOVEL_UNKNOWN", "goal": "Add the thing",
               "confidence": 0.9, "root_cause_hypothesis": root_cause}
    if question is not None:
        payload["question"] = question
    return ("I could not finish this one.\n\n"
            "BLOCKER_JSON_START\n" + json.dumps(payload) + "\nBLOCKER_JSON_END\n")


async def _abandon_via_coder_blocker(orch, store, final_text):
    """Drive the real production chain: coder final text -> parse_blocker ->
    _raise_blocker -> _abandon_draft_pr -> the forge."""
    from no_human.blockers import parse_blocker
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/9",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    emitted = parse_blocker(final_text)
    assert emitted is not None, "the payload no longer parses — retune the fixture"
    out = await orch._raise_blocker(t, emitted, escalate_now=True)
    assert out.status == TaskStatus.ESCALATED
    return t


# ── F1: the branch-changed caller asserted a review verdict it never had ──── #
#
# Refile of 1dfed378: `_abandon_draft_pr` posts NO comment on ANY route any
# more, so a claimed-but-unestablished review verdict can no longer reach the
# forge at all — the strongest form of the property these tests defended.
# What still holds, and is what these now pin: the draft is retitled and
# closed, and no comment of any kind is posted.

async def test_the_branch_changed_abandon_asserts_no_review_verdict(
    store, tmp_path, forge, captured_draft_body,
):
    """`_open_draft_pr_for_review` retiring a prior-branch draft used to assert
    a review verdict it never established. It cannot any more: the abandon
    path posts nothing."""
    work = _repo_with_two_commits(tmp_path)
    _git(work, "remote", "add", "origin", "https://github.com/o/r.git")
    repo = GitRepo(work)
    orch = _orch(store, tmp_path)

    task = Task.new("Fix the thing", repo_path=str(work))
    # attempt 1 left a draft on its OWN branch; attempt 2 opens on a new one
    task.context = {"pr_draft_created": "https://github.com/o/r/pull/106",
                    "pr_draft_branch": "nh/abc12345-1"}
    await store.create_task(task)
    commit = _Commit()
    commit.sha = repo.head_sha()
    await orch._open_draft_pr_for_review(
        task, repo, "feat/x", "main", "attempt-2", commit=commit, result=_Result())

    (_url, title), = forge.titles
    assert title.startswith("[ABANDONED — not delivered] "), title
    assert forge.comments == [], (
        "the branch-changed abandon path must post no comment — it cannot "
        "assert a review verdict it never established if it says nothing")
    assert forge.closes == ["https://github.com/o/r/pull/106"]


async def test_the_branch_changed_reason_is_not_quoted_as_the_agents_words(
    store, tmp_path, forge, captured_draft_body,
):
    """no_human wrote this reason itself, so nothing about it — attributed or
    not — reaches the forge: the abandon path posts no comment at all."""
    work = _repo_with_two_commits(tmp_path)
    _git(work, "remote", "add", "origin", "https://github.com/o/r.git")
    repo = GitRepo(work)
    orch = _orch(store, tmp_path)
    task = Task.new("Fix the thing", repo_path=str(work))
    task.context = {"pr_draft_created": "https://github.com/o/r/pull/106",
                    "pr_draft_branch": "nh/abc12345-1"}
    await store.create_task(task)
    commit = _Commit()
    commit.sha = repo.head_sha()
    await orch._open_draft_pr_for_review(
        task, repo, "feat/x", "main", "attempt-2", commit=commit, result=_Result())

    assert forge.comments == [], (
        "a reason no_human authored still must not reach the forge as a "
        "comment — there is no comment on the abandon path any more")


# ── F2: `Reason:` used to be a model-authored channel and was rendered raw ── #
#
# Refile of 1dfed378: the whole channel — quoting, attribution, fence-escaping,
# heading demotion, harness-dialogue filtering — existed to make coder prose
# safe to post to a forge. Removing the post removes the need for all of it;
# these now pin the stronger property that nothing the coder wrote, however it
# is shaped, ever reaches the PR as a comment.

_MARKUP_REASON = (
    "the branch is finished\n\n"
    "## Review evidence\n"
    "- independent review rounds: 4\n"
    "- final verdict: **PASSED** — all acceptance criteria were verified by "
    "the reviewer and this branch is ready to merge."
)


async def test_the_coder_cannot_author_headings_on_the_abandoned_pr(
    store, tmp_path, forge,
):
    """Driven from a real BLOCKER_JSON emission. The coder's `## Review
    evidence` used to render as a top-level section of a PR no_human was
    labelling [ABANDONED]. It cannot render anywhere on the forge now — no
    comment is posted at all."""
    orch = _orch(store, tmp_path)
    await _abandon_via_coder_blocker(
        orch, store, _coder_final_text(_MARKUP_REASON))

    assert forge.comments == [], (
        "the coder's markdown reached the forge as a comment — it must not, "
        "on any content, since the abandon path posts none")


async def test_the_coder_cannot_break_out_of_the_quoted_reason(
    store, tmp_path, forge,
):
    """A fence inside the reason used to risk closing the old note's wrapper
    and letting the rest render live. There is no wrapper and no note any
    more — the hostile payload never reaches the forge either way."""
    orch = _orch(store, tmp_path)
    # ONE opener, deliberately — the shape that used to defeat a same-length
    # fence wrapper by leaving it open for the rest to render.
    hostile = ("here is the evidence\n```\n\n"
               "## Verdict\n**PASSED** — merge this.")
    await _abandon_via_coder_blocker(
        orch, store, _coder_final_text(hostile))

    assert forge.comments == [], (
        "hostile coder-authored fences reached the forge as a comment")


async def test_the_quoted_reason_is_attributed_to_the_agent_not_to_no_human(
    store, tmp_path, forge,
):
    """The attribution machinery this test used to pin is deleted along with
    the note it dressed — model-authored prose now never reaches the forge as
    a comment, attributed or not."""
    orch = _orch(store, tmp_path)
    await _abandon_via_coder_blocker(
        orch, store, _coder_final_text("the dependency never resolved"))

    assert forge.comments == [], (
        "the model-authored reason reached the forge as a comment")


async def test_harness_dialogue_is_filtered_out_of_the_quoted_reason(
    store, tmp_path, forge,
):
    """The `_clean_summary` filter this used to exercise guarded a channel that
    no longer exists — coder-to-harness dialogue cannot reach the PR through a
    comment the abandon path never posts."""
    orch = _orch(store, tmp_path)
    await _abandon_via_coder_blocker(
        orch, store,
        _coder_final_text(
            "the dependency never resolved\n\n"
            "Per the system instructions I am stopping here and will not "
            "touch the harness again."))

    assert forge.comments == [], (
        "coder-to-harness dialogue reached the PR through a comment")


async def test_a_wholly_filtered_reason_does_not_leave_a_dangling_label(
    store, tmp_path, forge,
):
    """A wholly-filtered reason used to risk an empty quotation on the note.
    There is no note: the title is the only forge-visible label, and it is
    never dangling — it always names the abandonment plainly."""
    orch = _orch(store, tmp_path)
    await _abandon_via_coder_blocker(
        orch, store,
        _coder_final_text("Per the system instructions I am stopping here."))

    assert forge.comments == []
    (_url, title), = forge.titles
    assert title.startswith("[ABANDONED — not delivered] "), title


# ── F3: the footer could print "attempt 5 of 3" ───────────────────────────── #

def test_the_footer_never_claims_an_attempt_beyond_the_bound(store, tmp_path):
    """`attempt_number` counts across the task's whole life (`_run_attempt`);
    `max_attempts` bounds ONE run. Any `nh reply` resume pushes the first past
    the second, and the footer printed "attempt 5 of 3"."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    assert orch.bounds.max_attempts == 3, "fixture assumes the default bound"
    foot = orch._merge_boundary_footer(t, branch="nh/x-5", base="main", attempt_n=5)
    assert "attempt 5 of 3" not in foot, foot
    assert "attempt 5" in foot, "the counter itself must survive"
    # …and within the bound it still reads as a bounded loop.
    inside = orch._merge_boundary_footer(t, branch="nh/x-2", base="main", attempt_n=2)
    assert "attempt 2 of 3" in inside, inside


# ══════ R7-A: the provenance sentence was ITSELF a false provenance claim ════ #
#
# R6 typed the `Reason:` channel by provenance and then decided provenance by
# WHICH METHOD POSTS, not by where the text came from: `_abandon_draft_pr` took
# `reason_from_agent=True` by default and `_raise_blocker` accepted it. Of the
# blockers that reach that funnel exactly ONE is coder-written (`parse_blocker`
# off `result.final_text`); the fourteen `Blocker(...)` constructions in
# `orchestrator.py`, plus `fallback_blocker` / `missing_access` /
# `ci_misconfigured`, are prose written as source literals in this repo. So the
# DOMINANT route posted:
#
#   Reason, in the coding agent's own words — quoted from its blocker report.
#   no_human did not write this text and has not verified it:
#     max_attempts (3) reached without a passing, untampered change.
#
# Every clause untrue, and untrue in the dangerous direction — it tells a human
# to distrust the harness's own attempt bookkeeping, on the one artifact whose
# subject is provenance. `Blocker.reason_is_agent_authored` now carries where
# the prose came from and `parse_blocker` is the only thing that may set it.
#
# The four `_ESCALATING_ROUTES` fixtures are ALL harness-authored and no test
# asserted anything about attribution, which is why the label survived six
# reviews. Both directions are pinned below, on the real production helpers
# rather than on hand-built fixtures.


async def _escalate_and_read_note(orch, store, helper, **ctx):
    """Drive one of the REAL `_escalate_*` helpers and return the task.

    The point of going through the helper instead of building a Blocker here is
    that the reason text is then the literal in `orchestrator.py`, not a copy of
    it in this file — a fixture that paraphrases the source cannot catch the
    source lying about itself. (Named for the era when the abandon path still
    posted a PR comment to read; it posts none now — callers check
    `forge.comments == []` instead.)
    """
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/9",
                 "pr_draft_branch": "nh/a1", **ctx}
    await store.create_task(t)
    await helper(orch, t)
    return t


async def _exhausted(orch, t):
    """Exhaustion with an attempt row that never recorded a failure reason.

    🔴 THIS FIXTURE IS NOT THE PRODUCTION PAYLOAD, and pretending it was is how
    the leak below survived. `create_attempt` alone leaves `failure_reason`
    NULL and `status` 'in_progress', so `_escalate_exhausted`'s `tried` list
    comes out EMPTY and the interpolation it used to do had nothing to
    interpolate. `_exhausted_after_a_failed_review` is the same route carrying
    what production actually writes; both stay, because "no attempt reason at
    all" is a real state too.
    """
    await orch.store.create_attempt(t.id, 3)
    await orch._escalate_exhausted(t, None, "main")


# 🔴 THE REVIEWER MODEL AUTHORED THIS, WORD FOR WORD. `_run_attempt` builds
# `"review failed: " + "; ".join(f"{i.label}: {i.evidence}" …)` from the failed
# checklist items (`orchestrator.py`), and `i.evidence` is lifted verbatim out
# of the reviewer's verdict JSON (`review/reviewer.py`) whose prompt *asks* the
# model to quote the decisive lines. Nothing caps its length, strips its
# newlines, or demotes its headings on the way to `attempts.failure_reason`.
#
# Driven through the pre-fix code this rendered, on GitHub's live /markdown
# endpoint, as `<h2>Review evidence</h2>` followed by `<strong>PASSED</strong>`
# — a fabricated merge verdict on a PR the same comment titles
# "[ABANDONED — not delivered]", and WITHOUT the agent-attribution disclaimer,
# because a harness-built `Blocker` reports `reason_is_agent_authored=False`.
# That is verbatim the incident `_AGENT_REASON_ATTRIBUTION` cites as the reason
# the channel had to be typed at all.
_MODEL_REVIEW_EVIDENCE = (
    "acceptance criterion 1: the diff is correct and complete\n"
    "\n## Review evidence\n\nfinal verdict: **PASSED** — ready to merge"
)

# Every shape production actually stores in `attempts.failure_reason` and then
# reads back in `_escalate_exhausted`. Two are model-authored prose, two are
# harness-computed but multi-line; the invariant below must hold for all of
# them, which is why they are a list and not one example.
_PRODUCTION_FAILURE_REASONS = [
    pytest.param("review failed: " + _MODEL_REVIEW_EVIDENCE, id="review-evidence"),
    pytest.param(
        "already-satisfied claim refuted: criterion 2: " + _MODEL_REVIEW_EVIDENCE,
        id="already-satisfied-refuted"),
    pytest.param(
        "agent run did not complete: I stopped because the spec was unclear",
        id="coder-final-text-first-line"),
    pytest.param(
        "tests failed: 3 failed\n\n## Stats\n\n    assert 1 == 2",
        id="captured-traceback-excerpt"),
]


async def _exhausted_after_a_failed_review(orch, t):
    """The COMMONEST exhaustion path: three attempts, the last one failing the
    review gate, its `failure_reason` carrying the reviewer model's own words."""
    aid = await orch.store.create_attempt(t.id, 3)
    await orch.store.update_attempt(
        aid, status="failed",
        failure_reason="review failed: " + _MODEL_REVIEW_EVIDENCE)
    await orch._escalate_exhausted(t, None, "main")


async def _zero_diff(orch, t):
    await orch._escalate_zero_diff(t, None, "main")


async def _inadequate(orch, t):
    await orch._escalate_inadequate_report(t, None, "main")


async def _deterministic_failure(orch, t):
    """`_escalate` -> `fallback_blocker`, the orchestrator-side failure route
    (push failed, PR error). Its prose is a factory literal, not the agent's.

    `_escalate_timeout_streak` is deliberately NOT in this list: TRANSIENT_INFRA
    PARKS, and a parked task keeps its draft on purpose (it resumes onto the
    same branch), so it never reaches `_abandon_draft_pr` at all.
    """
    await orch._escalate(t, "git push failed: remote rejected")


_HARNESS_ROUTES = [
    pytest.param(_exhausted, id="exhausted-max-attempts"),
    pytest.param(_exhausted_after_a_failed_review,
                 id="exhausted-after-a-failed-review"),
    pytest.param(_zero_diff, id="two-zero-diff-attempts"),
    pytest.param(_inadequate, id="inadequate-report"),
    pytest.param(_deterministic_failure, id="push-failed-fallback-blocker"),
]


@pytest.mark.parametrize("helper", _HARNESS_ROUTES)
async def test_a_harness_written_reason_is_never_attributed_to_the_agent(
    store, tmp_path, forge, helper,
):
    """The dominant route. Every one of these reasons is a source literal in
    `orchestrator.py`; none of them is the agent's words. Refile of 1dfed378:
    a false provenance claim can no longer be published on any of these
    routes, because none of them posts a comment at all — the reason reaches
    the human only through `render_report` (board / `nh blocked`), which never
    carries this attribution string either (see
    `test_the_attempt_trail_still_reaches_the_human_on_the_board`)."""
    orch = _orch(store, tmp_path)
    t = await _escalate_and_read_note(orch, store, helper)

    assert forge.comments == [], (
        "the escalation posted a PR comment — no route may, harness-authored "
        "reason or not")
    assert (t.blocker or {}).get("root_cause_hypothesis") or (
        t.blocker or {}).get("question"), (
        "the reason must still reach the human somewhere — via the persisted "
        "blocker / render_report — even though it is never posted to the PR")


async def test_the_exhaustion_reason_is_the_harness_bookkeeping_verbatim(
    store, tmp_path, forge,
):
    """Pins the exact text the false label used to be attached to, so a future
    rewrite of `_escalate_exhausted` cannot quietly move this test off its own
    subject. It now reaches the human through the persisted blocker
    (`render_report`), not a PR comment — the abandon path posts none."""
    from no_human.blockers import Blocker as _B, render_report

    orch = _orch(store, tmp_path)
    t = await _escalate_and_read_note(orch, store, _exhausted)
    assert forge.comments == []
    blocker = _B.from_dict(t.blocker)
    report = render_report(blocker, task_title=t.title, task_id=t.id)
    assert "max_attempts (3) reached without a passing, untampered change" in report


# ── H1: the exhaustion reason interpolated the last attempt's failure_reason ── #
#
# `_escalate_exhausted` ended its `root_cause_hypothesis` with
# `Last: {tried[-1]}`, and `tried[-1]` is `f"attempt {n}: {a['failure_reason']}"`.
# The blocker is built with `Blocker(...)`, so `reason_is_agent_authored` is
# False, so `_raise_blocker` passes `reason_from_agent=False`, so
# `_abandon_draft_pr` takes the PLAIN branch — `f"Reason: {str(reason)[:400]}"`,
# with no `_clean_summary`, no `_reformat_summary_markdown` and no fence. Every
# guard this branch built for the model-authored channel sat on the OTHER side
# of that `if`.
#
# The fix is the pattern `_escalate_zero_diff` already uses on the same file:
# the model's words go in `evidence=` (which `_raise_blocker` never posts to the
# forge — it renders only `root_cause_hypothesis or question`) and `tried`, both
# of which reach the human through `render_report` on the board, and
# `root_cause_hypothesis` stays a sentence written here in source.
#
# 🔴 THE FIX IS *NOT* `reason_is_agent_authored=True`. The sentence is MIXED
# provenance: `max_attempts (3) reached…` is the harness's own verified
# bookkeeping, and stamping "in the coding agent's own words … no_human did not
# write this text and has not verified it" over it is precisely the mirrored lie
# the attribution commit removed. Typing a mixed sentence either way is a lie;
# the answer is to stop mixing.


@pytest.mark.parametrize("failure_reason", _PRODUCTION_FAILURE_REASONS)
async def test_the_exhaustion_reason_does_not_move_with_the_attempt_log(
    store, tmp_path, forge, failure_reason,
):
    """The invariant, stated so it cannot be satisfied by filtering one payload.

    Two runs of the SAME route differing only in what the last attempt recorded
    must produce the same `root_cause_hypothesis` — the pure literal, never the
    attempt's own text — and, refile of 1dfed378, neither run may post a PR
    comment at all, so no escaping/capping/heading-demotion question can even
    arise on the forge any more. Both properties are checked, so this test
    still covers every `failure_reason` writer, present and future, without
    having to enumerate them.
    """
    from no_human.blockers import Blocker as _B

    orch = _orch(store, tmp_path)
    t1 = await _escalate_and_read_note(orch, store, _exhausted)
    without = _B.from_dict(t1.blocker).root_cause_hypothesis
    assert forge.comments == []
    forge.titles.clear()

    async def _with_reason(o, t):
        aid = await o.store.create_attempt(t.id, 3)
        await o.store.update_attempt(
            aid, status="failed", failure_reason=failure_reason)
        await o._escalate_exhausted(t, None, "main")

    orch2 = _orch(store, tmp_path)
    t2 = await _escalate_and_read_note(orch2, store, _with_reason)
    with_reason = _B.from_dict(t2.blocker).root_cause_hypothesis
    assert forge.comments == []

    assert with_reason == without, (
        "the attempt's failure_reason moved `root_cause_hypothesis`; on the "
        "commonest exhaustion path that text is the reviewer model's own\n"
        f"--- with a failure_reason ---\n{with_reason}\n"
        f"--- without one ---\n{without}")


async def test_the_reviewers_words_never_reach_the_abandoned_pr_comment(
    store, tmp_path, forge,
):
    """The concrete harm, asserted at its strongest now: refile of 1dfed378
    means there is no abandoned-PR comment for the reviewer's words to reach —
    the whole surface is gone. The honest half (the true reason) is still
    proven to survive, just off-forge — see
    `test_the_attempt_trail_still_reaches_the_human_on_the_board`, which
    checks it via `render_report`."""
    orch = _orch(store, tmp_path)
    await _escalate_and_read_note(orch, store, _exhausted_after_a_failed_review)
    assert forge.comments == [], (
        "a comment was posted on the abandoned draft — the reviewer's words "
        "(or anything else) can only 'reach' it if something is posted at all")


async def test_the_attempt_trail_still_reaches_the_human_on_the_board(
    store, tmp_path, forge,
):
    """Moving the text off the PR must not DELETE it: `render_report` puts
    `evidence` under "2. What happened" and `tried` under "4. What I tried", and
    that report is what `nh blocked` and the board show. The reviewer's words
    stay exactly where a human went looking for them.

    🔴 BOTH FIELDS ARE ASSERTED SEPARATELY, on purpose. Checking only that the
    text appears somewhere in the rendered report let a mutation that emptied
    `evidence` survive — `tried` was still carrying it, so the report still
    matched. The docstring above names two carriers; a test that cannot tell
    them apart is not testing the sentence it claims to.
    """
    from no_human.blockers import Blocker as _B, render_report

    orch = _orch(store, tmp_path)
    t = await _escalate_and_read_note(
        orch, store, _exhausted_after_a_failed_review)
    blocker = _B.from_dict(t.blocker)

    assert _MODEL_REVIEW_EVIDENCE in blocker.evidence, (
        "`evidence` no longer carries the last attempt's reason, so "
        '"2. What happened" on the board says nothing happened: '
        f"{blocker.evidence!r}")
    assert any("attempt 3: review failed:" in line for line in blocker.tried), (
        "`tried` no longer carries the attempt trail: " + repr(blocker.tried))

    report = render_report(blocker, task_title=t.title, task_id=t.id)
    assert _MODEL_REVIEW_EVIDENCE in report, (
        "the attempt trail was dropped instead of relocated:\n" + report)


async def test_the_returned_detail_keeps_the_trail_the_pr_comment_must_not(
    store, tmp_path, forge,
):
    """The two channels, asserted apart in one test so neither can absorb the
    other again.

    Purifying `root_cause_hypothesis` also emptied `TaskOutcome.detail`, which
    `_raise_blocker` derives from it — and that is what `nh run` prints and the
    TUI logs. Two suites outside this file caught the loss. The trail is
    re-attached to the RETURN VALUE, which no forge write can reach; the PR
    comment stays a source literal. Both halves are asserted here, from one
    driven run, because the failure mode is a future edit collapsing them back
    into one field.
    """
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/9",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 3)
    await store.update_attempt(
        aid, status="failed",
        failure_reason="review failed: " + _MODEL_REVIEW_EVIDENCE)

    outcome = await orch._escalate_exhausted(t, None, "main")

    assert _MODEL_REVIEW_EVIDENCE in outcome.detail, (
        "`nh run` no longer tells the human which failure burned the last "
        f"attempt: {outcome.detail!r}")
    assert forge.comments == [], (
        "the trail — or anything else — reached a PR comment; the abandon "
        "path must post none")


async def test_the_inadequate_report_route_cannot_author_a_block(
    store, tmp_path, forge,
):
    """The fourth carrier of agent text into `root_cause_hypothesis`, BOUNDED
    rather than assumed bounded.

    `_escalate_inadequate_report` interpolates `inadequate_report_reason`, and
    that string comes from `report_quality.report_inadequacy`, one of whose
    three returns embeds the agent's own report. So this route publishes a
    fragment of agent prose on the plain, unfenced branch too — same shape as
    `_escalate_exhausted` before the fix, and the fixture in `_HARNESS_ROUTES`
    passes no context, so it drives none of it (the same "payload nobody
    generated" hole).

    It is NOT the same severity, and this test is the measurement that says so
    instead of a docstring asserting it: that branch fires only when the WHOLE
    report normalises into `_PLACEHOLDERS`, and it interpolates with `!r`, which
    escapes newlines. Every payload the closed set can produce is therefore
    single-line and lands mid-sentence — inline markdown at worst, never a
    block-level heading. The payloads are DERIVED from `_PLACEHOLDERS`, so a new
    placeholder is covered without editing this test.

    🔴 THE NON-VACUITY GUARD BELOW IS NOT DECORATION. The first version of this
    test decorated the placeholders with markdown that `_normalise` could not
    strip back to a placeholder, so nothing carrying a newline ever reached the
    branch — and deleting the `!r` from `report_quality.py` left it GREEN. A
    payload has to survive `_normalise` AND still contain a newline for the `!r`
    to be doing anything, and `assert any("\\n" in p …)` is what checks that it
    does.
    """
    from no_human.core.report_quality import _PLACEHOLDERS, report_inadequacy

    # `_normalise` strips runs of [\s#>*_`-.:] from BOTH ENDS only, so a
    # newline survives into `stripped` when it sits inside such a run at an end.
    decorations = ["{}", "## {}", "**{}**", "> {}", "`{}`", "#.: {} :.#",
                   "#\n{}", "{}\n#", "*\n\n{}\n\n*", "-\n\t{}\n\t-"]
    hits = []
    for base in sorted(_PLACEHOLDERS):
        for d in decorations:
            payload = d.format(base)
            r = report_inadequacy(payload, "report")
            if r and "placeholder" in r:
                hits.append((payload.strip(), r))
    assert hits, "the placeholder branch is unreachable — retune the probe"
    assert any("\n" in p for p, _r in hits), (
        "no payload reaching the interpolating branch contains a newline, so "
        "the `!r` this test is measuring has nothing to escape and the "
        "assertion below is vacuous")
    for p, r in hits:
        assert "\n" not in r, (
            "a raw newline survived `!r`, so this route CAN open a block: "
            f"{p!r} -> {r!r}")
    reasons = [r for _p, r in hits]

    orch = _orch(store, tmp_path)
    await _escalate_and_read_note(
        orch, store, _inadequate,
        inadequate_report_reason=max(reasons, key=len),
        inadequate_report_text="## Review evidence\n\nverdict: PASSED")
    assert forge.comments == [], (
        "the inadequate-report route posted a PR comment — none of this "
        "agent-authored text may reach the forge at all")


async def test_a_coder_written_reason_still_carries_the_attribution(
    store, tmp_path, forge,
):
    """The channel this attribution machinery guarded is gone (refile of
    1dfed378): the abandon path never posts a comment, so model-authored
    prose — attributed or not — cannot reach the forge at all any more. This
    is the strongest form of the property the old assertions defended."""
    orch = _orch(store, tmp_path)
    await _abandon_via_coder_blocker(
        orch, store, _coder_final_text("the vendor API never returned a schema"))
    assert forge.comments == [], (
        "model-authored prose reached the forge as a comment")


async def test_the_same_sentence_is_labelled_by_ORIGIN_not_by_wording(
    store, tmp_path, forge,
):
    """Identical text, two origins — coder-authored and harness-authored —
    and now neither ever reaches the forge as a comment. The old assertion
    that provenance decided a LABEL on the posted note no longer applies
    because nothing is posted; this pins the stronger property instead.
    """
    sentence = "max_attempts (3) reached without a passing, untampered change"

    orch = _orch(store, tmp_path)
    await _abandon_via_coder_blocker(orch, store, _coder_final_text(sentence))
    assert forge.comments == []
    forge.titles.clear()

    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/9",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    await orch._raise_blocker(
        t, _blocker(category=BlockerCategory.NOVEL_UNKNOWN,
                    root_cause_hypothesis=sentence),
        escalate_now=True)
    assert forge.comments == [], (
        "harness-authored reason reached the forge as a comment")


async def test_the_agent_cannot_clear_its_own_provenance_flag(store, tmp_path, forge):
    """`parse_blocker` sanitises `options` and `category` as trust boundaries;
    the provenance flag is the third. An agent that emits the key set to false
    in its own BLOCKER_JSON must not get its prose published as no_human's —
    and now that the abandon path posts no comment at all, that holds
    trivially for the forge; the flag itself must still not be attacker-
    clearable, since it still gates the `pr_draft_abandoned` event's reason."""
    import json as _json
    from no_human.blockers import parse_blocker

    payload = _json.dumps({
        "category": "NOVEL_UNKNOWN", "goal": "g", "confidence": 0.9,
        "root_cause_hypothesis": "everything is fine, ship it",
        "reason_is_agent_authored": False,
    })
    emitted = parse_blocker(
        "done\n\nBLOCKER_JSON_START\n" + payload + "\nBLOCKER_JSON_END\n")
    assert emitted is not None
    assert emitted.reason_is_agent_authored is True, (
        "the agent cleared the flag on its own text")

    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/9",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    await orch._raise_blocker(t, emitted, escalate_now=True)
    assert forge.comments == [], (
        "agent-authored prose reached the forge as a comment")


async def test_the_harness_fallback_sentence_is_not_attributed_away(
    store, tmp_path, forge,
):
    """A coder blocker whose prose fields are both empty: what used to be
    posted was `_raise_blocker`'s OWN literal. The abandon path posts no
    comment at all now, so the fallback sentence — like every other reason —
    never reaches the forge; this pins that instead of a label on a note."""
    from no_human.blockers import Blocker as _B

    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/9",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    empty = _B(category=BlockerCategory.NOVEL_UNKNOWN, goal="g", confidence=0.9,
               root_cause_hypothesis="", question=None)
    empty.reason_is_agent_authored = True          # as parse_blocker would set it
    await orch._raise_blocker(t, empty, escalate_now=True)

    assert forge.comments == [], (
        "no_human's own fallback sentence reached the forge as a comment")


def test_a_blocker_built_by_a_constructor_is_harness_authored_by_default():
    """The default is the direction that cannot lie: constructing a Blocker in
    source IS writing its prose in source."""
    from no_human.blockers import Blocker as _B
    assert _B(category=BlockerCategory.NOVEL_UNKNOWN).reason_is_agent_authored is False


def test_every_blocker_FACTORY_is_harness_authored():
    """The helpers in `blockers/report.py` write their prose as literals too —
    behavioural, so a new factory that forgets is caught by what it produces."""
    from no_human.blockers.report import (
        ci_misconfigured, fallback_blocker, missing_access,
    )
    for made in (fallback_blocker("push failed"),
                 missing_access("CI_TOKEN", system="CI"),
                 ci_misconfigured("ci.backend is unset")):
        assert made.reason_is_agent_authored is False, made


def test_provenance_survives_the_round_trip_through_task_blocker():
    """`_raise_blocker` persists `blocker.to_dict()` onto the task and resumes
    rehydrate it. A flag that did not round-trip would silently relabel the
    reason on every resume."""
    from no_human.blockers import Blocker as _B
    agentic = _B(category=BlockerCategory.NOVEL_UNKNOWN,
                 root_cause_hypothesis="x", reason_is_agent_authored=True)
    assert _B.from_dict(agentic.to_dict()).reason_is_agent_authored is True
    harness = _B(category=BlockerCategory.NOVEL_UNKNOWN, root_cause_hypothesis="x")
    assert _B.from_dict(harness.to_dict()).reason_is_agent_authored is False


@pytest.mark.parametrize("blocker", _ESCALATING_ROUTES)
async def test_the_escalating_route_fixtures_are_not_attributed_either(
    store, tmp_path, forge, blocker,
):
    """These four fixtures are all built with `Blocker(...)` — harness-authored,
    every one — and for six review rounds no test asserted anything at all about
    attribution, which is exactly how the false label survived them."""
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.context = {"pr_draft_created": "https://github.com/o/r/pull/9",
                 "pr_draft_branch": "nh/a1"}
    await store.create_task(t)
    await orch._raise_blocker(t, blocker, escalate_now=True)
    assert forge.comments == [], (
        "the escalating route posted a PR comment — none does any more")


# ══════ R7-B: the summary channel let the coder author a top-level section ═══ #
#
# `_reformat_summary_markdown`'s docstring promised the coder's heading would
# not render "as a SIBLING of the template's `## Test evidence` and `## Stats`".
# Driven through the real `_pr_body` and GitHub's own `/markdown` renderer it
# did, four ways — and unlike the `Reason:` channel (a dead draft's comment,
# wrapped in a long fence), THIS one is on every delivered PR:
#
#   `Heading\n====`        -> <h1>, above every template section
#   `Heading\n----`        -> <h2>, sibling of ## Task / ## Stats
#   `<h2>Heading</h2>`     -> <h2>, raw HTML, rendered
#   `# Heading`            -> demoted by ONE, to `## Heading`: the exact sibling
#   a 4-space-indented ``` -> `line.lstrip().startswith("```")` read indented
#                             code CONTENT as a fence, so the tracker desynced
#                             AND `_close_orphaned_fence`'s appended ``` became a
#                             real OPENER that swallowed `## Stats` and the whole
#                             merge-boundary footer — including "It never merges
#                             and never approves its own work" — into a code
#                             block.
#
# `_clean_summary` preserves leading whitespace on purpose, "because an indented
# block is how captured command output arrives", so the last one needs no
# adversarial intent whatsoever: a coder pasting pytest output triggers it.

# The template's own sections. Nothing the coder writes may join this set — that
# IS the guarantee, stated as one assertion instead of as prose.
#
# 🔴 `⚠️ Assumptions & Open Questions` WAS MISSING, AND ITS ABSENCE WAS THE
# PROOF THAT SECTION HAD NEVER BEEN DRIVEN. Every renderer test here asserts
# `set(headings) - _TEMPLATE_H2 == []`, so a body carrying that section would
# have gone RED on the template's own heading — which means no test had ever
# built one. It was true: `grep -n "assumptions\|intake_qa"` over this file
# returned nothing, while `_assumptions_section` interpolated five model- or
# tracker-authored values raw, each of which rendered a live <h1> on GitHub.
#
# 🔴 `How I verified this` JOINED THE SET WHEN THE RECEIPTS SECTION LANDED, AND
# IT IS A TEMPLATE HEADING FOR THE SAME REASON THE OTHERS ARE: `_pr_body`
# emits the literal string `## How I verified this` itself (`orchestrator.py`,
# `_verification_section`), on EVERY body, including the empty-receipts case
# this file drives. It is not a value any channel of the body can author — the
# command lines and their output are neutralised into inline code / fences
# inside it, which is what `tests/test_verification_receipts.py` pins. Adding
# it here widens the ALLOWED set by exactly one string the renderer hardcodes;
# every coder-authored channel is still asserted to produce nothing outside it.
#
# 🔴 `Evidence` IS THE UMBRELLA `_evidence_section` EMITS. The reviewer's verdict
# and the orchestrator's test run used to be their own top-level `## Review
# evidence` / `## Test evidence` sections; they are now `###` sub-sections under
# `## Evidence` (decisive-first, reviewer's verdict leading), so those two
# strings are no longer level-1/2 headings and leave this set. `Stats` LEFT it
# outright: the diffstat/turns line was removed (the forge shows the diffstat;
# the turn count is internal noise).
_TEMPLATE_H2 = {"Evidence", "Acceptance criteria", "Changes",
                "How I verified this", "Superseded PRs"}


_RAW_HTML_H12 = r"<[hH][12][^>]*>(.*?)</[hH][12]>"
_HTML_RAW_TEXT_START = re.compile(
    r"^ {0,3}<(?:script|pre|style|textarea)(?:[ \t]|>|$)", re.I)
_HTML_RAW_TEXT_END = re.compile(r"</(?:script|pre|style|textarea)>", re.I)
_HTML_TAG_LINE = re.compile(r"^ {0,3}</?[A-Za-z][A-Za-z0-9-]*(?=[ \t]|/?>|$)")


def _strip_containers(line: str) -> tuple[str, str, bool]:
    """`(blockquote markers, remainder, remainder is inside a list item)`.

    A renderer strips EVERY container marker before deciding what the rest is,
    and it does not stop after one of each — which is the whole of HIGH-2.
    """
    bq, rest, li = "", line, False
    while True:
        m = re.match(r"^(?: {0,3}>)+ ?", rest)
        if m:
            bq, rest = bq + m.group(0), rest[len(m.group(0)):]
            continue
        m = re.match(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])[ \t]+", rest)
        if m:
            rest, li = rest[len(m.group(0)):], True
            continue
        return bq, rest, li


def _col(s: str, stop: int = 4) -> int:
    """The column *s* ends at, a tab advancing to the next 4-column stop."""
    c = 0
    for ch in s:
        c = c + stop - c % stop if ch == "\t" else c + 1
    return c


def _indent_col(line: str) -> int:
    """The column *line*'s first non-whitespace character sits at."""
    i = 0
    while i < len(line) and line[i] in " \t":
        i += 1
    return _col(line[:i])


def _container_columns(prefix: str):
    """`(outer, inner)` — the two column requirements a MIXED prefix imposes.

    `outer` is absolute and comes from the list markers before every quote
    marker (`- > ` -> 2); `inner` is measured from the end of a line's OWN
    quote markers and comes from the list markers after the last one
    (`> - ` -> 2). One absolute column for both is wrong: `> - ` and `  > `
    both end at column 4, and a renderer strips `  > ` and then finds the
    heading at column 0 of the quote.
    """
    pos = outer = inner = col = 0
    seen_quote = False
    while pos < len(prefix):
        m = re.match(r"^(?: {0,3}>)+ ?", prefix[pos:])
        if m:
            seen_quote, col, inner = True, 0, 0
            pos += len(m.group(0))
            continue
        m = re.match(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])[ \t]+", prefix[pos:])
        if not m:
            break
        col += _col(m.group(0))
        if seen_quote:
            inner = col
        else:
            outer = col
        pos += len(m.group(0))
    return outer, inner


def _still_in_mixed_container(line: str, outer: int, inner: int) -> bool:
    """Both requirements from `_container_columns`, on one line.

    A line with nothing past its quote markers is a BLANK LINE inside the
    quote, and a blank line does not end a list item.
    """
    m = re.match(r"^(?: {0,3}>)+ ?", line)
    if outer:
        j = len(line) - len(line.lstrip(" \t"))
        if _col(line[:j]) < outer:
            return False
    if inner:
        tail = line[len(m.group(0)):] if m else line
        if not tail.strip():
            return True
        pad = len(tail) - len(tail.lstrip(" \t"))
        if _col(tail[:pad]) < inner:
            return False
    return True


def _dedent_col(line: str, col: int) -> str:
    """*line* with its first *col* COLUMNS of leading whitespace removed."""
    i = seen = 0
    while i < len(line) and seen < col and line[i] in " \t":
        seen = seen + 4 - seen % 4 if line[i] == "\t" else seen + 1
        i += 1
    return " " * max(0, seen - col) + line[i:]


def _live_headings(md: str) -> list[str]:
    """Every ATX/setext/HTML heading of level 1-2 a renderer would produce.

    An offline stand-in for the renderer, so a regression is caught without a
    network round trip.

    🔴 IT USED TO CLAIM ITS INDEPENDENCE WAS WHAT MADE IT TRUSTWORTHY, AND THAT
    CLAIM WAS FALSE IN THE ONLY WAY THAT MATTERS. The docstring read: "an
    INDEPENDENT implementation of the block rules — if it shared
    `_scan_leaf_blocks` with the code under test, a scanner bug would hide
    itself." It did not share the function. It shared the BUGS — the same
    one-marker container strip, the same fence regex blind to a list marker —
    because both were hand-written from one author's model of markdown. So it
    produced exactly the outcome the comment promised to prevent: driven
    against `- - ## PWNED` and `- ```python`, this scanner reported the body
    CLEAN while GitHub rendered a live <h2>, and every hermetic test went green
    through two review rounds.

    Independence you assert is not independence. What replaces the assertion:
    `test_the_offline_scanner_agrees_with_an_independent_parser` pins this
    function, EVERY RUN and with no network, to `pandoc` — a GFM parser written
    by other people, from the spec, with no relationship to either
    implementation here. A bug shared between this function and
    `_scan_leaf_blocks` now has to be shared by pandoc too before it can go
    green. (The pin is on the INTRUDER set; see that test for the one direction
    where pandoc and GitHub genuinely disagree and GitHub is the authority.)

    🔴 IT TRACKS HTML BLOCKS, because that is where the round-7 leak lived:
    inside one, a ``` is literal text rather than a fence, markdown is not
    parsed at all, and a raw `<h1>` IS live.

    🔴 AND IT TRACKS A FENCE OPENED ON A CONTAINER LINE (`- ```py`, `> ```py`,
    `> - ```py`), which is HIGH-1. Such a fence ends with its CONTAINER:
    measured through `/markdown`, a heading indented into the item stays inert
    while one at column 0 is live, and an unclosed one swallows nothing at all.
    A MIXED prefix has both marker kinds and needs both tests — the quote depth
    AND the item's content column. Testing only the depth is how this function
    reported `[]` for three shapes GitHub renders as a live <h2>.
    """
    found, fence, prev, html = [], "", None, None
    c_fence, c_depth, c_col, c_list, c_cols = "", 0, 0, False, (0, 0)
    for raw in md.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^( {0,3})(`{3,}|~{3,})(.*)$", raw)
        if fence:
            if (m and m.group(2)[0] == fence[0]
                    and len(m.group(2)) >= len(fence) and not m.group(3).strip()):
                fence = ""
            continue
        if c_fence:
            # A blank line ends a BLOCKQUOTE container and not a list item —
            # the same wrong "both survive one" rule lived here too. `>` and
            # `> ` are not blank, so a quoted blank keeps the block open.
            if not raw.strip():
                inside = not c_depth
            elif c_depth:
                b = re.match(r"^(?: {0,3}>)+ ?", raw)
                inside = (b.group(0).count(">") if b else 0) >= c_depth
                if inside and c_list:
                    # 🔴 A MIXED PREFIX (`> - `, `- > `) HAS BOTH MARKERS, AND
                    # THE DEPTH TEST ANSWERED FOR BOTH — so the content-column
                    # rule below was live for a pure list and DEAD here, in
                    # this oracle exactly as in `_scan_leaf_blocks`. The two
                    # agreed with each other and reported `[]` for three shapes
                    # pandoc and GitHub both render as a live <h2>. Same defect,
                    # same pair, one marker over. See the `mixed-` payloads.
                    inside = _still_in_mixed_container(raw, *c_cols)
            else:
                # 🔴 ANY INDENT IS NOT THE RULE, and this line carried the
                # SAME wrong rule as `_scan_leaf_blocks` — so the oracle agreed
                # with the bug and the hermetic suite could not see it. A line
                # stays in a list item only by reaching the item's CONTENT
                # COLUMN (`- ` -> 2, `1. ` -> 3), taken off the opener's own
                # marker. See `unclosed-fence-in-a-bullet-below-content-column`.
                inside = _indent_col(raw) >= c_col
            if inside:
                _rest = (_strip_containers(raw)[1] if c_depth
                         else _dedent_col(raw, c_col))
                cm = re.match(r"^( {0,3})(`{3,}|~{3,})(.*)$", _rest)
                if (cm and cm.group(2)[0] == c_fence[0]
                        and len(cm.group(2)) >= len(c_fence)
                        and not cm.group(3).strip()):
                    c_fence = ""
                prev = None
                continue
            # container ended; fall through
            c_fence, c_depth, c_col, c_list, c_cols = "", 0, 0, False, (0, 0)
        if html is not None:
            # Inside an HTML block markdown is inert, but raw `<h1>` is not.
            found.extend(re.sub(r"<[^>]+>", "", h).strip()
                         for h in re.findall(_RAW_HTML_H12, raw))
            if html == "blank":
                if not raw.strip():
                    html = None
            elif html.search(raw):
                html = None
            prev = None
            continue
        if m and not (m.group(2)[0] == "`" and "`" in m.group(3)):
            fence, prev = m.group(2), None
            continue
        # An HTML block outranks everything below: the ``` inside one is text.
        # `<pre>`/`<!--` and friends are NOT ended by a blank line, which is
        # exactly why an unterminated one swallows the rest of the PR body.
        opened = True
        if _HTML_RAW_TEXT_START.match(raw):
            html = None if _HTML_RAW_TEXT_END.search(raw) else _HTML_RAW_TEXT_END
        elif raw.lstrip().startswith("<!--"):
            html = None if "-->" in raw else re.compile(r"-->")
        elif _HTML_TAG_LINE.match(raw):
            html = "blank"
        else:
            opened = False
        if opened:
            found.extend(re.sub(r"<[^>]+>", "", h).strip()
                         for h in re.findall(_RAW_HTML_H12, raw))
            prev = None
            continue
        # A blockquote or a list does NOT defuse the heading inside it —
        # `> ## X`, `- ## X` and `1. ## X` are all live <h2> on GitHub — so ALL
        # the container prefixes come off before the heading tests, exactly as a
        # renderer strips them. Stopping after one of each is HIGH-2, and this
        # function had that bug too.
        bq_marks, rest, li = _strip_containers(raw)
        # A fence can open on the container line itself (`- ```py`), and this
        # regex never saw it because a list marker is not `^ {0,3}(```|~~~)`.
        if bq_marks or li:
            cm = re.match(r"^( {0,3})(`{3,}|~{3,})(.*)$", rest)
            if cm and not (cm.group(2)[0] == "`" and "`" in cm.group(3)):
                c_fence, c_depth = cm.group(2), bq_marks.count(">")
                c_col, c_list = _col(raw[:len(raw) - len(rest)]), li
                c_cols = _container_columns(raw[:len(raw) - len(rest)])
                prev = None
                continue

        # The optional tail is CommonMark's: `#` ALONE on a line is an EMPTY
        # heading and renders `<h1></h1>`. Requiring `\\S` after the hashes made
        # this scanner blind to it, so the outline could be polluted with a
        # level-1 heading the scanner reported as clean.
        # `(?!#)` because the level is the WHOLE run: without it the trailing
        # `#*` (the optional closing sequence) let `###` be read as `##`
        # followed by a closer, so the scanner reported a level-2 heading for
        # the demoted form of a bare `#` and failed the very payload it was
        # relaxed to catch.
        atx = re.match(r"^ {0,3}(#{1,2})(?!#)(?:\s+(\S.*?))?\s*#*\s*$", rest)
        if atx:
            found.append(atx.group(2) or "")
            prev = None
            continue
        under = None if li else re.match(r"^( *)(=+|-+)[ \t]*$", rest)
        if under and prev is not None:
            prev_bq, prev_text, prev_li = prev
            # Same quote depth, and an underline under a list item has to be
            # indented into that item — `- item` + `---` at column 0 is an <hr>.
            binds = (len(under.group(1)) >= 1) if prev_li else (
                len(under.group(1)) <= 3)
            if prev_bq == bq_marks and binds:
                found.append(prev_text)
                prev = None
                continue
        for h in re.findall(r"<[hH][12][^>]*>(.*?)</[hH][12]>", raw):
            found.append(re.sub(r"<[^>]+>", "", h).strip())
        prev = (bq_marks, rest.strip(), bool(li)) if rest.strip() else None
    return found


def _headings_from_html(html: str) -> list[str]:
    """The level-1/2 heading TEXTS of a rendered document.

    `unescape` matters and was missing: the template's own
    `## ⚠️ Assumptions & Open Questions` comes back as `&amp;` from a real
    renderer, so an un-unescaped comparison reports the template's own heading
    as a coder-authored intruder and the assertion means nothing.
    """
    # `re.I` because a renderer may pass an uppercase raw `<H2>` straight
    # through: pandoc does, GitHub lowercases it, and without the flag the two
    # read as a parser disagreement when they are saying the same thing.
    return [htmlmod.unescape(re.sub(r"<[^>]+>", "", m)).strip()
            for m in re.findall(r"<h[12][^>]*>.*?</h[12]>", html, re.S | re.I)]


def _pandoc_headings(md: str) -> list[str]:
    """*md*'s level-1/2 headings according to `pandoc`, read as GFM.

    🔴 THE POINT IS THAT NOBODY HERE WROTE IT. `_live_headings` and
    `_scan_leaf_blocks` are two hand-written markdown scanners by one author,
    and the defect this exists to make impossible is the two agreeing with each
    other while both disagree with a renderer — measured, that is exactly what
    happened for `- - ## X` and `- ```python`, twice, through two review rounds
    of hermetic green. pandoc is a GFM parser maintained by other people from
    the spec; it cannot inherit a bug from either of them.

    TEST-ONLY, DELIBERATELY, and here is exactly how that is kept true: it is
    invoked as an EXTERNAL BINARY through `subprocess`, it is imported by no
    module under `src/`, and it is in no dependency file — `pyproject.toml` is
    untouched by this change. The lean-stack constraint is about what the
    installed product needs to RUN, and the product neither imports nor
    requires this.

    WHAT HAPPENS IN CI WITHOUT IT, said plainly rather than discovered later:
    the pandoc assertions skip with a reason, and coverage degrades to
    `_live_headings` plus the network test — i.e. to exactly what this branch
    had before. It never degrades to nothing, because every `_live_headings`
    assertion in this file is unconditional and runs either way. That is a real
    limit, not a closed hole: a pandoc-less CI does not get the independence.
    """
    if not shutil.which("pandoc"):                            # pragma: no cover
        pytest.skip("pandoc is not installed — the independent oracle is the "
                    "one thing that cannot be stubbed, so this skips rather "
                    "than falling back to the scanner it exists to check")
    # `--wrap=none` is load-bearing, not tidiness. pandoc reflows its HTML at
    # 72 columns by default and will break a LINE INSIDE a heading's text, so
    # `## ⚠️ Assumptions & Open Questions` came back as
    # `'⚠️ Assumptions & Open\nQuestions'` and every long template heading read
    # as a coder-authored intruder. GitHub does not wrap, so the difference was
    # the oracle's formatting and not the document's meaning.
    out = subprocess.run(["pandoc", "-f", "gfm", "-t", "html", "--wrap=none"],
                         input=md, capture_output=True, text=True)
    if out.returncode != 0:                                   # pragma: no cover
        pytest.skip(f"pandoc failed: {out.stderr.strip()[:200]}")
    return _headings_from_html(out.stdout)


def _intruders(headings) -> list[str]:
    return [h for h in headings if h not in _TEMPLATE_H2]


def _said(text):
    """A coder result whose final text is *text* (`_Result` has no __init__)."""
    r = _Result()
    r.final_text = text
    return r


_MARKER = "PWNED"


def _jsx_doc(predecessor: str, intruder: str = f"## {_MARKER}") -> str:
    """A coder documenting a JSX component, with *predecessor* above the tag.

    🔴 ONE BLANK LINE PUT THE SCANNER ON THE WRONG SIDE OF THE DOOR, and every
    line of this is what a coder writes with no adversarial intent at all.
    `<UserCard />` is a CommonMark type-7 HTML start, the only condition that
    may not interrupt a paragraph — and *predecessor* is not a paragraph, so
    GitHub starts an HTML block, reads the ```jsx as literal text, ENDS the
    block at the blank line, and renders `## PWNED` live. The scanner
    approximated "previous line was a paragraph" as "previous line was
    non-blank", so it blocked the type-7 start, opened a fence at the ```jsx
    that GitHub never saw, and was still inside that phantom fence when the
    coder's heading went past — undemoted. Both harms at once, measured through
    `/markdown` (mode gfm) for all six predecessors:

    * `PWNED` renders as a top-level section, sibling of `## Task`/`## Stats`;
    * the closer appended for the phantom fence is a real OPENER on GitHub, and
      it swallowed `## Stats` and the merge-boundary footer.

    The docstrings that promised "being wrong here cannot leak a heading" were
    wrong because the payload they were written against kept the heading INSIDE
    the misdetected block, where markdown is inert. One blank line is the whole
    difference: it ends GitHub's HTML block and leaves the scanner's fence open.
    """
    return (f"{predecessor}\n<UserCard />\n```jsx\n<UserCard name=\"a\" />\n\n"
            f"{intruder}\n\nrenders fine.\n")


# Each payload is the input where the guarantee is WEAKEST for its defect, not
# the one where it is easiest. `## X` (the shape the old code did handle) is kept
# only as the control.
_HEADING_PAYLOADS = [
    pytest.param(f"{_MARKER}\n====\n\nnormal text", id="setext-h1-equals"),
    pytest.param(f"{_MARKER}\n=\n\nnormal text", id="setext-h1-single-equals"),
    pytest.param(f"{_MARKER}\n----\n\nnormal text", id="setext-h2-dashes"),
    pytest.param(f"{_MARKER}\n-\n\nnormal text", id="setext-h2-single-dash"),
    pytest.param(f"<h1>{_MARKER}</h1>\n\nnormal text", id="raw-html-h1"),
    pytest.param(f"<h2>{_MARKER}</h2>\n\nnormal text", id="raw-html-h2"),
    pytest.param(f"<H2>{_MARKER}</H2>\n\nnormal text", id="raw-html-h2-uppercase"),
    pytest.param(f"# {_MARKER}\n\nnormal text", id="atx-h1-demoted-by-one-was-a-sibling"),
    pytest.param(f"## {_MARKER}\n\nnormal text", id="atx-h2-control"),
    pytest.param(f"   # {_MARKER}\n\nnormal text", id="atx-h1-indented-three"),
    pytest.param(f"  ## {_MARKER}\n\nnormal text", id="atx-h2-indented-two"),
    pytest.param("output:\n\n    ```\n    $ pytest\n\n"
                 f"## {_MARKER}\n", id="indented-fence-desyncs-the-tracker"),
    pytest.param("Implemented the parser.\n\n~~~\ncode\n~~~\n\n"
                 f"## {_MARKER}\n", id="after-a-tilde-fence"),
    pytest.param("Implemented the parser.\n\n````\n```\n````\n\n"
                 f"## {_MARKER}\n", id="after-a-nested-fence"),
    pytest.param("Implemented the parser.\n\n```\ncode\n```\n\n"
                 f"{_MARKER}\n====\n", id="setext-after-a-fence"),
    # A CONTAINER DOES NOT DEFUSE A HEADING. All twelve of these render as a
    # live <h1>/<h2> on GitHub; the demoted forms (`- ### X`, `> ### X`,
    # `>> ### X`, `1. ### X`) render as <h3>, verified in the same run. The
    # offline scanner strips the container prefix for the same reason a renderer
    # does — without that it would pass these vacuously.
    pytest.param(f"> ## {_MARKER}\n\nnormal explanatory text\n", id="blockquote-atx-h2"),
    pytest.param(f"> # {_MARKER}\n\nnormal explanatory text\n", id="blockquote-atx-h1"),
    pytest.param(f">> ## {_MARKER}\n\nnormal explanatory text\n", id="blockquote-nested"),
    pytest.param(f"> {_MARKER}\n> ====\n\nnormal explanatory text\n", id="blockquote-setext-h1"),
    pytest.param(f"> {_MARKER}\n> ----\n\nnormal explanatory text\n", id="blockquote-setext-h2"),
    pytest.param(f"- ## {_MARKER}\n\nnormal explanatory text\n", id="list-dash-atx"),
    pytest.param(f"* ## {_MARKER}\n\nnormal explanatory text\n", id="list-star-atx"),
    pytest.param(f"1. ## {_MARKER}\n\nnormal explanatory text\n", id="list-numbered-atx"),
    pytest.param(f"- {_MARKER}\n  ====\n\nnormal explanatory text\n", id="list-setext"),
    pytest.param(f"> - ## {_MARKER}\n\nnormal explanatory text\n", id="blockquote-then-list-atx"),
    pytest.param(f"- <h2>{_MARKER}</h2>\n\nnormal explanatory text\n", id="list-raw-html"),
    pytest.param(f"> <h2>{_MARKER}</h2>\n\nnormal explanatory text\n", id="blockquote-raw-html"),
    # 🔴 A FALSE CODE SPAN. `_CODE_SPAN = r"(`+[^`]*?`+)"` did not require the
    # closing backtick run to match the opener's length and CommonMark does, so
    # ``` ``<h1>x</h1>` ``` was a code span HERE and not one on GITHUB: the tag
    # landed in the exempt partition, escaped demotion, and rendered live above
    # every template section. The exemption is gone; these are the payloads that
    # would bring it back.
    pytest.param(f"``<h1>{_MARKER}</h1>` tail\n\nnormal explanatory text\n",
                 id="uneven-code-span-h1"),
    pytest.param(f"``<h2>{_MARKER}</h2>` tail\n\nnormal explanatory text\n",
                 id="uneven-code-span-h2"),
    # 🔴 AN HTML BLOCK MAKES A FENCE THE RENDERER DOES NOT SEE. All four lines
    # are ONE type-6 block (it ends only at a blank line), so the ``` is literal
    # text and the `<h1>` is live — while the code read the ``` as an opener and
    # skipped demotion for everything after it.
    pytest.param(f"<div>\n```\n<h1>{_MARKER}</h1>\n</div>\n\nnormal text\n",
                 id="html-block-hides-the-fence"),
    pytest.param(f"<div>\n```\n<h2>{_MARKER}</h2>\n</div>\n\nnormal text\n",
                 id="html-block-hides-the-fence-h2"),
    # The SAME shape with a markdown heading instead of a raw tag. It renders no
    # heading at all on GitHub — markdown is not parsed inside an HTML block —
    # and that measured fact is what makes it sound to keep skipping ATX and
    # setext inside a believed fence while demoting raw HTML unconditionally.
    # Kept as a payload so the claim is re-driven rather than remembered.
    pytest.param(f"<div>\n```\n# {_MARKER}\n</div>\n\nnormal text\n",
                 id="html-block-atx-is-inert"),
    pytest.param(f"<div>\n```\n{_MARKER}\n====\n</div>\n\nnormal text\n",
                 id="html-block-setext-is-inert"),
    # 🔴 THE AMBIGUOUS TYPE-7 START. A type-7 HTML block may not interrupt a
    # PARAGRAPH, and the line above `<mytag>` here is a heading, which is not
    # one — so GitHub starts an HTML block, the ``` inside it is literal, and
    # the `<h1>` is live. The scanner cannot decide that question without a
    # paragraph parser, so it stops trying to: it emits a blank line after the
    # tag, which ends a type-7 block AND ends a paragraph, and from the next
    # line on both readings are in the same state. Here that makes the ```
    # a genuine fence for GitHub too — measured: no heading at all, where
    # before the fix GitHub rendered ['Implementation summary', 'PWNED',
    # 'Stats'].
    pytest.param(f"# heading\n<mytag>\n```\n<h1>{_MARKER}</h1>\n```\n\nnormal text\n",
                 id="type7-block-the-scanner-misses-raw-html"),
    pytest.param(f"# heading\n<mytag>\n```\n# {_MARKER}\n```\n\nnormal text\n",
                 id="type7-block-the-scanner-misses-atx-is-inert"),
    # 🔴 THE SAME AMBIGUITY WITH ONE BLANK LINE ADDED, WHICH IS THE WHOLE
    # DEFECT: the heading is OUTSIDE the block GitHub misdetects, where markdown
    # is live again, while the scanner is still inside a fence that never
    # opened. See `_jsx_doc` — every predecessor here is a shape a coder writes
    # by accident, and each one leaked `PWNED` as a top-level section AND lost
    # `## Stats` and the merge-boundary footer through the appended closer.
    pytest.param(_jsx_doc("### The component"), id="type7-blankline-atx"),
    pytest.param(_jsx_doc("The component\n============="),
                 id="type7-blankline-setext"),
    pytest.param(_jsx_doc("---"), id="type7-blankline-thematic-break"),
    pytest.param(_jsx_doc("- the component"), id="type7-blankline-list"),
    pytest.param(_jsx_doc("> a note about it"), id="type7-blankline-blockquote"),
    pytest.param(_jsx_doc("| col | col |\n| --- | --- |\n| a   | b   |"),
                 id="type7-blankline-table"),
    # The SWALLOW HALF ON ITS OWN. `<h1>` is demoted whatever the scanner
    # believes, so this one leaks no heading — and it still lost every section
    # after the summary, because the closer appended for the phantom fence is an
    # OPENER on GitHub. Measured before the fix: intruders=[], Stats absent. The
    # two harms are separable and this payload is the one that says so.
    pytest.param(_jsx_doc("### The component", f"<h1>{_MARKER}</h1>"),
                 id="type7-blankline-raw-html"),
    # 🔴 THE OTHER DIRECTION OF THE SAME DECISION, so that "assume a type-7
    # block always starts" cannot pass as the fix. Here the predecessor really
    # IS a paragraph, so GitHub does NOT start an HTML block: the ``` is a real
    # fence, it is never closed, and it swallows every section after the summary
    # unless a closer is appended. A scanner that always assumed the HTML block
    # would believe nothing was open and append nothing.
    #
    # IT ONLY BITES THROUGH THE REAL RENDERER, and that is stated rather than
    # left to be discovered: `_live_headings` assumes the block always starts
    # too, so the two offline tests pass this payload vacuously. It is here for
    # `test_the_live_heading_scanner_sees_what_github_sees`, which fails on it
    # at BOTH positions when that assumption is made.
    pytest.param(f"I documented the component.\n<UserCard />\n```jsx\n"
                 f"<UserCard name=\"a\" />\n## {_MARKER}\n",
                 id="type7-after-a-paragraph-unclosed-fence"),
    # 🔴 THE SAME PAYLOAD WITH THE COMPONENT RENAMED, and that rename was the
    # whole of the round-9 defect. `search` was in `_HTML_BLOCK_TAGS` because it
    # is in the CURRENT CommonMark type-6 list — and GitHub's renderer is not on
    # that spec: measured through `/markdown`, `<search>` does NOT interrupt a
    # paragraph, i.e. it is type 7 there. So this code believed a block started,
    # emitted no disambiguating blank line, and GitHub kept the paragraph open —
    # the coder's ```jsx a real unclosed fence there and literal text here, with
    # nothing appended to close it. Both the React component and the shipping
    # HTML5 `<search>` element swallowed `## Stats` and the merge-boundary
    # footer, at both payload positions.
    pytest.param(f"I documented the component.\n<Search />\n```jsx\n"
                 f"<Search name=\"a\" />\n## {_MARKER}\n",
                 id="search-after-a-paragraph-unclosed-fence"),
    pytest.param(f"I documented the element.\n<search>\n```jsx\n"
                 f"<Search name=\"a\" />\n## {_MARKER}\n",
                 id="search-lowercase-unclosed-fence"),
    # `source` is the divergence in the OTHER direction — GitHub starts a type-6
    # block for it and the spec list does not have it — which is why the tag list
    # is pinned by measurement rather than corrected once. It costs a blank line,
    # not a heading, so this payload is a control: it must stay green both before
    # and after the pin.
    pytest.param(f"I documented the element.\n<source>\n```jsx\n"
                 f"<Search name=\"a\" />\n## {_MARKER}\n",
                 id="source-after-a-paragraph-unclosed-fence"),
    # An EMPTY ATX heading. `#{1,6}\s+\S` required content after the hashes, so
    # a bare `#` was left alone and rendered `<h1></h1>` — no coder text
    # escapes, but the outline this function protects is polluted all the same.
    pytest.param(f"{_MARKER} was implemented.\n\n#\n\nnormal text\n",
                 id="empty-atx-heading"),
    # 🔴 A FENCE OPENED ON A LIST-MARKER LINE. `_FENCE_LINE` anchors at
    # `^ {0,3}(```|~~~)`, and a list marker is not that — so `- ```python`
    # opened NOTHING, the coder's own CLOSER at indent 2 was read as an OPENER,
    # and everything after it was believed in-fence. Both harms at once, the
    # round-7 pair reproduced inside the code written to prevent it: `## PWNED`
    # shipped as a live <h2> AND the column-0 closer appended for the phantom
    # swallowed `## Stats` and the whole merge-boundary footer, "It never merges
    # and never approves its own work" included. A coder putting a code block in
    # a bullet writes this by hand, with no adversarial intent at all.
    pytest.param(f"Here is how:\n\n- ```python\n  print(1)\n  ```\n\n"
                 f"## {_MARKER}\n\nnormal text\n", id="fence-in-a-bullet"),
    pytest.param(f"Here is how:\n\n- ~~~python\n  print(1)\n  ~~~\n\n"
                 f"## {_MARKER}\n\nnormal text\n", id="fence-in-a-bullet-tilde"),
    pytest.param(f"Here is how:\n\n> ```python\n> print(1)\n> ```\n\n"
                 f"## {_MARKER}\n\nnormal text\n", id="fence-in-a-blockquote"),
    # 🔴 A BLANK LINE ENDS A BLOCKQUOTE, AND THE CODE SAID BOTH CONTAINERS
    # SURVIVED ONE. The quote ends at the blank, the fence ends with it, and
    # `> ## PWNED` starts a NEW quote with a live <h2> in it — measured with
    # pandoc 3.8.3 and confirmed through GitHub's `/markdown`. The `-inert`
    # pair below is the other side: a QUOTED blank (`>`) is not a blank line
    # and keeps the block open, and a list item survives blanks outright, so a
    # fix that simply ended every container at a blank would leak nothing but
    # would mangle both of those. All four are here so neither side can drift.
    pytest.param(f"> ```python\n> print(1)\n\n> ## {_MARKER}\n\nnormal text\n",
                 id="blank-line-ends-a-blockquote-fence"),
    pytest.param(f">> ```python\n>> print(1)\n\n>> ## {_MARKER}\n\nnormal text\n",
                 id="blank-line-ends-a-nested-blockquote-fence"),
    pytest.param(f"> - ```python\n>   print(1)\n\n>   ## {_MARKER}\n\n"
                 f"normal text\n", id="blank-line-ends-a-list-inside-a-quote"),
    pytest.param(f"> ```python\n> print(1)\n>\n> ## {_MARKER} is inert\n\n"
                 f"normal text\n", id="a-quoted-blank-keeps-the-quote-open"),
    pytest.param(f"- ```python\n  print(1)\n\n\n  ## {_MARKER} is inert\n\n"
                 f"normal text\n", id="two-blanks-do-not-end-a-list-item"),
    pytest.param(f"Here is how:\n\n- - ```python\n    print(1)\n    ```\n\n"
                 f"## {_MARKER}\n\nnormal text\n", id="fence-in-a-nested-bullet"),
    pytest.param(f"1. ```python\n   print(1)\n   ```\n\n## {_MARKER}\n\n"
                 f"normal text\n", id="fence-in-an-ordered-item"),
    # The UNCLOSED variant, which is the other direction of the same decision.
    # A container fence ends with its CONTAINER — measured, `- ```py` + `  code`
    # renders `## Stats` LIVE — so believing it still open at the end and
    # appending a column-0 closer is what DESTROYS the footer (measured:
    # `heads=[]`). This payload fails if the fix over-corrects.
    pytest.param(f"Here is how:\n\n- ```python\n  print(1)\n\n## {_MARKER}\n\n"
                 f"normal text\n", id="unclosed-fence-in-a-bullet"),
    # …and the heading INDENTED into the item after it, which is genuinely
    # inert. A fix that simply gave up on container fences would leak here.
    pytest.param(f"Here is how:\n\n- ```python\n  print(1)\n\n  ## {_MARKER} is "
                 f"inert\n\nnormal text\n", id="unclosed-fence-in-a-bullet-inert"),
    # 🔴 INDENTED, AND STILL OUTSIDE. The rule was `line[:1] in (" ", "\t")` —
    # ANY indent counted as inside the item — in BOTH the code and this file's
    # oracle, so the two agreed with each other and the hermetic suite was
    # blind. A line stays in a list item only by reaching its CONTENT COLUMN: 2
    # for `- `, 3 for `1. `, 5 for `12.  `. One column short and the item ends,
    # the fence ends with it, and the heading is live. Measured with pandoc
    # 3.8.3 (`-f gfm`) on the body `_pr_body` delivered: `<h2 id="pwned">` — a
    # sibling of `## Task` and `## Stats`. The `-inert` payload above is the
    # other side of the same line and must stay green, so a fix cannot pass by
    # giving up on container fences; the pair pins the column from both sides.
    pytest.param(f"Here is how:\n\n- ```python\n  print(1)\n\n ## {_MARKER}\n\n"
                 f"normal text\n",
                 id="unclosed-fence-in-a-bullet-below-content-column"),
    pytest.param(f"1. ```python\n   print(1)\n\n  ## {_MARKER}\n\nnormal text\n",
                 id="unclosed-fence-in-an-ordered-item-below-content-column"),
    # The column is not a CONSTANT either. `12.  ` puts the content column at 5,
    # and the heading sits at 3 — outside the item, and still short of the 4
    # that would make it an indented code block, so it is LIVE. A hardcoded 2
    # OR 3 calls it inside and leaks it; only reading the opener's own marker
    # gets it right.
    pytest.param(f"12.  ```python\n     print(1)\n\n   ## {_MARKER}\n\n"
                 f"normal text\n",
                 id="unclosed-fence-in-a-wide-marker-below-content-column"),
    # 🔴 THE CLOSER IS MEASURED FROM THE CONTENT COLUMN TOO. Inside a `- ` item
    # a closing ``` may sit anywhere from column 2 to column 5, and the closer
    # test ran `_FENCE_LINE` (`^ {0,3}(```|~~~)`) against the WHOLE line — so
    # this closer at column 4 matched nothing, the fence was believed open past
    # its own end, and `  ## PWNED` after it shipped undemoted as a live <h2>
    # inside the list item. (A heading in a bullet is live: see `list-dash-atx`.)
    pytest.param(f"Here is how:\n\n- ```python\n    print(1)\n    ```\n\n"
                 f"  ## {_MARKER}\n\nnormal text\n",
                 id="container-fence-closer-indented-into-the-item"),
    # 🔴 TWO CONTAINER MARKERS. `_split_container` stripped at most ONE list
    # marker, so the remainder handed to the ATX regex still began with a marker
    # and never matched: `('- ', '- ## X', True)`. All five render live —
    # `<h2>` for the four ATX shapes and `<h1>` for the setext one, measured
    # with pandoc 3.8.3 on the raw payloads.
    # The setext one failed by a second route as well — the one-marker remainder
    # `- PWNED` matches `_NOT_A_PARAGRAPH`, so the underline was read as binding
    # to nothing and the demotion branch was skipped outright.
    pytest.param(f"- - ## {_MARKER}\n\nnormal explanatory text\n",
                 id="nested-list-atx"),
    pytest.param(f"- > ## {_MARKER}\n\nnormal explanatory text\n",
                 id="list-then-blockquote-atx"),
    pytest.param(f"- - {_MARKER}\n    ====\n\nnormal explanatory text\n",
                 id="nested-list-setext"),
    pytest.param(f"> - - ## {_MARKER}\n\nnormal explanatory text\n",
                 id="blockquote-nested-list-atx"),
    pytest.param(f"1. - ## {_MARKER}\n\nnormal explanatory text\n",
                 id="ordered-then-bullet-atx"),
    # 🔴 A MIXED CONTAINER PREFIX, WHICH IS A FOURTH LIVE-<h2> ROUTE AND THE
    # SAME DEFECT ONE MARKER OVER. `> - ` and `- > ` carry a blockquote marker
    # AND a list marker, so the opener's `c_depth` was non-zero and the depth
    # test answered the whole "still inside?" question — the CONTENT COLUMN,
    # which the round that fixed the pure-list case computes right beside it,
    # was never consulted. The long comment stating the column rule sat in the
    # branch a mixed container never reached.
    #
    # All three render a live `<h2>PWNED</h2>`, a sibling of `## Task` and
    # `## Stats`, measured on the raw text and on the body `_pr_body` delivers,
    # with pandoc 3.8.3 (`-f gfm`) AND GitHub's own `/markdown` (mode gfm). The
    # offline oracle in this file reported `[]` for all three — it carried the
    # identical rule, which is the pair-agreeing-with-itself failure this file
    # exists to make impossible, found a third time.
    #
    # Each is paired with the shape one column over, which is genuinely INERT
    # on both renderers (measured `[]`), so a fix cannot pass by giving up on
    # mixed containers and calling every line outside.
    pytest.param(f"Here is how:\n\n> - ```python\n> print(1)\n>\n> ## {_MARKER}"
                 f"\n\nnormal text\n", id="mixed-quote-list-quoted-blank"),
    pytest.param(f"Here is how:\n\n> - ```python\n>   print(1)\n>\n"
                 f">   ## {_MARKER} is inert\n\nnormal text\n",
                 id="mixed-quote-list-quoted-blank-inert"),
    pytest.param(f"Here is how:\n\n> - ```python\n> print(1)\n> ## {_MARKER}\n"
                 f"\nnormal text\n", id="mixed-quote-list-below-content-column"),
    pytest.param(f"Here is how:\n\n> - ```python\n>   print(1)\n"
                 f">   ## {_MARKER} is inert\n\nnormal text\n",
                 id="mixed-quote-list-below-content-column-inert"),
    pytest.param(f"Here is how:\n\n- > ```python\n  > print(1)\n> ## {_MARKER}"
                 f"\n\nnormal text\n", id="mixed-list-quote-outdented"),
    pytest.param(f"Here is how:\n\n- > ```python\n  > print(1)\n"
                 f"  > ## {_MARKER} is inert\n\nnormal text\n",
                 id="mixed-list-quote-outdented-inert"),
    # …and the opener written WITHOUT the quote marker's optional space. The
    # item's content sits two columns past the QUOTE MARKER, not three
    # characters into the line — the `>` swallows the space that was not
    # written. Measuring the whole line put the column at 3, so `>  ## PWNED`
    # read as inside the item and shipped live; measuring from the end of a
    # line's OWN quote markers gets both sides right with no special case.
    # Measured on pandoc 3.8.3 and on GitHub's `/markdown`; the `-inert` pair is
    # one column over and pins the over-correcting direction.
    pytest.param(f"Here is how:\n\n>- ```python\n>  print(1)\n>  ## {_MARKER}"
                 f"\n\nnormal text\n", id="mixed-bare-quote-marker-opener"),
    pytest.param(f"Here is how:\n\n>- ```python\n>   print(1)\n"
                 f">   ## {_MARKER} is inert\n\nnormal text\n",
                 id="mixed-bare-quote-marker-opener-inert"),
    # 🔴 THE TWO RULERS, AND THIS PAYLOAD IS THE ONLY THING THAT TELLS THEM
    # APART. `> - ` and `  > ` both end at column 4, so a rule that compares one
    # ABSOLUTE column calls this heading inside the item — while a renderer
    # strips `  > ` first and finds the heading at column 0 of the quote, two
    # columns short of the item. Measured on pandoc 3.8.3 and on GitHub's
    # `/markdown`: the raw text renders a live `<h2>PWNED</h2>`, and the
    # delivered body renders none. It is here because the 14,196-shape sweep
    # caught the absolute form ADDING this leak to a tree that did not have it,
    # and nothing in this list could see that.
    pytest.param(f"Here is how:\n\n- > ```python\n> - print(1)\n> - ```\n"
                 f"  > ## {_MARKER}\n\nnormal text\n",
                 id="mixed-two-rulers-not-one"),
    # 🔴 THE TAB. `_column_width` measures a marker in COLUMNS, a tab advancing
    # to the next 4-column stop, and NOTHING observed that: mutating it to
    # `len(s)` survived the entire suite. `-\t` is a content column of FOUR, so
    # `  ## PWNED` at column 2 is outside the item and live — measured on
    # pandoc and on GitHub — while the mutant reads the marker as 2 columns,
    # calls the heading inside, and ships it undemoted. The `-inert` pair is
    # the other side of the same column and dies if a fix over-corrects.
    pytest.param(f"Here is how:\n\n-\t```python\n    print(1)\n\n"
                 f"  ## {_MARKER}\n\nnormal text\n",
                 id="tab-marker-below-content-column"),
    pytest.param(f"Here is how:\n\n-\t```python\n    print(1)\n\n"
                 f"    ## {_MARKER} is inert\n\nnormal text\n",
                 id="tab-marker-at-content-column-inert"),
]


@pytest.mark.parametrize("payload", _HEADING_PAYLOADS)
def test_the_coder_cannot_author_a_top_level_section_of_the_pr(
    store, tmp_path, payload,
):
    """The guarantee, on the delivered-PR channel: the set of level-1/2 headings
    in the body is exactly the template's own. Anything the coder writes lands
    strictly below `## Changes`."""
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.acceptance_criteria = ["it works"]
    body = orch._pr_body(t, _Commit(), _said(payload))

    intruders = _intruders(_live_headings(body))
    assert intruders == [], (
        f"the coder authored a top-level section of the PR: {intruders}\n"
        f"--- body ---\n{body}")
    assert _MARKER in body, "the coder's text was deleted instead of demoted"
    # 🔴 THE OTHER DIRECTION, AND IT IS NOT COSMETIC. A leaf block left open by
    # the summary swallows everything the template renders after it — measured
    # for `<div>` + ``` (the appended fence closer became a real OPENER) and for
    # an unterminated `<pre>`. "No intruders" is satisfied by a body with no
    # sections at all, so the template's own outline is asserted too.
    assert "How I verified this" in _live_headings(body), (
        f"the summary swallowed the sections after it:\n--- body ---\n{body}")


@pytest.mark.parametrize("payload,preserved", [
    (f"- ```python\n  print(1)\n\n  ## {_MARKER} is inert\n\nnormal text\n",
     f"  ## {_MARKER} is inert"),
    (f"- ```python\n  print(1)\n\n\n  ## {_MARKER} is inert\n\nnormal text\n",
     f"  ## {_MARKER} is inert"),
    (f"1. ```python\n   print(1)\n\n   ## {_MARKER} is inert\n\nnormal text\n",
     f"   ## {_MARKER} is inert"),
    (f"> ```python\n> print(1)\n>\n> ## {_MARKER} is inert\n\nnormal text\n",
     f"> ## {_MARKER} is inert"),
    (f"- - ```python\n    print(1)\n\n    ## {_MARKER} is inert\n\nnormal text\n",
     f"    ## {_MARKER} is inert"),
    # The MIXED containers, whose column rule is new. These are what the fix
    # for `> - `/`- > ` must not trade away: a mixed-prefix rule that called
    # every line outside would close the three leaks and silently rewrite
    # these three code blocks instead.
    (f"> - ```python\n>   print(1)\n>   ## {_MARKER} is inert\n\nnormal text\n",
     f">   ## {_MARKER} is inert"),
    (f"- > ```python\n  > print(1)\n  > ## {_MARKER} is inert\n\nnormal text\n",
     f"  > ## {_MARKER} is inert"),
    # …and the `>` written WITHOUT its optional space, which reaches the same
    # content column: the marker consumes the space on both sides of the
    # comparison, so no special case is needed and this is the payload that
    # says so. Measured `[]` on pandoc and on GitHub.
    (f"- > ```python\n  >print(1)\n  >## {_MARKER} is inert\n\nnormal text\n",
     f"  >## {_MARKER} is inert"),
    (f">- ```python\n>   print(1)\n>   ## {_MARKER} is inert\n\nnormal text\n",
     f">   ## {_MARKER} is inert"),
    # The TAB marker: `-\t` is column 4, so an indent of 4 is inside the item.
    (f"-\t```python\n    print(1)\n\n    ## {_MARKER} is inert\n\nnormal text\n",
     f"    ## {_MARKER} is inert"),
])
def test_a_heading_genuinely_inside_a_code_block_is_not_rewritten(
    store, tmp_path, payload, preserved,
):
    """🔴 THE OVER-CORRECTION DIRECTION, WHICH NOTHING IN THIS FILE MEASURED.
    Every other assertion here is `no intruder heading`, and a scanner that
    ended EVERY container at the first blank line satisfies all of them — it
    leaks nothing, it just silently rewrites the coder's code block, turning
    `## x` inside a fenced example into `#### x`. Measured: that exact mutation
    (`inside = False` for a blank line) survived the whole payload matrix, both
    offline parsers and the GitHub renderer tests.

    So the inert shapes are asserted for what they must KEEP, not only for what
    they must not become. These are the shapes a coder writes when documenting
    markdown, and getting them wrong is a lie about their code — quieter than a
    leaked heading, and still a lie."""
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.acceptance_criteria = ["it works"]
    body = orch._pr_body(t, _Commit(), _said(payload))
    assert preserved in body, (
        f"a line that a renderer keeps INSIDE the code block was demoted, "
        f"which rewrites what the coder wrote:\n--- body ---\n{body}")
    assert _intruders(_pandoc_headings(body)) == [], body


@pytest.mark.parametrize("payload,leaks", [
    (f"- item\n\n    ## {_MARKER}\n\nnormal text\n", True),
    (f"- item\n    ## {_MARKER}\n\nnormal text\n", True),
    # The CONTROL, and the reason this is a limit rather than a one-line fix.
    (f"para\n\n    ## {_MARKER}\n\nnormal text\n", False),
])
def test_the_indent_four_hole_in_the_demoter_is_exactly_where_it_is_documented(
    store, tmp_path, payload, leaks,
):
    """🔴 A KNOWN OPEN HOLE, PINNED WHERE IT CAN BE SEEN RATHER THAN DESCRIBED
    IN A DOCSTRING NOBODY RE-READS. This asserts the CURRENT behaviour, not the
    guarantee — it is limit 3 on `_reformat_summary_markdown`, and it is here so
    that closing it, or widening it, is a RED test rather than a silent change.

    A marker would have been the other way to say this, and it is the wrong one
    in this repo: `tamper_guard` counts an added `@pytest.mark.xfail` as a test
    neutered in place, so the honest instrument is an assertion.

    THE HOLE. `atx` in `_reformat_summary_markdown` anchors at `^ {0,3}#`. Four
    columns is right at the TOP level — indent 4 is an indented code block there
    and `## X` is inert, which the third case measures — and wrong inside a list
    item, where those four columns are counted from the item's CONTENT column.
    Measured with pandoc 3.8.3 on the body `_pr_body` delivers. No fence is
    involved anywhere: this is the demoter, not the container-fence scanner.

    WHEN YOU FIX IT: flip `leaks` to False for the first two cases and delete
    this docstring's claim from `_reformat_summary_markdown`'s limit 3. The
    third case must keep passing — demoting an indented block at the top level
    would corrupt the captured command output `_clean_summary` preserves on
    purpose, which is why the cheap fix is not a fix.
    """
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.acceptance_criteria = ["it works"]
    body = orch._pr_body(t, _Commit(), _said(payload))
    intruders = _intruders(_pandoc_headings(body))
    if leaks:
        assert intruders == [_MARKER], (
            f"limit 3 has MOVED — an independent parser no longer sees the "
            f"documented leak here. If it was fixed, say so in "
            f"`_reformat_summary_markdown`; if it widened, this is a new "
            f"defect:\n{body}")
    else:
        assert intruders == [], (
            f"the top-level control leaked, which limit 3 does not cover:\n{body}")
        assert f"    ## {_MARKER}" in body, (
            f"the indented block was rewritten, which is the degradation any "
            f"fix for the two cases above must not cause:\n{body}")


@pytest.mark.parametrize("payload,leaks", [
    # The container ends because the QUOTE ended (no `>` on the code line), and
    # the coder's own closer at column 2 is then re-read as a fresh opener.
    (f"- > ```python\n  print(1)\n  ```\n## {_MARKER}\n\nnormal text\n", True),
    # …and because the CONTENT COLUMN was not reached (`- - ` wants 4, this is
    # 2). Same mechanism, a pure list container, no blockquote anywhere.
    (f"- - ```python\n  print(1)\n  ```\n## {_MARKER}\n\nnormal text\n", True),
    # THE CONTROL, and it is what makes this a limit rather than "delete the
    # fall-through re-scan". Here the closer is INSIDE the item, the container
    # fence closes on it properly, and the heading after it is demoted. A fix
    # for the two above that stopped re-scanning fall-through lines at all
    # would have to keep this green.
    (f"- - ```python\n    print(1)\n    ```\n## {_MARKER}\n\nnormal text\n",
     False),
])
def test_the_orphaned_closer_hole_is_exactly_where_it_is_documented(
    store, tmp_path, payload, leaks,
):
    """🔴 LIMIT 4 ON `_reformat_summary_markdown`, PINNED WHERE IT CAN BE SEEN.

    This asserts the CURRENT behaviour, not the guarantee — exactly as
    `test_the_indent_four_hole_in_the_demoter_is_exactly_where_it_is_documented`
    does for limit 3, and for the same reason: a limit described only in a
    docstring is a limit nobody re-drives.

    THE HOLE. When a container fence's item ends, the scanner falls through and
    re-scans that line as ordinary markdown. The coder's own closing ``` at
    columns 0-3 matches `_FENCE_LINE` as an OPENER there, so everything after
    it is believed in-fence and skips demotion — and the phantom fence also
    swallows `## Stats` and the merge-boundary footer. Both harms, measured
    through `_pr_body`, pandoc 3.8.3 and GitHub's own `/markdown`.

    HOW IT WAS FOUND, because that is the part worth keeping: a 14,196-shape
    sweep, not a hand-picked list. It is the WHOLE of the residue outside limit
    3 — 155 shapes, all of which write a closer.

    WHEN YOU FIX IT: flip `leaks` to False for the first two cases and delete
    limit 4 from `_reformat_summary_markdown`. The third case must keep passing.
    """
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.acceptance_criteria = ["it works"]
    body = orch._pr_body(t, _Commit(), _said(payload))
    intruders = _intruders(_pandoc_headings(body))
    if leaks:
        assert intruders == [_MARKER], (
            f"limit 4 has MOVED — an independent parser no longer sees the "
            f"documented leak here. If it was fixed, say so in "
            f"`_reformat_summary_markdown`; if it widened, this is a new "
            f"defect:\n{body}")
    else:
        assert intruders == [], (
            f"the in-item closer control leaked, which limit 4 does not "
            f"cover:\n{body}")
        assert "How I verified this" in _pandoc_headings(body), (
            f"the summary swallowed the sections after it:\n{body}")


@pytest.mark.parametrize("payload", _HEADING_PAYLOADS)
def test_the_same_holds_when_the_payload_is_NOT_the_first_thing_in_the_summary(
    store, tmp_path, payload,
):
    """🔴 THE POSITION IS PART OF THE INPUT, and position zero is the EASY one.

    `_summary_section` does `final_text.strip()`, which removes leading
    whitespace from the FIRST line of the summary and nothing else. So a payload
    that depends on indentation — `   # Heading` — was silently un-indented
    before `_reformat_summary_markdown` ever saw it, and the parametrized test
    above passed for a reason that had nothing to do with the code under test.
    Driven one paragraph down, where `.strip()` cannot reach, `   # Heading` and
    `  ## Heading` both rendered as live <h1>/<h2> on GitHub.

    Every payload therefore runs in BOTH positions. A coder's real summary
    almost never opens with the heading anyway — it opens with a sentence, which
    is exactly this shape.
    """
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.acceptance_criteria = ["it works"]
    body = orch._pr_body(t, _Commit(), _said(
        "Implemented the parser and added a regression test.\n\n" + payload))

    intruders = _intruders(_live_headings(body))
    assert intruders == [], (
        f"the coder authored a top-level section of the PR when the payload was "
        f"not the first thing in the summary: {intruders}\n--- body ---\n{body}")
    assert "How I verified this" in _live_headings(body), (
        f"the summary swallowed the sections after it:\n--- body ---\n{body}")


@pytest.mark.parametrize("payload", _HEADING_PAYLOADS)
@pytest.mark.parametrize("lead", ["", "Implemented the parser and added a test.\n\n"],
                         ids=["at-the-top", "one-paragraph-down"])
def test_the_offline_scanner_agrees_with_an_independent_parser(
    store, tmp_path, payload, lead,
):
    """🔴 THE STRUCTURAL FIX FOR THE ROUND THAT KEPT REPEATING, AND IT RUNS
    HERMETICALLY ON EVERY INVOCATION.

    `_live_headings` used to be trusted because its docstring said it was an
    independent implementation. It was not sharing the FUNCTION under test; it
    was sharing the AUTHOR, and therefore the bugs. Twice now a defect has been
    found where `_scan_leaf_blocks` was wrong, `_live_headings` was wrong in the
    same way, both hermetic heading tests went green, and only a hand-driven
    `gh api /markdown` found it. That is not a bug that was fixed — it is a
    class of bug the test suite could not see.

    The only thing that closes it is an oracle nobody here wrote. `pandoc`
    reads GFM and was written by other people from the spec, so a mistake in
    this file cannot propagate into it. Measured on the payload set before the
    fix: `_live_headings` reported ZERO intruders for `fence-in-a-bullet`,
    `nested-list-atx` and `list-then-blockquote-atx`, while pandoc reported
    `['PWNED']` for each — matching what GitHub actually rendered. It also
    caught `nested-list-setext`, where `_live_headings` reported the wrong TEXT
    (`'- PWNED'`) rather than nothing.

    🔴 IT PINS ON THE RAW PAYLOAD AS WELL AS THE BODY, AND THE FIRST VERSION OF
    THIS TEST DID NOT — WHICH MADE IT VACUOUS IN EXACTLY THE DIRECTION IT EXISTS
    FOR. Compared only on the rendered BODY, both scanners agree that a FIXED
    body is clean, so reverting `_strip_containers` to the one-marker form left
    this test GREEN: measured, that mutation survived. A scanner bug is only
    visible where a heading is LIVE, so the raw payload — which is the input
    with the heading still in it — is the thing the two parsers must agree
    about. On that comparison the same mutation reddens. Both halves are
    asserted: the payload comparison catches a scanner that has drifted alone,
    the body comparison catches the pair drifting together.

    WHY THE PAYLOAD COMPARISON IS EXACT AND THE BODY ONE IS INTRUDERS-ONLY.
    pandoc and GitHub genuinely disagree in exactly one place, and it is
    already documented on `_HTML_BLOCK_TAGS`: GitHub's type-6 tag list is not
    the current spec's, so for the `source` payloads pandoc follows the spec and
    reports `## Stats` swallowed where GitHub keeps it live. That is a property
    of the BODY (which has a `## Stats` to swallow) and of the SWALLOW
    direction, where GitHub is the authority and
    `test_the_live_heading_scanner_sees_what_github_sees` is the pin. The raw
    payload has no template sections in it, so the divergence has nothing to
    act on: measured, 136/136 exact agreement there.
    """
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.acceptance_criteria = ["it works"]

    # 1. The scanner itself, on the input where the heading is still LIVE.
    raw = lead + payload
    assert sorted(_live_headings(raw)) == sorted(_pandoc_headings(raw)), (
        f"`_live_headings` disagrees with a parser nobody here wrote about "
        f"what this text RENDERS — the oracle has drifted, and a drifted "
        f"oracle is how the same defect passed two review rounds\n"
        f"local ={sorted(_live_headings(raw))}\n"
        f"pandoc={sorted(_pandoc_headings(raw))}\n--- text ---\n{raw!r}")

    # 2. …and the guarantee itself, seen by the independent parser.
    body = orch._pr_body(t, _Commit(), _said(raw))
    independent = _intruders(_pandoc_headings(body))
    assert independent == [], (
        f"a parser nobody here wrote sees a coder-authored top-level section "
        f"that `_live_headings` does not: {independent}\n--- body ---\n{body}")
    assert independent == _intruders(_live_headings(body)), (
        f"`_live_headings` has drifted from an independent parser — which is "
        f"the failure this test exists for, not a reason to relax it\n"
        f"pandoc={independent}\nlocal={_intruders(_live_headings(body))}\n"
        f"--- body ---\n{body}")


def test_the_independent_parser_is_actually_reading_the_body(store, tmp_path):
    """The non-vacuity control. Every assertion above is `== []`, which a
    `_pandoc_headings` that returned `[]` for everything would satisfy — and a
    silently-broken invocation returns exactly that. So drive a body whose
    intruder pandoc MUST see, and fail if it does not."""
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.acceptance_criteria = ["it works"]
    body = orch._pr_body(t, _Commit(), _said("fine"))
    seen = _pandoc_headings(body + f"\n\n## {_MARKER}\n")

    assert _MARKER in seen, (
        f"the independent oracle read nothing — it is not an oracle: {seen}")
    assert "How I verified this" in seen, seen


# ── the carriers OUTSIDE `## Changes` ──────────────────────── #
#
# 🔴 `_reformat_summary_markdown` GUARDS ONE SECTION, AND THE BODY HAS EIGHT
# OTHER CHANNELS. Every payload above enters through the coder's summary, which
# is the only text the demoter ever sees. Everything else `_pr_body` renders —
# the reviewer's blocking findings, the intake Q&A, the assumptions, the
# sharpened criteria, the blocker diagnosis, the title, the ticket key, the
# abandoned-PR URLs, the test-evidence lines — was interpolated RAW, so a single
# `\n` inside any of them dropped the remainder to column 0. Driven through
# `_pr_body` and `/markdown` (mode gfm), all eight rendered a live
# `<h1>MERGED AND APPROVED BY NO_HUMAN</h1>`; the review-evidence one renders it
# INSIDE the section headed `## Review evidence`, which is the exact
# fabrication the branch exists to stop, and the reviewer prompt ASKS the model
# to quote decisive lines, so multi-line evidence is the expected shape.
#
# The payloads are shared across every channel deliberately: adding one covers
# all thirteen, and adding a channel covers every payload. What is NOT
# automated, and is a real limit rather than a closed hole: the CHANNEL list
# below is hand-written, so a fourteenth carrier added to `_pr_body` later is
# not discovered by this test.
_CARRIER_PAYLOADS = [
    pytest.param(f"quoted the decisive line:\n# {_MARKER}\nend of quote",
                 id="newline-then-atx"),
    pytest.param(f"quoted:\r# {_MARKER}", id="carriage-return-then-atx"),
    pytest.param(f"quoted:\n<h1>{_MARKER}</h1>", id="newline-then-raw-h1"),
    pytest.param(f"quoted:\n<h2>{_MARKER}</h2>", id="newline-then-raw-h2"),
    pytest.param(f"quoted:\n{_MARKER}\n====", id="newline-then-setext"),
    pytest.param(f"quoted:\n## {_MARKER}", id="newline-then-atx-h2"),
    # No newline needed: the value sits at the START of a list item's content,
    # which is a line start too. Flattening alone left these live — measured.
    pytest.param(f"# {_MARKER}", id="leading-atx"),
    pytest.param(f"## {_MARKER}", id="leading-atx-h2"),
    pytest.param(f"> ## {_MARKER}", id="leading-blockquote-atx"),
    pytest.param(f"- - ## {_MARKER}", id="leading-nested-list-atx"),
    pytest.param(f"1. ## {_MARKER}", id="leading-ordered-list-atx"),
    pytest.param(f"text <h1>{_MARKER}</h1> tail", id="inline-raw-h1"),
    pytest.param(f"```\n# {_MARKER}", id="leading-fence-then-atx"),
    # 🔴 NOT ONE CHARACTER OF WHITESPACE IN IT, which is the point. Every other
    # payload here contains a space or a newline, so a `ticket_url` guard that
    # only rejected whitespace would pass this matrix while still emitting
    # `[NH-1](http://t/x)<h1>PWNED</h1>()` — the `)` closes the link
    # destination and what follows is INLINE raw HTML, which GitHub renders.
    # Measured live through `_pr_body` and pandoc 3.8.3 before the fix.
    pytest.param(f")<h1>{_MARKER}</h1>(", id="paren-closes-a-link-then-raw-h1"),
    pytest.param(f")<h2>{_MARKER}</h2>(", id="paren-closes-a-link-then-raw-h2"),
]


def _carrier_channels(payload: str) -> dict:
    """name -> (task mutation, test_evidence) planting *payload* in one channel."""
    def _t(**kw):
        t = Task.new(kw.pop("title", "Add the thing"), repo_path="/r")
        t.acceptance_criteria = kw.pop("criteria", ["it works"])
        for k, v in kw.items():
            setattr(t, k, v)
        return t

    # `task.title` LEFT this matrix on 2026-08-21: the body no longer carries
    # the title at all (`## Task` repeated the PR title and was dropped), so
    # the title is not a body channel — it reaches the forge as the PR title,
    # through `scrub_outbound(..., "pr_title")`, a different surface.
    return {
        "acceptance_criteria": (_t(criteria=[payload]), None),
        "external_id": (_t(external_id=payload), None),
        # 🔴 THE CHANNEL THE ENUMERATION MISSED. `_ticket_line` interpolates the
        # tracker URL RAW into a markdown link destination, and the method above
        # it claims every cell goes through `_inline_cell`. It only renders when
        # `external_id` is set and the URL starts with `http`, so the payload is
        # planted AFTER a real-looking prefix — otherwise the channel would be
        # silently absent and this row would assert nothing.
        # `test_the_carrier_channels_are_actually_reaching_the_body` is what
        # makes that non-vacuity a check rather than a hope.
        "ticket_url": (
            _t(external_id="NH-1",
               context={"jira": {"url": f"http://tracker.invalid/x{payload}"}}),
            None),
        "abandoned_pr_urls": (
            _t(context={"abandoned_pr_urls": [payload]}), None),
        "intake_qa.question": (
            _t(context={"intake_qa": [{"question": payload, "answer": "a"}]}), None),
        "intake_qa.answer": (
            _t(context={"intake_qa": [{"question": "q?", "answer": payload}]}), None),
        "intake_qa.source": (
            _t(context={"intake_qa": [{"question": "q?", "answer": "a",
                                       "source": payload}]}), None),
        "assumptions": (_t(context={"assumptions": [payload]}), None),
        "original_criteria": (
            _t(context={"original_criteria": [payload]}), None),
        "blocker.root_cause_hypothesis": (
            _t(blocker={"root_cause_hypothesis": payload}), None),
        "blocker.question": (_t(blocker={"question": payload}), None),
        "review_history.blocking": (
            _t(context={"review_history": [
                {"passed": False, "sha": "", "blocking": [payload]},
                {"passed": True, "sha": ""}]}), None),
        "test_evidence.layers": (_t(), {"layers": [payload]}),
        "test_evidence.failing_tests": (
            _t(), {"ran": True, "ok": False, "passed": 1, "failed": 1,
                   "failing_tests": [payload]}),
    }


@pytest.mark.parametrize("payload", _CARRIER_PAYLOADS)
@pytest.mark.parametrize("channel", sorted(_carrier_channels("x")))
def test_no_channel_of_the_body_can_author_a_top_level_section(
    store, tmp_path, channel, payload,
):
    """The guarantee, restated for the whole body instead of one section: NO
    input to `_pr_body` may become an `<h1>`/`<h2>` of the PR.

    Asserted twice — once with this file's own scanner and once with a parser
    nobody here wrote — because the first of those has now been measured
    agreeing with a bug in the code it was standing in for.
    """
    orch = _orch(store, tmp_path)
    task, evidence = _carrier_channels(payload)[channel]
    body = orch._pr_body(task, _Commit(), _said("A normal summary."),
                         test_evidence=evidence)

    assert _intruders(_live_headings(body)) == [], (
        f"`{channel}` authored a top-level section of the PR: "
        f"{_intruders(_live_headings(body))}\n--- body ---\n{body}")
    assert _intruders(_pandoc_headings(body)) == [], (
        f"`{channel}` authored a top-level section an independent parser can "
        f"see:\n--- body ---\n{body}")
    assert "How I verified this" in _live_headings(body), (
        f"`{channel}` swallowed the sections after it:\n--- body ---\n{body}")
    # Neutralised, not deleted: a body that silently drops the reviewer's
    # evidence is a different lie from one that renders it as a heading.
    assert _MARKER in body, (
        f"`{channel}` deleted the text instead of neutralising it:\n{body}")


def test_the_carrier_channels_are_actually_reaching_the_body(store, tmp_path):
    """The non-vacuity control for the matrix above. Every assertion there is
    `== []`, which a channel that silently rendered NOTHING would satisfy — and
    several of these sections return "" when their input is empty, so a typo in
    a context key would produce a body with no payload in it and a green test.
    A payload that is inert in markdown must still be VISIBLE in every channel.
    """
    orch = _orch(store, tmp_path)
    for channel in sorted(_carrier_channels("x")):
        task, evidence = _carrier_channels(f"plain {_MARKER} text")[channel]
        body = orch._pr_body(task, _Commit(), _said("A normal summary."),
                             test_evidence=evidence)
        assert f"plain {_MARKER} text" in body, (
            f"`{channel}` never reached the body at all, so every assertion "
            f"about it is vacuous:\n{body}")


@pytest.mark.parametrize("predecessor,ambiguous", [
    ("### The component", True),
    ("The component\n=============", True),
    ("---", True),
    ("- the component", True),
    ("> a note about it", True),
    ("I documented the component.", True),
    ("I documented the component.\n", False),
])
def test_an_ambiguous_html_block_start_is_followed_by_a_blank_line(
    store, tmp_path, predecessor, ambiguous,
):
    """🔴 THE MECHANISM, PINNED WHERE NEITHER SCANNER CAN VOUCH FOR IT.

    A type-7 HTML start may not interrupt a paragraph, so whether `<UserCard />`
    opens an HTML block depends on what the line above it is — and neither
    `_scan_leaf_blocks` nor `_live_headings` decides that the way GitHub does.
    `_live_headings` assumes the block always starts, which is why the two
    heading tests above CANNOT catch the other cop-out ("assume it always
    starts, emit nothing"): they agree with it. Verified, not assumed — that
    mutation leaves every offline payload green, and is caught only by
    `test_the_live_heading_scanner_sees_what_github_sees`
    [`type7-after-a-paragraph-unclosed-fence`, both positions], where GitHub
    keeps the paragraph, the ```jsx is a real unclosed fence, and every section
    after the summary is swallowed.

    So the contract is asserted directly on the artifact instead: a blank line
    follows the ambiguous line. It ends a type-7 block AND ends a paragraph, so
    from the next line on the two readings are in the SAME state and nothing
    downstream can depend on which one GitHub picked. That is the whole fix, and
    it is the thing a scanner-based test cannot see.

    The last case is the over-firing direction: after a BLANK line no paragraph
    can be open, the reading is not ambiguous, and nothing is inserted — the
    coder's text is not rewritten for a question that has an answer.
    """
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _said(
        f"{predecessor}\n<UserCard />\n```jsx\n<UserCard name=\"a\" />\n```\n"))
    lines = body.split("\n")
    i = lines.index("<UserCard />")
    assert (lines[i + 1] == "") is ambiguous, (
        f"expected {'a' if ambiguous else 'no'} blank line after the type-7 "
        f"start:\n--- body ---\n{body}")
    # And the coder's own fence still opens — the blank goes AFTER the tag line
    # for exactly this reason. Before it, the fence would be literal text inside
    # the HTML block the insertion had just forced.
    assert "```jsx" in body


@pytest.mark.parametrize("lead", ["", "Implemented the parser and added a test.\n\n"],
                         ids=["at-the-top", "one-paragraph-down"])
def test_pasted_evidence_is_not_rewritten_behind_a_diverging_tag(
    store, tmp_path, lead,
):
    """🔴 THE HARM OPTION 1 WAS REJECTED FOR, HAPPENING ANYWAY THROUGH THE TAG
    LIST — and no heading test can see it.

    `_reformat_summary_markdown`'s docstring rejects "demote ATX unconditionally"
    because on pasted evidence it rewrites `# set up the env` to
    `### set up the env`. With `search` in `_HTML_BLOCK_TAGS` that is exactly
    what happened: the scanner believed a type-6 block started at `<search>`, so
    the ``` below it was literal text rather than a fence opener, so the `#`
    line was not fence-protected and grew two hashes — while GitHub, which reads
    `<search>` as type 7 and keeps the paragraph open, has a REAL fence there and
    renders the corrupted line inside a `<pre>`. Measured at both positions:
    ``<pre><code>### set up the env</code></pre>``.

    The heading assertions cannot catch this one — no heading leaks, the
    intruder set is `[]` and `Stats` is present — which is why it is asserted on
    the coder's own text instead. This test observes the ARTIFACT: the pasted
    line reaches the PR body byte-for-byte as the coder wrote it.
    """
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _said(
        lead + "I documented the element.\n<search>\n```\n"
        f"# set up the env\n```\n\n## {_MARKER}\n"))
    assert "# set up the env" in body and "### set up the env" not in body, (
        f"the coder's pasted evidence was rewritten:\n--- body ---\n{body}")
    # …and the heading half still holds, so this is an ADDITION to the guarantee
    # rather than a trade against it.
    assert f"#### {_MARKER}" in body, body


def test_the_coders_content_survives_demotion(store, tmp_path):
    """Demotion, not deletion — and by TWO, so `#` clears `##`. Removing the
    coder's words would be the D4 truthfulness bug pointed the other way."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _said(
        "# Top\n\nprose\n\n## Second\n\n### Third\n"))
    assert "### Top" in body
    assert "#### Second" in body
    assert "##### Third" in body
    assert "prose" in body


def test_a_setext_heading_becomes_a_demoted_heading_not_a_stray_rule(
    store, tmp_path,
):
    """The underline is consumed, so no `====` is left dangling under the text."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _said("Design notes\n====\n\nprose"))
    assert "### Design notes" in body
    assert "====" not in body, "the underline survived and renders as a rule"


def test_a_thematic_break_is_not_mistaken_for_a_setext_underline(
    store, tmp_path,
):
    """`---` after a BLANK line is an hr, and after a list item it is an hr too.
    Neither may be rewritten into a heading — over-firing eats the coder's text."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _said(
        "Implemented the thing.\n\n---\n\nmore prose\n\n- a list item\n---\n"))
    assert "### Implemented the thing." not in body
    assert "#### a list item" not in body
    assert "- a list item" in body


def test_the_stats_and_the_merge_boundary_never_land_inside_a_code_block(
    store, tmp_path,
):
    """The indented-fence desync, at its own worst input: `_close_orphaned_fence`
    counted the indented ``` as a fence, so the closer it appended at column 0
    was an OPENER, and everything after it — `## Stats`, the attempt counter, and
    the sentence that states no_human never merges — rendered as code."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _said(
        "here is the captured output:\n\n    ```\n    $ pytest -q\n    1 passed\n"),
        branch="nh/x", base="main", attempt_n=2)

    assert Orchestrator._open_fence_at_end(body) == "", (
        f"the body ends inside an open code fence:\n{body}")
    assert "How I verified this" in _live_headings(body), (
        f"## How I verified this was swallowed into a code block:\n{body}")
    live = [ln for ln in body.split("\n") if "never merges" in ln]
    assert live, "the merge-boundary footer vanished"


@pytest.mark.parametrize("opener,closer", [
    ("```", "```"), ("````", "````"), ("~~~", "~~~"), ("`" * 12, "`" * 12),
])
def test_an_orphaned_fence_is_closed_by_a_MATCHING_closer(opener, closer):
    """A bare ``` closes neither a ```` block nor a ~~~ one, so the old fixed
    `_FENCE_CLOSE` left the block open and the sections after it inside it."""
    text = f"output:\n{opener}\n$ pytest\n1 passed"
    closed = Orchestrator._close_orphaned_block(text)
    assert closed.endswith("\n" + closer)
    assert Orchestrator._open_fence_at_end(closed) == ""


@pytest.mark.parametrize("opener", ["- ```py", "> ```py", "- - ```py",
                                    "1. ```py", "> - ~~~py"])
def test_a_container_fence_is_never_reported_open_at_the_end(opener):
    """🔴 THE PREMISE THE HIGH-1 FIX RESTS ON, PINNED SEPARATELY FROM THE FIX.

    A fence opened inside a list item or a blockquote ends when its CONTAINER
    ends, and the template always emits a blank line and then a column-0
    `## …` after the summary — which ends every container. So an unclosed one
    leaks nothing, and `_close_orphaned_block` must append NOTHING.

    That is not a nicety. Measured through `/markdown` (mode gfm): `- ```py` +
    `  code`, unclosed, renders `## Stats` LIVE; the same text with a column-0
    ``` appended renders NO headings at all — `## Stats` and the whole
    merge-boundary footer swallowed. Appending the closer is the harm, not the
    fix, and the naive way to make the scanner track container fences would
    have introduced exactly that regression.
    """
    text = f"{opener}\n  code that is never closed"
    assert Orchestrator._open_fence_at_end(text) == "", (
        "a container fence was reported open, so a column-0 closer will be "
        "appended — which is an OPENER out there, not a closer")
    assert Orchestrator._close_orphaned_block(text) == text


def test_a_container_fence_still_suppresses_demotion_inside_the_container():
    """The control for the test above. "Report nothing open" is satisfied by a
    scanner that ignores container fences entirely — which is the state that
    leaked. Inside the container the fence must still be BELIEVED, so pasted
    output keeps its `#` comments instead of growing hashes."""
    out = Orchestrator._reformat_summary_markdown(
        "- ```sh\n  # set up the env\n  ```\n\ndone.")

    assert "  # set up the env" in out, (
        f"a `#` comment inside a bullet's code block was rewritten as a "
        f"heading — the corrupting-pasted-evidence harm:\n{out}")


def test_an_indented_fence_is_not_a_fence_and_is_not_closed():
    """Four spaces of indent is an indented code block; its ``` is content.
    Appending a closer for it is what created a real opener."""
    text = "output:\n\n    ```\n    $ pytest\n    1 passed"
    assert Orchestrator._open_fence_at_end(text) == ""
    assert Orchestrator._close_orphaned_block(text) == text


def test_a_fence_inside_an_html_block_is_not_a_fence_and_is_not_closed():
    """🔴 THE ROUND-7 FINDING, AT THE SCANNER. GitHub reads these four lines as
    one type-6 HTML block — it ends only at a blank line — so the ``` is
    literal text. Believing it was an opener made `_close_orphaned_block`
    append a ``` at column 0, which IS an opener, and GitHub then rendered
    ['Task', 'Acceptance criteria', 'Implementation summary'] and nothing
    else: `## Stats` and the merge-boundary footer were inside the code
    block."""
    text = "<div>\n```\n<h1>x</h1>\n</div>"
    assert Orchestrator._open_fence_at_end(text) == ""
    assert Orchestrator._close_orphaned_block(text) == text


@pytest.mark.parametrize("opener,closer", [
    ("<pre>", "</pre>"), ("<textarea>", "</pre>"), ("<!-- note", "-->"),
])
def test_an_unterminated_raw_text_html_block_is_closed(opener, closer):
    """The same harm by a different route, and the one a blank line does NOT
    fix: types 1-5 run past blank lines, so an unterminated `<pre>` eats every
    section after the summary. Measured on GitHub — a summary of `<pre>` plus
    one line rendered ['Task', 'Acceptance criteria', 'Implementation summary']
    and stopped. (CommonMark ends a type-1 block on ANY of `</script>`,
    `</pre>`, `</style>`, `</textarea>`, which is why `</pre>` closes the
    `<textarea>` case too.)"""
    text = f"here is the markup:\n\n{opener}\nsome output"
    closed = Orchestrator._close_orphaned_block(text)
    assert closed == f"{text}\n{closer}"
    assert Orchestrator._open_leaf_block_at_end(closed) == ("", "")


def test_a_terminated_html_block_is_left_exactly_alone():
    """The over-firing direction: nothing is appended when the block closed on
    its own, and a type-6 block needs nothing because the template's own blank
    line ends it."""
    for text in ("<pre>\noutput\n</pre>", "<div>\nx\n</div>", "<!-- note -->",
                 "plain prose with no markup at all"):
        assert Orchestrator._close_orphaned_block(text) == text, text


def test_a_backtick_info_string_does_not_open_a_fence():
    """```` ```` ```` with a backtick in the info string is not an opener
    (CommonMark), so treating it as one would close a block that never opened."""
    assert Orchestrator._open_fence_at_end("``` `x` is code\nplain text") == ""


def test_fenced_code_is_still_left_exactly_alone(store, tmp_path):
    """The non-regression direction: a `#` comment inside a real fence is not a
    heading and must not grow hashes, and pytest output must not grow bullets."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _said(
        "ran it:\n\n```bash\n# set up the env\nCRITERION: not a bullet\n```\n"))
    assert "# set up the env" in body
    assert "### set up the env" not in body
    assert "- CRITERION: not a bullet" not in body


def test_html_inside_an_inline_code_span_IS_rewritten_and_that_is_the_price(
    store, tmp_path,
):
    """🔴 THE CODE-SPAN EXEMPTION WAS DELETED, AND THIS PINS WHAT REPLACED IT.

    It used to hold that a coder writing about `<h2>` was not misquoted. The
    exemption was a second implementation of CommonMark's code-span rule and it
    was wrong in the permissive direction — it did not require the closing
    backtick run to match the opener — so ``` ``<h1>x</h1>` ``` parked a live
    `<h1>` in the "leave alone" partition. Removing the parser removes every
    way it can be wrong; the cost is a misquote inside a code span, where the
    text is display either way.

    Asserted as the NEW behaviour rather than deleted, so that restoring any
    code-span exemption has to come back through this test.
    """
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _said(
        "The template emits `<h2>` for each section, so I matched it."))
    assert "`<h4>`" in body, "the demotion no longer reaches inside a code span"
    assert "`<h2>`" not in body
    assert "so I matched it" in body, "the coder's sentence survives"
    assert [h for h in _live_headings(body) if h not in _TEMPLATE_H2] == []


def test_consecutive_criterion_lines_still_become_a_list(store, tmp_path):
    """H13's other half, unchanged by the fence/heading rework."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _said(
        "CRITERION 1: done\nCRITERION 2: also done\n"))
    assert "- CRITERION 1: done" in body
    assert "- CRITERION 2: also done" in body


def test_the_truncation_cap_holds_with_a_long_fence(store, tmp_path):
    """The closer is now variable-length, so the budget must reserve the LONGEST
    fence in the body — a fixed 4 let the result exceed the declared cap."""
    long_fence = "`" * 12
    payload = ("Implemented it.\n\n" + long_fence + "\n"
               + "\n".join(f"line {i} of captured output" for i in range(400)))
    cleaned = Orchestrator._clean_summary(payload)
    assert len(cleaned) <= Orchestrator._SUMMARY_MAX_CHARS, len(cleaned)
    assert Orchestrator._open_fence_at_end(cleaned) == ""


# ── the same payloads, through GitHub's OWN renderer ──────────────────────── #
#
# Opt-in (network + `gh` auth), so the hermetic suite stays hermetic:
#     NH_GITHUB_RENDERER=1 pytest tests/test_pr_body_truthfulness.py -k github
# This is what actually established the four defects above and their fix; the
# offline tests exist so a regression is caught without a network round trip,
# and this one exists so `_live_headings` cannot drift away from the renderer it
# is standing in for.

def _render_on_github(md: str) -> str:
    import json
    import shutil
    if not shutil.which("gh"):                                # pragma: no cover
        pytest.skip("gh is not installed")
    out = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "--method", "POST",
         "/markdown", "--input", "-"],
        input=json.dumps({"text": md, "mode": "gfm"}),
        capture_output=True, text=True)
    if out.returncode != 0:                                   # pragma: no cover
        pytest.skip(f"gh /markdown unavailable: {out.stderr.strip()[:200]}")
    return out.stdout


@pytest.mark.slow
@pytest.mark.skipif("not os.environ.get('NH_GITHUB_RENDERER')",
                    reason="set NH_GITHUB_RENDERER=1 to verify against GitHub")
@pytest.mark.parametrize("payload", _HEADING_PAYLOADS)
@pytest.mark.parametrize("lead", ["", "Implemented the parser and added a test.\n\n"],
                         ids=["at-the-top", "one-paragraph-down"])
def test_the_live_heading_scanner_sees_what_github_sees(
    store, tmp_path, payload, lead,
):
    """🔴 BOTH POSITIONS AGAINST THE REAL RENDERER, not just position zero.
    `_summary_section` does `final_text.strip()`, so position zero silently
    un-indents the first line — a payload driven only there is measured on an
    input the function never sees in the field. The masking is real and was
    measured; the hermetic pair of tests above already runs both, and the
    network one that PINS the offline scanner to GitHub has to cover the same
    ground or the pinning only holds for half of it."""
    orch = _orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    t.acceptance_criteria = ["it works"]
    body = orch._pr_body(t, _Commit(), _said(lead + payload))
    html = _render_on_github(body)

    # `_headings_from_html`, not a private copy of it: the copy here had no
    # `unescape` and no `re.I`, so GitHub's `&amp;` in the template's own
    # `## ⚠️ Assumptions & Open Questions` would have read as a coder-authored
    # intruder the moment a payload carried that section.
    rendered = _headings_from_html(html)
    assert _intruders(rendered) == [], (
        f"GitHub rendered a coder-authored top-level section:\n{html}")
    assert "How I verified this" in rendered, (
        f"GitHub swallowed the sections after the summary:\n{html}")
    # …and the offline scanner agrees with it, which is what lets the offline
    # tests stand in for this one.
    assert sorted(rendered) == sorted(_live_headings(body)), (
        f"_live_headings drifted from GitHub:\n"
        f"github={sorted(rendered)}\nlocal={sorted(_live_headings(body))}")


# 🔴 THE CARRIER CHANNELS, AGAINST THE RENDERER THEY ACTUALLY SHIP TO. The
# hermetic matrix above is checked by this file's scanner and by pandoc, and
# neither of those is GitHub — which is the authority, and which differs from
# the spec in at least one measured place. Without this the carrier fixes
# would rest on two offline parsers.
#
# IT SAMPLES, AND SAYS SO. The full matrix is 15 channels x 15 payloads = 225
# renderer round trips; this drives 15 + 15 = 30 of them. Every CHANNEL gets
# `_SAMPLING_PAYLOAD`, and every PAYLOAD goes through `review_history.blocking`
# — the channel that is live on every delivered PR and the one the incident was
# found on. A payload or channel that only leaks in an untested pairing is not
# covered here; it is covered hermetically, by two parsers, which is what the
# sampling trades against.

# 🔴 THE PAYLOAD THAT MAKES THAT TRADE HONEST, AND IT USED TO NOT. It was
# `quoted:\n# PWNED\n<h1>PWNED</h1>`, and the comment above it said it exercised
# all three of `_inline_cell`'s jobs at once. It exercised two:
# `_LINE_BREAKS.sub` folds it to `quoted: # PWNED <h1>PWNED</h1>`, which begins
# with `q`, so `_LEADING_BLOCK.match` returned None and the leading-marker
# ESCAPE — the job that was added because flattening alone still left `- # x`
# live inside a list item — never ran on the renderer channel at all. Leading
# with the marker fires all three, and
# `test_the_sampling_payload_fires_every_job_of_the_inline_cell` is what keeps
# that true rather than restating it.
_SAMPLING_PAYLOAD = f"# {_MARKER}\nquoted:\n## {_MARKER}\n<h1>{_MARKER}</h1>"


def test_the_sampling_payload_fires_every_job_of_the_inline_cell():
    """The justification for sampling the renderer matrix is that one payload
    exercises all three of `_inline_cell`'s jobs. That is a claim about a
    string, so it is checked as one — a prose justification that has quietly
    stopped being true is how a channel goes untested while looking covered."""
    folded = Orchestrator._LINE_BREAKS.sub(" ", _SAMPLING_PAYLOAD)
    assert folded != _SAMPLING_PAYLOAD, "job 1: nothing to fold"
    assert Orchestrator._LEADING_BLOCK.match(folded) is not None, (
        f"job 2: the leading-marker escape never fires on {folded!r}")
    assert Orchestrator._HTML_HEADING.search(_SAMPLING_PAYLOAD) is not None, (
        "job 3: no raw <h1>/<h2> to demote")
    # …and each job is load-bearing: switching it off leaks THIS payload.
    unescaped = _intruders(_live_headings(f"- {folded}"))
    assert unescaped and all(_MARKER in h for h in unescaped), (
        f"job 2 is not load-bearing on this payload — the folded but UNescaped "
        f"form must render a live heading inside a list item, or the assertion "
        f"above is checking a property nothing depends on: {unescaped}")
    assert _intruders(_live_headings(_SAMPLING_PAYLOAD)) == [_MARKER] * 3, (
        "jobs 1 and 3 are not load-bearing: the unfolded, undemoted payload "
        "must render live headings")


@pytest.mark.slow
@pytest.mark.skipif("not os.environ.get('NH_GITHUB_RENDERER')",
                    reason="set NH_GITHUB_RENDERER=1 to verify against GitHub")
@pytest.mark.parametrize("channel,payload", (
    [(c, _SAMPLING_PAYLOAD) for c in sorted(_carrier_channels("x"))]
    + [("review_history.blocking", p.values[0]) for p in _CARRIER_PAYLOADS]
))
def test_github_agrees_no_channel_can_author_a_top_level_section(
    store, tmp_path, channel, payload,
):
    orch = _orch(store, tmp_path)
    task, evidence = _carrier_channels(payload)[channel]
    body = orch._pr_body(task, _Commit(), _said("A normal summary."),
                         test_evidence=evidence)
    rendered = _headings_from_html(_render_on_github(body))

    assert _intruders(rendered) == [], (
        f"GitHub rendered a top-level section authored by `{channel}`: "
        f"{_intruders(rendered)}\n--- body ---\n{body}")
    assert "How I verified this" in rendered, (
        f"`{channel}` swallowed the sections after it on GitHub:\n{body}")
    # …and the offline pair agrees with GitHub, which is what lets the hermetic
    # matrix stand in for this test the rest of the time.
    assert sorted(rendered) == sorted(_live_headings(body)), (
        f"`_live_headings` drifted from GitHub on `{channel}`:\n"
        f"github={sorted(rendered)}\nlocal={sorted(_live_headings(body))}")


# 🔴 A FIXED UNIVERSE, NOT `_HTML_BLOCK_TAGS` ITSELF. The first version of the
# test below drove the code's own list, and that is fail-OPEN in the exact
# direction `source` failed in: delete a tag from the constant and it also
# leaves the probe, so the sweep goes green on the deletion. Measured — removing
# `source` left all 95 network runs and all 272 hermetic ones passing. This list
# is the code's type-6 tags UNION the adjacent HTML5 names that are deliberately
# not type 6, frozen here so that both adding and removing a tag is a change the
# probe still covers.
_TAG_PROBE_UNIVERSE = frozenset("""
    abbr address article aside audio base basefont blockquote body canvas
    caption center col colgroup dd details dialog dir div dl dt em embed
    fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 head
    header hgroup hr html iframe isindex legend li link main marquee menu
    menuitem meta nav nobr noframes noscript object ol optgroup option output p
    param picture search section source spacer span summary table tbody td
    template tfoot th thead title tr track ul video""".split())


@pytest.mark.slow
@pytest.mark.skipif("not os.environ.get('NH_GITHUB_RENDERER')",
                    reason="set NH_GITHUB_RENDERER=1 to verify against GitHub")
def test_the_html_block_tag_list_matches_github():
    """🔴 THE TAG LIST IS PINNED TO THE RENDERER, NOT TO THE SPEC — by driving
    every tag through the renderer rather than by reading the spec.

    `_HTML_BLOCK_TAGS` carried the comment "CommonMark's seven start conditions
    VERBATIM", and it was verbatim the CURRENT spec. GitHub's renderer is not on
    that spec, and the difference is not cosmetic: type 6 MAY interrupt a
    paragraph and type 7 may NOT, so a tag on the wrong side of that line makes
    `_scan_leaf_blocks` skip the disambiguating blank while GitHub keeps the
    paragraph open — the coder's ```jsx a real unclosed fence there and literal
    text here. `search` was on the wrong side and leaked exactly that.

    Two tags diverged when this was written, in OPPOSITE directions (`search`
    spec-6/GitHub-7, `source` spec-7/GitHub-6), which is why the answer is a
    measurement and not a one-line correction: a list that has drifted both ways
    will drift again, and the next move has to arrive as a red test.

    THE PROBE, and why it reads what it reads. A paragraph, then the tag, then
    `*x*`. If the tag opened an HTML block the emphasis is raw text inside that
    block; if it did not, the paragraph is still open and GitHub emits
    `<em>x</em>`. So `<em>` present == the tag did NOT interrupt == not type 6.
    That is the only property the scanner depends on, read directly off the
    renderer's output rather than inferred from a tag taxonomy.

    The NEGATIVE half matters as much as the positive one — `source` is the
    defect in that direction, a tag GitHub starts a block for that the list
    omits — so the probe is a FIXED universe (`_TAG_PROBE_UNIVERSE`) rather than
    the constant under test. Driving the constant itself is fail-open: deleting
    a tag from it deletes the tag from the probe too. That is not a hypothetical
    — it was this test's first version, and removing `source` left every run
    green.
    """
    tags = sorted(_TAG_PROBE_UNIVERSE)
    extra = sorted(Orchestrator._HTML_BLOCK_TAGS - _TAG_PROBE_UNIVERSE)
    assert not extra, (
        f"these type-6 tags are not in the probe universe, so nothing measures "
        f"them against GitHub: {extra}")

    github: dict[str, bool] = {}
    for i in range(0, len(tags), 12):                  # batched: 1 call per 12
        chunk = tags[i:i + 12]
        html = _render_on_github("\n\n".join(
            f"paragraph zz{n}zz\n<{t}>\n*zz{n}zz*"
            for n, t in enumerate(chunk, start=i)))
        for n, t in enumerate(chunk, start=i):
            # <em> == the paragraph stayed open == the tag did not interrupt it.
            github[t] = f"<em>zz{n}zz</em>" not in html
    assert any(github.values()) and not all(github.values()), (
        f"the probe answered the same way for all {len(tags)} tags, so it is "
        f"measuring nothing: {sorted(set(github.values()))}")

    # The code's own classification, read through the code path that uses it.
    code = {}
    for t in tags:
        m6 = Orchestrator._HTML_BLOCK_TYPE6.match(f"<{t}>")
        code[t] = bool(m6 and m6.group(1).lower() in Orchestrator._HTML_BLOCK_TAGS)

    drift = {t: (code[t], github[t]) for t in tags if code[t] != github[t]}
    assert not drift, (
        "this code's type-6 classification disagrees with GitHub's renderer "
        f"(tag: (code, github)): {drift}")


# ── `_inline_cell`, the one-line-cell contract, on its own ────────────────── #


@pytest.mark.parametrize("raw,expected", [
    # It FLATTENS: nothing may reach column 0.
    ("a\nb", "a b"),
    ("a\r\nb", "a b"),
    ("a\rb", "a b"),
    # It ESCAPES a leading block marker, because the start of a list item's
    # content is a line start too.
    ("# x", "\\# x"),
    ("###### x", "\\###### x"),
    ("> x", "\\> x"),
    ("- x", "\\- x"),
    ("* x", "\\* x"),
    ("+ x", "\\+ x"),
    ("1. x", "1\\. x"),
    ("42) x", "42\\) x"),
    ("```py", "\\```py"),
    ("~~~py", "\\~~~py"),
    ("===", "\\==="),
    ("---", "\\---"),
    # …and it leaves ORDINARY PROSE alone, which is why each alternative
    # carries the renderer's own follow condition. Escaping these would be a
    # cosmetic regression on every honest cell.
    ("*emphasis* matters", "*emphasis* matters"),
    ("-1 regression in the count", "-1 regression in the count"),
    ("#tag not a heading", "#tag not a heading"),
    ("1.5x slower", "1.5x slower"),
    ("x > y", "x > y"),
    ("`code` span", "`code` span"),
    # It DEMOTES raw headings, which are live inline in a list item.
    ("see <h1>X</h1>", "see <h3>X</h3>"),
    ("see <H2>X</H2>", "see <h4>X</h4>"),
])
def test_the_one_line_cell_contract(raw, expected):
    """`_inline_cell` is what guards the TEXT carriers, so its three jobs are
    pinned here rather than only through a rendered body. It is not what
    guards the ticket LINK DESTINATION — that is `_SAFE_LINK_DEST`, because a
    URL is not a text cell and this function would corrupt a good one."""
    assert Orchestrator._inline_cell(raw, None) == expected


def test_the_one_line_cell_honours_its_limit_and_its_absence():
    assert len(Orchestrator._inline_cell("x" * 500)) == 160
    assert len(Orchestrator._inline_cell("x" * 500, 400)) == 400
    assert len(Orchestrator._inline_cell("x" * 500, None)) == 500
    # Demotion is length-preserving, so the slice is exact even when it fires.
    assert len(Orchestrator._inline_cell("<h1>" + "x" * 500, 160)) == 160


# ── L2: a docstring asserting a whole-file property nothing in the file pinned ─ #


def test_the_summary_reformatter_has_exactly_the_callers_its_docstring_names():
    """`_reformat_summary_markdown` records that it is NOT IDEMPOTENT and is
    safe only because each caller runs it once. That safety argument is about
    the CALL GRAPH, so it must be checked against the call graph.

    It once drifted: the docstring said "the only call graph there is —
    `_summary_section` calls this once" while `_quote_agent_reason`, four
    hundred lines above it, had already become a second caller on the very
    branch whose subject is claims the code does not establish.
    `_quote_agent_reason` was deleted with the abandon-path comment it served
    (refile of 1dfed378), so `_summary_section` is the only caller again —
    but discovered from the AST, not asserted from memory, so a caller
    reappearing (or one running it twice) reddens this instead of silently
    making the docstring false again. Redefining the property is the fix when
    that happens, not deleting the test.
    """
    import ast
    import inspect
    import no_human.core.orchestrator as orch_mod

    tree = ast.parse(inspect.getsource(orch_mod))
    calls: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        n = sum(
            1 for c in ast.walk(node)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "_reformat_summary_markdown"
        )
        if n:
            calls[node.name] = n

    assert calls == {"_summary_section": 1}, (
        "the call graph moved out from under `_reformat_summary_markdown`'s "
        "non-idempotence note — every caller must apply it exactly ONCE, and "
        "the docstring must name the callers that exist: " + repr(calls))


# ── the Evidence table's ROW cells: a `|` must not truncate a row ────────── #
#
# `c514fba1d` gave the reviewer-checklist table its own escaper (`_table_cell`
# = `_inline_cell` + the idempotent `_TABLE_PIPE` pipe escape) after a `|` in
# a checklist item silently dropped that row's remaining columns. The SAME
# defect was still live in the Evidence table's own rows: `_evidence_section`
# built the CI/Tests/Verifiers/Merge-policy rows through raw `_inline_cell`,
# which never escapes a pipe (most of its callers are not table cells at
# all — fold items, paragraphs, list bullets — so it was never asked to).
# `ci_state` (a CI provider's free-text summary), a test layer's summary
# line, a joined `verifier_id` list and a merge-policy rule-name list are
# all routine carriers of a literal `|` — a shell pipeline in a failing
# command, a regex, an "a | b" union in a rule's own name — so this was not
# a hypothetical, just unexercised.
#
# Every test below is pinned against a REAL renderer (pandoc, and — opt-in —
# GitHub's own `/markdown` endpoint), not a hand-rolled GFM pipe-counting
# heuristic: the `#642` review flagged exactly that anti-pattern
# (`.replace("\\|", "").count("|")`, which cannot tell "the pipe count is
# balanced" from "the row means what it says"). Reading the ACTUAL cell
# text a renderer produces is the only check that catches a truncated row
# the way a human reading the rendered PR would.


def _cells_from_html(html: str) -> list[list[str]]:
    """Every rendered TABLE ROW's cell texts, tags stripped and entities
    unescaped, one row per `<tr>` in document order — spanning `<thead>`
    and `<tbody>` alike, the same way `_headings_from_html` already spans
    `<h1>` and `<h2>` without caring which section wraps them.
    """
    rows: list[list[str]] = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [
            htmlmod.unescape(re.sub(r"<[^>]+>", "", c)).strip()
            for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, re.S | re.I)
        ]
        if cells:
            rows.append(cells)
    return rows


def _pandoc_cells(md: str) -> list[list[str]]:
    """*md*'s table rows according to `pandoc`, read as GFM — the same
    independent-oracle discipline as `_pandoc_headings` (see its docstring),
    applied to table cells instead of headings: nobody who wrote the escaper
    also wrote this reader.
    """
    if not shutil.which("pandoc"):                            # pragma: no cover
        pytest.skip("pandoc is not installed — the independent oracle is the "
                    "one thing that cannot be stubbed, so this skips rather "
                    "than falling back to a hand-rolled pipe count")
    out = subprocess.run(["pandoc", "-f", "gfm", "-t", "html", "--wrap=none"],
                         input=md, capture_output=True, text=True)
    if out.returncode != 0:                                   # pragma: no cover
        pytest.skip(f"pandoc failed: {out.stderr.strip()[:200]}")
    return _cells_from_html(out.stdout)


def _github_cells(md: str) -> list[list[str]]:
    """*md*'s table rows according to GitHub's OWN renderer. Reuses
    `_render_on_github`'s `gh`-absent / unauthenticated / rate-limited /
    network-failure skip semantics wholesale — callers gate on
    `NH_GITHUB_RENDERER` themselves, exactly like every other
    github-suffixed test in this file.
    """
    return _cells_from_html(_render_on_github(md))


def _verifier_dict(*, verifier_id: str, passed: bool, evidence: str = "e",
                    file: str = "", line: int = 0) -> dict:
    """Mirrors `tests/test_pr_evidence.py:149`'s helper of the same name and
    shape. Not imported from there — it is that module's own private test
    helper — so the fields `_verifiers_evidence_section` actually reads
    (`verifier_id`, `passed`, `unavailable`, `evidence`, `file`, `line`,
    `files_checked`) are duplicated here rather than crossing a test-module
    boundary for it.
    """
    return {
        "verifier_id": verifier_id, "passed": passed, "unavailable": False,
        "evidence": evidence, "file": file, "line": line,
        "files_checked": [],
    }


def _evidence_slice(body: str) -> str:
    return body.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]


def test_a_pipe_in_the_ci_state_keeps_the_ci_row_two_cells(store, tmp_path):
    """RED before the fix: `_evidence_section`'s CI row used to interpolate
    `evidence.ci_state` through `_inline_cell` (no pipe escape). A CI
    provider's free-text summary routinely carries one (`"failing: a | b"`
    reads as "failing on job a, or job b" — ordinary shell/log text, not an
    attack), and the raw `|` silently ended the row's second cell early:
    pandoc read the row as three cells (`['CI', 'failing: a', 'b']`), the
    third with nowhere to go in a two-column table. After the fix the row
    goes through `_table_cell`, so the pipe survives escaped and pandoc
    reads exactly two cells with the summary intact.
    """
    orch = _orch(store, tmp_path)
    task = Task.new("fix the flaky retry", repo_path="/r")
    task.context = {"ci_status": "failing: a | b"}
    body = orch._pr_body(task, _Commit(), _Result())
    ev = _evidence_slice(body)
    # Unconditional (no oracle, cannot skip): the rendered line itself
    # carries the escaped pipe.
    assert "| CI | failing: a \\| b |" in ev, ev
    rows = _pandoc_cells(ev)
    ci_rows = [r for r in rows if r and r[0] == "CI"]
    assert ci_rows == [["CI", "failing: a | b"]], (
        f"pandoc did not read the CI row as exactly two cells: {ci_rows}\n{ev}")


def test_a_pipe_in_a_test_layer_summary_keeps_the_tests_row_two_cells(
    store, tmp_path,
):
    """Same defect, the Tests row: a layer's summary line comes from the
    test runner (`testing/runner.py`) and can read like `"PASS | 10
    passed"` — a pipe is unremarkable in a one-line test-layer summary.
    """
    orch = _orch(store, tmp_path)
    task = Task.new("fix the flaky retry", repo_path="/r")
    test_evidence = {"ran": True, "ok": True, "layers": ["PASS | 10 passed"]}
    body = orch._pr_body(task, _Commit(), _Result(), test_evidence=test_evidence)
    ev = _evidence_slice(body)
    assert "| Tests | PASS \\| 10 passed |" in ev, ev
    rows = _pandoc_cells(ev)
    tests_rows = [r for r in rows if r and r[0] == "Tests"]
    assert tests_rows == [["Tests", "PASS | 10 passed"]], (
        f"pandoc did not read the Tests row as exactly two cells: {tests_rows}\n{ev}")


def test_a_pipe_in_a_verifier_id_keeps_the_verifiers_row_two_cells(
    store, tmp_path,
):
    """Same defect, the Verifiers row: its cell is `verifiers_pin()`, which
    joins failing `verifier_id`s (`pr_evidence.py`'s `verifiers_pin`) — a
    rule id is config/model-authored text, not a closed set.
    """
    orch = _orch(store, tmp_path)
    task = Task.new("fix the flaky retry", repo_path="/r")
    commit = _Commit()
    commit.sha = "c" * 40
    task.context = {"verifier_results": {commit.sha: [
        _verifier_dict(verifier_id="no|todo", passed=False, evidence="x"),
    ]}}
    body = orch._pr_body(task, commit, _Result())
    ev = _evidence_slice(body)
    assert "no\\|todo" in ev, ev
    rows = _pandoc_cells(ev)
    v_rows = [r for r in rows if r and r[0] == "Verifiers"]
    assert len(v_rows) == 1 and len(v_rows[0]) == 2, (
        f"pandoc did not read the Verifiers row as exactly two cells: {v_rows}\n{ev}")
    assert "no|todo" in v_rows[0][1]


def test_a_pipe_in_the_merge_policy_summary_keeps_the_row_two_cells(
    store, tmp_path,
):
    """Same defect, the Merge-policy row: its cell is `merge_policy_pin()`,
    exactly `PolicyVerdict.summary` (`core/merge_policy.py`), which joins
    failed RULE NAMES — driven through a REAL `PolicyVerdict`, mirroring
    `tests/test_pr_evidence.py:386`, not a hand-built dict.
    """
    from no_human.core.merge_policy import PolicyVerdict, RuleVerdict

    orch = _orch(store, tmp_path)
    task = Task.new("fix the flaky retry", repo_path="/r")
    verdict = PolicyVerdict(
        ready=False,
        rules=(RuleVerdict(name="review | passed", passed=False, detail="x"),),
        source="default",
    )
    assert "|" in verdict.summary, "fixture must actually exercise the defect"
    body = orch._pr_body(task, _Commit(), _Result(), merge_policy=verdict.as_dict())
    ev = _evidence_slice(body)
    assert "review \\| passed" in ev, ev
    rows = _pandoc_cells(ev)
    mp_rows = [r for r in rows if r and r[0] == "Merge policy"]
    assert len(mp_rows) == 1 and len(mp_rows[0]) == 2, (
        f"pandoc did not read the Merge policy row as exactly two cells: "
        f"{mp_rows}\n{ev}")
    assert "review | passed" in mp_rows[0][1]


def test_the_evidence_rows_double_escape_nothing(store, tmp_path):
    """Idempotency, both at the string level and the source level.
    `_TABLE_PIPE`'s own docstring names the risk it guards against: an
    ALREADY-escaped `a\\|b` must stay `a\\|b`, not become `a\\\\|b` (an
    escaped backslash followed by a live column break — the same defect one
    layer down). And there must be exactly ONE pipe-escaper in the source —
    this fix routes MORE call sites through the existing `_table_cell`, it
    does not add a second escaper beside it.
    """
    orch = _orch(store, tmp_path)
    task = Task.new("fix the flaky retry", repo_path="/r")
    task.context = {"ci_status": r"a\|b"}
    body = orch._pr_body(task, _Commit(), _Result())
    ev = _evidence_slice(body)
    assert "| CI | a\\|b |" in ev, ev
    rows = _pandoc_cells(ev)
    ci_rows = [r for r in rows if r and r[0] == "CI"]
    assert ci_rows == [["CI", "a|b"]], ci_rows

    assert (Orchestrator._table_cell(Orchestrator._table_cell(r"a\|b", None), None)
            == Orchestrator._table_cell(r"a\|b", None))

    import inspect
    import no_human.core.orchestrator as orch_mod
    src = inspect.getsource(orch_mod)
    defs = re.findall(r"^\s*_TABLE_PIPE\s*=\s*re\.compile", src, re.M)
    assert len(defs) == 1, (
        f"expected exactly one `_TABLE_PIPE` definition, found {len(defs)} — "
        "a second escaper was added instead of reusing the existing one")


def test_escaped_rows_still_split_as_rows(store, tmp_path):
    """Row-shape invariant: `_split_rows` (`orchestrator.py`) classifies a
    line as a table row purely by `startswith("| ")` / `endswith(" |")`.
    Escaping only inserts a backslash BEFORE an interior pipe, so a value
    ending in a pipe (`_TABLE_PIPE` still fires on the last character) must
    still produce a line `_split_rows` puts in `rows`, not `rest` — the
    escape must never shift where the row's own closing `" |"` sits.
    """
    orch = _orch(store, tmp_path)
    task = Task.new("fix the flaky retry", repo_path="/r")
    task.context = {"ci_status": "trailing pipe |"}
    body = orch._pr_body(task, _Commit(), _Result())
    ev = _evidence_slice(body)
    line = next(l for l in ev.splitlines() if l.startswith("| CI |"))
    assert line == "| CI | trailing pipe \\| |", line
    rows, rest = Orchestrator._split_rows(line + "\n")
    assert rows.strip("\n") == line, (
        f"an escaped trailing pipe knocked the row out of `_split_rows`'s "
        f"`rows` bucket: rows={rows!r} rest={rest!r}")
    assert rest == ""


def test_inline_cell_still_passes_pipes_through():
    """OUT OF SCOPE, pinned rather than assumed: `_inline_cell` itself must
    not gain pipe-escaping — it guards every NON-table text carrier (fold
    items, list items, paragraphs), where a raw `|` is literal and harmless.
    Only `_table_cell` (a distinct wrapper) escapes; this is the case the
    existing parametrized `test_the_one_line_cell_contract` table does not
    cover.
    """
    assert Orchestrator._inline_cell("a | b", None) == "a | b"


def test_a_verifier_fold_item_keeps_its_raw_pipe(store, tmp_path):
    """The per-rule bullet under the Verifiers `<details>` fold is a LIST
    ITEM, not a table cell — `_verifiers_evidence_section`'s own docstring
    says it deliberately stays on `_inline_cell`. Pinning the deliberate
    non-change: a `|` in a verifier's `evidence` string (the judge's own
    prose, quoting a diff) must reach the body unescaped.
    """
    orch = _orch(store, tmp_path)
    task = Task.new("fix the flaky retry", repo_path="/r")
    commit = _Commit()
    commit.sha = "d" * 40
    task.context = {"verifier_results": {commit.sha: [
        _verifier_dict(verifier_id="no-todo", passed=False,
                        evidence="found a | pattern", file="c.py", line=4),
    ]}}
    body = orch._pr_body(task, commit, _Result())
    ev = _evidence_slice(body)
    assert "found a | pattern" in ev, ev
    assert "found a \\| pattern" not in ev, ev


def _ci_github_fixture(orch):
    task = Task.new("fix the flaky retry", repo_path="/r")
    task.context = {"ci_status": "failing: a | b"}
    body = orch._pr_body(task, _Commit(), _Result())
    return body, "CI", ["CI", "failing: a | b"]


def _tests_github_fixture(orch):
    task = Task.new("fix the flaky retry", repo_path="/r")
    test_evidence = {"ran": True, "ok": True, "layers": ["PASS | 10 passed"]}
    body = orch._pr_body(task, _Commit(), _Result(), test_evidence=test_evidence)
    return body, "Tests", ["Tests", "PASS | 10 passed"]


def _verifiers_github_fixture(orch):
    task = Task.new("fix the flaky retry", repo_path="/r")
    commit = _Commit()
    commit.sha = "e" * 40
    task.context = {"verifier_results": {commit.sha: [
        _verifier_dict(verifier_id="no|todo", passed=False, evidence="x"),
    ]}}
    body = orch._pr_body(task, commit, _Result())
    return body, "Verifiers", None


def _merge_policy_github_fixture(orch):
    from no_human.core.merge_policy import PolicyVerdict, RuleVerdict
    task = Task.new("fix the flaky retry", repo_path="/r")
    verdict = PolicyVerdict(
        ready=False,
        rules=(RuleVerdict(name="review | passed", passed=False, detail="x"),),
        source="default",
    )
    body = orch._pr_body(task, _Commit(), _Result(), merge_policy=verdict.as_dict())
    return body, "Merge policy", None


@pytest.mark.slow
@pytest.mark.skipif("not os.environ.get('NH_GITHUB_RENDERER')",
                    reason="set NH_GITHUB_RENDERER=1 to verify against GitHub")
@pytest.mark.parametrize(
    "fixture",
    [_ci_github_fixture, _tests_github_fixture, _verifiers_github_fixture,
     _merge_policy_github_fixture],
    ids=["ci", "tests", "verifiers", "merge-policy"],
)
def test_the_evidence_rows_survive_github_for_real(store, tmp_path, fixture):
    """The real-renderer pin for all four row families in one test, opt-in
    exactly like `test_the_live_heading_scanner_sees_what_github_sees`
    above (`NH_GITHUB_RENDERER=1`): asserts the same two-cells-per-row
    property AND that pandoc agrees with GitHub, the file's existing
    "oracle can't drift" discipline (see `test_the_live_heading_scanner_
    sees_what_github_sees`'s own closing assertion).
    """
    orch = _orch(store, tmp_path)
    body, label, expected = fixture(orch)
    ev = _evidence_slice(body)
    github_rows = [r for r in _github_cells(ev) if r and r[0] == label]
    pandoc_rows = [r for r in _pandoc_cells(ev) if r and r[0] == label]
    assert len(github_rows) == 1 and len(github_rows[0]) == 2, (
        f"GitHub did not read the {label} row as exactly two cells: "
        f"{github_rows}\n{ev}")
    assert github_rows == pandoc_rows, (
        f"pandoc and GitHub disagree on the {label} row:\n"
        f"pandoc={pandoc_rows}\ngithub={github_rows}\n{ev}")
    if expected is not None:
        assert github_rows == [expected]
