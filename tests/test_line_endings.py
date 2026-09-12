"""Every tracked text file uses LF, because one CRLF file hides its own diff.

A file rewritten with Windows line endings renders as a whole-file change: on
PR #262 a 47-line addition showed as 983 lines removed and 1029 added, and the
one new test in it was unreadable in review until `--ignore-cr-at-eol` was
passed by hand. The same push did it to two other files on PR #263.

Nothing caught it. There is no `.gitattributes` in this repo and no gate, so
CI stayed green and the only reason it did not land was a reviewer noticing
the diff was the wrong shape. That is not a control.

Measured before this file was written: of every tracked text blob on `main`,
ZERO contained a CR byte (`git grep -I -l -P '\r'` finds nothing), so this
adds no exception list and breaks nothing that exists. If a file ever
genuinely needs CRLF — a Windows fixture asserting its own parsing — add it to
`CRLF_ALLOWED` with the reason, rather than weakening the rule.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Paths that may legitimately contain CR. Empty by measurement, not by
#: optimism — see the module docstring.
CRLF_ALLOWED: frozenset[str] = frozenset()

#: The byte itself, named so the assertion message reads clearly.
CR = bytes([13])


def parse_eol_line(line: str) -> tuple[str, str] | None:
    """`(path, index-eol)` from one `git ls-files --eol` record, or None.

    The record looks like::

        i/lf    w/crlf  attr/                 <TAB>some/file.py

    The three eol/attr fields are SPACE-padded and only the path is
    tab-separated, so the eol value is the first whitespace-separated token,
    not everything before the first tab. Splitting the block on "/" instead
    yields ``lf    w/crlf  attr/``, which matches no known value and makes the
    whole guard pass on a tree that genuinely has CRLF in the index. That is
    the mistake this function exists to keep in one testable place.
    """
    if not line.strip():
        return None
    fields = line.split("\t")
    if len(fields) < 2:
        return None
    token = fields[0].split()[0]          # "i/lf"
    if "/" not in token:
        return None
    return fields[-1].strip(), token.split("/", 1)[1]


def _index_eol() -> list[tuple[str, str]]:
    """`(path, index-eol)` for every tracked file, as git itself reports it.

    `git ls-files --eol` reports the INDEX and the WORKING TREE separately,
    and only the `i/` half is ours to police. See the module docstring for why
    reading the working tree instead reports a clean checkout as broken.

    Parsed line by line rather than with `-z`: git quotes any path containing
    a newline or a tab, so the path is always the last tab-separated field.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--eol"],
        capture_output=True, text=True, check=True).stdout
    return [row for row in map(parse_eol_line, out.splitlines()) if row]


def carries_cr(data: bytes) -> bool:
    """Whether `data` is TEXT that contains a CR byte.

    A NUL byte means binary, which is the same heuristic `git grep -I` uses:
    a PNG or a font is full of incidental CRs and none of them is a line
    ending.

    Retained and still exercised below. `git ls-files --eol` now does this
    classification itself, but the predicate is the readable statement of what
    the rule means and the non-vacuity test drives it directly.
    """
    return b"\x00" not in data and b"\r" in data


def test_no_tracked_text_file_uses_crlf() -> None:
    offenders = [
        rel for rel, index_eol in _index_eol()
        if rel not in CRLF_ALLOWED and index_eol in ("crlf", "mixed")
    ]
    assert offenders == [], (
        "these tracked text files are stored with CR bytes in the INDEX, so "
        "their next edit will render as a whole-file diff:\n  "
        + "\n  ".join(offenders)
        + "\n\nRewrite them with LF (`dos2unix <file>`), or add a path to "
          "CRLF_ALLOWED with the reason."
    )


def test_the_check_can_actually_fail() -> None:
    """Non-vacuity: a rule whose only evidence is a clean tree is a rule
    nobody has read. Drive the predicate directly on planted content."""
    assert carries_cr(b"line one\r\nline two\r\n") is True
    assert carries_cr(b"line one\nline two\n") is False
    assert carries_cr(b"\x00\r\r\r binary-ish") is False, "NUL means binary"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("i/lf\tw/lf\tattr/\t\tsrc/a.py", ("src/a.py", "lf")),
        ("i/crlf\tw/crlf\tattr/\t\ttests/b.py", ("tests/b.py", "crlf")),
        ("i/mixed\tw/crlf\tattr/\t\tc.txt", ("c.txt", "mixed")),
        ("i/-text\tw/-text\tattr/\t\td.png", ("d.png", "-text")),
        ("", None),
        ("   ", None),
        ("nonsense", None),
    ],
)
def test_the_eol_line_parser_reads_the_index_field(line, expected):
    """Non-vacuity for the PARSER, not just the predicate.

    The first version of `_index_eol` split the eol block on "/" and produced
    `lf    w/crlf  attr/` as the value. It matched nothing, so the guard passed
    on a tree with a genuine CRLF blob in the index and looked identical to a
    clean tree. A guard that scores zero because its parser is broken is the
    same shape as the bug this whole file is about.
    """
    assert parse_eol_line(line) == expected


def test_the_guard_reads_the_index_and_not_the_working_tree():
    """The distinction the whole fix turns on.

    `core.autocrlf=true` is what the Git for Windows installer recommends.
    Under it git writes CRLF to disk while the blob stays LF, so a clean
    checkout has CRLF in every working-tree file and LF in every index entry.
    Reading the working tree reports 1535 offenders on a repository where
    nothing is wrong.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--eol"],
        capture_output=True, text=True, check=True).stdout
    sample = [ln for ln in out.splitlines() if ln.strip()][:1]
    assert sample, "git reported no tracked files"
    # The parser must take the i/ field. If it ever took w/, this repository
    # would still pass on linux and fail for every Windows contributor, which
    # is precisely the failure that is invisible to CI.
    assert sample[0].split()[0].startswith("i/"), (
        "git ls-files --eol no longer reports the index first; the parser "
        "assumes field order"
    )
