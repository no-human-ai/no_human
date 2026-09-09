"""`scripts/reanchor_citations.py` — the mechanical re-anchor helper for the
drift-tolerant line citations `tests/test_readme_claims.py` checks.

Written as fail-open probes, per `tests/test_verify_artefact.py`'s stated
idiom: each test constructs an input that is wrong in one specific way (an
ambiguous match, content that is genuinely gone, a stale spec) and asserts
the helper reports it rather than guessing or silently writing something
wrong. No real doc or the real `tests/test_readme_claims.py` is ever mutated
by these tests — synthetic fixtures in `tmp_path`, or pure in-memory text,
throughout. Only `test_check_mode_is_clean_on_this_tree` touches the real
tree, and only to read mtimes and run `--check`, which must never write.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "reanchor_citations.py"


def _load():
    spec = importlib.util.spec_from_file_location("_nh_reanchor_citations", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses needs the module registered
    spec.loader.exec_module(mod)
    return mod


ra = _load()


def test_apply_rewrites_a_drifted_citation_in_doc_and_table(tmp_path, monkeypatch):
    """A drifted row must rewrite BOTH the doc's backtick citation and its
    CITATION_TABLE row, to the same new spec, in one batch."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    doc_path = tmp_path / "docs" / "fake.md"
    table_path = tmp_path / "tests" / "test_readme_claims.py"
    doc_path.write_text("See `widget.py:5` for details.\n", encoding="utf-8")
    table_path.write_text(
        'CITATION_TABLE = (\n'
        '    ("fake.md", "widget.py:5", "widget.py", "line 5"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "REPO", tmp_path)

    fake_mod = types.SimpleNamespace(_CITATION_DOC_PATHS={"fake.md": doc_path})
    drift = ra.Drift(doc="fake.md", old_raw="widget.py:5", new_raw="widget.py:8",
                      resolve_path="widget.py")

    texts, unresolved = ra._apply_all(fake_mod, [drift])

    assert not unresolved
    assert texts[doc_path] == "See `widget.py:8` for details.\n"
    assert '"widget.py:8"' in texts[table_path]
    assert '"widget.py:5"' not in texts[table_path]


def test_apply_is_idempotent():
    """A second `plan()` over the already-corrected rows must find nothing
    left to do — the whole point of writing the new line number back into
    both surfaces is that the next run reads it as `"exact"`, not
    `"drifted"` again.
    """
    def fake_locate(resolve_path, spec, token):
        if spec == "5":
            return "drifted", 8, ""
        if spec == "8":
            return "exact", 8, ""
        raise AssertionError(f"unexpected spec {spec!r}")

    fake_mod = types.SimpleNamespace(
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
        _locate_line_citation=fake_locate,
    )
    first_rows = [("fake.md", "widget.py:5", "widget.py", "line 5")]
    drifts, unfixable = ra.plan(fake_mod, first_rows)
    assert not unfixable
    assert [d.new_raw for d in drifts] == ["widget.py:8"]

    second_rows = [("fake.md", d.new_raw, "widget.py", "line 5") for d in drifts]
    drifts2, unfixable2 = ra.plan(fake_mod, second_rows)
    assert drifts2 == []
    assert unfixable2 == []


def test_ambiguous_or_missing_citation_is_reported_not_guessed():
    """0 occurrences, or 2+ occurrences, of the raw citation text must both
    refuse to rewrite rather than guessing which one is meant — and a row
    whose content is genuinely gone (`"missing"`) must come back as
    Unfixable, never silently dropped or silently anchored somewhere wrong.
    """
    assert ra.rewrite("no citation here", "widget.py:5", "widget.py:8") is None
    dup_text = "see `widget.py:5` and also `widget.py:5` again"
    assert ra.rewrite(dup_text, "widget.py:5", "widget.py:8") is None
    ok_text = "see `widget.py:5` here"
    assert ra.rewrite(ok_text, "widget.py:5", "widget.py:8") == "see `widget.py:8` here"

    dup_table = (
        'CITATION_TABLE = (\n'
        '    ("fake.md", "widget.py:5", "widget.py", "a"),\n'
        '    ("fake.md", "widget.py:5", "widget.py", "b"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n'
    )
    assert ra.rewrite_table_row(dup_table, "widget.py:5", "widget.py:8") is None

    def fake_locate(resolve_path, spec, token):
        return "missing", None, "not found anywhere in widget.py"

    fake_mod = types.SimpleNamespace(
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
        _locate_line_citation=fake_locate,
    )
    drifts, unfixable = ra.plan(
        fake_mod, [("fake.md", "widget.py:5", "widget.py", "line 5")]
    )
    assert drifts == []
    assert len(unfixable) == 1
    assert "not found anywhere" in unfixable[0].reason


def test_rewrite_table_row_only_touches_the_citation_table_slice():
    """A quoted string that happens to match the raw citation OUTSIDE the
    `CITATION_TABLE = ( ... )` literal — a comment above it, an unrelated
    tuple below it — must not count toward the one-occurrence rule and must
    not be rewritten. The rewrite is scoped to the table literal, never a
    whole-file replace.
    """
    text = (
        '# see "widget.py:5" in a comment above the table\n'
        'CITATION_TABLE = (\n'
        '    ("fake.md", "widget.py:5", "widget.py", "line 5"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n'
        '\nOTHER_TABLE = (\n    "widget.py:5",\n)\n'
    )
    new_text = ra.rewrite_table_row(text, "widget.py:5", "widget.py:8")
    assert new_text is not None
    assert '"widget.py:5"' in new_text.splitlines()[0], (
        "the comment above the table must be untouched"
    )
    assert 'CITATION_TABLE = (\n    ("fake.md", "widget.py:8"' in new_text
    assert '"widget.py:5"' in new_text.split("OTHER_TABLE")[1], (
        "text after the table must be untouched"
    )


def test_check_mode_is_clean_on_this_tree():
    """`--check` against the real repository must be clean (exit 0, the
    same VERDICT `test_every_line_citation_currently_resolves_exactly`
    implies) and must never write — the fail-open probe here is a `--check`
    run that would otherwise silently touch a file's mtime.
    """
    docs = [REPO / "docs" / d for d in ("security.md", "eval.md", "KNOWN_ISSUES.md")]
    table_path = REPO / "tests" / "test_readme_claims.py"
    watched = docs + [table_path]
    before = {p: p.stat().st_mtime_ns for p in watched}

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True, text=True, check=False,
    )

    after = {p: p.stat().st_mtime_ns for p in watched}
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VERDICT=OK" in result.stdout
    assert before == after, "--check must never modify a file"


def test_cited_source_specs_is_the_resolve_path_column():
    """`cited_source_specs` is the single source of truth
    `no_human.testing.citations.cited_source_files` pairs against (see that
    function's own docstring) — it must be exactly the resolve-path (index
    2) column of every row, deduplicated, nothing more."""
    rows = (
        ("a.md", "pkg/x.py:1", "pkg/x.py", "tok"),
        ("b.md", "y.py:2", "y.py", "tok2"),
        ("c.md", "pkg/x.py:9", "pkg/x.py", "tok3"),  # same resolve_path again
    )
    assert ra.cited_source_specs(rows) == {"pkg/x.py", "y.py"}


def test_cited_files_mode_lists_them_and_writes_nothing():
    """`--cited-files` is read-only, like `--check` — same mtime-probe idiom
    as `test_check_mode_is_clean_on_this_tree` — and lists every resolve-path
    spec from the real CITATION_TABLE, one per line, sorted."""
    docs = [REPO / "docs" / d for d in ("security.md", "eval.md", "KNOWN_ISSUES.md")]
    table_path = REPO / "tests" / "test_readme_claims.py"
    watched = docs + [table_path]
    before = {p: p.stat().st_mtime_ns for p in watched}

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--cited-files"],
        capture_output=True, text=True, check=False,
    )

    after = {p: p.stat().st_mtime_ns for p in watched}
    assert result.returncode == 0, result.stdout + result.stderr
    assert before == after, "--cited-files must never modify a file"
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines == sorted(lines)
    assert lines, "the real CITATION_TABLE cites at least one source file"


def test_the_harness_reader_agrees_with_the_script_on_this_tree():
    """`no_human.testing.citations.cited_source_files` (an `ast`-only read,
    never importing this file or `tests/test_readme_claims.py`) must resolve
    to exactly the same repo-relative path set as the script's own
    `--cited-files`, re-resolved through the harness's own `_resolve_one` —
    the two readers are pinned against each other as the single source of
    truth for "which source files carry a citation" (see
    `cited_source_specs`'s own docstring).

    One spec, `ci_gate/enrich.py`, is excluded from this parity check: this
    checkout's own `tests/test_readme_claims.py` (`_ABSENT_OK`) already
    exempts it from resolution — the file is "drop-classified but real" in a
    private fork and genuinely absent here. Both readers correctly skip an
    unresolvable spec rather than raising (fail-open, per each one's own
    docstring), so it is expected to be missing from BOTH sets, not a
    disagreement between them.
    """
    from no_human.testing import citations

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--cited-files"],
        capture_output=True, text=True, check=True,
    )
    specs = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    specs -= {"ci_gate/enrich.py"}
    resolved_from_script = set()
    for spec in specs:
        hit = citations._resolve_one(REPO, spec)
        assert hit is not None, f"harness reader cannot resolve {spec!r}"
        resolved_from_script.add(hit)

    assert resolved_from_script == citations.cited_source_files(REPO)
