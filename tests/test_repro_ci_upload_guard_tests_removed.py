"""Repro for the staff-review follow-up on 101913a7: the source-text guard
tests inside tests/test_ci_upload_assertions_not_line_ending_dependent.py
were themselves the class of defect that broke public main — a substring
ban (`BANNED_SNIPPETS`, including the word "flat") scanned across
desktop/packagedFiles.test.mjs and tests/test_release_updater_feed_shipped.py,
so any future unrelated edit to either file that happens to add "flat"
(`.flat()`, `flatMap`, a variable named `flatten`, ...) would turn main red
again for a reason unrelated to the release contract. The fix deletes those
two guard tests and the BANNED_SNIPPETS tuple, keeping only
`test_retained_path_list_contract_survives_a_crlf_checkout` — the test that
parses ci.yml as YAML (CRLF-safe) and observes the actual artefact list.

This test reads that sibling file's source directly (never copied over by
the repro-gate's before/after test-file swap, since only THIS file is
declared in .no_human/repro_tests.json) so it fails at the base commit
(101913a7, which still has three tests including the two guard tests and
the BANNED_SNIPPETS tuple) and passes once the guard tests are gone.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "tests" / "test_ci_upload_assertions_not_line_ending_dependent.py"


def test_ci_upload_guard_tests_removed_leaving_only_the_crlf_contract_test():
    source = TARGET.read_text(encoding="utf-8")
    assert "BANNED_SNIPPETS" not in source, (
        "the substring-ban tuple over ci.yml comment prose must be gone — it "
        "is the same source-text-guard-over-a-test-file defect class that "
        "broke public main on a CRLF checkout"
    )

    tree = ast.parse(source)
    test_names = sorted(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )
    assert test_names == ["test_retained_path_list_contract_survives_a_crlf_checkout"], (
        f"expected only the CRLF artefact-list contract test to remain, found {test_names}"
    )
