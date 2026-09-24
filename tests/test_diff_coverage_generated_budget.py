"""`diff_coverage.split_generated` — the diff BUDGET excludes allow-listed,
generated, hash-verified patches (`RELEASE_MANIFEST.txt` at minimum) before
`run_gate`/`ci_action.run` ever compare it against the cap.

This is an ALLOW-LIST, not a pattern: only the exact repo-root paths in
`BUDGET_EXEMPT_GENERATED` (seeded from `derived_conflict.DERIVED_ARTEFACTS`)
are dropped. Everything else — including a path merely shaped like one of
them — is fully counted, and the tamper guard, `TRUSTED_COVERAGE_EXCLUSIONS`
and `budget_diff`'s own ledger are untouched by this module.
"""

from __future__ import annotations

from no_human.review.diff_coverage import (
    BUDGET_EXEMPT_GENERATED,
    GeneratedSplit,
    split_generated,
)
from no_human.vcs.derived_conflict import DERIVED_ARTEFACTS


def _chunk(path: str, body: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n-old\n+" + body + "\n"
    )


def test_the_allow_list_is_exactly_the_repo_s_derived_artefacts():
    # Pinned literal, not a re-derivation: if `DERIVED_ARTEFACTS` is ever
    # widened for a reason unrelated to the diff budget, this test goes red
    # instead of the budget hole silently widening along with it.
    assert BUDGET_EXEMPT_GENERATED == frozenset({"RELEASE_MANIFEST.txt"})
    assert BUDGET_EXEMPT_GENERATED == frozenset(DERIVED_ARTEFACTS)


def test_the_manifest_patch_leaves_the_budgeted_diff():
    manifest_chunk = _chunk("RELEASE_MANIFEST.txt", "M" * 4000)
    source_chunk = _chunk("src/x.py", "S" * 100)
    raw = manifest_chunk + source_chunk

    split = split_generated(raw)

    assert isinstance(split, GeneratedSplit)
    assert "src/x.py" in split.budgeted
    assert "RELEASE_MANIFEST.txt" not in split.budgeted
    assert split.excluded_chars == len(manifest_chunk)
    assert split.excluded_paths == ["RELEASE_MANIFEST.txt"]


def test_a_re_pinned_row_counts_once_not_twice():
    old_sha = "a" * 64
    new_sha = "b" * 64
    added_sha = "c" * 64
    manifest_chunk = (
        "diff --git a/RELEASE_MANIFEST.txt b/RELEASE_MANIFEST.txt\n"
        "--- a/RELEASE_MANIFEST.txt\n"
        "+++ b/RELEASE_MANIFEST.txt\n"
        "@@ -1,2 +1,3 @@\n"
        f"-{old_sha}  src/p.py\n"
        f"+{new_sha}  src/p.py\n"
        f"+{added_sha}  src/q.py\n"
    )
    raw = manifest_chunk + _chunk("src/x.py", "S" * 10)

    split = split_generated(raw)

    assert split.excluded_rows == {"RELEASE_MANIFEST.txt": 2}


def test_a_path_shaped_like_the_manifest_is_not_exempt():
    nested = _chunk("docs/RELEASE_MANIFEST.txt", "N" * 500)
    build = _chunk("build/generated.txt", "B" * 500)
    raw = nested + build

    split = split_generated(raw)

    assert split.budgeted == raw
    assert split.excluded_chars == 0
    assert split.excluded_paths == []
    assert "docs/RELEASE_MANIFEST.txt" in split.budgeted
    assert "build/generated.txt" in split.budgeted


def test_a_manifest_only_diff_is_returned_whole():
    raw = _chunk("RELEASE_MANIFEST.txt", "M" * 4000)

    split = split_generated(raw)

    assert split.budgeted is raw
    assert split.excluded_chars == 0
    assert split.excluded_paths == []
    assert split.excluded_rows == {}


def test_an_unparseable_diff_is_counted_whole():
    raw = "not a diff at all, no per-file boundaries here\njust text\n"

    split = split_generated(raw)

    assert split.budgeted is raw
    assert split.excluded_chars == 0
    assert split.excluded_paths == []
    assert split.excluded_rows == {}
