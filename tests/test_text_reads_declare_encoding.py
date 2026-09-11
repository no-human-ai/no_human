"""Every `read_text()` in the harness states its encoding.

Issue #267. `Path.read_text()` with no `encoding=` resolves to the platform's
preferred encoding, which on Windows is the ANSI code page rather than UTF-8.
Eighty tracked text files in this repository are valid UTF-8 and undecodable as
cp1252, seventeen of them under `src/no_human/`, including the three the gates
read most: `core/orchestrator.py`, `api/app.py` and `cli/commands.py`.

The result on Windows was not a handful of edge cases:

  * `tests/test_knowledge_triggers.py` reads `core/orchestrator.py` at MODULE
    scope, so pytest raised during COLLECTION and the suite could not be built
    at all;
  * 44 failures and errors across 12 test files, all from this one cause; and
  * `test_structural_budget.py`'s `scan_tree` walks every `*.py` under `src/`
    with `path.read_text()`, so the structural budget gate could not run.

CI never saw it. The Linux jobs default to UTF-8 and pass for free, and the
`Windows bundle (unsigned)` job runs three test files, none of which reads a
repository source file.

WHY THIS GUARD IS A FLAT ZERO RATHER THAN A RATCHET
---------------------------------------------------
The fix was written twice. The first attempt tried to flag only the calls that
READ REPOSITORY FILES, by tracking names rooted at a repo anchor, at
`__file__`, or at a `glob()` walk. It kept missing:

  * `scan_tree`, because `files = root.rglob(...)` and then `for path in
    files:` is two hops, not one;
  * `_ORCHESTRATOR_SRC`, a module constant whose name was in no anchor list;
  * `test_db_concurrency.py`, where the path comes out of a function call,
    `for path in _package_sources()`, which no static rule is going to follow.

Every attempt to be clever about WHICH reads touch repository files left a hole
in a different shape. A flat "every read says what it is decoding" has no
holes, needs no heuristic, and is the same rule a reader can apply by eye.

SCOPE, AND WHAT IS DELIBERATELY LEFT OUT
-----------------------------------------
`write_text` is not covered. There are ~1700 unencoded `write_text` calls under
`tests/`, essentially all of them ASCII literals a test writes and reads back,
where the platform default round-trips fine. Sweeping them would be noise. The
one that mattered, `test_egress_allowlist.py` writing source it had just read,
is fixed, because once the read is correct the write is what raises next.

`src/` is not covered either, and that is a real remaining exposure rather than
an oversight: 57 unencoded `read_text` and 35 unencoded `write_text` calls,
which is the PRODUCT reading a user's files rather than the harness reading its
own. It deserves its own change and its own thought about what should happen
when a user's file genuinely is not UTF-8.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The harness: code that reads THIS repository's own files. `src/` is out of
#: scope on purpose; see the module docstring.
GUARDED_AREAS = ("tests", "scripts")


def _unencoded_read_text(path: pathlib.Path) -> list[int]:
    """Line numbers of `read_text()` calls with no `encoding=` argument."""
    try:
        tree = ast.parse(path.read_bytes())
    except SyntaxError:
        return []
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "read_text"
        and not any(kw.arg == "encoding" for kw in node.keywords)
    ]


@pytest.mark.parametrize("area", GUARDED_AREAS)
def test_no_read_text_in_the_harness_omits_its_encoding(area):
    offenders = []
    for path in sorted((REPO_ROOT / area).rglob("*.py")):
        for lineno in _unencoded_read_text(path):
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}")

    assert offenders == [], (
        "these read a file without saying how to decode it, so they use the "
        "platform's preferred encoding and die on the first UTF-8 multi-byte "
        "character when run on Windows (issue #267). Pass "
        'encoding="utf-8": ' + ", ".join(offenders)
    )


def test_the_guard_can_actually_see_an_offender():
    """A guard that scores zero because its scanner is broken looks exactly
    like a guard that scores zero because the tree is clean. This pins the
    scanner against a known positive, so a refactor that quietly stops
    matching `read_text` fails here instead of going green forever."""
    planted = ast.parse(b"import pathlib\npathlib.Path('x').read_text()\n")
    found = [
        n.lineno for n in ast.walk(planted)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "read_text"
        and not any(kw.arg == "encoding" for kw in n.keywords)
    ]
    assert found == [2], "the scanner no longer recognises an unencoded read"

    encoded = ast.parse(b'import pathlib\npathlib.Path("x").read_text(encoding="utf-8")\n')
    still = [
        n.lineno for n in ast.walk(encoded)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "read_text"
        and not any(kw.arg == "encoding" for kw in n.keywords)
    ]
    assert still == [], "the scanner flags a read that already declares utf-8"


def test_the_files_the_gates_read_are_utf_8_and_not_cp1252_decodable():
    """The premise of the whole issue, pinned rather than asserted in prose.

    If these ever became pure ASCII the guard above would still be correct but
    would stop being load-bearing, and a reader should be able to tell which
    situation they are in.
    """
    sources = [
        REPO_ROOT / "src" / "no_human" / "core" / "orchestrator.py",
        REPO_ROOT / "src" / "no_human" / "api" / "app.py",
        REPO_ROOT / "src" / "no_human" / "cli" / "commands.py",
    ]
    for path in sources:
        raw = path.read_bytes()
        raw.decode("utf-8")  # valid UTF-8; nothing is wrong with the file
        with pytest.raises(UnicodeDecodeError):
            raw.decode("cp1252")
