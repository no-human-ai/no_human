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


def test_apply_diagnoses_pre_existing_divergence(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    doc_path = tmp_path / "docs" / "fake.md"
    table_path = tmp_path / "tests" / "test_readme_claims.py"

    doc_path.write_text("See `widget.py:bench_run:8217` for details.\n", encoding="utf-8")
    table_path.write_text(
        'CITATION_TABLE = (\n'
        '    ("fake.md", "widget.py:bench_run:8213", "widget.py", "token"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "REPO", tmp_path)

    fake_mod = types.SimpleNamespace(
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
    )

    drift = ra.Drift(doc="fake.md", old_raw="widget.py:bench_run:8213", new_raw="widget.py:bench_run:8220", resolve_path="widget.py")

    texts, unresolved = ra._apply_all(fake_mod, [drift])

    assert texts is None
    assert len(unresolved) == 1
    assert "Citation is already divergent" in unresolved[0].reason
    assert "documentation: `widget.py:bench_run:8217`" in unresolved[0].reason
    assert "table:         `widget.py:bench_run:8213`" in unresolved[0].reason
    assert "source:        `widget.py:bench_run:8220`" in unresolved[0].reason
    assert "Use `--reconcile`" in unresolved[0].reason

    assert "8220" not in doc_path.read_text(encoding="utf-8")
    assert "8220" not in table_path.read_text(encoding="utf-8")

def test_reconcile_converges_diverged_symbol_citation(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    doc_path = tmp_path / "docs" / "fake.md"
    table_path = tmp_path / "tests" / "test_readme_claims.py"

    doc_path.write_text("See `widget.py:bench_run:8217` for details.\n", encoding="utf-8")
    table_path.write_text(
        'CITATION_TABLE = (\n'
        '    ("fake.md", "widget.py:bench_run:8213", "widget.py", "token"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "REPO", tmp_path)

    fake_mod = types.SimpleNamespace(
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
    )

    recon = ra.Reconciliation(
        doc="fake.md", raw="widget.py:bench_run:8213",
        stable_prefix="widget.py:bench_run", new_raw="widget.py:bench_run:8220",
        resolve_path="widget.py"
    )

    texts, unresolved, total_changed = ra._reconcile_all(fake_mod, [recon])

    assert texts is not None
    assert not unresolved
    assert total_changed > 0
    assert "8220" in texts[doc_path]
    assert "8217" not in texts[doc_path]
    assert "8220" in texts[table_path]
    assert "8213" not in texts[table_path]

def test_reconcile_is_idempotent(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    doc_path = tmp_path / "docs" / "fake.md"
    table_path = tmp_path / "tests" / "test_readme_claims.py"

    doc_path.write_text("See `widget.py:bench_run:8220` for details.\n", encoding="utf-8")
    table_path.write_text(
        'CITATION_TABLE = (\n'
        '    ("fake.md", "widget.py:bench_run:8220", "widget.py", "token"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "REPO", tmp_path)

    fake_mod = types.SimpleNamespace(
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
    )

    recon = ra.Reconciliation(
        doc="fake.md", raw="widget.py:bench_run:8220",
        stable_prefix="widget.py:bench_run", new_raw="widget.py:bench_run:8220",
        resolve_path="widget.py"
    )

    texts, unresolved, total_changed = ra._reconcile_all(fake_mod, [recon])

    assert texts is not None
    assert not unresolved
    assert total_changed == 0

def test_reconcile_refuses_legacy_line_citations():
    fake_mod = types.SimpleNamespace(
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
    )
    rows = [("fake.md", "widget.py:5", "widget.py", "token")]
    recons, unfixable = ra.reconcile_plan(fake_mod, rows)
    assert not recons
    assert len(unfixable) == 1
    assert "no stable symbol identity" in unfixable[0].reason

def test_reconcile_refuses_duplicate_prefix(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    doc_path = tmp_path / "docs" / "fake.md"
    table_path = tmp_path / "tests" / "test_readme_claims.py"

    doc_path.write_text("See `widget.py:bench_run:10` and `widget.py:bench_run:20`\n", encoding="utf-8")
    table_path.write_text(
        'CITATION_TABLE = (\n'
        '    ("fake.md", "widget.py:bench_run:15", "widget.py", "token"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "REPO", tmp_path)

    fake_mod = types.SimpleNamespace(
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
    )

    recon = ra.Reconciliation(
        doc="fake.md", raw="widget.py:bench_run:15",
        stable_prefix="widget.py:bench_run", new_raw="widget.py:bench_run:8220",
        resolve_path="widget.py"
    )

    texts, unresolved, total_changed = ra._reconcile_all(fake_mod, [recon])

    assert texts is None
    assert len(unresolved) == 1
    assert "does not occur exactly once" in unresolved[0].reason

def test_reconcile_converges_multi_citation_per_symbol(tmp_path, monkeypatch):
    """Pin the exact shape from eval.md where one symbol is cited multiple
    times in the same document (one qualified, two shorthand).
    Exact matching the citation string avoids the 'does not occur exactly once'
    error that a naive symbol-only substring search would trip on."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    doc_path = tmp_path / "docs" / "fake.md"
    table_path = tmp_path / "tests" / "test_readme_claims.py"

    doc_text = (
        "Here are three citations for the same symbol:\n"
        "`src/no_human/cli/commands.py:bench_run:8255`\n"
        "`:bench_run:8406`\n"
        "`:bench_run:8284`\n"
    )
    doc_path.write_text(doc_text, encoding="utf-8")
    table_text = (
        'CITATION_TABLE = (\n'
        '    ("fake.md", ":bench_run:8406", "src/no_human/cli/commands.py", "token"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n'
    )
    table_path.write_text(table_text, encoding="utf-8")
    monkeypatch.setattr(ra, "REPO", tmp_path)

    fake_mod = types.SimpleNamespace(
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
    )

    recon = ra.Reconciliation(
        doc="fake.md", raw=":bench_run:8406",
        stable_prefix=":bench_run", new_raw=":bench_run:9000",
        resolve_path="src/no_human/cli/commands.py"
    )

    texts, unresolved, total_changed = ra._reconcile_all(fake_mod, [recon])

    assert texts is not None, f"Expected successful rewrite, got unresolved: {unresolved}"
    assert len(unresolved) == 0
    assert total_changed == 1

    # Verify the document replaced only the target citation
    new_doc = texts[doc_path]
    assert "`src/no_human/cli/commands.py:bench_run:8255`" in new_doc
    assert "`:bench_run:9000`" in new_doc
    assert "`:bench_run:8284`" in new_doc
    assert "`:bench_run:8406`" not in new_doc

    # Verify the table row was rewritten
    new_table = texts[table_path]
    assert '":bench_run:9000"' in new_table
    assert '":bench_run:8406"' not in new_table

def test_reconcile_preserves_single_symbol_in_new_raw(tmp_path, monkeypatch):
    """Pin the exact shape of a fully qualified symbol-backed citation to ensure
    reconciliation does not duplicate the symbol in the new citation string."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    doc_path = tmp_path / "docs" / "fake.md"
    table_path = tmp_path / "tests" / "test_readme_claims.py"

    doc_text = "See `cli/commands.py:merge_stack_run:3206`\n"
    doc_path.write_text(doc_text, encoding="utf-8")
    table_text = (
        'CITATION_TABLE = (\n'
        '    ("fake.md", "cli/commands.py:merge_stack_run:3206", "cli/commands.py", "token"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n'
    )
    table_path.write_text(table_text, encoding="utf-8")
    monkeypatch.setattr(ra, "REPO", tmp_path)

    fake_mod = types.SimpleNamespace(
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
    )

    recon = ra.Reconciliation(
        doc="fake.md", raw="cli/commands.py:merge_stack_run:3206",
        stable_prefix="cli/commands.py:merge_stack_run", new_raw="cli/commands.py:merge_stack_run:9000",
        resolve_path="cli/commands.py"
    )

    texts, unresolved, total_changed = ra._reconcile_all(fake_mod, [recon])

    assert texts is not None, f"Expected successful rewrite, got unresolved: {unresolved}"
    assert len(unresolved) == 0
    assert total_changed == 1

    new_doc = texts[doc_path]
    # Assert exact expected string
    assert "`cli/commands.py:merge_stack_run:9000`" in new_doc
    # Assert explicitly that the duplicated symbol does NOT occur
    assert "`cli/commands.py:merge_stack_run:merge_stack_run:" not in new_doc

def test_main_reconcile_reports_only_modified_files(tmp_path, monkeypatch, capsys):
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    doc_path = tmp_path / "docs" / "fake.md"
    table_path = tmp_path / "tests" / "test_readme_claims.py"
    resolve_path = tmp_path / "widget.py"

    doc_path.write_text("See `widget.py:bench_run:8217` for details.\n", encoding="utf-8")
    table_path.write_text(
        'CITATION_TABLE = (\n'
        '    ("fake.md", "widget.py:bench_run:8213", "widget.py", "token"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n',
        encoding="utf-8",
    )
    resolve_path.write_text("def bench_run():\n    pass # token\n", encoding="utf-8")

    monkeypatch.setattr(ra, "REPO", tmp_path)

    fake_mod = types.SimpleNamespace(
        CITATION_TABLE=(("fake.md", "widget.py:bench_run:8213", "widget.py", "token"),),
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
        _cited_line=lambda tail: 8213,
        _resolve_source=lambda path: [resolve_path],
        _token_line_in_symbol=lambda text, sym, tok: 2,
    )
    monkeypatch.setattr(ra, "_load_checker", lambda: fake_mod)

    ret = ra.main(["--reconcile"])
    assert ret == 0

    stdout = capsys.readouterr().out
    assert "Reconciled 1 citation(s)." in stdout
    assert "Modified:" in stdout
    # Both doc and table changed
    assert "tests/test_readme_claims.py" in stdout
    assert "docs/fake.md" in stdout

def test_main_reconcile_reports_no_modified_files_when_zero_changes(tmp_path, monkeypatch, capsys):
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    doc_path = tmp_path / "docs" / "fake.md"
    table_path = tmp_path / "tests" / "test_readme_claims.py"
    resolve_path = tmp_path / "widget.py"

    doc_path.write_text("See `widget.py:bench_run:2` for details.\n", encoding="utf-8")
    table_path.write_text(
        'CITATION_TABLE = (\n'
        '    ("fake.md", "widget.py:bench_run:2", "widget.py", "token"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n',
        encoding="utf-8",
    )
    resolve_path.write_text("def bench_run():\n    pass # token\n", encoding="utf-8")

    monkeypatch.setattr(ra, "REPO", tmp_path)

    fake_mod = types.SimpleNamespace(
        CITATION_TABLE=(
            ("fake.md", "widget.py:bench_run:2", "widget.py", "token"),
            ("fake.md", "legacy:5", "legacy", "token"),
        ),
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
        _cited_line=lambda tail: 2,
        _resolve_source=lambda path: [resolve_path],
        _token_line_in_symbol=lambda text, sym, tok: 2,
    )
    monkeypatch.setattr(ra, "_load_checker", lambda: fake_mod)

    ret = ra.main(["--reconcile"])
    assert ret == 1

    stdout = capsys.readouterr().out
    assert "Reconciled 0 citation(s)." in stdout
    assert "Modified:" not in stdout
    assert "Unfixable citations remain: 1" in stdout
    assert "VERDICT=FAIL" in stdout


def test_apply_writes_survive_a_python39_write_text_signature(tmp_path, monkeypatch):
    """Same defect class as `check_release_manifest.py --write`: the `--apply`
    write path must not depend on `Path.write_text(..., newline=...)`, since
    that keyword only exists on Python >=3.10 and this script is reachable
    through an older system `python3` the same way `check_release_manifest.py`
    is. This stub mimics the real Python 3.9 `Path.write_text` signature —
    any extra keyword is a TypeError."""
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

    def fake_locate(resolve_path, spec, token):
        assert spec == "5"
        return "drifted", 8, ""

    fake_mod = types.SimpleNamespace(
        CITATION_TABLE=(("fake.md", "widget.py:5", "widget.py", "line 5"),),
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
        _locate_line_citation=fake_locate,
    )
    monkeypatch.setattr(ra, "_load_checker", lambda: fake_mod)

    def stub_write_text(self, data, encoding=None, errors=None):
        return Path.write_bytes(self, data.encode(encoding or "utf-8"))

    monkeypatch.setattr(Path, "write_text", stub_write_text, raising=True)

    ret = ra.main(["--apply"])
    assert ret == 0

    assert b"\r" not in doc_path.read_bytes()
    assert b"\r" not in table_path.read_bytes()
    assert "widget.py:8" in doc_path.read_text(encoding="utf-8")
