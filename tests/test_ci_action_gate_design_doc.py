"""Guards `docs/design/untrusted-pr-review-gate.md`: the design record for
moving the review gate off `pull_request` to a `workflow_run` split.

This design is documentation-only — `src/no_human/ci_action/run.py` is
unchanged by it (`git diff --stat -- src/no_human/ci_action/run.py` prints
nothing). These tests guard four things so the document cannot silently rot:

1. it exists and is indexed from `docs/README.md`;
2. it actually answers all four required points (trigger, prompt injection,
   tamper guard without a tree, cost bound);
3. every `file:line` citation in it resolves to a real line in this tree;
4. the behavioural invariants it describes (`_is_fork_pr` keeps skipping
   forks, `pull_request_target` stays refused) are true of the *code*, not
   just asserted in prose.

No fixtures, no autouse, no new pytest marker. `test_run_py_behaviour_is_unchanged`
does take pytest's own built-in `monkeypatch` fixture (to set
`GITHUB_EVENT_NAME` for the duration of that one test, restored
automatically at teardown) — that is not a fixture this module defines, and
it is not autouse.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from no_human.ci_action import run as ci_run

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "design" / "untrusted-pr-review-gate.md"
README_PATH = REPO_ROOT / "docs" / "README.md"

_CITATION_RE = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|yml|md)):(\d+)(?:-(\d+))?")

# A stricter form: a backtick-quoted citation immediately followed by a
# verbatim quote of the source text it claims to be citing, e.g.
# `` `path/to/file.py:26-27`: "the exact source text" ``. Unlike
# `_CITATION_RE` (which only checks the file has enough lines — a citation
# with every line number replaced by `:1` still passes that), this checks
# the claimed line range actually *contains* the quoted text.
_QUOTED_CITATION_RE = re.compile(
    r"`([A-Za-z0-9_./-]+\.(?:py|yml|md)):(\d+)(?:-(\d+))?`:\s*\"([^\"]+)\""
)


def _doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def test_design_doc_exists_and_is_indexed():
    assert DOC_PATH.is_file(), f"expected a design doc at {DOC_PATH}"

    readme = README_PATH.read_text(encoding="utf-8")
    assert "design/untrusted-pr-review-gate.md" in readme, (
        "docs/README.md must index the new design doc under Reference and reports"
    )


def test_design_doc_answers_all_four_points():
    text = _doc_text()

    # (c) trigger
    assert re.search(r"^## A\..*Trigger", text, re.MULTILINE), "missing a trigger section"
    assert "workflow_run" in text and "pull_request_target" in text

    # (a) prompt injection — must say explicitly this is NOT fixed
    injection_heading = re.search(r"^## C\..*prompt injection", text, re.MULTILINE | re.IGNORECASE)
    assert injection_heading is not None, "missing a prompt-injection section"
    assert "NOT" in text[injection_heading.start(): injection_heading.start() + 4000]
    assert "UNTRUSTED_PR_REVIEW.md" in text

    # (b) tamper guard without a tree
    tamper_heading = re.search(r"^## D\..*tamper guard", text, re.MULTILINE | re.IGNORECASE)
    assert tamper_heading is not None, "missing a tamper-guard section"

    # (d) cost bound
    cost_heading = re.search(r"^## E\..*[Cc]ost bound", text, re.MULTILINE)
    assert cost_heading is not None, "missing a cost-bound section"
    assert "max_files" in text


def test_every_citation_in_the_design_doc_resolves():
    text = _doc_text()
    citations = list(_CITATION_RE.finditer(text))
    assert len(citations) >= 15, "expected many file:line citations in a design doc this detailed"

    checked = 0
    for match in citations:
        rel_path, start_line, end_line = match.group(1), match.group(2), match.group(3)
        path = REPO_ROOT / rel_path
        assert path.is_file(), f"citation {match.group(0)!r} points at a path that does not exist: {rel_path}"

        line_count = sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
        last_line = int(end_line) if end_line else int(start_line)
        assert line_count >= last_line, (
            f"citation {match.group(0)!r} claims line {last_line} but {rel_path} only has {line_count} lines"
        )
        checked += 1

    assert checked == len(citations)


def test_every_quoted_citation_in_the_design_doc_matches_the_source():
    """A citation with a trailing verbatim quote must be checked against the
    actual text at that line range, not just against the file's line count —
    that is exactly the gap that let a stale `run.py:25-26` citation survive
    a rewrite that moved the quoted text to line 27."""
    text = _doc_text()
    matches = list(_QUOTED_CITATION_RE.finditer(text))
    assert matches, "expected at least one citation with a verbatim source quote"

    for match in matches:
        rel_path, start_line, end_line, quoted = match.groups()
        path = REPO_ROOT / rel_path
        assert path.is_file(), f"quoted citation points at a missing path: {rel_path}"
        lines = path.read_text(encoding="utf-8").splitlines()
        start = int(start_line)
        end = int(end_line) if end_line else start
        window = " ".join(line.strip() for line in lines[start - 1 : end])
        normalized_window = re.sub(r"\s+", " ", window)
        normalized_quote = re.sub(r"\s+", " ", quoted.strip())
        assert normalized_quote in normalized_window, (
            f"citation claims {rel_path}:{start_line}"
            f"{'-' + end_line if end_line else ''} contains {quoted!r}, "
            f"but that text is not on those lines (found: {normalized_window!r})"
        )


def test_design_doc_states_the_fork_and_pull_request_target_invariants():
    text = _doc_text()
    assert "_is_fork_pr" in text
    assert "pull_request_target" in text

    stays_section = text[text.index("## B. What stays"):]
    assert "keeps skipping" in stays_section or "skip" in stays_section.lower()
    assert "refused" in stays_section


def test_run_py_behaviour_is_unchanged(monkeypatch):
    # _is_fork_pr still treats a mismatched (or absent) head repo as a fork.
    fork_event = {
        "repository": {"full_name": "no-human-ai/no_human"},
        "pull_request": {"head": {"repo": {"full_name": "someone-else/no_human"}}},
    }
    assert ci_run._is_fork_pr(fork_event) is True

    deleted_fork_event = {
        "repository": {"full_name": "no-human-ai/no_human"},
        "pull_request": {"head": {"repo": None}},
    }
    assert ci_run._is_fork_pr(deleted_fork_event) is True

    same_repo_event = {
        "repository": {"full_name": "no-human-ai/no_human"},
        "pull_request": {"head": {"repo": {"full_name": "no-human-ai/no_human"}}},
    }
    assert ci_run._is_fork_pr(same_repo_event) is False

    # pull_request_target is refused outright, regardless of anything else.
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    assert ci_run.main() == ci_run.EXIT_DID_NOT_RUN

    # workflow_run is not an accepted event today — the split is designed,
    # not landed, so run.py must still refuse it exactly like any other
    # unsupported event name.
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    assert ci_run.main() == ci_run.EXIT_DID_NOT_RUN


def test_design_doc_names_the_workflow_run_boundary_and_why_pull_request_is_not_one():
    text = _doc_text()
    assert "Named GitHub behaviour #1" in text
    assert "Named GitHub behaviour #2" in text

    behaviour_1 = text[text.index("Named GitHub behaviour #1"):]
    behaviour_1 = behaviour_1[: behaviour_1.index("Named GitHub behaviour #2")]
    assert "head" in behaviour_1.lower() and "secrets" in behaviour_1.lower()

    behaviour_2 = text[text.index("Named GitHub behaviour #2"):]
    assert "default branch" in behaviour_2.lower()
    assert "main" in behaviour_2


def test_design_doc_states_the_environment_secret_requirement():
    """The `workflow_run` split alone does not protect the credential — only
    scoping it to an environment with a `main`-only deployment-branch policy
    does. The doc must say so and must not leave `action.yml:14`'s
    "repository secret" language standing uncorrected."""
    text = _doc_text()
    assert "environment secret" in text or "environment-scoped" in text
    assert "deployment-branch policy" in text or "deployment branch policy" in text
    assert "action.yml:14" in text
    assert "repository secret" in text  # named as the thing being corrected


def test_design_doc_names_the_workflow_run_event_check_and_artifact_handling():
    """`workflow_run` fires on any completion of a workflow with the watched
    name, not just on a pull request — the doc must require re-checking
    `workflow_run.event`/`conclusion`, and must address (or explicitly scope
    out) the untrusted artifact download."""
    text = _doc_text()
    assert "workflow_run.event" in text
    assert "workflow_run.conclusion" in text
    assert "artifact" in text.lower()
    assert "path traversal" in text.lower() or "zip" in text.lower()


def test_design_doc_states_the_tamper_guard_did_not_run_sentence():
    text = _doc_text()
    sentence = (
        "Tamper guard: **did not run** — this pull request's diff was fetched\n"
        "> through the GitHub API with no checked-out test tree, so there was no\n"
        "> before/after comparison. This verdict covers the diff only."
    )
    # The doc quotes the sentence as blockquote lines; verify each fragment
    # appears, in order, so the implementer can copy it verbatim.
    assert "Tamper guard: **did not run**" in text
    idx = text.index("Tamper guard: **did not run**")
    quoted_block = text[idx: idx + 400]
    assert "GitHub API" in quoted_block
    assert "no checked-out test tree" in quoted_block
    assert "before/after comparison" in quoted_block
    assert "This verdict covers the diff only." in quoted_block

    # Exactly one occurrence of the sentence-opening phrase in the whole doc.
    assert text.count("Tamper guard: **did not run**") == 1
