"""`scripts/reanchor_citations.py` — the mechanical re-anchor helper for the
`path:symbol:line` citations `tests/test_readme_claims.py` checks.

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
    def fake_cited(spec):
        return int(spec.split(":")[-1])
    def fake_resolve(path):
        class FakePath:
            def read_text(self, encoding): return ""
        return [FakePath()]
    def fake_token_line(text, symbol, token, path=None):
        return 8

    fake_mod = types.SimpleNamespace(
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
        _cited_line=fake_cited,
        _resolve_source=fake_resolve,
        _token_line_in_symbol=fake_token_line,
        _new_symbol_spec=lambda spec, n: f"{spec.rsplit(':', 1)[0]}:{n}"
    )
    first_rows = [("fake.md", "widget.py:foo:5", "widget.py", "drifted")]
    drifts, unfixable = ra.plan(fake_mod, first_rows)
    assert not unfixable
    assert [d.new_raw for d in drifts] == ["widget.py:foo:8"]

    second_rows = [("fake.md", d.new_raw, "widget.py", "not drifted") for d in drifts]
    drifts2, unfixable2 = ra.plan(fake_mod, second_rows)
    assert drifts2 == []
    assert unfixable2 == []


def test_ambiguous_or_missing_citation_is_reported_not_guessed(tmp_path, monkeypatch):
    """0 occurrences, or 2+ occurrences, of a `symbol:line` citation's raw
    text must refuse to rewrite rather than guessing which one is meant; a
    row whose symbol or token cannot be found is never anchored anywhere; and
    a line-only row comes back as Unfixable, never silently dropped.
    """
    old, new = "widget.py:make:5", "widget.py:make:8"
    assert ra.rewrite("no citation here", old, new) is None
    dup_text = f"see `{old}` and also `{old}` again"
    assert ra.rewrite(dup_text, old, new) is None
    assert ra.rewrite(f"see `{old}` here", old, new) == f"see `{new}` here"

    dup_table = (
        'CITATION_TABLE = (\n'
        f'    ("fake.md", "{old}", "widget.py", "a"),\n'
        f'    ("fake.md", "{old}", "widget.py", "b"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n'
    )
    assert ra.rewrite_table_row(dup_table, old, new) is None

    # Through `_apply_all`: a doc citing the same row twice is refused, and
    # nothing is handed back to write.
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    doc_path = tmp_path / "docs" / "fake.md"
    doc_path.write_text(dup_text + "\n", encoding="utf-8")
    (tmp_path / "tests" / "test_readme_claims.py").write_text(
        'CITATION_TABLE = (\n'
        f'    ("fake.md", "{old}", "widget.py", "a"),\n'
        ')\n\nassert len(CITATION_TABLE) >= 20,\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "REPO", tmp_path)
    apply_mod = types.SimpleNamespace(
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
    )
    texts, unresolved = ra._apply_all(apply_mod, [ra.Drift(
        doc="fake.md", old_raw=old, new_raw=new, resolve_path="widget.py")])
    assert texts is None
    assert "does not occur exactly once" in unresolved[0].reason

    # A symbol row whose symbol/token no longer resolves: no drift is invented
    # (the checker reports it); a line-only row: unfixable, by form.
    plan_mod = types.SimpleNamespace(
        _LEGACY_LINE_SPEC_RE=re.compile(r"^\d+(?:-\d+)?$"),
        _cited_line=lambda tail: int(tail.rsplit(":", 1)[1]),
        _resolve_source=lambda path: [doc_path],
        _token_line_in_symbol=lambda text, symbol, token, path=None: None,
    )
    drifts, unfixable = ra.plan(plan_mod, [
        ("fake.md", old, "widget.py", "gone"),
        ("fake.md", "widget.py:5", "widget.py", "line 5"),
    ])
    assert drifts == []
    assert len(unfixable) == 1
    assert unfixable[0].raw == "widget.py:5"
    assert "line-only" in unfixable[0].reason


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
    same VERDICT `test_every_citation_currently_resolves_exactly`
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
        _token_line_in_symbol=lambda text, sym, tok, path=None: 2,
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
        _token_line_in_symbol=lambda text, sym, tok, path=None: 2,
    )
    monkeypatch.setattr(ra, "_load_checker", lambda: fake_mod)

    ret = ra.main(["--reconcile"])
    assert ret == 1

    stdout = capsys.readouterr().out
    assert "Reconciled 0 citation(s)." in stdout
    assert "Modified:" not in stdout
    assert "Unfixable citations remain: 1" in stdout
    assert "VERDICT=FAIL" in stdout


# --------------------------------------------------------------------------- #
# `--apply` actually reaching the filesystem.                                 #
#                                                                             #
# Everything above drives `_apply_all`, which returns a {path: new_text} dict  #
# and touches nothing. The one line that turns that dict into bytes on disk    #
# lives in `main()`, and nothing observed it: replacing it with `pass` left    #
# every test in this file green. So the wiring between "computed the new       #
# text" and "the file now holds it" was uncovered, which is the seam that      #
# matters — a helper that computes a perfect rewrite and never writes it is    #
# indistinguishable from a working one at the level of a returned dict.        #
# --------------------------------------------------------------------------- #

def _drift_fixture(tmp_path: Path):
    """A repo shaped like the real one, with exactly one drifted citation."""
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
    return doc_path, table_path


def test_apply_writes_the_rewritten_text_to_disk(tmp_path, monkeypatch):
    """The artifact, not the return value: after `--apply`, read the files."""
    doc_path, table_path = _drift_fixture(tmp_path)
    monkeypatch.setattr(ra, "REPO", tmp_path)

    fake_mod = types.SimpleNamespace(
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        CITATION_TABLE=(("fake.md", "widget.py:5", "widget.py", "line 5"),),
    )
    monkeypatch.setattr(ra, "_load_checker", lambda: fake_mod)
    monkeypatch.setattr(ra, "plan", lambda mod, table: (
        [ra.Drift(doc="fake.md", old_raw="widget.py:5", new_raw="widget.py:8",
                  resolve_path="widget.py")], []))

    assert ra.main(["--apply"]) == 0

    assert doc_path.read_text(encoding="utf-8") == "See `widget.py:8` for details.\n"
    assert '"widget.py:8"' in table_path.read_text(encoding="utf-8")


def test_apply_writes_lf_bytes_and_no_bom(tmp_path, monkeypatch):
    """Written as bytes so the platform's text layer never decides.

    On Windows a text-layer write turns every line of a rewritten doc into
    CRLF, and a four-citation change lands as a whole-file diff.
    """
    doc_path, _table_path = _drift_fixture(tmp_path)
    monkeypatch.setattr(ra, "REPO", tmp_path)

    fake_mod = types.SimpleNamespace(
        _CITATION_DOC_PATHS={"fake.md": doc_path},
        CITATION_TABLE=(("fake.md", "widget.py:5", "widget.py", "line 5"),),
    )
    monkeypatch.setattr(ra, "_load_checker", lambda: fake_mod)
    monkeypatch.setattr(ra, "plan", lambda mod, table: (
        [ra.Drift(doc="fake.md", old_raw="widget.py:5", new_raw="widget.py:8",
                  resolve_path="widget.py")], []))

    # Assert the run succeeded and the content actually changed before asking
    # about its bytes: without these two, every assertion below is satisfied by
    # the untouched fixture, and the test passes when the write is deleted.
    assert ra.main(["--apply"]) == 0

    written = doc_path.read_bytes()
    assert b"widget.py:8" in written
    assert b"\r" not in written
    assert not written.startswith(b"\xef\xbb\xbf")
    assert written.endswith(b"\n")


# --------------------------------------------------------------------------- #
# Issue #506: every citation carries a symbol. Driven through `main()` with   #
# the REAL checker module (only its table, doc paths and source resolution    #
# are pointed at a tmp fixture), so these observe the verdict a user gets.    #
# --------------------------------------------------------------------------- #

def _real_checker_on(tmp_path, monkeypatch, *, doc_text, rows, source_name,
                     source_text):
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    doc_path = tmp_path / "docs" / "fake.md"
    table_path = tmp_path / "tests" / "test_readme_claims.py"
    source = tmp_path / source_name
    doc_path.write_text(doc_text, encoding="utf-8")
    table_path.write_text(
        "CITATION_TABLE = (\n"
        + "".join(f'    ("{d}", "{raw}", "{rp}", "token"),\n'
                  for d, raw, rp, _ in rows)
        + ")\n\nassert len(CITATION_TABLE) >= 20,\n",
        encoding="utf-8",
    )
    source.write_text(source_text, encoding="utf-8")
    mod = ra._load_checker()
    monkeypatch.setattr(mod, "CITATION_TABLE", tuple(rows))
    monkeypatch.setattr(mod, "_CITATION_DOC_PATHS", {"fake.md": doc_path})
    monkeypatch.setattr(mod, "_resolve_source", lambda path: [source])
    monkeypatch.setattr(ra, "_load_checker", lambda: mod)
    monkeypatch.setattr(ra, "REPO", tmp_path)
    return doc_path, table_path


def test_check_rejects_a_line_only_citation_as_unfixable(
    tmp_path, monkeypatch, capsys
):
    """A line-only row fails `--check` even when its line is exactly right:
    it has no symbol, so the tool reports it unfixable instead of passing it
    (or offering a proximity guess) — and `--apply` writes nothing for it."""
    doc_path, table_path = _real_checker_on(
        tmp_path, monkeypatch,
        doc_text="See `widget.py:2` for details.\n",
        rows=[("fake.md", "widget.py:2", "widget.py", "line 2")],
        source_name="widget.py",
        source_text="def make_widget():\n    return 'line 2'\n",
    )
    before = (doc_path.read_bytes(), table_path.read_bytes())

    assert ra.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert ("FAIL: fake.md `widget.py:2` — citation is line-only; symbol "
            "missing, cannot auto-reanchor") in out
    assert "VERDICT=FAIL" in out
    assert "VERDICT=OK" not in out

    assert ra.main(["--apply"]) == 1
    assert "VERDICT=FAIL" in capsys.readouterr().out
    assert (doc_path.read_bytes(), table_path.read_bytes()) == before


def test_apply_reanchors_a_drifted_symbol_citation_exactly(
    tmp_path, monkeypatch, capsys
):
    """A symbol-anchored citation whose code moved 40 lines down — far outside
    the checker's drift window — is rewritten to the exact new line on both
    surfaces by `--apply`, and a second `--check` is clean.

    The source is `.mjs` (it fails to parse as Python, so the regex fallback
    resolves the symbols), covering both shapes the desktop citations use: an
    indented `async function` and a `const` binding."""
    body = (
        "export const FEED = 'github';\n"
        "  async function checkForUpdates({ manual = false } = {}) {\n"
        "    return autoUpdater.checkForUpdates();\n"
        "  }\n"
    )
    pad = "".join(f"// padding {i}\n" for i in range(40))
    doc_path, table_path = _real_checker_on(
        tmp_path, monkeypatch,
        doc_text=("Feed `widget.mjs:FEED:1`, checked by "
                  "`widget.mjs:checkForUpdates:3`.\n"),
        rows=[
            ("fake.md", "widget.mjs:FEED:1", "widget.mjs", "'github'"),
            ("fake.md", "widget.mjs:checkForUpdates:3", "widget.mjs",
             "autoUpdater.checkForUpdates()"),
        ],
        source_name="widget.mjs",
        source_text=pad + body,
    )

    assert ra.main(["--apply"]) == 0
    capsys.readouterr()
    assert doc_path.read_text(encoding="utf-8") == (
        "Feed `widget.mjs:FEED:41`, checked by "
        "`widget.mjs:checkForUpdates:43`.\n")
    table = table_path.read_text(encoding="utf-8")
    assert '"widget.mjs:FEED:41"' in table
    assert '"widget.mjs:checkForUpdates:43"' in table
    assert ":1\"" not in table and ":3\"" not in table

    # The checker reads the monkeypatched tuple, not the fixture file, so hand
    # it the rewritten rows, as a fresh run would read them, and re-check.
    monkeypatch.setattr(ra._load_checker(), "CITATION_TABLE", (
        ("fake.md", "widget.mjs:FEED:41", "widget.mjs", "'github'"),
        ("fake.md", "widget.mjs:checkForUpdates:43", "widget.mjs",
         "autoUpdater.checkForUpdates()"),
    ))
    assert ra.main(["--check"]) == 0
    assert "VERDICT=OK" in capsys.readouterr().out


def _check_warnings(tmp_path, monkeypatch, capsys, *, name, source, symbol,
                    token, line):
    """Run `--check` on a one-row fixture; return (exit, stdout, warnings)."""
    import warnings

    raw = f"{name}:{symbol}:{line}"
    _real_checker_on(
        tmp_path, monkeypatch,
        doc_text=f"See `{raw}`.\n",
        rows=[("fake.md", raw, name, token)],
        source_name=name,
        source_text=source,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        code = ra.main(["--check"])
    return code, capsys.readouterr().out, [str(w.message) for w in caught]


def test_check_resolves_a_js_symbol_without_a_python_ast_warning(
    tmp_path, monkeypatch, capsys
):
    """A `.mjs` source is never Python, so resolving a symbol in it must not
    attempt `ast.parse` and warn "AST parse failed" on every run — that noise
    buries the warning that means a real `.py` file is broken."""
    code, out, caught = _check_warnings(
        tmp_path, monkeypatch, capsys, name="widget.mjs",
        source="export function outer() {\n  return 'js token';\n}\n",
        symbol="outer", token="'js token'", line=2,
    )
    assert code == 0 and "VERDICT=OK" in out
    assert not [m for m in caught if "AST parse failed" in m], caught


def test_check_still_warns_when_a_python_source_fails_to_parse(
    tmp_path, monkeypatch, capsys
):
    """Positive control: a `.py` file with a syntax error elsewhere still
    resolves its healthy symbol through the regex fallback, AND says so."""
    code, out, caught = _check_warnings(
        tmp_path, monkeypatch, capsys, name="widget.py",
        source="def healthy():\n    return 'py token'\n\ndef broken(:\n    pass\n",
        symbol="healthy", token="'py token'", line=2,
    )
    assert code == 0 and "VERDICT=OK" in out
    assert [m for m in caught if "AST parse failed" in m], caught
