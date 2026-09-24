from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.review.diff_coverage import (
    DiffCoverageError,
    InspectionTracker,
    _patch_path,
    _unquote_path,
    budget_diff,
)
from no_human.review.reviewer import (
    AdversarialReviewer,
    ReviewerUnavailable,
    _DIFF_CAP,
    _git_diff,
)


def _chunk(path: str, body: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n-old\n+" + body + "\n"
    )


def _passing_block() -> str:
    return (
        "REVIEW_JSON_START\n"
        '{"passed": true, "items": [{"label": "ok", "passed": true, '
        '"severity": "low", "evidence": "covered"}]}\n'
        "REVIEW_JSON_END\n"
    )


def test_small_diff_is_byte_identical():
    raw = _chunk("a.py", "new")
    rendered, cut = budget_diff(raw, len(raw) + 10)
    assert rendered == raw
    assert cut == []


def test_large_diff_gives_every_file_a_patch_share_and_names_cut_paths():
    raw = "stat header\n" + _chunk("a.py", "A" * 4000) + _chunk(
        "tests/test_a.py", "B" * 4000
    ) + _chunk("z.py", "C" * 4000)
    rendered, cut = budget_diff(raw, 2500)

    assert len(rendered) <= 2500
    assert "diff --git a/a.py b/a.py" in rendered
    assert "diff --git a/tests/test_a.py b/tests/test_a.py" in rendered
    assert "diff --git a/z.py b/z.py" in rendered
    assert cut
    for path in cut:
        assert f"- {path}\n" in rendered


def test_impossible_file_count_fails_instead_of_second_level_truncation():
    raw = "".join(_chunk(f"very-long-file-name-{i:03d}.py", "x") for i in range(40))
    with pytest.raises(DiffCoverageError):
        budget_diff(raw, 700)


def test_octal_escaped_non_ascii_header_path_decodes_to_the_real_path():
    """git C-escapes non-ASCII bytes octal-per-byte, one octal triplet per raw
    BYTE (e.g. \\303\\251 for the two UTF-8 bytes of `é`). `ast.literal_eval`
    reads those as Python string-literal escapes and produces one code point
    per octal group instead of decoding the byte sequence as UTF-8 — this
    fails on main, which yields "docs/Ã©val.md" (mojibake) instead."""
    escaped = r"docs/\303\251val.md"
    assert _unquote_path(f'"a/{escaped}"') == "docs/éval.md"

    chunk = (
        f'diff --git "a/{escaped}" "b/{escaped}"\n'
        f'--- "a/{escaped}"\n'
        f'+++ "b/{escaped}"\n'
        "@@ -1 +1 @@\n-old\n+" + "E" * 4000 + "\n"
    )
    assert _patch_path(chunk) == "docs/éval.md"

    raw = "stat header\n" + chunk + _chunk("a.py", "A" * 4000) + _chunk("z.py", "B" * 4000)
    rendered, cut = budget_diff(raw, 2500)
    assert "docs/éval.md" in cut
    assert "- docs/éval.md\n" in rendered


@pytest.mark.parametrize(("token", "expected"), [
    ("a/src/a.py", "src/a.py"),
    ('"a/src/a.py"', "src/a.py"),
    ('"b/dir/with space.py"', "dir/with space.py"),
    ("b/plain/path.py", "plain/path.py"),
])
def test_ascii_paths_quoted_and_unquoted_decode_unchanged(token, expected):
    """Pin the ordinary cases so the shared decoder cannot alter them: a
    plain unquoted token, a quoted plain path, and a quoted path with a
    space (git quotes for the space, not for encoding reasons)."""
    assert _unquote_path(token) == expected


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", message], check=True)


def test_git_diff_integration_keeps_late_paths_visible(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    for path in ("a.py", "tests/test_a.py", "z.py"):
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("old\n")
    _commit(repo, "base")
    for path, char in (("a.py", "A"), ("tests/test_a.py", "B"), ("z.py", "C")):
        (repo / path).write_text(char * (_DIFF_CAP // 2) + "\n")
    _commit(repo, "large change")

    rendered, total, cut = _git_diff(repo)
    assert total > _DIFF_CAP
    assert len(rendered) <= _DIFF_CAP
    assert "diff --git a/a.py b/a.py" in rendered
    assert "diff --git a/tests/test_a.py b/tests/test_a.py" in rendered
    assert "diff --git a/z.py b/z.py" in rendered
    assert cut


class _CoverageBackend:
    model = "test"

    def __init__(self, inspect: bool):
        self.inspect = inspect
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kwargs):
        self.calls += 1
        if self.inspect and on_event is not None:
            on_event(AgentEvent(
                "tool_use",
                tool_name="Read",
                tool_input={"file_path": "tests/hidden.py"},
            ))
        return AgentResult(
            final_text=_passing_block(),
            num_turns=1,
            is_error=False,
            tokens_used=10,
            session_id="coverage",
            stop_reason="end_turn",
        )


@pytest.mark.asyncio
async def test_uninspected_cut_file_routes_through_reviewer_unavailable(tmp_path):
    backend = _CoverageBackend(inspect=False)
    reviewer = AdversarialReviewer(backend=backend, timeout=1)
    with pytest.raises(ReviewerUnavailable):
        await reviewer._agent_review(
            "prompt", tmp_path, max_turns=1,
            required_inspections=["tests/hidden.py"],
        )
    assert backend.calls == 2


@pytest.mark.asyncio
async def test_inspected_cut_file_allows_the_real_verdict(tmp_path):
    backend = _CoverageBackend(inspect=True)
    reviewer = AdversarialReviewer(backend=backend, timeout=1)
    decision = await reviewer._agent_review(
        "prompt", tmp_path, max_turns=1,
        required_inspections=["tests/hidden.py"],
    )
    assert decision.passed is True
    assert backend.calls == 1


class _NonAsciiCoverageBackend:
    model = "test"

    def __init__(self, inspect: bool):
        self.inspect = inspect
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kwargs):
        self.calls += 1
        if self.inspect and on_event is not None:
            on_event(AgentEvent(
                "tool_use",
                tool_name="Read",
                tool_input={"file_path": "docs/éval.md"},
            ))
        return AgentResult(
            final_text=_passing_block(),
            num_turns=1,
            is_error=False,
            tokens_used=10,
            session_id="coverage",
            stop_reason="end_turn",
        )


@pytest.mark.asyncio
async def test_reviewer_reaches_a_verdict_over_a_non_ascii_cut_path(tmp_path):
    """The end-to-end path: a required inspection spelled with a non-ASCII
    character must be creditable, not just decodable — this exercises the
    fix through InspectionTracker and the reviewer, not only `_unquote_path`."""
    backend = _NonAsciiCoverageBackend(inspect=True)
    reviewer = AdversarialReviewer(backend=backend, timeout=1)
    decision = await reviewer._agent_review(
        "prompt", tmp_path, max_turns=1,
        required_inspections=["docs/éval.md"],
    )
    assert decision.passed is True
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_reviewer_reports_unavailable_when_non_ascii_path_never_named(tmp_path):
    """The negative twin: the check must stay fail-closed for a non-ASCII
    required path exactly as it does for an ASCII one."""
    backend = _NonAsciiCoverageBackend(inspect=False)
    reviewer = AdversarialReviewer(backend=backend, timeout=1)
    with pytest.raises(ReviewerUnavailable):
        await reviewer._agent_review(
            "prompt", tmp_path, max_turns=1,
            required_inspections=["docs/éval.md"],
        )
    assert backend.calls == 2


# ── InspectionTracker ─────────────────────────────────────────────────── #
# The reviewer-level tests above prove the wiring. These pin the traversal
# itself, which is the part that decides whether a real tool call counts.


def _tool_use(payload):
    return AgentEvent("tool_use", tool_name="Read", tool_input=payload)


def test_tracker_credits_a_non_ascii_path_named_in_a_tool_input():
    """Even a correctly decoded path is uncreditable if `_PATH_TOKEN` splits
    it on the non-ASCII byte — this fails on main against the ASCII-only
    class regardless of the decoder fix."""
    tracker = InspectionTracker(["docs/éval.md"])
    tracker.note_event(_tool_use({"file_path": "docs/éval.md"}))
    assert tracker.unreferenced() == []
    assert tracker.rejection() == ""

    free_form = InspectionTracker(["docs/éval.md"])
    free_form.note_event(_tool_use({"pattern": "read docs/éval.md now"}))
    assert free_form.unreferenced() == []
    assert free_form.rejection() == ""


def test_tracker_still_rejects_ascii_near_miss():
    """`_PATH_TOKEN` widened to admit non-ASCII word characters, but must not
    admit anything the ASCII class did not — an ASCII near-miss must still
    be rejected exactly as before."""
    tracker = InspectionTracker(["Dockerfile"])
    tracker.note_event(_tool_use({"file_path": "Dockerfile.mcp"}))
    assert tracker.unreferenced() == ["Dockerfile"]


def test_tracker_accepts_a_path_named_anywhere_in_a_nested_tool_input():
    """A tool input is arbitrary nested JSON — a path can arrive under a key,
    inside a list of edits, or embedded in a search pattern. The walker must
    find it in all three, or the check rejects verdicts that did the work."""
    tracker = InspectionTracker(["tests/hidden.py", "src/deep.py", "src/pat.py"])
    tracker.note_event(_tool_use({"file_path": "tests/hidden.py"}))
    tracker.note_event(_tool_use({"edits": [{"path": "src/deep.py", "old": "x"}]}))
    tracker.note_event(_tool_use({"pattern": "def f", "glob": "src/pat.py"}))
    assert tracker.unreferenced() == []
    assert tracker.rejection() == ""


def test_tracker_counts_only_tool_calls_that_actually_ran():
    """Prose is not evidence, and neither is a BLOCKED call. `denied` events
    carry a `tool_input` of their own — `agent/codex_backend.py` emits one for
    every guard-blocked call — so without the `tool_use` guard a reviewer whose
    read was refused would satisfy the coverage check having seen nothing."""
    tracker = InspectionTracker(["tests/hidden.py"])
    tracker.note_event(AgentEvent("text", text="I reviewed tests/hidden.py closely"))
    tracker.note_event(AgentEvent("thinking", text="tests/hidden.py looks fine"))
    tracker.note_event(AgentEvent("denied", tool_name="Read",
                                  tool_input={"file_path": "tests/hidden.py"}))
    assert tracker.unreferenced() == ["tests/hidden.py"]


def test_tracker_rejection_names_every_missing_path_and_claims_only_reference():
    tracker = InspectionTracker(["b.py", "a.py"])
    tracker.note_event(_tool_use({"file_path": "a.py"}))
    rejection = tracker.rejection()
    assert "b.py" in rejection and "a.py" not in rejection.split("file(s): ")[1]
    # The evidence is a tool INPUT, so "referencing" is all it establishes.
    assert "without referencing" in rejection
    assert "inspecting" not in rejection


def test_tracker_with_nothing_required_never_rejects():
    """The common path: the diff fit under the cap, so nothing was cut."""
    tracker = InspectionTracker(None)
    tracker.note_event(_tool_use({"file_path": "whatever.py"}))
    assert tracker.unreferenced() == []
    assert tracker.rejection() == ""


def test_tracker_survives_a_tool_input_that_is_missing_or_not_a_mapping():
    """A backend may emit `tool_use` with no input at all. Raising here would
    kill a review session over a malformed event."""
    tracker = InspectionTracker(["a.py"])
    tracker.note_event(AgentEvent("tool_use", tool_name="Read", tool_input=None))
    tracker.note_event(_tool_use({"n": 3, "ok": True, "none": None}))
    assert tracker.unreferenced() == ["a.py"]


@pytest.mark.parametrize(("token", "required"), [
    ("tests/hidden.py", "tests/hidden.py"),                 # the ordinary case
    ("./src/a.py", "src/a.py"),                             # cwd-relative spelling
    ("/private/tmp/nh-x/src/a.py", "src/a.py"),             # absolute, under a clone root
    ("/private/tmp/nh-x/Dockerfile", "Dockerfile"),         # ... including a root-level file
])
def test_tracker_counts_every_spelling_of_a_genuine_read(token, required):
    tracker = InspectionTracker([required])
    tracker.note_event(_tool_use({"file_path": token}))
    assert tracker.unreferenced() == []


@pytest.mark.parametrize(("token", "required"), [
    ("Dockerfile.mcp", "Dockerfile"),                       # name-prefix collision
    ("data.py", "a.py"),                                    # bare substring
    ("web/.gitignore", ".gitignore"),                       # same basename, other directory
    ("testdata/corpus/case-x/base/src/no_human/api/app.py",
     "src/no_human/api/app.py"),                            # a fixture COPY of the real file
])
def test_tracker_does_not_count_a_different_file(token, required):
    """Containment let the WRONG file satisfy a required path. Over this
    repository's own tracked files 92 pairs are substrings of each other, so
    this is not hypothetical: a reviewer that read `Dockerfile.mcp` was
    recorded as having covered `Dockerfile`, and the verdict stood."""
    tracker = InspectionTracker([required])
    tracker.note_event(_tool_use({"file_path": token}))
    assert tracker.unreferenced() == [required]


def test_tracker_finds_a_path_named_inside_free_form_text():
    """A tool input is not one path — a prompt or a `file.py:14` citation has
    to be split before the comparison can be exact."""
    tracker = InspectionTracker(["tests/a.py", "src/b.py"])
    tracker.note_event(_tool_use({"prompt": "Read tests/a.py, then src/b.py:14"}))
    assert tracker.unreferenced() == []


def test_no_coverage_note_when_nothing_was_actually_cut():
    """A large prefix can push the raw diff over the cap while every patch
    still fits. The header alone then told the reviewer that patches had been
    cut and listed none — an instruction it could not follow, about something
    that did not happen."""
    prefix = "commit log line\n" * 900
    raw = prefix + _chunk("a.py", "new") + _chunk("b.py", "new")
    rendered, cut = budget_diff(raw, 4000)
    assert len(raw) > 4000 and cut == []
    assert "DIFF COVERAGE" not in rendered


def test_an_absolute_fixture_copy_still_counts_and_the_docstring_says_so():
    """The residual the module docstring names, pinned so it cannot quietly
    change meaning: relative fixture copies are rejected, absolute ones are
    not, because anchoring the suffix needs a repo root this layer lacks."""
    from no_human.review.diff_coverage import _names_path
    required = "src/no_human/api/app.py"
    assert not _names_path("testdata/corpus/x/base/" + required, required)
    assert _names_path("/root/testdata/corpus/x/base/" + required, required)
    assert _names_path("/root/" + required, required)
