"""Exhaustiveness: every `console.print`/`.rule`/`.add_row` call site in
`src/no_human/cli/*.py` that interpolates externally-sourced text (a task
title/description, a blocker's question, a DB row's free-text field, ...)
must protect it with `esc(...)`/`escape(...)` before Rich's markup parser
sees it. `tests/_cli_external_text_guard.py` is the structural (AST-based)
measurement that establishes this by shape, not by re-reading the bug
report; this module drives it to prove (a) the CLI package is currently
clean, (b) the guard itself actually catches the three known-bad shapes it
claims to catch, and (c) it does not silently shrink its own scan set.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _cli_external_text_guard import _scan, find_unprotected_external_prints  # noqa: E402

CLI_DIR = Path(__file__).resolve().parent.parent / "src" / "no_human" / "cli"

#: Call sites the guard flags but which are not, in fact, unprotected
#: external text. Empty by design: every measured offender in the CLI
#: package has been wrapped in `esc(...)`. Keyed on the unparsed root
#: expression so a genuinely new offender (different expression text) still
#: fails this test even if it happens to land on an already-listed line.
_JUSTIFIED: dict[str, str] = {}


# --------------------------------------------------------------------------- #
# AC4 — exhaustive by measurement, and fails if a new offender is added       #
# --------------------------------------------------------------------------- #

def test_no_cli_print_interpolates_unprotected_external_text():
    offenders: list[str] = []
    for path in sorted(CLI_DIR.glob("*.py")):
        for lineno, expr in find_unprotected_external_prints(path):
            if expr in _JUSTIFIED:
                continue
            offenders.append(f"{path.relative_to(CLI_DIR.parent.parent.parent)}:{lineno}: {expr}")

    assert offenders == [], (
        "unprotected externally-sourced interpolation(s) found — wrap each "
        "in esc(...) at the point of interpolation:\n" + "\n".join(offenders)
    )


def test_guard_is_not_vacuous():
    """A guard that reports zero offenders because it stopped looking is
    worse than no guard at all. Feed it a synthetic module shaped like the
    three known-bad patterns from the bug report and confirm each is still
    caught."""
    source = '''
from no_human.core.task import Task

def show(console, t: Task, row):
    # 1. f-string attribute access on a Task text field.
    console.print(f"description: {t.description}")

    # 2. bare subscript access with a known external-text key, no f-string.
    console.print(row["text"])

    # 3. loop-target taint: `c` is bound from an external-shaped field.
    for c in t.acceptance_criteria:
        console.print(f"- {c}")
'''
    offenders = _scan(source)
    exprs = {expr for _lineno, expr in offenders}

    assert "t.description" in exprs
    # `ast.unparse` normalises string-literal quoting to single quotes,
    # regardless of how the source wrote it (`row["text"]` in the synthetic
    # source above) — match the unparsed form, not the source form.
    assert "row['text']" in exprs
    assert "c" in exprs
    assert len(offenders) == 3


def test_guard_correctly_ignores_protected_call_sites():
    """The inverse check: a module where every external interpolation is
    already wrapped in `esc(...)` (or the whole call opts out via
    `markup=False`) reports zero offenders — so the exhaustiveness test
    above is actually discriminating, not unconditionally empty."""
    source = '''
from no_human.core.task import Task
from no_human.cli.render import esc

def show(console, t: Task, row):
    console.print(f"description: {esc(t.description)}")
    console.print(escape(row["text"]))
    console.print(t.description, markup=False)
    for c in t.acceptance_criteria:
        console.print(f"- {esc(c)}")
'''
    assert _scan(source) == []


# --------------------------------------------------------------------------- #
# Coverage — the guard scans the whole CLI package, not a hand-picked subset  #
# --------------------------------------------------------------------------- #

def test_guard_covers_the_whole_cli_package():
    scanned = {p.name for p in sorted(CLI_DIR.glob("*.py"))}

    assert "commands.py" in scanned
    assert "init_cmd.py" in scanned
    assert "verifiers_cmd.py" in scanned
    assert scanned == {p.name for p in CLI_DIR.iterdir() if p.suffix == ".py"}
