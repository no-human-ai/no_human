"""Tests for the PR comment watcher (Phase C — WS-C)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from no_human.vcs.pr_watcher import (
    PrComment,
    PrFeedback,
    check_pr_comments,
)
from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus


# --------------------------------------------------------------------------- #
# PrComment / PrFeedback unit tests                                           #
# --------------------------------------------------------------------------- #


def test_pr_comment_basic():
    c = PrComment(author="alice", body="Fix the null check", created_at="2026-01-01T00:00:00Z")
    assert c.author == "alice"
    assert c.body == "Fix the null check"


def test_pr_feedback_to_send_back_entries():
    feedback = PrFeedback(
        pr_url="https://github.com/org/repo/pull/42",
        comments=[
            PrComment(
                author="alice", body="This is wrong",
                path="src/main.py", line=10,
                diff_hunk="- old_line\n+ new_line",
                created_at="2026-01-01T00:00:00Z",
            ),
            PrComment(
                author="bob", body="Please fix",
                created_at="2026-01-01T01:00:00Z",
            ),
        ],
    )
    entries = feedback.to_send_back_entries()
    assert len(entries) == 2

    # First entry: inline comment with path + line + diff hunk.
    assert "[src/main.py:10]" in entries[0]["message"]
    assert "This is wrong" in entries[0]["message"]
    assert "old_line" in entries[0]["message"]
    assert entries[0]["author"] == "alice"
    assert entries[0]["source"] == "pr_comment"

    # Second entry: general comment, no path.
    assert "Please fix" in entries[1]["message"]
    assert entries[1]["author"] == "bob"


def test_pr_feedback_empty():
    feedback = PrFeedback(pr_url="https://x", comments=[])
    assert feedback.to_send_back_entries() == []


# --------------------------------------------------------------------------- #
# check_pr_comments dispatch logic (no real CLI — tests format parsing)        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_check_pr_comments_unrecognized_format():
    # Unrecognized format → empty list, no crash.
    result = await check_pr_comments("just_a_string")
    assert result == []


@pytest.mark.asyncio
async def test_check_pr_comments_bad_github_number():
    result = await check_pr_comments("org/repo#abc")
    assert result == []


@pytest.mark.asyncio
async def test_check_pr_comments_bad_gitlab_iid():
    result = await check_pr_comments("project_id!abc")
    assert result == []


# --------------------------------------------------------------------------- #
# Wake condition: pr_comment_on:<ref>                                         #
# --------------------------------------------------------------------------- #


def _cfg(**over):
    base = {"blockers": {"max_park_duration": "48h"}}
    base["blockers"].update(over)
    return base


async def _park(store, *, status, blocker, wake_at=None):
    t = Task.new("PR task", repo_path="/tmp/r")
    await store.create_task(t)
    t.blocker = blocker
    t.wake_check_at = wake_at
    await store.update_task(t)
    await store.set_status(t, status, validate=False)
    return t


@pytest.mark.asyncio
async def test_pr_comment_condition_resumes_with_feedback(store):
    """When pr_comment_on fires, the task gets resumed AND comments are injected."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    comment = PrComment(author="reviewer", body="Fix the edge case", created_at=now.isoformat())

    async def pr_comment_checker(ref):
        assert ref == "org/repo#42"
        return [comment]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions

    # Verify comments were injected into task context.
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    feedback = refreshed.context.get("send_back_feedback", [])
    assert len(feedback) >= 1
    assert "Fix the edge case" in feedback[-1]["message"]
    assert feedback[-1]["source"] == "pr_comment"


@pytest.mark.asyncio
async def test_pr_comment_condition_no_comments_not_satisfied(store):
    """If the PR has no new comments, condition is not satisfied."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    async def pr_comment_checker(ref):
        return []  # no new comments

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_pr_comment_condition_no_checker_not_satisfied(store):
    """No checker wired → never satisfied."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_pr_comment_condition_checker_error_safe(store):
    """Checker throwing → not satisfied, not crashed."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    async def pr_comment_checker(ref):
        raise RuntimeError("API down")

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_pr_comment_inline_formatting(store):
    """Inline comments (with path/line) get formatted with file:line prefix."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    comment = PrComment(
        author="alice", body="Null check missing",
        path="src/handler.py", line=55,
        diff_hunk="+ if value is not None:",
        created_at=now.isoformat(),
    )

    async def pr_comment_checker(ref):
        return [comment]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions

    refreshed = await store.get_task(t.id)
    fb = refreshed.context["send_back_feedback"][-1]
    assert "[src/handler.py:55]" in fb["message"]
    assert "Null check missing" in fb["message"]
    assert "if value is not None" in fb["message"]


async def test_post_reply_comment_stamps_the_agent_marker(monkeypatch):
    """Every comment no_human posts must carry the invisible marker — it is
    the only thing distinguishing the product's comments from the operator's
    (same gh login; the 2026-07-10 self-resume incident)."""
    from no_human.vcs import pr_watcher

    captured = {}

    async def fake_run_cli(cmd):
        captured["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(pr_watcher, "_run_cli", fake_run_cli)
    ok = await pr_watcher.post_reply_comment("host/o/r#5", "hello reviewer")
    assert ok
    body = captured["cmd"][captured["cmd"].index("--body") + 1]
    assert body.startswith(pr_watcher.AGENT_COMMENT_MARKER)
    assert "hello reviewer" in body
    assert pr_watcher.is_agent_comment(body)
    assert not pr_watcher.is_agent_comment("hello reviewer")


def test_every_product_marker_is_recognized_as_self():
    """R18 (2026-08-10, PR #147): the verification-receipts comment carries
    `<!-- no_human:verification-receipts -->`, NOT AGENT_COMMENT_MARKER, so the
    wake watcher's `_is_self_or_bot` let the product's own receipt re-wake the
    finished task into a wasted attempt 22 seconds after the PR opened. Every
    product-authored comment marker shares the `<!-- no_human` prefix; the
    filter must key on the family, not one member."""
    from no_human.vcs import pr_watcher
    assert pr_watcher.is_agent_comment(
        "<!-- no_human:verification-receipts -->\n## How I verified this")
    assert pr_watcher.is_agent_comment(
        "<!-- no_human-agent-comment -->\naddressed the feedback")
    # A future marker in the same family is covered without a code change.
    assert pr_watcher.is_agent_comment("<!-- no_human:some-future-surface -->")
    # Human comments — including ones that merely mention the product.
    assert not pr_watcher.is_agent_comment("no_human should also fix X")
    assert not pr_watcher.is_agent_comment("")
    assert not pr_watcher.is_agent_comment(None)


def test_a_quote_reply_carrying_the_raw_marker_is_not_self():
    """GitHub's "Quote reply" copies the RAW markdown — HTML comments and all —
    and prefixes every quoted line with "> ". An unanchored substring test read
    that as the product's own comment; because the verdict gates WAKING, the
    task then never woke at all and escalated 48h later as a timeout with the
    human's review undelivered. The marker must OPEN a line to count as ours."""
    from no_human.vcs import pr_watcher
    quoted = ("> <!-- no_human-agent-comment -->\n"
              "> Abandoned by no_human.\n\n"
              "No — reopen it, the retry path is still wrong.")
    assert not pr_watcher.is_agent_comment(quoted)
    # GitLab quotes the same way; so does a human indenting the paste.
    assert not pr_watcher.is_agent_comment(">>> <!-- no_human:verification-receipts -->")
    assert not pr_watcher.is_agent_comment("    <!-- no_human-agent-comment -->")
    # ...and a human simply mentioning the marker mid-sentence.
    assert not pr_watcher.is_agent_comment(
        "your bot stamps <!-- no_human-agent-comment --> on everything")
    # Quote-reply spelt every way a forge and a human produce it.
    assert not pr_watcher.is_agent_comment(">> > <!-- no_human-agent-comment -->")
    assert not pr_watcher.is_agent_comment(">\t<!-- no_human-agent-comment -->")
    assert not pr_watcher.is_agent_comment("><!-- no_human-agent-comment -->")
    assert not pr_watcher.is_agent_comment("> <!-- no_human-agent-comment -->\r\n> x")
    # Under CRLF the marker still opens the body, so it is still ours.
    assert pr_watcher.is_agent_comment("<!-- no_human-agent-comment -->\r\nhi")


def test_a_marker_below_the_first_line_is_a_human_paste_not_self():
    """The anchor is the START OF THE BODY, not the start of any line.

    Anchoring per-line still read four HUMAN shapes as the product's own, and
    because the verdict gates WAKING the cost is the whole review: the task
    never wakes and escalates 48h later as a timeout with the comment
    undelivered. Every producer puts the marker at line 1, column 0 —
    `pr_watcher.post_reply_comment`, `pr_watcher.upsert_agent_comment`,
    `orchestrator`'s verification receipts, and `comment_poster._stamped` —
    so a marker anywhere below line 1 came from a human's keyboard."""
    from no_human.vcs import pr_watcher
    # A human pasting our comment inside a fenced code block.
    assert not pr_watcher.is_agent_comment(
        "```\n<!-- no_human-agent-comment -->\n```\nplease fix the retry")
    # A human's own text, then a raw paste at column 0 on line 2+.
    assert not pr_watcher.is_agent_comment(
        "I ran it and got:\n<!-- no_human-agent-comment -->\nAbandoned by no_human.")
    # `str.splitlines` also splits on separators no forge renders as newlines,
    # so a marker after one of those is not at the start of any *rendered* line.
    assert not pr_watcher.is_agent_comment(
        "look at this\x0c<!-- no_human-agent-comment -->")
    assert not pr_watcher.is_agent_comment(
        "look at this\u2028<!-- no_human-agent-comment -->")
    assert not pr_watcher.is_agent_comment(
        "look at this\x1c<!-- no_human-agent-comment -->")


async def test_every_producer_shape_is_still_self(monkeypatch):
    """Every producer is CALLED and the body it actually sends is captured, so a
    producer that stops putting the marker at position 0 fails here.

    Hand-written f-string mirrors of the producers did NOT do this: they restated
    the source instead of running it, so `body = f"### CI\\n{MARKER}..."` in
    `upsert_agent_comment` left the whole suite green while the CI-gate comment
    read as HUMAN to `_is_self_or_bot` and re-woke the task onto its own comment.
    Whether a FOURTH producer exists at all is a different guard and lives in
    `tests/test_comment_poster.py::test_no_module_outside_the_two_stampers_posts_a_pr_comment`.

    "Every producer" means every IN-REPO producer: the coder/reviewer agents have
    an unrestricted Bash tool and could `gh pr comment` an unmarked body. That
    reads as human under the old line-anchored predicate and the new body-anchored
    one alike, so it is a pre-existing residual, not a regression of this change.
    """
    from no_human.core.orchestrator import Orchestrator
    from no_human.vcs import comment_poster, pr_watcher

    sent: list[str] = []
    listing = "[]"

    async def record(cmd):
        for i, arg in enumerate(cmd):
            if arg in ("--body", "-f", "--field") and i + 1 < len(cmd):
                sent.append(cmd[i + 1].removeprefix("body="))
        return listing

    monkeypatch.setattr(pr_watcher, "_run_cli", record)
    # Both upsert send paths (create when nothing is there, update when one is)
    # on both forges — a divergent body on any one of them fails here.
    for listing in ("[]", '[{"id": 9, "body": "<!-- nh:ci_gate -->"}]'):
        for ref in ("host/o/r#1", "o/r!1"):  # GitHub/GHE, then GitLab
            assert await pr_watcher.post_reply_comment(ref, "x")
            assert await pr_watcher.upsert_agent_comment(ref, "x", key="ci_gate")

    def record_receipt(url, body, marker):
        sent.append(body)
        return {"ok": True, "mode": "posted"}

    monkeypatch.setattr(comment_poster, "post_to_pr_once", record_receipt)
    orch = Orchestrator.__new__(Orchestrator)  # the body f-string is what is under test
    orch._verification_section = lambda *a, **k: "## How I verified this"
    orch._backend_is_observable = lambda: True
    orch.emit = lambda *a, **k: None
    # `_post_verification_comment` swallows every exception; route the swallow
    # into `sent` so a silent failure fails the assertion instead of passing it.
    orch._advisory = lambda msg: sent.append(f"advisory: {msg}")
    assert await orch._post_verification_comment(None, "https://github.com/o/r/pull/1", [])

    assert len(sent) == 9, sent  # 4 producer calls × 2 listings + 1 receipt
    produced = sent + [
        comment_poster._stamped("a finding", "`src/a.py:12` — "),
        comment_poster._stamped(
            f"{Orchestrator.VERIFICATION_COMMENT_MARKER}\nevidence", "`src/a.py:12` — "),
    ]
    for body in produced:
        assert pr_watcher.is_agent_comment(body), body


# ── upsert_agent_comment: update one comment, never pile up (PR #7004 had 17) ──

async def test_upsert_updates_existing_github_comment_instead_of_posting_new(monkeypatch):
    import no_human.vcs.pr_watcher as pw

    calls = []

    async def fake_run(cmd):
        calls.append(cmd)
        joined = " ".join(cmd)
        if "/issues/" in joined and joined.endswith("--paginate"):
            # an existing agent comment for key "ci_gate"
            return '[{"id": 99, "body": "<!-- no_human-agent-comment --><!-- nh:ci_gate -->\\nold"}]'
        return "{}"  # PATCH/POST succeed

    monkeypatch.setattr(pw, "_run_cli", fake_run)
    ok = await pw.upsert_agent_comment("code.example.com/dev/query-service#7004", "new status", key="ci_gate")
    assert ok is True
    # It PATCHed comment 99, and did NOT POST a new one.
    assert any("PATCH" in " ".join(c) and "/issues/comments/99" in " ".join(c) for c in calls)
    assert not any("-X" in c and "POST" in c and "/issues/7004/comments" in " ".join(c) for c in calls)


async def test_upsert_creates_when_none_exists(monkeypatch):
    import no_human.vcs.pr_watcher as pw

    calls = []

    async def fake_run(cmd):
        calls.append(cmd)
        if " ".join(cmd).endswith("--paginate"):
            return "[]"  # no existing comment
        return "{}"

    monkeypatch.setattr(pw, "_run_cli", fake_run)
    ok = await pw.upsert_agent_comment("code.example.com/dev/r#5", "hi", key="ci_gate")
    assert ok is True
    assert any("POST" in c for c in calls if isinstance(c, list) for c in [" ".join(c)]) or \
        any("POST" in " ".join(c) for c in calls)


def test_upsert_body_never_says_no_human_visibly():
    # The visible text must not mention no_human; only the invisible HTML marker does.
    from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER
    assert AGENT_COMMENT_MARKER.startswith("<!--")  # invisible


# --------------------------------------------------------------------------- #
# GitLab merge requests must resolve, not park forever                         #
#                                                                              #
# `default_pr_merged` / `default_pr_state` early-returned for every non-GitHub #
# ref, with no error anywhere: a task parked on `pr_merged:<gitlab MR>` could  #
# never wake, and the awaiting-approval watcher (`blockers/wake.py`, rung 1)   #
# never saw the MR merge. Comment fetch/post were already GitLab-aware, so     #
# the silence was specific to the lifecycle calls.                             #
# --------------------------------------------------------------------------- #

@pytest.fixture
def cli_recorder(monkeypatch):
    """Record every CLI argv `pr_watcher` shells out to, and script replies."""
    from no_human.vcs import pr_watcher as pw

    calls: list[list[str]] = []
    replies: dict[str, str | None] = {}

    async def fake_run_cli(cmd):
        calls.append(cmd)
        return replies.get(cmd[0])

    monkeypatch.setattr(pw, "_run_cli", fake_run_cli)
    monkeypatch.setattr(pw.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls, replies


GITLAB_MR_URL = "https://gitlab.acme.net/grp/svc/-/merge_requests/7"
GITLAB_MR_REF = "grp%2Fsvc!7"
#: GitLab's OWN native short form, and the only one a human or an LLM writes by
#: hand: `group/project!7`, with a raw slash. The pre-encoded constant above
#: could not see the bug below — it arrives already correct, and the recorder
#: mock ignores the path it is handed.
GITLAB_MR_REF_RAW = "grp/svc!7"
GITLAB_MR_REF_SUBGROUP = "grp/sub/proj!7"
GITHUB_PR_URL = "https://github.com/acme/svc/pull/7"


def _api_path(calls) -> str:
    """The `projects/…` argument out of the recorded glab argv."""
    return next(a for c in calls for a in c if "merge_requests" in a)


@pytest.mark.parametrize("ref,expect_path", [
    (GITLAB_MR_REF_RAW, "projects/grp%2Fsvc/merge_requests/7"),
    (GITLAB_MR_REF_SUBGROUP, "projects/grp%2Fsub%2Fproj/merge_requests/7"),
    # Idempotency: an already-encoded ref must NOT become grp%252Fsvc.
    (GITLAB_MR_REF, "projects/grp%2Fsvc/merge_requests/7"),
    # A mixed ref is normalized rather than left half-encoded.
    ("grp%2Fsub/proj!7", "projects/grp%2Fsub%2Fproj/merge_requests/7"),
])
async def test_a_raw_slash_short_ref_is_url_encoded_for_the_gitlab_api(
    cli_recorder, ref, expect_path,
):
    """GitLab addresses a project as `group%2Frepo`; a raw slash 404s.

    Verified against the live API (2026-08-06):
    `projects/gitlab-org%2Fgitlab-foss` -> HTTP 200,
    `projects/gitlab-org/gitlab-foss` -> HTTP 404. Every project has a
    namespace, so this is the normal case.

    The 404 is SILENT — `_run_cli` returns None, the state reads "", and
    `default_pr_merged` is False forever with no error anywhere. That is
    verbatim the failure GitLab lifecycle support was added to remove, so the
    feature was undelivered for the one ref form a human actually types.
    """
    from no_human.vcs.pr_watcher import default_pr_state

    calls, replies = cli_recorder
    replies["glab"] = json.dumps({"state": "merged", "iid": 7})

    assert await default_pr_state(ref) == "MERGED"
    assert _api_path(calls) == expect_path
    assert "%252F" not in _api_path(calls), "double-encoded"


@pytest.mark.parametrize("fn,verb", [
    ("post_reply_comment", "POST"),
    ("upsert_agent_comment", "list/POST"),
])
async def test_the_comment_writers_encode_a_raw_slash_short_ref_too(
    cli_recorder, fn, verb,
):
    """Same defect, same ref form, three other call sites: comment fetch and
    both comment writers built the same `projects/{project}/…` path by hand.
    Fixing only the lifecycle query would have left a task able to SEE its MR
    merge while still unable to reply on it."""
    import no_human.vcs.pr_watcher as pw

    calls, replies = cli_recorder
    replies["glab"] = "[]"
    await getattr(pw, fn)(GITLAB_MR_REF_RAW, "hello")
    assert calls, f"{fn} ({verb}) asked glab nothing"
    assert all("projects/grp%2Fsvc/" in a
               for c in calls for a in c if a.startswith("projects/")), calls


@pytest.mark.parametrize("ref", [GITLAB_MR_REF_RAW, GITLAB_MR_URL])
@pytest.mark.parametrize("fn,empty", [
    ("default_pr_checks", []),
    ("default_pr_head", ""),
    ("default_pr_mergeable", {"mergeable": "", "mergeStateStatus": ""}),
    ("default_pr_files", []),
])
async def test_the_gitlab_bound_on_the_gh_only_helpers_is_what_docs_say(
    cli_recorder, ref, fn, empty,
):
    """These four are GitHub-only, and that bound lived ONLY in a Python
    docstring until `docs/adapters.md` gained the table this pins.

    It is silent in both directions: the empty value they return for a GitLab
    ref is identical to "no checks / no files", so a GitLab operator cannot
    tell "nothing is failing" from "this is never read here". Asserting the
    exact empties AND zero CLI calls is what makes the doc table checkable —
    if someone implements the GitLab path, this test fails and the doc gets
    updated with it.
    """
    import no_human.vcs.pr_watcher as pw

    calls, replies = cli_recorder
    replies["gh"] = replies["glab"] = json.dumps(
        {"statusCheckRollup": [{"name": "x", "conclusion": "FAILURE"}],
         "headRefOid": "deadbeef", "mergeable": "CONFLICTING",
         "files": [{"path": "a.py"}]})

    assert await getattr(pw, fn)(ref) == empty
    assert calls == [], f"{fn} is documented as not implemented for GitLab: {calls}"


async def test_the_same_helpers_do_resolve_for_github(cli_recorder):
    """Non-vacuity control for the bound above: a helper that had simply
    stopped working would pass every assertion in that test."""
    import no_human.vcs.pr_watcher as pw

    calls, replies = cli_recorder
    replies["gh"] = json.dumps(
        {"statusCheckRollup": [{"name": "x", "conclusion": "FAILURE"}],
         "headRefOid": "deadbeef", "mergeable": "CONFLICTING",
         "files": [{"path": "a.py"}]})

    assert await pw.default_pr_head("acme/svc#7") == "deadbeef"
    assert await pw.default_pr_files("acme/svc#7") == ["a.py"]
    assert (await pw.default_pr_mergeable("acme/svc#7"))["mergeable"] == "CONFLICTING"
    assert await pw.default_pr_checks("acme/svc#7") != []
    assert calls and all(c[0] == "gh" for c in calls)


async def test_comment_fetch_encodes_a_raw_slash_short_ref(cli_recorder):
    """The read side of the same defect (`check_pr_comments` -> notes)."""
    calls, replies = cli_recorder
    replies["glab"] = "[]"
    await check_pr_comments(GITLAB_MR_REF_RAW)
    assert _api_path(calls) == "projects/grp%2Fsvc/merge_requests/7/notes"


@pytest.mark.parametrize("ref", [GITLAB_MR_URL, GITLAB_MR_REF])
@pytest.mark.parametrize("gitlab_state,expect_state,expect_merged", [
    ("merged", "MERGED", True),
    ("opened", "OPEN", False),
    ("closed", "CLOSED", False),
])
async def test_gitlab_mr_lifecycle_resolves_via_glab(
    cli_recorder, ref, gitlab_state, expect_state, expect_merged,
):
    from no_human.vcs.pr_watcher import default_pr_merged, default_pr_state

    calls, replies = cli_recorder
    replies["glab"] = json.dumps({"state": gitlab_state, "iid": 7})

    assert await default_pr_state(ref) == expect_state
    assert await default_pr_merged(ref) is expect_merged
    assert calls, "a GitLab MR ref must actually ask glab — it asked nothing"
    assert all(c[0] == "glab" for c in calls), f"wrong CLI: {calls}"
    assert any("merge_requests/7" in a for c in calls for a in c), calls


async def test_gitlab_mr_url_targets_the_mr_host_not_gitlab_com(cli_recorder):
    """A self-hosted MR URL must carry --hostname: `glab` defaults to
    gitlab.com, which is the failure this repo already documents for
    `glab ci run`."""
    from no_human.vcs.pr_watcher import default_pr_state

    calls, replies = cli_recorder
    replies["glab"] = json.dumps({"state": "merged"})
    await default_pr_state(GITLAB_MR_URL)
    cmd = calls[0]
    assert "--hostname" in cmd and cmd[cmd.index("--hostname") + 1] == "gitlab.acme.net"


async def test_gitlab_unknown_state_is_unknown_never_merged(cli_recorder):
    """An unmapped/garbled state must read as "" (no action), never MERGED."""
    from no_human.vcs.pr_watcher import default_pr_merged, default_pr_state

    _calls, replies = cli_recorder
    replies["glab"] = json.dumps({"state": "locked"})
    assert await default_pr_state(GITLAB_MR_REF) == ""
    assert await default_pr_merged(GITLAB_MR_REF) is False


async def test_github_pr_lifecycle_still_resolves_via_gh(cli_recorder):
    """Known-positive control: the GitHub path is unchanged and still uses
    `gh`, so a green GitLab test above cannot be an artefact of a broken
    harness."""
    from no_human.vcs.pr_watcher import default_pr_merged, default_pr_state

    calls, replies = cli_recorder
    replies["gh"] = json.dumps({"state": "MERGED"})
    assert await default_pr_state(GITHUB_PR_URL) == "MERGED"
    assert await default_pr_merged("acme/svc#7") is True
    assert all(c[0] == "gh" for c in calls), f"wrong CLI: {calls}"


async def test_a_gitlab_mr_wakes_a_pr_merged_blocker(store, cli_recorder):
    """End to end through the WakeWatcher: the condition the product writes
    into a parked task must actually fire for a GitLab MR."""
    from no_human.vcs.pr_watcher import default_pr_merged

    _calls, replies = cli_recorder
    replies["glab"] = json.dumps({"state": "merged"})

    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    watcher = WakeWatcher(store, _cfg(), pr_merged=default_pr_merged)
    args = dict(raised_at=now - timedelta(hours=1), now=now, wake_check_at=None)

    assert await watcher.condition_satisfied(
        f"pr_merged:{GITLAB_MR_URL}", **args) is True

    replies["glab"] = json.dumps({"state": "opened"})
    assert await watcher.condition_satisfied(
        f"pr_merged:{GITLAB_MR_URL}", **args) is False


@pytest.mark.asyncio
async def test_the_tasks_own_marked_comment_does_not_wake_it(store):
    """R18: the marker must close the loop, not just the injection half.

    A task parked on ``pr_comment_on:<pr>`` posts its own (correctly marked)
    comment — an abandoned-draft note, a verification receipt, a CI_GATE table.
    The injection path filters it as self, so the task woke with ZERO feedback
    and burned an attempt: the wake rung satisfied itself on ``len(comments) >
    0`` with no self/bot filter at all. The marker only pays for itself if the
    rung honours it too.
    """
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )
    own = PrComment(author="operator",
                    body="<!-- no_human-agent-comment -->\nAbandoned by no_human.",
                    created_at=now.isoformat())
    bot = PrComment(author="ci-bot[bot]", body="pipeline green", created_at=now.isoformat())

    async def pr_comment_checker(ref):
        return [own, bot]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.BLOCKED
    assert not refreshed.context.get("send_back_feedback")


@pytest.mark.asyncio
async def test_one_human_comment_among_the_agents_own_still_wakes(store):
    """The filter must not swallow real feedback that arrives alongside it."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )
    own = PrComment(author="operator",
                    body="<!-- no_human:verification-receipts -->\nevidence",
                    created_at=now.isoformat())
    human = PrComment(author="alice", body="this breaks the retry", created_at=now.isoformat())

    async def pr_comment_checker(ref):
        return [own, human]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions
    feedback = (await store.get_task(t.id)).context.get("send_back_feedback", [])
    assert len(feedback) == 1, feedback
    assert "this breaks the retry" in feedback[-1]["message"]


@pytest.mark.asyncio
async def test_a_blip_between_the_two_fetches_does_not_burn_an_attempt(store):
    """The rung and the injection each call the checker. Routing both through
    one predicate made them agree on WHAT COUNTS as feedback, not on the DATA:
    a 502 (or a deleted comment) between the two calls left `_human_pr_comments`
    returning [] on the second read, `_inject_pr_feedback` returning None, and
    the caller resuming anyway with an empty `send_back_feedback` — an attempt
    spent on nothing, which is precisely the failure the marker work set out to
    remove. Nothing injected now means nothing resumed."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )
    state = {"n": 0, "flaky": True}

    async def pr_comment_checker(ref):
        state["n"] += 1
        if state["flaky"] and state["n"] > 1:
            raise RuntimeError("forge 502")
        return [PrComment(author="alice", body="this breaks the retry",
                          created_at=now.isoformat())]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions, actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.BLOCKED
    assert not (refreshed.context or {}).get("send_back_feedback")
    # ...and the next tick, on a forge that answers, delivers the comment.
    state["flaky"] = False
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions, actions
    feedback = (await store.get_task(t.id)).context.get("send_back_feedback", [])
    assert len(feedback) == 1 and "this breaks the retry" in feedback[0]["message"]


@pytest.mark.asyncio
async def test_a_task_that_goes_terminal_between_the_two_fetches_is_not_resumed(store):
    """Same fall-through, different cause: `_inject_pr_feedback` also returns
    None when its own terminal re-check fires (a POST /shipped landing during
    the fetch). The caller used to resume the finished task regardless."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    async def pr_comment_checker(ref):
        return [PrComment(author="alice", body="fix this", created_at=now.isoformat())]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    calls = {"n": 0}
    real_terminal = watcher._is_terminal

    async def flips_terminal(task):
        calls["n"] += 1
        return calls["n"] > 1 or await real_terminal(task)

    watcher._is_terminal = flips_terminal
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions, actions
    assert (await store.get_task(t.id)).status == TaskStatus.BLOCKED


# ── the over-filtering direction: every shape of real feedback still wakes ──
# A filter that wakes a task spuriously costs one attempt. A filter that never
# wakes it costs the whole review: the task sits BLOCKED until max_park and
# escalates as a timeout, with the human's comment never delivered. So each
# shape a genuine human comment arrives in gets a row here.

_HUMAN = "this breaks the retry"
_MARKED_SELF = "<!-- no_human:verification-receipts -->\nevidence"
_QUOTE_REPLY = ("> <!-- no_human-agent-comment -->\n"
                "> Abandoned by no_human.\n\n" + _HUMAN)
_FENCED_MARKER = "```\n<!-- no_human-agent-comment -->\n```\n" + _HUMAN
_PASTE_AFTER_TEXT = "I ran it and got:\n<!-- no_human-agent-comment -->\nAbandoned."


@pytest.mark.parametrize("shape,ref,comments,expect_in_message", [
    ("human only", "org/repo#42",
     [PrComment(author="alice", body=_HUMAN)], _HUMAN),
    ("human beside the agent's own marked comment", "org/repo#42",
     [PrComment(author="operator", body=_MARKED_SELF),
      PrComment(author="alice", body=_HUMAN)], _HUMAN),
    ("human beside a foreign bot", "org/repo#42",
     [PrComment(author="dependabot[bot]", body="bump lodash"),
      PrComment(author="alice", body=_HUMAN)], _HUMAN),
    ("human quote-replying to the agent", "org/repo#42",
     [PrComment(author="alice", body=_QUOTE_REPLY)], _HUMAN),
    ("inline review comment", "org/repo#42",
     [PrComment(author="alice", body="null check missing",
                path="src/a.py", line=10, diff_hunk="- old\n+ new")],
     "[src/a.py:10] null check missing"),
    ("GitLab merge-request ref", "grp/svc!7",
     [PrComment(author="alice", body=_HUMAN)], _HUMAN),
    ("plain-string comment", "org/repo#42", ["please fix the retry"],
     "please fix the retry"),
    ("human pasting the marker inside a fence", "org/repo#42",
     [PrComment(author="alice", body=_FENCED_MARKER)], _HUMAN),
    ("human's own text, then a raw paste at column 0", "org/repo#42",
     [PrComment(author="alice", body=_PASTE_AFTER_TEXT)], "I ran it and got"),
    ("form-feed before the marker", "org/repo#42",
     [PrComment(author="alice", body=f"{_HUMAN}\x0c<!-- no_human-agent-comment -->")],
     _HUMAN),
    ("U+2028 line separator before the marker", "org/repo#42",
     [PrComment(author="alice", body=f"{_HUMAN}\u2028<!-- no_human-agent-comment -->")],
     _HUMAN),
])
@pytest.mark.asyncio
async def test_every_shape_of_human_feedback_still_wakes_the_task(
        store, shape, ref, comments, expect_in_message):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": f"pr_comment_on:{ref}",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    async def pr_comment_checker(_ref):
        return list(comments)

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions, f"{shape}: never woke — {actions}"
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING, shape
    feedback = (refreshed.context or {}).get("send_back_feedback", [])
    assert len(feedback) == 1, f"{shape}: {feedback}"
    assert expect_in_message in feedback[0]["message"], f"{shape}: {feedback}"


# ── the guard must not strand the task: a divergence that never clears ──


@pytest.mark.asyncio
async def test_an_alternating_forge_still_escalates_at_max_park(store):
    """`rounds is None` returned from `_evaluate` BEFORE the max_park check, so
    the comment at that guard ("escalated by the max_park timeout below") was
    false. An ALTERNATING forge — answers the rung, fails the injection, which
    this design hits twice as often because it fetches twice per tick (gh
    secondary rate limits do exactly this) — parked the task forever: no
    resume, no escalation, the human's review never delivered. Strictly worse
    than the empty attempt the guard was added to prevent."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    raised = now - timedelta(days=30)  # long past max_park (48h)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": raised.isoformat(), "confidence": 0.9,
        },
    )
    calls = {"n": 0}

    async def alternating(_ref):
        calls["n"] += 1
        if calls["n"] % 2 == 0:      # the injection's fetch, every tick
            raise RuntimeError("gh: secondary rate limit")
        return [PrComment(author="alice", body="this breaks the retry",
                          created_at=now.isoformat())]

    watcher = WakeWatcher(store, _cfg(), pr_comment=alternating)
    for _ in range(5):
        actions = await watcher.tick(now=now)
        if (t.id, "escalated_timeout") in actions:
            break
    else:
        raise AssertionError(f"5 ticks, 30 days parked, never escalated: {actions}")
    assert (t.id, "resumed") not in actions, actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.ESCALATED, refreshed.status


@pytest.mark.asyncio
async def test_a_flat_forge_outage_still_escalates_at_max_park(store):
    """Control for the test above: when the rung itself never satisfies, the
    timeout path was already reached. It must stay reached."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": (now - timedelta(days=30)).isoformat(), "confidence": 0.9,
        },
    )

    async def always_down(_ref):
        raise RuntimeError("gh: 503")

    watcher = WakeWatcher(store, _cfg(), pr_comment=always_down)
    actions = await watcher.tick(now=now)
    assert (t.id, "escalated_timeout") in actions, actions


@pytest.mark.asyncio
async def test_a_divergence_inside_max_park_still_stays_parked(store):
    """The fall-through must not become an escalation: before max_park, a
    divergent tick still parks quietly and re-decides next tick."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )
    calls = {"n": 0}

    async def alternating(_ref):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise RuntimeError("gh: secondary rate limit")
        return [PrComment(author="alice", body="this breaks the retry",
                          created_at=now.isoformat())]

    watcher = WakeWatcher(store, _cfg(), pr_comment=alternating)
    actions = await watcher.tick(now=now)
    assert actions == [] or all(a[1] == "wake_tick" for a in actions), actions
    assert (await store.get_task(t.id)).status == TaskStatus.BLOCKED


async def test_default_ci_annotations_hits_the_check_runs_rest_path_with_hostname(monkeypatch):
    """BLOCKING review finding (2026-08-12): `_gh_repo_and_number` returns a
    HOST-PREFIXED repo arg meant for `gh`'s `--repo` flag; `gh api`, unlike
    `gh pr view --repo`, does NOT accept that prefix in the REST path —
    interpolating it straight in 404s on every real PR. The host must be
    split off and passed via `--hostname` instead (the same split
    `upsert_agent_comment`'s GitHub branch already uses)."""
    from no_human.vcs import pr_watcher as pw

    calls: list[list[str]] = []

    async def fake_run_cli(cmd):
        calls.append(cmd)
        if cmd[1:3] == ["pr", "view"]:
            return json.dumps({"headRefOid": "deadbeef"})
        if cmd[1] == "api":
            path = cmd[-1]
            if path.endswith("/annotations"):
                return json.dumps([{
                    "title": "build",
                    "message": (
                        "The job was not started because recent account "
                        "payments have failed or your spending limit needs "
                        "to be increased."
                    ),
                }])
            if path.endswith("/check-runs"):
                return json.dumps({"check_runs": [
                    {"id": 999, "conclusion": "action_required", "name": "build"},
                ]})
        return None

    monkeypatch.setattr(pw, "_run_cli", fake_run_cli)
    monkeypatch.setattr(pw.shutil, "which", lambda name: f"/usr/bin/{name}")

    result = await pw.default_ci_annotations(
        "https://code.example.com/dev/x/pull/7004", "build")
    assert "spending limit needs to be increased" in result

    api_calls = [c for c in calls if c[1] == "api"]
    assert len(api_calls) == 2, calls
    for c in api_calls:
        assert "--hostname" in c, c
        assert c[c.index("--hostname") + 1] == "code.example.com"
        # No host segment leaked into the REST path itself (the exact 404).
        assert not c[-1].startswith("code.example.com/")
    check_runs_call = next(c for c in api_calls if c[-1].endswith("/check-runs"))
    assert check_runs_call[-1] == "repos/dev/x/commits/deadbeef/check-runs"
    annotations_call = next(c for c in api_calls if c[-1].endswith("/annotations"))
    assert annotations_call[-1] == "repos/dev/x/check-runs/999/annotations"


# --------------------------------------------------------------------------- #
# CI log excerpt: SSO credentials and the fetch are scoped to the configured   #
# Jenkins controller over verified TLS                                         #
#                                                                              #
# `link` is forge-supplied PR check data (a check's targetUrl points wherever  #
# the forge says), so `default_ci_log_excerpt` sends the SSO Basic-auth        #
# credentials — and makes any request at all — ONLY to the configured Jenkins  #
# controller, over TLS it always verifies (a CA bundle when set, else the      #
# system store), and only over https.                                          #
# --------------------------------------------------------------------------- #

class _FakeCfg:
    def __init__(self, controller="", ca_bundle=""):
        self.data = {"ci_gate": {
            "jenkins_controller": controller, "jenkins_ca_bundle": ca_bundle}}


def _patch_ci_log(monkeypatch, *, controller="", ca_bundle="",
                  sso=("u", "p"), body="ERROR: boom\nmore\n"):
    """Wire `default_ci_log_excerpt`'s config + env + httpx seams and record
    whether an AsyncClient was constructed and with what verify/auth."""
    import httpx

    from no_human import config

    rec: dict = {"constructed": False}
    monkeypatch.setattr(config, "load_config", lambda *a, **k: _FakeCfg(controller, ca_bundle))
    monkeypatch.setattr(
        config, "load_env_var",
        lambda name, *a, **k: {"SSO_USERNAME": sso[0], "SSO_PASSWORD": sso[1]}.get(name)
        if sso else None,
    )

    class _FakeClient:
        def __init__(self, **kwargs):
            rec["constructed"] = True
            rec["verify"] = kwargs.get("verify")
            rec["auth"] = kwargs.get("auth")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            rec["url"] = url
            return httpx.Response(200, text=body)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return rec


async def test_ci_log_excerpt_sends_nothing_to_a_foreign_host(monkeypatch):
    from no_human.vcs.pr_watcher import default_ci_log_excerpt

    rec = _patch_ci_log(monkeypatch, controller="https://jenkins.internal.example")
    out = await default_ci_log_excerpt("https://evil.example/job/x/42")
    assert out == ""
    assert rec["constructed"] is False  # no request, so no Authorization header


async def test_ci_log_excerpt_refuses_cleartext_http(monkeypatch):
    from no_human.vcs.pr_watcher import default_ci_log_excerpt

    rec = _patch_ci_log(monkeypatch, controller="https://jenkins.internal.example")
    out = await default_ci_log_excerpt("http://jenkins.internal.example/job/x/42")
    assert out == ""
    assert rec["constructed"] is False


async def test_ci_log_excerpt_returns_empty_when_no_controller_configured(monkeypatch, caplog):
    """SSO credentials set but no `ci_gate.jenkins_controller`: no request, and
    the operator is told why the excerpt is missing (the silent "" was a
    support round-trip); without SSO credentials there is nothing to warn about."""
    from no_human.vcs.pr_watcher import default_ci_log_excerpt

    rec = _patch_ci_log(monkeypatch, controller="")
    with caplog.at_level("WARNING", logger="no_human.pr_watcher"):
        out = await default_ci_log_excerpt("https://jenkins.internal.example/job/x/42")
    assert out == ""
    assert rec["constructed"] is False
    assert "ci_gate.jenkins_controller is empty" in caplog.text

    caplog.clear()
    _patch_ci_log(monkeypatch, controller="", sso=None)
    with caplog.at_level("WARNING", logger="no_human.pr_watcher"):
        assert await default_ci_log_excerpt("https://jenkins.internal.example/job/x/42") == ""
    assert "jenkins_controller" not in caplog.text


async def test_ci_log_excerpt_fetches_matching_host_with_auth_and_ca_bundle(monkeypatch):
    from no_human.vcs.pr_watcher import default_ci_log_excerpt

    rec = _patch_ci_log(
        monkeypatch, controller="https://jenkins.internal.example",
        ca_bundle="/etc/ssl/internal-ca.pem")
    out = await default_ci_log_excerpt("https://jenkins.internal.example/job/x/42/")
    assert "ERROR: boom" in out
    assert rec["auth"] == ("u", "p")
    assert rec["verify"] == "/etc/ssl/internal-ca.pem"
    assert rec["url"] == "https://jenkins.internal.example/job/x/42/consoleText"


async def test_ci_log_excerpt_verifies_against_system_store_without_a_bundle(monkeypatch):
    from no_human.vcs.pr_watcher import default_ci_log_excerpt

    rec = _patch_ci_log(monkeypatch, controller="https://jenkins.internal.example", ca_bundle="")
    await default_ci_log_excerpt("https://jenkins.internal.example/job/x/42")
    assert rec["verify"] is True  # never verify=False


async def test_ci_log_excerpt_scopes_to_the_controller_path_and_port(monkeypatch):
    """`jenkins_controller` is a URL with a controller path; the same host on
    another path or port is NOT the controller and gets no request."""
    from no_human.vcs.pr_watcher import default_ci_log_excerpt

    ctrl = "https://build.example.com/ctrl"
    for link in ("https://build.example.com/other/job/x/42",
                 "https://build.example.com/ctrl2/job/x/42",
                 "https://build.example.com:8443/ctrl/job/x/42",
                 "https://build.example.com/job/x/42"):
        rec = _patch_ci_log(monkeypatch, controller=ctrl)
        assert await default_ci_log_excerpt(link) == "", link
        assert rec["constructed"] is False, link
    rec = _patch_ci_log(monkeypatch, controller=ctrl)
    assert "ERROR: boom" in await default_ci_log_excerpt("https://build.example.com:443/ctrl/job/x/42/")
    assert rec["url"] == "https://build.example.com:443/ctrl/job/x/42/consoleText"


async def test_ci_log_excerpt_warns_when_the_controller_is_not_an_https_url(monkeypatch, caplog):
    from no_human.vcs.pr_watcher import default_ci_log_excerpt

    for ctrl in ("http://build.example.com/ctrl", "build.example.com"):
        caplog.clear()
        rec = _patch_ci_log(monkeypatch, controller=ctrl)
        with caplog.at_level("WARNING", logger="no_human.pr_watcher"):
            assert await default_ci_log_excerpt("https://build.example.com/ctrl/job/x/42") == ""
        assert rec["constructed"] is False
        assert "not an https URL" in caplog.text and ctrl in caplog.text, ctrl


async def test_ci_log_excerpt_refuses_dot_segments_in_the_link(monkeypatch):
    """httpx applies RFC 3986 dot-segment removal AFTER our prefix compare, so
    `/ctrl/../other` would pass a raw startswith and be sent to `/other`: a
    link containing any `.`/`..` segment, encoded or not, gets no request."""
    from no_human.vcs.pr_watcher import default_ci_log_excerpt

    ctrl = "https://build.example.com/ctrl"
    for link in ("https://build.example.com/ctrl/../secrets/job/1",
                 "https://build.example.com/ctrl/a/../../other/job/1",
                 "https://build.example.com/ctrl/%2e%2e/evil/job/1",
                 "https://build.example.com/ctrl/%2E%2E/evil/job/1",
                 "https://build.example.com/ctrl/./job/1",
                 "https://build.example.com/ctrl/..%2fevil/job/1"):
        rec = _patch_ci_log(monkeypatch, controller=ctrl)
        assert await default_ci_log_excerpt(link) == "", link
        assert rec["constructed"] is False, link
    # positive control: a dot INSIDE a segment is an ordinary name
    rec = _patch_ci_log(monkeypatch, controller=ctrl)
    assert "ERROR: boom" in await default_ci_log_excerpt("https://build.example.com/ctrl/job/x.y/42")


async def test_ci_log_excerpt_refuses_path_parameter_dot_segments(monkeypatch):
    """`..;x` is a dot segment once a servlet container strips the path
    parameter; the guard refuses it without relying on server ordering."""
    from no_human.vcs.pr_watcher import default_ci_log_excerpt

    ctrl = "https://build.example.com/ctrl"
    for link in ("https://build.example.com/ctrl/..;/evil/job/1",
                 "https://build.example.com/ctrl/%2e%2e;x/evil/job/1",
                 "https://build.example.com/ctrl/.;/job/1"):
        rec = _patch_ci_log(monkeypatch, controller=ctrl)
        assert await default_ci_log_excerpt(link) == "", link
        assert rec["constructed"] is False, link
    rec = _patch_ci_log(monkeypatch, controller=ctrl)  # a `;` inside an ordinary name is fine
    assert "ERROR: boom" in await default_ci_log_excerpt("https://build.example.com/ctrl/job/a;b/42")
