"""A citation is a `file.py:LINE` reference a doc makes into code; any edit
above that line drifts it. A coder's otherwise-correct change can pass review
with zero blocking findings and only fail later, in TESTING's full-suite run
of the target repo's own `tests/test_readme_claims.py` — a whole attempt
spent on drift the coder could not have known about at review time (task
7a9e7998, attempt 1, 2026-09-09: exactly this).

FIX: `Orchestrator._citation_drift_preflight` runs before review, alongside
`_structural_budget_preflight` (same shape, not modified by this file). It
shells out to the TARGET repo's own `scripts/reanchor_citations.py --apply`
(`src/no_human/testing/citation_drift.py`, also not modified by this file —
this file only exercises its PUBLIC behaviour, never its source text) and
either commits a mechanical re-anchor on THIS attempt's branch, or — when the
script names something it will not guess at — buys ONE bounded corrective
round (`_repro_corrective_round`, reused) before falling through to review.
Never consumes one of the task's `max_attempts`.

Two layers below, both behavioural — no test in this file reads
`citation_drift.py`'s or `orchestrator.py`'s own source text:

  * Layer 1 (module-level): drives `citation_drift.run_reanchor`/
    `should_run` directly against a miniature, self-contained
    `scripts/reanchor_citations.py` + `docs/cite.md` + `pkg/mod.py` fixture
    (real subprocess, real filesystem) — proves AC3's fail-closed modes
    (unreadable file, an erroring subprocess, a citation that does not
    occur exactly once), each observably distinct from a clean run.

  * Layer 2 (integration): the same house pattern as
    `tests/test_structural_budget_preflight.py` — a real bare-repo checkout
    + a scripted backend, driving `orch._run_attempt` directly so the whole
    pipeline (preflight, mechanical fix or corrective round, commit, tamper
    check, review) runs for real except for the LLM call itself. Proves
    AC1 (drift detected before the attempt is graded) and AC2 (fixed or
    named without spending one of the 8 attempts).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.config import load_config
from no_human.core import orchestrator as orch_mod
from no_human.core.infra_breaker import infra_breaker
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing import citation_drift
from no_human.vcs import GitError, GitRepo, commit_with_manifest_repair


@pytest.fixture(autouse=True)
def _clean_infra_breaker_singleton():
    """The breaker is a process-wide singleton; reset it around every test in
    this file so one test's infra failures can never leak into the next
    one's assertions — copied from `test_structural_budget_preflight.py`."""
    infra_breaker().reset()
    yield
    infra_breaker().reset()


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


# --------------------------------------------------------------------------- #
# Fixture convention: docs/cite.md contains exactly one `mod.py:N` substring, #
# which must equal the 1-based line number of `def foo():` in pkg/mod.py.    #
# A miniature, real, standalone `scripts/reanchor_citations.py` fixture      #
# script enforces it — the exact stdout contract                             #
# (`DRIFT:`/`FAIL:`/`applied N re-anchor(s)`/`VERDICT=OK|FAIL`) is real, own #
# process output, never mocked.                                              #
# --------------------------------------------------------------------------- #

_FIXTURE_REANCHOR_SCRIPT = r'''"""Miniature reanchor_citations.py -- fixture only, not the real script.

Convention: docs/cite.md contains exactly one substring `mod.py:N` that must
equal the 1-based line number of `def foo():` inside pkg/mod.py.

Failure-injection knob, fixture-only: FIXTURE_CRASH=1 in the environment
raises before any output is printed, simulating a crash with no VERDICT
marker -- exactly the shape `citation_drift.classify` cannot name.
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "cite.md"
MOD = ROOT / "pkg" / "mod.py"

CITE_RE = re.compile(r"mod\.py:(\d+)")


def _foo_line():
    lines = MOD.read_text().splitlines()
    for i, line in enumerate(lines, start=1):
        if line.strip().startswith("def foo("):
            return i
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if os.environ.get("FIXTURE_CRASH"):
        raise RuntimeError("fixture-injected crash")

    if os.environ.get("FIXTURE_CHECKER_IMPORT_FAILS"):
        # Mirrors the REAL script's own `_load_checker` try/except in
        # `main()` exactly (same two `print`s, same `return 2`) -- but the
        # `except` here is live, not scripted: whether it fires depends on
        # whether THIS interpreter can actually `import pytest`, same as
        # the real checker's own module-scope `import pytest`.
        try:
            import pytest  # noqa: F401
        except Exception as exc:
            print(f"FAIL: could not load tests/test_readme_claims.py: {exc}")
            print("VERDICT=FAIL")
            return 2

    doc_text = DOC.read_text()
    actual = _foo_line()
    if actual is None:
        print("VERDICT=FAIL")
        print("FAIL: cite.md `mod.py:?` — foo() not found")
        return 1

    matches = list(CITE_RE.finditer(doc_text))
    if len(matches) != 1:
        raw = matches[0].group(0) if matches else "mod.py:?"
        print(f"FAIL: cite.md `{raw}` — does not occur exactly once")
        print("VERDICT=FAIL")
        return 1

    cur = int(matches[0].group(1))
    if cur == actual:
        print("VERDICT=OK")
        return 0

    old = f"mod.py:{cur}"
    new = f"mod.py:{actual}"
    verb = "re-anchoring" if args.apply else "would re-anchor"
    print(f"DRIFT: cite.md `{old}` -> `{new}` ({verb})")
    if args.apply:
        DOC.write_text(doc_text.replace(old, new, 1))
        print("applied 1 re-anchor(s)")
        print("VERDICT=OK")
        return 0
    print("VERDICT=FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

_MOD_BASELINE = "def foo():\n    return 1\n"  # foo() at line 1
_DOC_BASELINE = "See mod.py:1 for foo().\n"  # matches — clean

# Drift: two lines inserted above foo(), so it now lives at line 5.
_MOD_DRIFTED = "def helper():\n    return 0\n\n\ndef foo():\n    return 1\n"

# Ambiguous: the citation string occurs twice — the script must refuse to
# guess which one to rewrite.
_DOC_DUPLICATE = "See mod.py:1 for foo(). Also mod.py:1 again.\n"

# Missing: the citation string occurs zero times.
_DOC_MISSING = "No citation here.\n"


def _write_fixture_layout(root: Path, *, mod_text: str, doc_text: str) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "mod.py").write_text(mod_text)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "cite.md").write_text(doc_text)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "reanchor_citations.py").write_text(_FIXTURE_REANCHOR_SCRIPT)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    # Existence only — this fixture's script never imports it; only
    # `citation_drift.convention_present` cares that the file is there.
    (root / "tests" / "test_readme_claims.py").write_text(
        "# placeholder checker — existence only\n"
    )


# --------------------------------------------------------------------------- #
# Layer 1 — `citation_drift` module behaviour, direct: a clean run, a        #
# mechanically fixable drift, and AC3's three fail-closed modes.             #
# --------------------------------------------------------------------------- #


def test_should_run_false_when_convention_absent(tmp_path):
    assert citation_drift.should_run(tmp_path) is False


def test_should_run_true_when_both_halves_present(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    assert citation_drift.should_run(tmp_path) is True


def test_inapplicable_when_only_the_script_is_present(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    (tmp_path / "tests" / "test_readme_claims.py").unlink()
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.INAPPLICABLE
    assert outcome.blocking is False


def test_clean_tree_reports_clean_and_writes_nothing(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    before = (tmp_path / "docs" / "cite.md").read_text(encoding="utf-8")
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.CLEAN
    assert outcome.blocking is False
    assert (tmp_path / "docs" / "cite.md").read_text(encoding="utf-8") == before


def test_drifted_citation_is_mechanically_reanchored(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_DRIFTED, doc_text=_DOC_BASELINE)
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.REANCHORED
    assert outcome.blocking is False
    assert outcome.docs == ("docs/cite.md",)
    rewritten = (tmp_path / "docs" / "cite.md").read_text(encoding="utf-8")
    assert "mod.py:5" in rewritten
    assert "mod.py:1" not in rewritten


def test_duplicate_citation_is_unfixable_and_distinguishable_from_clean(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_DRIFTED, doc_text=_DOC_DUPLICATE)
    before = (tmp_path / "docs" / "cite.md").read_text(encoding="utf-8")
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.UNFIXABLE
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN
    assert outcome.failures, outcome
    assert (tmp_path / "docs" / "cite.md").read_text(encoding="utf-8") == before, (
        "an ambiguous citation must never be guessed at"
    )


def test_missing_citation_is_unfixable_and_distinguishable_from_clean(tmp_path):
    _write_fixture_layout(tmp_path, mod_text=_MOD_DRIFTED, doc_text=_DOC_MISSING)
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.UNFIXABLE
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN


def test_self_contradictory_ok_verdict_with_drift_and_no_applied_marker_is_unknown():
    """Send-back finding: `classify` is a pure, exhaustive translator and
    must never trust one half of a self-contradictory shape. A `VERDICT=OK`
    (rc 0) alongside an unresolved `DRIFT:` line but no `applied N
    re-anchor(s)` marker is exactly that — the real script never emits this
    combination (a resolved drift always earns its `applied` line before
    printing `VERDICT=OK`), but a pure function that only pattern-matches
    stdout must still handle it correctly rather than assume the shape can
    never occur.

    BUGGY behaviour this pins against: `verdict == "OK"` with `applied`
    False fell straight through to `Status.CLEAN` — "rewrote nothing", per
    `CitationOutcome`'s own docstring — even with a live `DRIFT:` line sitting
    in the same stdout. `Orchestrator._citation_drift_preflight` returns
    `None` on `Status.CLEAN` before ever consulting `repo.has_changes()`, so
    an uncommitted rewrite from a run this self-contradictory could silently
    ride into review as if it were the coder's own change. FIXED: this shape
    now reports `Status.UNKNOWN` (blocking), not `Status.CLEAN`. Pure
    function, no subprocess needed — direct input/output only, never a read
    of `classify`'s own source text."""
    stdout = (
        "DRIFT: cite.md `mod.py:1` -> `mod.py:5` (re-anchoring)\n"
        "VERDICT=OK\n"
    )
    outcome = citation_drift.classify(0, stdout, "")
    assert outcome.status is citation_drift.Status.UNKNOWN
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN
    assert outcome.docs == ("docs/cite.md",)


def test_self_contradictory_ok_verdict_with_fail_line_is_unknown_not_clean():
    """Send-back finding (Blocker B, sibling shape A): `VERDICT=OK` (rc 0)
    alongside an unresolved `FAIL:` line, with no `DRIFT:`/`applied` markers
    at all. In the real script's own `main()`, `VERDICT=OK` is only ever
    printed when the plan-level `unfixable` list is empty, and a `FAIL:`
    line can only come from that list — so this exact combination never
    occurs in practice, but `classify` is a pure pattern-matcher over stdout
    and must still fail closed on it rather than assume the contract holds.

    BUGGY behaviour this pins against: `verdict == "OK"` fell straight to
    the trailing `return CitationOutcome(Status.CLEAN, ...)` whenever
    `applied` and `drifts` were both empty, silently dropping a named
    unfixable citation as if the run were spotless. FIXED: any `fails` at
    all under `VERDICT=OK` now blocks with `Status.UNKNOWN`, checked before
    either the `applied` or `drifts` branch is even considered."""
    stdout = (
        "FAIL: cite.md `mod.py:1` — occurs 0 times\n"
        "VERDICT=OK\n"
    )
    outcome = citation_drift.classify(0, stdout, "")
    assert outcome.status is citation_drift.Status.UNKNOWN
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN
    assert outcome.docs == ("docs/cite.md",)
    assert outcome.failures == ("cite.md:mod.py:1",)


def test_self_contradictory_ok_verdict_with_applied_and_fail_line_is_unknown():
    """Send-back finding (Blocker B, sibling shape B): `VERDICT=OK` (rc 0)
    with BOTH an `applied N re-anchor(s)` marker (so a naive check would read
    it as `Status.REANCHORED`) AND an unresolved `FAIL:` line for a separate,
    unfixable citation the script's `_apply_all` batch never touched.

    BUGGY behaviour this pins against: `verdict == "OK"` + `applied` fell
    straight to `Status.REANCHORED`, reporting only the `drifts` it fixed and
    silently discarding the named `fails` entry — `failures` would have come
    back empty even though the raw stdout named an unfixable citation right
    next to the applied one. FIXED: the `if fails:` check runs before the
    `if applied:` check, so this shape blocks as `Status.UNKNOWN` with the
    `FAIL:` line's document folded into `docs` and its raw citation into
    `failures`, same as the no-`applied` sibling above."""
    stdout = (
        "DRIFT: cite.md `mod.py:1` -> `mod.py:5` (re-anchoring)\n"
        "applied 1 re-anchor(s)\n"
        "FAIL: cite.md `mod.py:9` — occurs 0 times\n"
        "VERDICT=OK\n"
    )
    outcome = citation_drift.classify(0, stdout, "")
    assert outcome.status is citation_drift.Status.UNKNOWN
    assert outcome.status is not citation_drift.Status.REANCHORED
    assert outcome.blocking is True
    assert outcome.docs == ("docs/cite.md",)
    assert outcome.failures == ("cite.md:mod.py:9",)


def test_interpreter_prefers_target_repos_own_venv_over_sys_executable(
        tmp_path, monkeypatch):
    """Send-back finding (Blocker C): `scripts/reanchor_citations.py` is
    stdlib-only, but it loads `tests/test_readme_claims.py` by path, and
    THAT module `import pytest`s at module scope — a dev-only dependency a
    plain `pip install no-human` run of this pipeline's own `sys.executable`
    is not guaranteed to have. `_interpreter` must prefer the TARGET REPO's
    own venv (which has its dev dependencies, including pytest) over
    whatever `sys.executable` happens to be.

    No real venv or subprocess needed to pin the ROUTING decision itself:
    `_venv_bin` (reused from `testing/runner.py`, not duplicated) only
    checks that `<name>/bin/python` exists as a path, so a stub file is
    exactly as decisive here as a real interpreter — the separate,
    end-to-end pytest-less-interpreter reproduction (real subprocess, this
    process's own interpreter run with `-S` to genuinely drop
    site-packages) lives below, in
    `test_checker_import_failure_for_missing_pytest_is_inapplicable_not_unknown`,
    where the difference in behaviour (not just routing) is observable."""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    stub_python = venv_bin / "python"
    stub_python.write_text("#!/bin/sh\n")
    stub_python.chmod(0o755)
    monkeypatch.setattr(sys, "executable", "/definitely/not/the/repos/venv")
    assert citation_drift._interpreter(tmp_path) == str(stub_python)


def test_interpreter_falls_back_to_sys_executable_when_repo_has_no_venv(
        tmp_path, monkeypatch):
    """Sibling of the test above: a repo that ships no venv at all (no
    `.venv`, `venv`, or `.venv*` directory containing a `bin/python`) must
    still work — `_interpreter` falls back to `sys.executable` rather than
    raising or returning something that does not exist."""
    monkeypatch.setattr(sys, "executable", "/some/real/interpreter/python3")
    assert citation_drift._interpreter(tmp_path) == "/some/real/interpreter/python3"


def test_checker_import_failure_for_missing_pytest_is_inapplicable_not_unknown(
        tmp_path, monkeypatch):
    """Send-back finding (N3): before this fix, `classify` had no way to
    tell "the checker never loaded, so there is no finding to report"
    (`_interpreter` falls back to a `sys.executable` that lacks the
    dev-only `pytest` `tests/test_readme_claims.py` imports) apart from the
    generic "VERDICT=FAIL with no recognizable finding" shape, which is
    `Status.UNKNOWN` — BLOCKING. That misclassified an environment fact as
    an unfixable citation, buying a corrective round every single attempt
    run under such an interpreter, one the coder can never actually clear
    (there is no citation to fix).

    Real subprocess, real failure: the fixture script's
    `FIXTURE_CHECKER_IMPORT_FAILS` knob attempts a genuine `import pytest`
    and only prints the real script's exact `FAIL:`/`VERDICT=FAIL`/`exit 2`
    shape if that import actually raises — forced here by running this
    process's OWN interpreter with `-S` (skip `site`, so site-packages,
    where pytest lives, is never added to `sys.path`), not by hand-writing
    the exception text. Never mocked, never a read of `classify`'s or
    `citation_drift`'s own source text."""
    _write_fixture_layout(tmp_path, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    before = (tmp_path / "docs" / "cite.md").read_text(encoding="utf-8")
    monkeypatch.setenv("FIXTURE_CHECKER_IMPORT_FAILS", "1")
    monkeypatch.setattr(
        citation_drift, "reanchor_command",
        lambda repo_path, *, apply: [
            sys.executable, "-S",
            str(repo_path / citation_drift.SCRIPT_RELPATH),
            "--apply" if apply else "--check",
        ],
    )
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.INAPPLICABLE
    assert outcome.blocking is False
    assert outcome.status is not citation_drift.Status.UNKNOWN
    assert "pytest" in outcome.detail
    assert (tmp_path / "docs" / "cite.md").read_text(encoding="utf-8") == before


def test_checker_import_failure_for_another_reason_stays_unknown_not_inapplicable():
    """Sibling/anti-bypass case for N3: `classify`'s new branch is narrowed
    to the ONE reason `_interpreter`'s docstring documents
    (`No module named 'pytest'`) on purpose. A checker that fails to import
    for ANY other reason — a syntax error, a genuinely broken import a
    coder introduced — must still block as `Status.UNKNOWN`, exactly as
    before this fix. Otherwise a coder could dodge a real citation defect
    entirely by breaking `tests/test_readme_claims.py`'s import some other
    way and having it wrongly read as an inert environment fact. Pure
    function, direct input/output, same idiom as this file's other
    `classify(...)`-only tests."""
    stdout = (
        "FAIL: could not load tests/test_readme_claims.py: "
        "SyntaxError: invalid syntax (test_readme_claims.py, line 42)\n"
        "VERDICT=FAIL\n"
    )
    outcome = citation_drift.classify(2, stdout, "")
    assert outcome.status is citation_drift.Status.UNKNOWN
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.INAPPLICABLE


def test_revert_worktree_writes_unguarded_requires_component_argument():
    """Send-back finding (Blocker A) mutation test: `component` on
    `_revert_worktree_writes_unguarded` must be a required keyword-only
    argument, not a silently-defaulted one — a caller added later (or a
    refactor of an existing one) that forgets to name its own writer must
    fail LOUDLY at the call site, not credit (or blame) the wrong component
    for writes it did not make. Called unbound, with no repo/store/config at
    all, because the argument-binding failure this test pins happens before
    any of `self`'s attributes are ever touched — a real `Orchestrator`
    instance is unnecessary machinery for what `TypeError` alone already
    proves."""
    with pytest.raises(TypeError):
        Orchestrator._revert_worktree_writes_unguarded(  # type: ignore[call-arg]
            object(), object(), {})


def test_send_back_message_names_shown_failures_and_omitted_count():
    """N6/N7: `citation_drift_send_back_message` is module-level and pure
    (see its docstring) — tested here directly against its return value for
    given inputs, never by reading its own source text. With more failures
    than `_CITATION_DRIFT_FAILURES_NAMED`, the message must name only the
    first N and say how many more were omitted, must quote the script path
    (never a bare `python`/`python3` invocation — `reanchor_command`'s own
    docstring explains why that would not work in a uv/venv project), must
    tell the coder not to hand-edit a line number, and must mention the
    CITATION_TABLE row exception (the one carve-out N1 added)."""
    named = orch_mod._CITATION_DRIFT_FAILURES_NAMED
    failures = [f"docs/x.md `pkg/mod.py:{i}` — ambiguous" for i in range(named + 3)]
    msg = orch_mod.citation_drift_send_back_message(failures, "some stdout detail")

    for f in failures[:named]:
        assert f in msg
    for f in failures[named:]:
        assert f not in msg
    assert "(+3 more)" in msg
    assert str(citation_drift.SCRIPT_RELPATH) in msg
    assert "python " not in msg and not msg.startswith("python")
    assert "do not hand-edit a line number" in msg.lower()
    assert str(citation_drift.CHECKER_RELPATH) in msg
    assert "CITATION_TABLE" in msg
    assert "some stdout detail" in msg


def test_send_back_message_omits_more_count_when_nothing_is_omitted():
    """Sibling of the above: with failures at or below
    `_CITATION_DRIFT_FAILURES_NAMED`, nothing was actually omitted, so the
    "(+N more)" clause must not appear at all — it would otherwise falsely
    claim failures were dropped that never existed."""
    msg = orch_mod.citation_drift_send_back_message(["docs/x.md `mod.py:1` — dup"], "detail")
    assert "more)" not in msg
    assert "docs/x.md `mod.py:1` — dup" in msg


def test_send_back_message_with_no_named_failures_reports_indeterminate_run():
    """N7: when `failures` is empty (an `UNKNOWN` outcome from a timeout,
    crash, or unparsable output — never a specific citation the script
    refused to guess at), the message must say the run itself did not
    finish cleanly, and must NOT claim a citation was named or that a fix
    is needed "for the doc(s) named above" — no doc was named. Both this
    test and the one above go through the same function; together they
    prove it differentiates the two paths rather than always returning the
    same templated text."""
    msg = orch_mod.citation_drift_send_back_message([], "a crash happened")
    assert "could not confirm this repo's doc citations are clean" in msg
    assert "for the doc(s) named above" not in msg
    assert "a crash happened" in msg
    # Still carries the shared, non-branch-specific guidance both paths need.
    assert str(citation_drift.SCRIPT_RELPATH) in msg
    assert str(citation_drift.CHECKER_RELPATH) in msg
    assert "CITATION_TABLE" in msg


def test_send_back_message_truncates_overlong_detail():
    """The embedded script stdout/stderr is bounded by
    `_CITATION_DRIFT_DETAIL_CHARS` before it ever reaches the prompt (see
    the function's own docstring) — proven here by actually overflowing it
    with a real, oversized string and checking the result is shorter than
    the input and carries a visible truncation marker, not by reading the
    slicing code itself."""
    limit = orch_mod._CITATION_DRIFT_DETAIL_CHARS
    detail = "x" * (limit + 500)
    msg = orch_mod.citation_drift_send_back_message(["docs/x.md `mod.py:1` — dup"], detail)
    assert "(truncated)" in msg
    assert "x" * (limit + 500) not in msg
    assert "x" * limit in msg


def test_repro_round_scope_note_unchanged_with_no_allow_paths():
    """N1/N7: `_repro_round_scope_note` is what actually reaches the coder
    (see `_repro_corrective_round`'s call site). With no `allow_paths` — the
    three pre-existing callers (repro-waived, declared-files,
    structural-budget) never pass it — this must return
    `_REPRO_ROUND_SCOPE_NOTE` completely unchanged, byte for byte, so this
    fix cannot alter their prompt text at all."""
    assert orch_mod._repro_round_scope_note() == orch_mod._REPRO_ROUND_SCOPE_NOTE
    assert orch_mod._repro_round_scope_note([]) == orch_mod._REPRO_ROUND_SCOPE_NOTE
    assert "docs/" not in orch_mod._REPRO_ROUND_SCOPE_NOTE


def test_repro_round_scope_note_names_allowed_doc_paths():
    """N1/N7: with `allow_paths` given (only `_citation_drift_preflight`
    does), the note must still contain the original text (a coder relying
    on the base rule — tests/manifest — must keep seeing it) AND must
    additionally name every given path as in-scope — proving the
    enforcement (`_repro_round_out_of_scope(..., extra_ok=allow_paths)`)
    and the prompt the coder reads never disagree."""
    note = orch_mod._repro_round_scope_note(["docs/security.md", "docs/eval.md"])
    assert orch_mod._REPRO_ROUND_SCOPE_NOTE in note
    assert "docs/security.md" in note
    assert "docs/eval.md" in note
    assert note != orch_mod._REPRO_ROUND_SCOPE_NOTE


def test_unreadable_file_fails_closed_and_is_distinguishable_from_clean(tmp_path):
    """A real, unreadable file (mode 000) makes the fixture script's own
    `DOC.read_text()` raise `PermissionError`, uncaught — exactly the
    "crash with no VERDICT marker" shape `classify` cannot name and must not
    guess at. Skipped when running as root, where permission bits are not
    enforced (the file would still be readable and the test's premise would
    not hold)."""
    if os.name != "posix":
        pytest.skip("posix permission bits only")
    if os.geteuid() == 0:
        pytest.skip("root ignores file permission bits")
    _write_fixture_layout(tmp_path, mod_text=_MOD_DRIFTED, doc_text=_DOC_BASELINE)
    doc_path = tmp_path / "docs" / "cite.md"
    doc_path.chmod(0o000)
    try:
        outcome = citation_drift.run_reanchor(tmp_path)
    finally:
        doc_path.chmod(0o644)
    assert outcome.status is citation_drift.Status.UNKNOWN
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN
    assert outcome.detail


def test_erroring_subprocess_fails_closed_and_is_distinguishable_from_clean(
        tmp_path, monkeypatch):
    """The fixture script's own `FIXTURE_CRASH` knob raises before printing
    anything — a real subprocess that exits non-zero with no `VERDICT=`
    marker on stdout. `subprocess.run` inherits the parent environment by
    default, so setting this in the test process reaches the child."""
    _write_fixture_layout(tmp_path, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    monkeypatch.setenv("FIXTURE_CRASH", "1")
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.UNKNOWN
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN
    assert outcome.detail


def test_subprocess_that_cannot_even_start_fails_closed(tmp_path, monkeypatch):
    """Distinct from the crash above: here the OS itself cannot start the
    process at all (`FileNotFoundError`, an `OSError` subclass) —
    `run_reanchor`'s own `except OSError` branch. Forced behaviourally, by
    monkeypatching the public `reanchor_command` helper to point at a
    nonexistent interpreter — never by reading or asserting on
    `run_reanchor`'s source."""
    _write_fixture_layout(tmp_path, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    monkeypatch.setattr(
        citation_drift, "reanchor_command",
        lambda repo_path, *, apply: [
            "/definitely/does/not/exist/no-such-python-xyz",
            str(repo_path / citation_drift.SCRIPT_RELPATH),
            "--apply",
        ],
    )
    outcome = citation_drift.run_reanchor(tmp_path)
    assert outcome.status is citation_drift.Status.UNKNOWN
    assert outcome.blocking is True
    assert outcome.status is not citation_drift.Status.CLEAN
    assert outcome.detail


# --------------------------------------------------------------------------- #
# Layer 2 — integration: `orch._run_attempt` against a real bare-repo        #
# checkout, driven by a scripted backend, mirroring                          #
# `test_structural_budget_preflight.py`'s house pattern.                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                    capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    _write_fixture_layout(work, mod_text=_MOD_BASELINE, doc_text=_DOC_BASELINE)
    (work / "README.md").write_text("fixture repo\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


async def _run_one_task_attempt(store, bare_repo, tmp_path, backend, *, kind="feature"):
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                         event_sink=events.append)
    task = Task.new("shift helper() above foo()", repo_path=str(bare_repo), kind=kind)
    task.acceptance_criteria = ["helper() exists in pkg/mod.py"]
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, task, repo, events


def _is_review_boundary(event: dict) -> bool:
    kind = event.get("kind", "")
    return kind == "review_advisory" or kind.startswith("review_")


class _DriftsThenLeavesItBackend:
    """One turn: shifts `foo()` down by inserting `helper()` above it, and
    commits — a real change, citation left stale. Never touches the doc
    itself; the preflight's own mechanical re-anchor must be what fixes it,
    with no second backend call needed at all."""

    def __init__(self):
        self.calls = 0
        self.prompts = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "pkg/mod.py"}))
        cwd.joinpath("pkg", "mod.py").write_text(_MOD_DRIFTED)
        return AgentResult(final_text="added helper()", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s1", stop_reason="end_turn")


async def test_a_drifted_citation_is_mechanically_reanchored_before_review_no_extra_attempt(
        bare_repo, tmp_path, store):
    """AC1 + AC2: the drift is caught and fixed BEFORE review even runs, and
    the whole attempt count stays at 1 — proven by the preflight's own
    behaviour (event emitted, commit landed, single attempt row), never by
    reading `_citation_drift_preflight`'s source."""
    backend = _DriftsThenLeavesItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1, "a mechanical re-anchor needs no extra coder turn"

    kinds = [e["kind"] for e in events]
    assert kinds.count("citation_drift") == 1, events
    assert "citation_drift_corrective_round" not in kinds, events

    drift_idx = kinds.index("citation_drift")
    review_idx = next(i for i, e in enumerate(events) if _is_review_boundary(e))
    assert drift_idx < review_idx, events

    committed = subprocess.run(
        ["git", "show", "HEAD:docs/cite.md"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert "mod.py:5" in committed
    assert "mod.py:1" not in committed

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


async def test_the_mechanical_fix_commit_reports_a_manifest_repair_not_silently(
        bare_repo, tmp_path, store, monkeypatch):
    """N8 (send-back finding): every other `commit_with_manifest_repair`
    call site in orchestrator.py passes `on_repair` and drains it into a
    `manifest_repaired` event (`_emit_manifest_repairs`) — the citation
    drift preflight's own mechanical-fix commit was the one call site that
    silently dropped a repair callback on the floor, so a re-approved pinned
    file this commit incidentally touched would never reach the task's
    event record. Proven behaviourally: `orch_mod.commit_with_manifest_repair`
    is replaced with a fake that (like a real repair) invokes `on_repair`
    once before delegating to the real function, and this test asserts the
    resulting `manifest_repaired` event actually carries what the callback
    reported — never by reading `_citation_drift_preflight`'s source for the
    keyword argument."""
    seen_kwargs = {}

    def fake_commit(repo, paths, message, on_repair=None):
        # This patches the module-level name every commit call site in
        # orchestrator.py resolves, including the attempt's own initial
        # commit earlier in the same `_run_attempt` — scoped to the citation
        # drift auto-fix commit specifically (its own distinct message,
        # same marker the "no stray file" test above keys off of) so this
        # fake does not also inject a repair into the unrelated main commit.
        if "citation drift: auto-re-anchored" in message:
            seen_kwargs["on_repair"] = on_repair
            if on_repair is not None:
                on_repair(["docs/cite.md"], "re-approved a stale pin")
        return commit_with_manifest_repair(repo, paths, message, on_repair=on_repair)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair", fake_commit)

    backend = _DriftsThenLeavesItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    assert "on_repair" in seen_kwargs, (
        "the citation drift preflight's commit must pass on_repair, like "
        "every other commit_with_manifest_repair call site in this file"
    )
    assert seen_kwargs["on_repair"] is not None

    repaired_events = [e for e in events if e["kind"] == "manifest_repaired"]
    assert len(repaired_events) == 1, events
    assert "docs/cite.md" in repaired_events[0]["paths"]
    assert "re-approved a stale pin" in repaired_events[0]["notes"]


class _DriftsAndLeavesAnUnannouncedStrayFileBackend:
    """Same mechanically-fixable drift as `_DriftsThenLeavesItBackend`
    (`pkg/mod.py` shifted, `docs/cite.md` left stale) — but this turn ALSO
    writes a second file, `STRAY_SCRATCH.txt`, straight to disk WITHOUT ever
    reporting it through `on_event`. The orchestrator's own attempt-commit
    only stages what it was told the coder edited (`_agent_edited_files`),
    so this file rides along, uncommitted and untracked, into the citation
    preflight's own mechanical-fix step — exactly the "something unrelated
    already sitting uncommitted" shape the preflight's own commit must never
    sweep in under a message claiming the re-anchor script produced it."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "pkg/mod.py"}))
        cwd.joinpath("pkg", "mod.py").write_text(_MOD_DRIFTED)
        # Never announced via on_event — must not be swept into ANY commit
        # this preflight makes on the coder's behalf.
        cwd.joinpath("STRAY_SCRATCH.txt").write_text("unrelated scratch data\n")
        return AgentResult(final_text="added helper()", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s1", stop_reason="end_turn")


async def test_the_mechanical_fix_commit_never_sweeps_in_an_unannounced_stray_file(
        bare_repo, tmp_path, store, monkeypatch):
    """Reproduces the second send-back bug: `_citation_drift_preflight`'s own
    mechanical-fix commit called `commit_with_manifest_repair(repo, None,
    commit_msg)` — and `paths=None` means `repo.commit_all(...)`, which
    stages and commits EVERYTHING currently dirty in the worktree, not just
    what the re-anchor script itself wrote. A file the coder's turn dropped
    on disk but never told the orchestrator about (so it was never part of
    the coder's OWN commit) would ride along into this commit under a
    message that claims the re-anchor script produced it — a false
    attribution the commit's own contents contradict.

    FIXED: the method already captures `before = self._worktree_state(repo)`
    near its top; it must diff that against the worktree state right before
    committing and pass exactly that delta as `paths`, never `None`.

    Proven two ways, both behavioural: (1) intercepting
    `commit_with_manifest_repair` to capture the exact `paths` argument used
    for the "citation drift: auto-re-anchored" commit, and (2) checking HEAD
    itself afterwards — never by reading `_citation_drift_preflight`'s
    source text."""
    seen_paths = {}

    def capturing_commit(repo, paths, message, on_repair=None):
        if "citation drift: auto-re-anchored" in message:
            seen_paths["paths"] = list(paths) if paths else paths
        return commit_with_manifest_repair(repo, paths, message, on_repair=on_repair)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair", capturing_commit)

    backend = _DriftsAndLeavesAnUnannouncedStrayFileBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1, "a mechanical re-anchor needs no extra coder turn"

    assert "paths" in seen_paths, events
    assert seen_paths["paths"], (
        "the mechanical-fix commit must be scoped to a concrete path list, "
        f"never None/empty: {events}"
    )
    assert "STRAY_SCRATCH.txt" not in seen_paths["paths"], (
        "an unannounced stray file must never be scoped into the citation "
        f"preflight's own commit: {seen_paths}"
    )
    assert any(p.endswith("docs/cite.md") for p in seen_paths["paths"]), seen_paths

    # HEAD itself must show the same story: the doc fix landed, the stray
    # file never did.
    committed = subprocess.run(
        ["git", "show", "HEAD:docs/cite.md"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert "mod.py:5" in committed

    stray_in_head = subprocess.run(
        ["git", "show", "HEAD:STRAY_SCRATCH.txt"], cwd=repo.path,
        capture_output=True, text=True,
    )
    assert stray_in_head.returncode != 0, (
        "STRAY_SCRATCH.txt must never reach HEAD via the citation "
        f"preflight's own commit: {stray_in_head.stdout!r}"
    )

    # Still sitting in the worktree, untracked — never silently discarded
    # either, just correctly left out of THIS commit.
    status = subprocess.run(
        ["git", "status", "--porcelain", "STRAY_SCRATCH.txt"],
        cwd=repo.path, check=True, capture_output=True, text=True,
    ).stdout
    assert "STRAY_SCRATCH.txt" in status, status

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


class _AmbiguousDriftThenFixesItBackend:
    """Turn 1: shifts `foo()` AND makes the citation ambiguous (duplicates
    it) — the script will not guess which occurrence to rewrite, so this
    needs the one bounded corrective round. Turn 2 (the round): rewrites the
    doc by hand to a single, correct citation."""

    def __init__(self):
        self.calls = 0
        self.prompts = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "pkg/mod.py"}))
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "docs/cite.md"}))
            cwd.joinpath("pkg", "mod.py").write_text(_MOD_DRIFTED)
            cwd.joinpath("docs", "cite.md").write_text(_DOC_DUPLICATE)
            return AgentResult(final_text="added helper(), touched doc", num_turns=2,
                               is_error=False, tokens_used=100, session_id="s1",
                               stop_reason="end_turn")
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "docs/cite.md"}))
        cwd.joinpath("docs", "cite.md").write_text("See mod.py:5 for foo().\n")
        return AgentResult(final_text="fixed the citation by hand", num_turns=1,
                           is_error=False, tokens_used=10, session_id="s2",
                           stop_reason="end_turn")


async def test_an_unfixable_citation_buys_one_corrective_round_before_review_no_extra_attempt(
        bare_repo, tmp_path, store):
    """AC1 + AC2, the other branch: the script names the drift but refuses
    to guess (ambiguous citation) — one bounded round, before review, still
    only ONE attempt row total."""
    backend = _AmbiguousDriftThenFixesItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    kinds = [e["kind"] for e in events]
    assert kinds.count("citation_drift") >= 1, events
    assert kinds.count("citation_drift_corrective_round") == 1, events

    round_idx = kinds.index("citation_drift_corrective_round")
    review_idx = next(i for i, e in enumerate(events) if _is_review_boundary(e))
    assert round_idx < review_idx, events

    # The corrective round's instruction names the citation the script would
    # not guess at — the coder is never left to re-derive it.
    instruction = backend.prompts[1]
    assert "cite.md" in instruction
    assert "mod.py:1" in instruction

    # N1/N7 (send-back finding): the SCOPE text appended to this round's
    # prompt must itself say the doc path is writable — before this fix,
    # `_REPRO_ROUND_SCOPE_NOTE` was appended unconditionally and said "this
    # round may write ONLY the reproduction manifest ... and test file(s)
    # ... nothing else. Any other path you change is discarded uncommitted
    # and the attempt fails.", flatly contradicting the instruction above
    # that tells the coder to fix the doc. `_repro_round_out_of_scope`
    # already admitted the doc path via `extra_ok`/`allow_paths`, but a
    # compliant coder reading only the prompt had no way to know that. This
    # checks the actual rendered SCOPE clause, not the failures list quoted
    # above it (which also happens to mention "cite.md").
    scope_text = instruction[instruction.index("SCOPE:"):]
    assert "docs/cite.md" in scope_text, (
        "the SCOPE clause itself must name the doc path as in-scope, "
        f"matching what the enforcement already permits: {scope_text!r}"
    )

    committed = subprocess.run(
        ["git", "show", "HEAD:docs/cite.md"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert "mod.py:5" in committed

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


async def test_a_re_entered_attempt_does_not_buy_a_second_round(bare_repo, tmp_path, store):
    """Once-per-attempt: a second pass over the SAME attempt id must not
    dispatch a second backend call or emit a second corrective-round event,
    mirroring the sibling preflights' own re-entrance test."""
    backend = _AmbiguousDriftThenFixesItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    attempt_id = attempts[0]["id"]

    again = await orch._citation_drift_preflight(
        task, repo, attempt_id=attempt_id, branch="main",
        attempt_n=1, tamper_before=repo.head_sha(),
    )

    assert again is None
    assert backend.calls == 2, "a re-entered attempt must not dispatch a third backend call"
    assert len(
        [e for e in events if e["kind"] == "citation_drift_corrective_round"]
    ) == 1, events


class _DriftsAmbiguouslyAndNeverFixesItBackend:
    """Turn 1: the same ambiguous-citation drift as
    `_AmbiguousDriftThenFixesItBackend` (shifts `foo()`, duplicates the
    citation). Turn 2 (the corrective round): does NOT touch the doc at
    all — the round ends with the citation just as broken as before it
    ran. Models the once-per-attempt latch's OTHER case, distinct from
    `_AmbiguousDriftThenFixesItBackend`: there, by the time of re-entry the
    tree is genuinely clean, so a re-check returning "nothing to do" is
    ambiguous between "the latch fired" and "run_reanchor found it clean on
    its own." Here the tree is still genuinely broken at re-entry time, so
    the same "nothing to do" result can only be the latch."""

    def __init__(self):
        self.calls = 0
        self.prompts = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "pkg/mod.py"}))
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "docs/cite.md"}))
            cwd.joinpath("pkg", "mod.py").write_text(_MOD_DRIFTED)
            cwd.joinpath("docs", "cite.md").write_text(_DOC_DUPLICATE)
            return AgentResult(final_text="added helper(), touched doc", num_turns=2,
                               is_error=False, tokens_used=100, session_id="s1",
                               stop_reason="end_turn")
        return AgentResult(final_text="did not touch the citation", num_turns=1,
                           is_error=False, tokens_used=10, session_id="s2",
                           stop_reason="end_turn")


async def test_a_re_entered_attempt_while_still_drifted_does_not_buy_a_second_round(
        bare_repo, tmp_path, store):
    """N5 (send-back finding): `test_a_re_entered_attempt_does_not_buy_a_second_round`
    above only proves the latch where the corrective round's own turn
    happened to FIX the citation before re-entry — a second call returning
    "nothing to do" there is consistent with either the latch firing OR
    `run_reanchor` genuinely finding a clean tree on its own, so it does not
    by itself prove the latch holds once the tree is CONFIRMED still broken.

    This test closes that gap: the corrective round's turn leaves the
    citation exactly as ambiguous as before it ran, confirmed independently
    with a fresh, direct `run_reanchor(..., apply=False)` call (never
    trusting the premise), and a second, direct call to
    `_citation_drift_preflight` with the SAME attempt_id must still return
    None and must not dispatch a third backend call or emit a second
    corrective-round event — proving the once-per-attempt latch keys off
    the attempt id alone, never off whether the problem actually got
    fixed."""
    backend = _DriftsAmbiguouslyAndNeverFixesItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    attempt_id = attempts[0]["id"]

    # Confirm, independently of the preflight under test, that the tree
    # really is still drifted — a fresh, read-only re-check, never an
    # assumption. If this were CLEAN, the assertion below would prove
    # nothing about the latch.
    fresh = citation_drift.run_reanchor(repo.path, apply=False)
    assert fresh.status is citation_drift.Status.UNFIXABLE, fresh

    again = await orch._citation_drift_preflight(
        task, repo, attempt_id=attempt_id, branch="main",
        attempt_n=1, tamper_before=repo.head_sha(),
    )

    assert again is None, (
        "the once-per-attempt latch must hold even while the citation is "
        "still genuinely broken — a second bounded round must never be "
        "bought for the same attempt id"
    )
    assert backend.calls == 2, "a re-entered attempt must not dispatch a third backend call"
    assert len(
        [e for e in events if e["kind"] == "citation_drift_corrective_round"]
    ) == 1, events


class _TouchesNothingCitedBackend:
    """A change that never comes near the citation convention at all — the
    preflight must stay silent and free."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "README.md"}))
        cwd.joinpath("README.md").write_text("fixture repo — updated\n")
        return AgentResult(final_text="updated README", num_turns=1, is_error=False,
                           tokens_used=10, session_id="s", stop_reason="end_turn")


async def test_a_clean_tree_emits_no_citation_drift_event(bare_repo, tmp_path, store):
    backend = _TouchesNothingCitedBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 1
    kinds = [e["kind"] for e in events]
    assert "citation_drift" not in kinds, events
    assert "citation_drift_corrective_round" not in kinds, events

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


class _DriftsThenFixedByRoundBackend:
    """Turn 1: the same mechanically-fixable drift as
    `_DriftsThenLeavesItBackend` — never touches the doc itself, so the
    preflight's own auto-re-anchor write is the only thing that could fix
    it. Turn 2 exists ONLY for the case where that auto-fix could not be
    committed and a bounded corrective round is bought instead: it writes
    the doc citation by hand, since the mechanical rewrite was reverted."""

    def __init__(self):
        self.calls = 0
        self.prompts = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "pkg/mod.py"}))
            cwd.joinpath("pkg", "mod.py").write_text(_MOD_DRIFTED)
            return AgentResult(final_text="added helper()", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s1", stop_reason="end_turn")
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "docs/cite.md"}))
        cwd.joinpath("docs", "cite.md").write_text("See mod.py:5 for foo().\n")
        return AgentResult(final_text="fixed the citation by hand after commit failure",
                           num_turns=1, is_error=False, tokens_used=10,
                           session_id="s2", stop_reason="end_turn")


async def test_a_commit_failure_after_mechanical_reanchor_buys_a_round_not_false_success(
        bare_repo, tmp_path, store, monkeypatch):
    """Reproduces the dead-attempt failure mode a prior review round flagged:
    a `GitError` from `commit_with_manifest_repair` right after the
    preflight's OWN mechanical re-anchor write landed on disk must never be
    reported as "auto-re-anchored" success while that fix never actually
    reached the branch — a preflight that cannot COMMIT its fix has not
    fixed anything. Forced behaviourally: `commit_with_manifest_repair` is
    made to fail for exactly the preflight's own auto-fix commit (identified
    by its own commit message, which names the doc auto-fix — never by
    call order, since the coder's own attempt commit runs through the same
    helper first), then delegates to the real implementation for every
    other commit (the coder's own, and the corrective round's) — never by
    reading `_citation_drift_preflight`'s source.

    On the buggy code this reproduces: the failure was only logged as an
    advisory and the method fell straight through to reporting
    `Status.REANCHORED` success and returning `None` — the coder's backend
    was never called a second time (`backend.calls` stayed 1) and the
    never-committed doc rewrite was left sitting uncommitted in the
    worktree, never reaching `HEAD`. FIXED: the failed write is discarded
    and the run falls through to the SAME bounded corrective round an
    unfixable citation gets, so the coder's own (this time successfully
    committed) fix is what actually lands on the branch."""
    failed_once = []

    def flaky_commit(repo, paths, message, on_repair=None):
        if "citation drift: auto-re-anchored" in message and not failed_once:
            failed_once.append(message)
            raise GitError("simulated: manifest gate wedged")
        return commit_with_manifest_repair(repo, paths, message, on_repair=on_repair)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair", flaky_commit)

    backend = _DriftsThenFixedByRoundBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2, (
        "a commit failure on the preflight's own mechanical fix must buy "
        "the coder a bounded corrective round, never report false success "
        "off turn 1 alone"
    )

    kinds = [e["kind"] for e in events]
    assert "citation_drift_corrective_round" in kinds, events

    # The reverted, never-committed mechanical rewrite must never be what
    # ships on the branch — only the round's OWN, successfully committed
    # hand fix may land at HEAD.
    committed = subprocess.run(
        ["git", "show", "HEAD:docs/cite.md"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert "mod.py:5" in committed

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


async def test_an_indeterminate_run_lets_the_corrective_round_land_a_doc_fix(
        bare_repo, tmp_path, store, monkeypatch):
    """The `FIXTURE_CRASH` knob forces a genuine `Status.UNKNOWN` with NO
    doc named at all (no VERDICT marker, no DRIFT/FAIL line whatsoever — a
    plain crash) for every invocation of the fixture script, including the
    preflight's own initial `--apply` and its post-round `--check`. Unlike
    `test_an_unfixable_citation_buys_one_corrective_round...` (whose script
    names `cite.md` before refusing to guess), this outcome carries
    `outcome.docs == ()`: the script gave the preflight nothing to scope a
    corrective round to.

    The bounded round it buys must still be ABLE to commit whatever fix the
    coder makes by hand — never silently discard that fix as "out of scope"
    for the sole reason that the run which bought the round could not name
    a doc. Forced entirely behaviourally (an env var reaching a real
    subprocess); nothing here reads `_citation_drift_preflight`'s or
    `citation_drift.py`'s own source text."""
    monkeypatch.setenv("FIXTURE_CRASH", "1")
    backend = _DriftsThenFixedByRoundBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2, (
        "an indeterminate (crashed) run must still buy exactly one "
        "corrective round"
    )

    kinds = [e["kind"] for e in events]
    assert kinds.count("citation_drift_corrective_round") == 1, events

    # The round's own hand fix must actually reach HEAD — never discarded
    # as "out of scope" merely because the crashed script named no doc to
    # scope the round to.
    committed = subprocess.run(
        ["git", "show", "HEAD:docs/cite.md"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert "mod.py:5" in committed, (
        "the corrective round's own doc fix for an indeterminate run must "
        f"land at HEAD, not be discarded as out of scope: {events}"
    )

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


class _UnfixableDriftWithStrayFileThenFixesItBackend:
    """Turn 1: the SAME ambiguous (duplicate) citation as
    `_AmbiguousDriftThenFixesItBackend` — the script names it and refuses to
    guess, writing NOTHING to disk itself — but this turn ALSO drops a
    second file, `STRAY_SCRATCH.txt`, straight to disk WITHOUT ever
    reporting it through `on_event`, exactly like
    `_DriftsAndLeavesAnUnannouncedStrayFileBackend`. The combination is the
    gap the sibling stray-file test does not cover: THAT test's drift is
    mechanically fixable (`changed` is non-empty — the script's own write),
    so it never exercises what happens when the run writes nothing at all
    (`changed` empty) while the worktree is still dirty for an unrelated
    reason. Turn 2 (the bought corrective round): fixes the doc by hand."""

    def __init__(self):
        self.calls = 0
        self.prompts = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "pkg/mod.py"}))
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "docs/cite.md"}))
            cwd.joinpath("pkg", "mod.py").write_text(_MOD_DRIFTED)
            cwd.joinpath("docs", "cite.md").write_text(_DOC_DUPLICATE)
            # Never announced via on_event — rides along, uncommitted and
            # untracked, into the preflight's own decision of whether to
            # commit anything at all.
            cwd.joinpath("STRAY_SCRATCH.txt").write_text("unrelated scratch data\n")
            return AgentResult(final_text="added helper(), touched doc", num_turns=2,
                               is_error=False, tokens_used=100, session_id="s1",
                               stop_reason="end_turn")
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "docs/cite.md"}))
        cwd.joinpath("docs", "cite.md").write_text("See mod.py:5 for foo().\n")
        return AgentResult(final_text="fixed the citation by hand", num_turns=1,
                           is_error=False, tokens_used=10, session_id="s2",
                           stop_reason="end_turn")


async def test_an_unfixable_run_with_an_unrelated_dirty_stray_file_never_commits_it(
        bare_repo, tmp_path, store, monkeypatch):
    """BLOCKER send-back finding: the mechanical-fix commit branch used to
    be gated on `repo.has_changes()` (true for ANY dirty path anywhere in
    the worktree) rather than on `changed` (the before/after porcelain
    delta — true only when THIS run itself wrote something). An UNFIXABLE
    run (an ambiguous, duplicate citation the script will not guess at)
    writes nothing on disk, so `changed` is empty — but if the worktree is
    ALSO dirty for an unrelated reason (an unannounced stray file, exactly
    like `test_the_mechanical_fix_commit_never_sweeps_in_an_unannounced_
    stray_file`'s scenario, but for the REANCHORED path only), the OLD code
    still entered the commit branch with an EMPTY `changed` list.
    `commit_with_manifest_repair(repo, [], message)` treats an empty list
    exactly like `None` (`if paths: commit_paths(...) else: commit_all(...)`
    — `[]` is falsy) and fell through to `commit_all`, sweeping the stray
    file into a commit whose message credits the re-anchor script for
    content it never produced. Reproduced by the same recipe the human
    reviewer used: an UNFIXABLE (not REANCHORED) drift plus a stray file.

    FIXED: the commit branch is now gated on `changed` truthiness alone, so
    an UNFIXABLE run that writes nothing never even attempts a commit —
    proven here by capturing every `commit_with_manifest_repair` call whose
    message claims the auto-re-anchor and asserting there are none, and by
    checking that `STRAY_SCRATCH.txt` never reaches HEAD."""
    seen_auto_fix_commits = []

    def capturing_commit(repo, paths, message, on_repair=None):
        if "citation drift: auto-re-anchored" in message:
            seen_auto_fix_commits.append(list(paths) if paths else paths)
        return commit_with_manifest_repair(repo, paths, message, on_repair=on_repair)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair", capturing_commit)

    backend = _UnfixableDriftWithStrayFileThenFixesItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2, "an unfixable citation still buys one bounded round"

    assert seen_auto_fix_commits == [], (
        "an UNFIXABLE run that wrote nothing must never attempt a "
        f"mechanical-fix commit at all: {seen_auto_fix_commits}"
    )

    kinds = [e["kind"] for e in events]
    assert kinds.count("citation_drift_corrective_round") == 1, events

    # STRAY_SCRATCH.txt must never reach HEAD via any commit this preflight
    # made on the coder's behalf — only the round's own hand fix may land.
    stray_in_head = subprocess.run(
        ["git", "show", "HEAD:STRAY_SCRATCH.txt"], cwd=repo.path,
        capture_output=True, text=True,
    )
    assert stray_in_head.returncode != 0, (
        "STRAY_SCRATCH.txt must never reach HEAD via the citation "
        f"preflight's own commit: {stray_in_head.stdout!r}"
    )

    # Still sitting in the worktree, untracked — never silently discarded,
    # just correctly left out of every commit this preflight made.
    status = subprocess.run(
        ["git", "status", "--porcelain", "STRAY_SCRATCH.txt"],
        cwd=repo.path, check=True, capture_output=True, text=True,
    ).stdout
    assert "STRAY_SCRATCH.txt" in status, status

    committed = subprocess.run(
        ["git", "show", "HEAD:docs/cite.md"], cwd=repo.path,
        check=True, capture_output=True, text=True,
    ).stdout
    assert "mod.py:5" in committed

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1


async def test_the_post_round_verification_recheck_never_mutates_the_worktree(
        bare_repo, tmp_path, store, monkeypatch):
    """Send-back finding: a claimed fix ("the post-round verification
    re-check now passes `apply=False` so it can never itself mutate the
    worktree") was not pinned by any test — flipping that literal back to
    `apply=True` would leave the whole existing suite green, since the
    fixture's own hand fix already leaves the tree clean either way and
    nothing else distinguishes the two call shapes.

    Pinned directly and behaviourally: wrap the REAL
    `citation_drift.run_reanchor` so that, for exactly the call this
    preflight makes with `apply=False` (the post-round re-check), the
    wrapper snapshots the full worktree state — `git status --porcelain`
    PLUS the actual bytes of every fixture file — immediately before and
    after the real, unwrapped call runs, and records whether they are
    byte-for-byte identical. Never by reading `_citation_drift_preflight`'s
    source to see which literal it passes at that call site."""
    real_run_reanchor = citation_drift.run_reanchor
    recheck_snapshots = []

    def _snapshot(repo_path):
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_path, check=True, capture_output=True, text=True,
        ).stdout
        contents = []
        for rel in ("docs/cite.md", "pkg/mod.py", "STRAY_SCRATCH.txt"):
            p = Path(repo_path) / rel
            contents.append(p.read_bytes() if p.exists() else b"<absent>")
        return (status, tuple(contents))

    def _wrapped(repo_path, *, apply=True, timeout=citation_drift.DEFAULT_TIMEOUT_S):
        if apply:
            return real_run_reanchor(repo_path, apply=apply, timeout=timeout)
        before_snap = _snapshot(repo_path)
        result = real_run_reanchor(repo_path, apply=apply, timeout=timeout)
        after_snap = _snapshot(repo_path)
        recheck_snapshots.append((before_snap, after_snap))
        return result

    monkeypatch.setattr(citation_drift, "run_reanchor", _wrapped)

    backend = _AmbiguousDriftThenFixesItBackend()
    orch, task, repo, events = await _run_one_task_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert recheck_snapshots, (
        "the round must be verified by a read-only (`apply=False`) re-check"
    )
    for before_snap, after_snap in recheck_snapshots:
        assert before_snap == after_snap, (
            "the post-round verification re-check must never itself mutate "
            "the worktree"
        )
