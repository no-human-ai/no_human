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
Both spellings of a read are covered: `path.read_text()` and the BUILTIN
`open(path).read()`. The first version of this guard matched only `read_text`,
so `open()` walked straight past it while
`test_store_fixture_convergence_guard.py` was reading this repository's own
sources that way and failing under an ASCII preferred encoding. Only the
builtin is matched, by `ast.Name`: `tarfile.open` and `urllib`'s `opener.open`
are attribute calls that take no encoding and would be false reports.

`Path.open()` is out of scope, and that is measured rather than assumed. Only
the BUILTIN `open` is matched, so `p.open()` shares the same default and slips
past; matching `.open` by attribute name instead would report `tarfile.open`
and `urllib`'s `opener.open`, neither of which takes an encoding. Review
scanned the guarded areas for text-mode `.open()` calls and found **zero**,
positive-controlling the scanner first so the zero meant something: it flags
`p.open()` and ignores `p.open("rb")` and `p.open(encoding=...)`. So the gap is
real in principle and empty in practice, and a false-positive-free rule is
worth more here than a noisier one. If `p.open()` ever appears, add it as an
`ast.Attribute` case with a receiver check rather than by name.

WRITES are not covered, and unlike `open()` that is a decision rather than an
oversight. There are ~1700 unencoded `write_text` calls under `tests/` and
eight write-mode `open()` calls, essentially all of them ASCII literals a test
writes and reads back, where the platform default round-trips fine. Sweeping
them would be noise. The one write that mattered, `test_egress_allowlist.py`
writing source it had just read, is fixed, because once the read is correct the
write is what raises next.

`src/` is covered too, as of the sweep that gave its 89 reads and writes an
explicit encoding. The policy there was the conservative one: `encoding="utf-8"`
and nothing else. On linux and in CI the preferred encoding is ALREADY utf-8,
so that changes nothing; on Windows it replaces a silent mis-decode with the
same loud error CI would have given. Adding `errors=` would have been a
behaviour change on every platform, suppressing failures that surface today,
and that is a product decision rather than an encoding one. The eleven call
sites that already passed `errors=` kept it.

WRITES are still not pinned by this guard, though `src/` now declares them.
There are ~1700 unencoded `write_text` calls under `tests/` and eight
write-mode `open()` calls, essentially all ASCII literals a test writes and
reads back, where the platform default round-trips fine. Sweeping those would
be noise.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Every directory that ships. `src/` joined the rest once its 89 unencoded
#: reads and writes were given an explicit utf-8, which removed the last
#: reason to reason about WHOSE files a call touches before trusting it.
#:
#: The boundary used to be drawn by DIRECTORY and it was wrong twice, in two
#: different shapes: `src/no_human/testing/` is harness living on the product
#: side of the folder line, and `e2e/` was outside the list entirely until the
#: ASCII locale lane caught it reading `web/src/boardLanes.js` at module
#: scope. Both were found by someone else, after a sweep that claimed to be
#: complete. Enumerating every root here is the answer to that.
GUARDED_AREAS = ("tests", "scripts", "src", "e2e")


def _read_mode(call: ast.Call) -> str:
    """The mode string a builtin `open()` call was given, `"r"` by default."""
    mode = None
    if len(call.args) > 1 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return str(mode) if mode is not None else "r"


def _is_unencoded_text_read(node: ast.AST) -> bool:
    """Whether `node` reads text without saying how to decode it.

    Two spellings, because the rule is about the act and not the function
    name. `open(f).read()` walked straight past the first version of this
    guard, and `test_store_fixture_convergence_guard.py` was reading this
    repository's own sources through it.
    """
    if not isinstance(node, ast.Call):
        return False
    if any(kw.arg == "encoding" for kw in node.keywords):
        return False

    if isinstance(node.func, ast.Attribute) and node.func.attr == "read_text":
        return True

    # The BUILTIN open only. `tarfile.open` and `opener.open` are Attribute
    # calls and take no encoding, so matching by name alone reports them.
    if isinstance(node.func, ast.Name) and node.func.id == "open":
        mode = _read_mode(node)
        # Binary carries no encoding, and write mode is excluded for the same
        # reason `write_text` is; see the module docstring.
        return "b" not in mode and not any(c in mode for c in "wax+")

    return False


def _unencoded_read_text(path: pathlib.Path) -> list[int]:
    """Line numbers of text reads that do not declare an encoding."""
    try:
        tree = ast.parse(path.read_bytes())
    except SyntaxError:
        return []
    return [node.lineno for node in ast.walk(tree)
            if _is_unencoded_text_read(node)]


@pytest.mark.parametrize("area", GUARDED_AREAS)
def test_no_read_text_in_the_harness_omits_its_encoding(area):
    offenders = []
    for path in sorted((REPO_ROOT / area).rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno in _unencoded_read_text(path):
            offenders.append(f"{rel}:{lineno}")

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


@pytest.mark.parametrize(
    ("source", "flagged", "why"),
    [
        (b"open('f').read()", True, "the spelling that escaped the first version"),
        (b"open('f', 'r').read()", True, "explicit read mode"),
        (b"open('f', encoding='utf-8').read()", False, "already declares it"),
        (b"open('f', 'rb').read()", False, "binary carries no encoding"),
        (b"open('f', 'w').write('x')", False, "writes are excluded, like write_text"),
        (b"open('f', 'a').write('x')", False, "append is a write"),
        (b"tarfile.open('f')", False, "not the builtin open"),
        (b"opener.open('http://x')", False, "urllib opener, not the builtin"),
    ],
)
def test_the_open_branch_matches_reads_and_nothing_else(source, flagged, why):
    """The `open()` half, pinned case by case.

    Review found this gap by hand after the first version shipped. Matching
    `open` by NAME alone would report `tarfile.open` and `urllib`'s
    `opener.open`, neither of which takes an encoding, so the last two cases
    are the ones that keep the rule usable rather than merely strict.
    """
    hits = [n for n in ast.walk(ast.parse(source)) if _is_unencoded_text_read(n)]
    assert bool(hits) is flagged, why


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


def test_the_ascii_locale_lane_still_guards_collection():
    """The CI lane that catches this class before a Windows runner does.

    Issue #267 was invisible to CI: the linux jobs default to UTF-8 and pass
    for free, and the Windows job runs three test files, none of which reads a
    repository source. `LC_ALL=C` gives an ASCII preferred encoding, which
    fails on a strict SUPERSET of the bytes cp1252 fails on, so one job catches
    the class earlier than a whole platform does.

    Pinned here because the lane is the only thing standing between this bug
    and a green CI, and a workflow edit that quietly drops it would otherwise
    be silent. The three env vars are each load-bearing: without `PYTHONUTF8=0`
    the run can pass for the wrong reason, with UTF-8 mode enabled by the
    environment rather than by the tree being correct.
    """
    import yaml

    ci = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert "locale" in ci["jobs"], (
        "the ASCII-locale lane is gone; issue #267 could land again with CI "
        "green, which is exactly how it landed the first time"
    )
    steps = ci["jobs"]["locale"]["steps"]
    envs = [s.get("env") or {} for s in steps]
    assert any(e.get("LC_ALL") == "C" for e in envs), "the lane no longer forces LC_ALL=C"
    assert any(str(e.get("PYTHONUTF8")) == "0" for e in envs), (
        "PYTHONUTF8 is not pinned to 0, so UTF-8 mode can silently defeat the lane"
    )
    runs = " ".join(s.get("run", "") for s in steps)
    assert "--collect-only" in runs, (
        "the lane no longer collects; collection is the part that broke and "
        "the part with no locale-sensitive assertions in it"
    )
