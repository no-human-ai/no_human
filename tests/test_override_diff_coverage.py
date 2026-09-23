"""`diff_override`'s own truncation-visibility coverage — the twin of
`tests/test_diff_coverage.py` for the entry point that has no refs and no
tools. See `_bounded_override_diff` (src/no_human/review/reviewer.py) and
its call site in `AdversarialReviewer.review` for the decision this file
locks in: DISCLOSURE, not the refs path's inspection requirement.

`tests/test_diff_coverage.py` itself is untouched by this change (AC4) —
this file exists precisely so that one does not have to grow to cover a
different contract.
"""

from __future__ import annotations

import subprocess

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.core.task import Task
from no_human.review.diff_coverage import DiffCoverageError, budget_diff
from no_human.review.oneshot import GateUnavailable, run_gate
from no_human.review.reviewer import (
    _DIFF_CAP,
    AdversarialReviewer,
    ReviewerUnavailable,
    _bounded_override_diff,
)
from tests.test_gate_oneshot import _git, _make_repo_with_origin, _ok_credential
from no_human.review import oneshot


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


class _CapturingBackend:
    """Records the prompt it was handed; never calls a tool. Mirrors
    `_CoverageBackend` in `tests/test_diff_coverage.py` but keeps its own
    copy — that file is frozen untouched (AC4)."""

    model = "test"

    def __init__(self):
        self.prompts: list[str] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None,
                  resume=None, on_event=None, supervisor_hook=None):
        self.prompts.append(prompt)
        return AgentResult(
            final_text=_passing_block(),
            num_turns=1,
            is_error=False,
            tokens_used=10,
            session_id="override-coverage",
            stop_reason="end_turn",
        )


def _big_override_diff() -> str:
    """4 chunks, comfortably over `_DIFF_CAP`, with `z_last.py` deliberately
    last — git orders `src/` before `tests/` before a bare top-level name,
    so a blind prefix cut drops it first."""
    return (
        _chunk("src/a.py", "A" * 20_000)
        + _chunk("src/b.py", "B" * 20_000)
        + _chunk("tests/test_a.py", "C" * 20_000)
        + _chunk("z_last.py", "D" * 5_000)
    )


async def test_an_over_cap_override_diff_still_names_its_last_file(tmp_path):
    """FAILS ON MAIN: `review()`'s `diff_override` branch did
    `diff_override[:_DIFF_CAP]`, a blind prefix cut. `z_last.py` sits after
    ~60K chars of earlier chunks, so a plain prefix slice never reaches it
    and the reviewer's prompt never mentions it at all — the exact failure
    issue #437 fixed on the refs path. This asserts the fixed path either
    keeps the file's patch or names it in the coverage ledger."""
    big = _big_override_diff()
    assert len(big) > _DIFF_CAP

    backend = _CapturingBackend()
    reviewer = AdversarialReviewer(backend=backend, timeout=1)
    t = Task.new("Review PR")
    decision = await reviewer.review(t, repo_path=tmp_path, diff_override=big)

    assert decision.passed is True
    prompt = backend.prompts[0]
    assert "diff --git a/z_last.py" in prompt or "- z_last.py" in prompt


async def test_the_override_ledger_discloses_and_does_not_demand_reads(tmp_path):
    """The decision recorded at the call site: the override path DISCLOSES
    what was cut, it does not demand the refs path's tool inspection — that
    instruction is unfollowable in a single-turn, no-tools, no-refs pass.
    The refs path's own wording (`budget_diff` default) is untouched."""
    big = _big_override_diff()
    backend = _CapturingBackend()
    reviewer = AdversarialReviewer(backend=backend, timeout=1)
    t = Task.new("Review PR")
    await reviewer.review(t, repo_path=tmp_path, diff_override=big)

    prompt = backend.prompts[0]
    assert "NO tools in this pass" in prompt
    assert "Inspect every listed path with read/search tools" not in prompt

    # The refs path's note is unchanged by the new keyword's default.
    raw = "stat\n" + _chunk("a.py", "A" * 5000) + _chunk("z.py", "B" * 5000)
    rendered, cut = budget_diff(raw, 2000)
    assert cut
    assert "Inspect every listed path with read/search tools" in rendered


async def test_an_over_cap_override_verdict_is_not_rejected_for_uninspected_paths(tmp_path):
    """The #437 inspection guard (`InspectionTracker` / `required_inspections`)
    must NOT apply here: the backend never calls a tool, and the verdict
    must still stand — a hard rejection would turn a silent truncation into
    a hard stop, which the plan explicitly rejects."""
    big = _big_override_diff()
    backend = _CapturingBackend()
    reviewer = AdversarialReviewer(backend=backend, timeout=1)
    t = Task.new("Review PR")

    decision = await reviewer.review(t, repo_path=tmp_path, diff_override=big)

    assert decision.passed is True


def test_an_unsplittable_over_cap_override_says_it_was_cut():
    """No `diff --git` boundaries at all -> `budget_diff` cannot allocate a
    per-file share and raises `DiffCoverageError`. `_bounded_override_diff`
    must not propagate that (issue #437 was about a silent cut, not about
    replacing it with a crash): it falls back to a prefix cut plus an
    explicit note naming that the cut happened."""
    raw = "x" * (_DIFF_CAP + 5000)
    rendered, total = _bounded_override_diff(raw)

    assert len(rendered) <= _DIFF_CAP
    assert total == len(raw)
    assert "truncated" in rendered.lower()


def test_an_under_cap_override_is_byte_identical():
    small = _chunk("a.py", "hello")
    rendered, total = _bounded_override_diff(small)
    assert rendered == small
    assert total == len(small)


def test_the_gate_refusal_names_the_files_it_could_not_show(tmp_path, monkeypatch):
    """AC3 evidence: `nh gate` (`run_gate`) refuses BEFORE constructing a
    reviewer when the branch diff exceeds `_DIFF_CAP`, and the refusal
    names at least one of the changed files whose patch could not be shown
    — not just the size."""
    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    for name in ("big_one.txt", "big_two.txt", "big_three.txt"):
        (repo / name).write_text("\n".join(f"line {i}" for i in range(8_000)))
        _git(repo, "add", name)
    _git(repo, "commit", "-m", "three huge files")

    _ok_credential(monkeypatch)
    constructed = []

    class _NeverConstructed:
        @classmethod
        def from_config(cls, data, **kw):
            constructed.append(True)
            return cls()

        async def review(self, task, *, repo_path, diff_override, before_ref, **kw):
            constructed.append(True)
            raise AssertionError("reviewer must never be constructed over the cap")

    monkeypatch.setattr(oneshot, "AdversarialReviewer", _NeverConstructed)

    import asyncio
    with pytest.raises(GateUnavailable, match=r"60,000|_DIFF_CAP|characters") as excinfo:
        asyncio.run(run_gate(repo))

    assert not constructed
    message = str(excinfo.value)
    assert any(name in message for name in
               ("big_one.txt", "big_two.txt", "big_three.txt"))
