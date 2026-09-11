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

REPO = Path(__file__).resolve().parent.parent

#: Paths that may legitimately contain CR. Empty by measurement, not by
#: optimism — see the module docstring.
CRLF_ALLOWED: frozenset[str] = frozenset()

#: The byte itself, named so the assertion message reads clearly.
CR = bytes([13])


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p]


def carries_cr(data: bytes) -> bool:
    """Whether `data` is TEXT that contains a CR byte.

    A NUL byte means binary, which is the same heuristic `git grep -I` uses —
    a PNG or a font is full of incidental CRs and none of them is a line
    ending.
    """
    return b"\0" not in data and b"\r" in data


def test_no_tracked_text_file_uses_crlf() -> None:
    offenders = []
    for rel in _tracked_files():
        if rel in CRLF_ALLOWED:
            continue
        path = REPO / rel
        try:
            data = path.read_bytes()
        except (OSError, ValueError):
            continue          # a path git knows and the filesystem does not
        if carries_cr(data):
            offenders.append(f"{rel} ({data.count(CR)} CR byte(s))")
    assert offenders == [], (
        "these tracked text files contain CR bytes, so their next edit will "
        "render as a whole-file diff:\n  " + "\n  ".join(offenders)
        + "\n\nRewrite them with LF (`dos2unix <file>`), or add a path to "
          "CRLF_ALLOWED with the reason."
    )


def test_the_check_can_actually_fail() -> None:
    """Non-vacuity: a rule whose only evidence is a clean tree is a rule
    nobody has read. Drive the predicate directly on planted content."""
    assert carries_cr(b"line one\r\nline two\r\n") is True
    assert carries_cr(b"line one\nline two\n") is False
    assert carries_cr(b"\x00\r\r\r binary-ish") is False, "NUL means binary"
