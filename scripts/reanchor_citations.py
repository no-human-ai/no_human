#!/usr/bin/env python3
"""Rewrite drifted `file.py:LINE[-LINE]` citations, mechanically.

`tests/test_readme_claims.py` tolerates small drift (±5 lines, see
`_CITATION_DRIFT_WINDOW`) so an unrelated edit above a citation does not turn
the suite red — but a drifted citation should still get re-anchored, not left
to rely on the tolerance forever. This script finds every drifted legacy
`path:line[-line]` citation across docs/security.md, docs/eval.md,
docs/KNOWN_ISSUES.md, docs/WINDOWS.md and rewrites both the doc text and the matching
CITATION_TABLE row to the line the content now lives on.

It imports the checker's own `_locate_line_citation`/`CITATION_TABLE` by path
rather than re-implementing the search, so this script can never disagree
with what `test_doc_citations_resolve_to_the_code_they_describe` actually
checks.

`--check` (default): read-only; reports drift and exits 1 if anything needs
attention. `--apply`: writes. A citation whose content cannot be found at all
(deleted, reworded, moved beyond the window) is never guessed at — it is
reported as unfixable and left for a human, same as an ambiguous match (the
raw citation text occurs zero or more than once in the doc, or in the
CITATION_TABLE literal). Every fixable drift in a run is written together;
this file never writes a doc without its matching table row, or vice versa.

This file is classified `ship`: it is doc-maintenance tooling useful to any
reader carrying the same line-citation convention, not export machinery.

Standard library only. No dependencies, by requirement (see
CONTRIBUTING.md's "do not add to the stack").
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The exact bracketing of the CITATION_TABLE literal in
# tests/test_readme_claims.py — used to scope table-row rewrites to that
# tuple, never touching a similarly-quoted string elsewhere in the file.
_CITATION_TABLE_START = "CITATION_TABLE = (\n"
_CITATION_TABLE_END = "\n)\n\nassert len(CITATION_TABLE) >= 20,"


def _load_checker():
    """The checker owns the citation grammar; load it by path so this script
    can never define a second, divergent copy of `_locate_line_citation`."""
    path = REPO / "tests" / "test_readme_claims.py"
    spec = importlib.util.spec_from_file_location("_nh_test_readme_claims", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Reconciliation:
    doc: str
    raw: str
    stable_prefix: str
    new_raw: str
    resolve_path: str

@dataclass(frozen=True)
class Drift:
    doc: str
    old_raw: str
    new_raw: str
    resolve_path: str


@dataclass(frozen=True)
class Unfixable:
    doc: str
    raw: str
    reason: str


def _new_spec(tail: str, found_line: int) -> str:
    """The replacement `line` or `line-line` spec, preserving the cited
    range's original length (a shift moves the whole span, it does not
    resize it)."""
    if "-" in tail:
        start_s, end_s = tail.split("-", 1)
        delta = found_line - int(start_s)
        return f"{found_line}-{int(end_s) + delta}"
    return str(found_line)


def _new_symbol_spec(tail: str, found_line: int) -> str:
    """`symbol:line` or `symbol:start-end`, re-anchored to *found_line*.

    A range keeps its span: the end moves by the same delta as the start, so an
    edit inside the symbol does not silently change what the range claims to
    cover. Only the start is ever verified — a symbol's length legitimately
    changes whenever its body is edited.
    """
    head, _, line_part = tail.rpartition(":")
    return f"{head}:{_new_spec(line_part, found_line)}"


def _symbol_drift(mod, resolve_path: str, tail: str, token: str) -> int | None:
    """Where *token* really is for a `symbol:line` row, or None if it is right.

    None also when the row carries no line at all, when the path does not
    resolve to exactly one file, or when the symbol or token cannot be found —
    all of those are the checker's business to report, not this script's to
    guess at.
    """
    cited = mod._cited_line(tail)
    if cited is None:
        return None
    hits = mod._resolve_source(resolve_path)
    if len(hits) != 1:
        return None
    symbol = tail.rsplit(":", 1)[0]
    actual = mod._token_line_in_symbol(
        hits[0].read_text(encoding="utf-8"), symbol, token)
    return None if actual is None or actual == cited else actual


def plan(mod, rows) -> tuple[list[Drift], list[Unfixable]]:
    """Classify every row in *rows*: drifted (fixable), missing (unfixable —
    nothing to anchor to), or fine (neither, skipped).

    Both citation forms are handled, and they are fixable for different reasons.
    A legacy line-form row is anchored by PROXIMITY, so a match beyond the drift
    window is a guess and is refused. A `symbol:line` row is anchored by the
    SYMBOL, which resolves however far the code has moved, so its number can be
    rewritten exactly at any distance (issue #93).
    """
    drifts: list[Drift] = []
    unfixable: list[Unfixable] = []
    for doc, raw, resolve_path, token in rows:
        tail = raw.split(":", 1)[1]
        if not mod._LEGACY_LINE_SPEC_RE.match(tail):
            actual = _symbol_drift(mod, resolve_path, tail, token)
            if actual is not None:
                prefix = raw.split(":", 1)[0]
                drifts.append(Drift(
                    doc, raw, f"{prefix}:{_new_symbol_spec(tail, actual)}",
                    resolve_path))
            continue
        status, found_line, detail = mod._locate_line_citation(resolve_path, tail, token)
        if status in ("exact", "unresolved"):
            continue
        if status == "missing":
            unfixable.append(Unfixable(
                doc, raw,
                detail or f"{token!r} not found near `{raw}` in {resolve_path}"))
            continue
        prefix = raw.split(":", 1)[0]
        new_raw = f"{prefix}:{_new_spec(tail, found_line)}"
        drifts.append(Drift(doc, raw, new_raw, resolve_path))
    return drifts, unfixable



def diagnose_divergence(doc_text: str, doc_path: Path, d: Drift, is_table: bool, mod) -> str:
    tail = d.old_raw.split(":", 1)[1]
    if mod._LEGACY_LINE_SPEC_RE.match(tail):
        return (f"`{d.old_raw}` does not occur exactly once in {doc_path.name}\n"
                f"  `--apply` will not guess which surface is authoritative.\n"
                f"  Legacy line citations have no stable symbol identity and must be resolved manually.")

    prefix = d.old_raw.rsplit(":", 1)[0]
    pattern = r"`(" + re.escape(prefix) + r":\d+(?:-\d+)?)" + r"`" if not is_table else r'"(' + re.escape(prefix) + r':\d+(?:-\d+)?)"'
    matches = re.findall(pattern, doc_text)
    if len(matches) == 1:
        doc_val = matches[0]
        doc_str = doc_val if not is_table else d.old_raw
        tab_str = d.old_raw if not is_table else doc_val
        return (f"Citation is already divergent:\n"
                f"  documentation: `{doc_str}`\n"
                f"  table:         `{tab_str}`\n"
                f"  source:        `{d.new_raw}`\n\n"
                f"  `--apply` will not guess which surface is authoritative.\n"
                f"  Use `--reconcile` to derive both from the source, or resolve manually.")
    else:
        return (f"`{d.old_raw}` does not occur exactly once in {doc_path.name} "
                f"— will not guess which occurrence to rewrite")

def rewrite(text: str, raw: str, new_raw: str) -> str | None:
    """Replace the single backtick-wrapped occurrence of *raw* in *text*
    with *new_raw*. Pure. Returns None — never guesses — if `` `raw` ``
    occurs zero or more than once."""
    needle = f"`{raw}`"
    if text.count(needle) != 1:
        return None
    return text.replace(needle, f"`{new_raw}`", 1)


def _table_slice(text: str) -> tuple[int, int]:
    start = text.index(_CITATION_TABLE_START) + len(_CITATION_TABLE_START)
    end = text.index(_CITATION_TABLE_END, start)
    return start, end


def rewrite_table_row(text: str, raw: str, new_raw: str) -> str | None:
    """Replace the single double-quoted occurrence of *raw* inside the
    CITATION_TABLE literal in *text* with *new_raw*. Pure. Returns None —
    never guesses — if `"raw"` occurs zero or more than once in that slice,
    or the CITATION_TABLE literal cannot be located at all."""
    try:
        start, end = _table_slice(text)
    except ValueError:
        return None
    body = text[start:end]
    needle = f'"{raw}"'
    if body.count(needle) != 1:
        return None
    new_body = body.replace(needle, f'"{new_raw}"', 1)
    return text[:start] + new_body + text[end:]


def _apply_all(
    mod, drifts: list[Drift]
) -> tuple[dict[Path, str] | None, list[Unfixable]]:
    """Build every new file text in memory; only hand any of them back if
    EVERY drift resolved on both surfaces (doc + table row) — an
    all-or-nothing batch, so a partial apply can never leave a doc and its
    CITATION_TABLE row pointing at different lines."""
    table_path = REPO / "tests" / "test_readme_claims.py"
    doc_texts: dict[Path, str] = {}
    table_text = table_path.read_text(encoding="utf-8")
    unresolved: list[Unfixable] = []

    changed_paths: set[Path] = set()

    for d in drifts:
        doc_path = mod._CITATION_DOC_PATHS[d.doc]
        doc_text = doc_texts.get(doc_path, doc_path.read_text(encoding="utf-8"))
        new_doc_text = rewrite(doc_text, d.old_raw, d.new_raw)
        if new_doc_text is None:
            unresolved.append(Unfixable(
                d.doc, d.old_raw,
                diagnose_divergence(doc_text, doc_path, d, False, mod)))
            continue
        new_table_text = rewrite_table_row(table_text, d.old_raw, d.new_raw)
        if new_table_text is None:
            unresolved.append(Unfixable(
                d.doc, d.old_raw,
                diagnose_divergence(table_text, table_path, d, True, mod)))
            continue
        doc_texts[doc_path] = new_doc_text
        table_text = new_table_text
        changed_paths.add(doc_path)
        changed_paths.add(table_path)

    if unresolved:
        return None, unresolved

    final_texts = {}
    for path in changed_paths:
        if path == table_path:
            final_texts[path] = table_text
        else:
            final_texts[path] = doc_texts[path]
    return final_texts, unresolved



def reconcile_plan(mod, rows) -> tuple[list[Reconciliation], list[Unfixable]]:
    reconciliations: list[Reconciliation] = []
    unfixable: list[Unfixable] = []
    for doc, raw, resolve_path, token in rows:
        tail = raw.split(":", 1)[1]
        if mod._LEGACY_LINE_SPEC_RE.match(tail):
            unfixable.append(Unfixable(
                doc, raw,
                "legacy line-only citation cannot be auto-reconciled (no stable symbol identity)"))
            continue

        cited = mod._cited_line(tail)
        if cited is None:
            continue
        hits = mod._resolve_source(resolve_path)
        if len(hits) != 1:
            unfixable.append(Unfixable(doc, raw, f"source path does not resolve to exactly one file"))
            continue

        symbol = tail.rsplit(":", 1)[0]
        actual = mod._token_line_in_symbol(hits[0].read_text(encoding="utf-8"), symbol, token)
        if actual is None:
            unfixable.append(Unfixable(doc, raw, f"symbol {symbol!r} or token not found in source"))
            continue

        prefix = raw.split(":", 1)[0]
        stable_prefix = f"{prefix}:{symbol}"
        new_raw = f"{prefix}:{_new_symbol_spec(tail, actual)}"
        reconciliations.append(Reconciliation(doc, raw, stable_prefix, new_raw, resolve_path))

    return reconciliations, unfixable

def rewrite_reconcile(text: str, stable_prefix: str, old_raw: str, new_raw: str) -> tuple[str | None, bool]:
    needle = f"`{old_raw}`"
    if text.count(needle) == 1:
        return text.replace(needle, f"`{new_raw}`", 1), old_raw != new_raw

    pattern = r"`(" + re.escape(stable_prefix) + r":\d+(?:-\d+)?)" + r"`"
    matches = re.findall(pattern, text)
    if len(matches) != 1:
        return None, False
    found_raw = matches[0]
    changed = found_raw != new_raw
    return text.replace(f"`{found_raw}`", f"`{new_raw}`", 1), changed

def rewrite_table_row_reconcile(text: str, stable_prefix: str, old_raw: str, new_raw: str) -> tuple[str | None, bool]:
    try:
        start, end = _table_slice(text)
    except ValueError:
        return None, False
    body = text[start:end]

    needle = f'"{old_raw}"'
    if body.count(needle) == 1:
        new_body = body.replace(needle, f'"{new_raw}"', 1)
        return text[:start] + new_body + text[end:], old_raw != new_raw

    pattern = r'"(' + re.escape(stable_prefix) + r':\d+(?:-\d+)?)"'
    matches = re.findall(pattern, body)
    if len(matches) != 1:
        return None, False
    found_raw = matches[0]
    changed = found_raw != new_raw
    new_body = body.replace(f'"{found_raw}"', f'"{new_raw}"', 1)
    return text[:start] + new_body + text[end:], changed

def _reconcile_all(
    mod, reconciliations: list[Reconciliation]
) -> tuple[dict[Path, str] | None, list[Unfixable], int]:
    table_path = REPO / "tests" / "test_readme_claims.py"
    doc_texts: dict[Path, str] = {}
    table_text = table_path.read_text(encoding="utf-8")
    unresolved: list[Unfixable] = []
    total_changed = 0

    changed_paths: set[Path] = set()

    for r in reconciliations:
        doc_path = mod._CITATION_DOC_PATHS[r.doc]
        doc_text = doc_texts.get(doc_path, doc_path.read_text(encoding="utf-8"))

        new_doc_text, doc_changed = rewrite_reconcile(doc_text, r.stable_prefix, r.raw, r.new_raw)
        if new_doc_text is None:
            unresolved.append(Unfixable(
                r.doc, r.raw,
                f"`{r.raw}` does not occur exactly once in {doc_path.name} "
                f"— will not guess which occurrence to reconcile"))
            continue

        new_table_text, table_changed = rewrite_table_row_reconcile(table_text, r.stable_prefix, r.raw, r.new_raw)
        if new_table_text is None:
            unresolved.append(Unfixable(
                r.doc, r.raw,
                f'"{r.stable_prefix}" does not occur exactly once in CITATION_TABLE '
                f"— will not guess which row to reconcile"))
            continue

        doc_texts[doc_path] = new_doc_text
        table_text = new_table_text
        if doc_changed or table_changed:
            total_changed += 1
        if doc_changed:
            changed_paths.add(doc_path)
        if table_changed:
            changed_paths.add(table_path)

    if unresolved:
        return None, unresolved, 0

    final_texts = {}
    for path in changed_paths:
        if path == table_path:
            final_texts[path] = table_text
        else:
            final_texts[path] = doc_texts[path]
    return final_texts, unresolved, total_changed

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="reanchor_citations.py",
        description="Report or rewrite drifted file.py:LINE[-LINE] "
                     "citations in docs/security.md, docs/eval.md, "
                     "docs/KNOWN_ISSUES.md, docs/WINDOWS.md and their CITATION_TABLE rows in "
                     "tests/test_readme_claims.py.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                       help="read-only (default): report drift, write nothing")
    mode.add_argument("--apply", action="store_true",
                       help="rewrite the doc and CITATION_TABLE row for "
                            "every drifted citation")
    mode.add_argument("--reconcile", action="store_true",
                       help="force the doc and CITATION_TABLE to the source-derived citation (opt-in)")
    args = ap.parse_args(argv)

    try:
        mod = _load_checker()
    except Exception as exc:  # the checker failed to import or parse
        print(f"FAIL: could not load tests/test_readme_claims.py: {exc}")
        print("VERDICT=FAIL")
        return 2

    if args.reconcile:
        recs, unfixable = reconcile_plan(mod, mod.CITATION_TABLE)
        for u in unfixable:
            print(f"FAIL: {u.doc} `{u.raw}` — {u.reason}")

        texts, unresolved, total_changed = _reconcile_all(mod, recs)
        if texts is None:
            for u in unresolved:
                print(f"FAIL: {u.doc} `{u.raw}` — {u.reason}")
            print("VERDICT=FAIL")
            return 1

        if not unfixable and total_changed == 0:
            print("VERDICT=OK")
            return 0

        for path, new_text in texts.items():
            # Write raw LF-terminated bytes (not write_text) so the platform's
            # text layer never translates '\n' to CRLF (#32), and so this
            # keeps working under a pre-3.10 `python3` — see the `--apply`
            # write below for the full rationale.
            path.write_bytes(new_text.encode("utf-8"))

        print(f"Reconciled {total_changed} citation(s).")
        if texts:
            print("Modified:")
            for path in texts:
                print(f"  {path.relative_to(REPO).as_posix()}")

        if unfixable:
            print(f"\nUnfixable citations remain: {len(unfixable)}")

        print("VERDICT=" + ("FAIL" if unfixable else "OK"))
        return 1 if unfixable else 0

    drifts, unfixable = plan(mod, mod.CITATION_TABLE)

    for u in unfixable:
        print(f"FAIL: {u.doc} `{u.raw}` — {u.reason}")

    if not drifts and not unfixable:
        print("VERDICT=OK")
        return 0

    for d in drifts:
        verb = "re-anchoring" if args.apply else "would re-anchor"
        print(f"DRIFT: {d.doc} `{d.old_raw}` -> `{d.new_raw}` ({verb})")

    if not args.apply:
        print("VERDICT=FAIL")
        return 1

    if not drifts:
        # Nothing fixable to write; the unfixable rows above still block.
        print("VERDICT=FAIL")
        return 1

    texts, unresolved = _apply_all(mod, drifts)
    if texts is None:
        for u in unresolved:
            print(f"FAIL: {u.doc} `{u.raw}` — {u.reason}")
        print("VERDICT=FAIL")
        return 1

    for path, new_text in texts.items():
        # Write raw LF-terminated bytes (not write_text) for the same reason
        # `check_release_manifest.py --write` does (#32): without it the write
        # goes through the platform's text layer, so on Windows every line in
        # the file comes out CRLF and a four-citation change lands as a
        # thousand-line diff. write_text(..., newline="\n") would give the same
        # LF guarantee, but that keyword only exists on Python >=3.10, and this
        # script can be invoked through an older system `python3` (see
        # check_release_manifest.py's write_manifest for the same constraint).
        path.write_bytes(new_text.encode("utf-8"))

    print(f"Applied {len(drifts)} re-anchor(s).")
    if texts:
        print("Modified:")
        for path in texts:
            print(f"  {path.relative_to(REPO).as_posix()}")

    if unfixable:
        print(f"\nUnfixable citations remain: {len(unfixable)}")

    print("VERDICT=" + ("FAIL" if unfixable else "OK"))
    return 1 if unfixable else 0


if __name__ == "__main__":
    raise SystemExit(main())
