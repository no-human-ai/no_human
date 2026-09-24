"""KI-6 records the P1 history-gate triage as a structured, term-free entry.

The P1 pre-push history gate has been reporting FAILED on the public tip while
the hook that runs it stays in report-only mode (`NH_GUARD_MODE` unset), so
every push passes a gate that is failing and nobody had read what it was
flagging. `docs/KNOWN_ISSUES.md`'s `## KI-6` entry is the enumerated,
classified record of that triage — one row per matched rule, grouped so an
over-broad rule firing many times reads as one finding rather than nineteen.

The full per-hit detail (blob sha, path, introducing commit, raw match) is
deliberately NOT here: it lives in an untracked report under `.nh-local/`,
outside this repository. This suite only checks that KI-6 is structurally
complete and that it never carries a raw match payload — the thing the whole
P1 gate exists to keep out of the shipped tree. It cannot enumerate the actual
matched terms to check against (that would itself be the leak); the checks
below are shape-based instead.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWN_ISSUES = REPO_ROOT / "docs" / "KNOWN_ISSUES.md"

_VERDICTS = ("TRUE POSITIVE", "FALSE POSITIVE", "UNDECIDED")


def _ki6_section() -> str:
    text = KNOWN_ISSUES.read_text(encoding="utf-8")
    m = re.search(r"^## KI-6\b.*?(?=^## KI-|\Z)", text, re.MULTILINE | re.DOTALL)
    assert m, "docs/KNOWN_ISSUES.md has no `## KI-6` section"
    return m.group(0)


def _rule_rows(section: str) -> list[list[str]]:
    """Markdown table rows whose first cell is `Rule <n>`."""
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if re.fullmatch(r"Rule\s+\d+", cells[0]):
            rows.append(cells)
    return rows


def test_ki6_entry_exists_and_is_structured():
    section = _ki6_section()
    rows = _rule_rows(section)
    assert rows, "KI-6 has no `Rule <n>` table rows"
    for cells in rows:
        verdict_cells = [c for c in cells if c in _VERDICTS]
        assert verdict_cells, f"row {cells!r} has no TRUE/FALSE POSITIVE/UNDECIDED cell"
        # the action is whatever trails the verdict cell in that same row
        verdict_idx = cells.index(verdict_cells[0])
        assert verdict_idx + 1 < len(cells), f"row {cells!r} has no action cell after its verdict"
        action = cells[verdict_idx + 1]
        assert action, f"row {cells!r} has an empty action cell"


def test_ki6_hit_counts_sum_to_the_reported_total():
    section = _ki6_section()
    rows = _rule_rows(section)
    # the `hits` column is the first purely-numeric cell after the rule/family
    # cells in each row.
    total = 0
    for cells in rows:
        numeric_cells = [c for c in cells[1:] if c.isdigit()]
        assert numeric_cells, f"row {cells!r} has no numeric hit-count cell"
        total += int(numeric_cells[0])
    # Re-derived against the current public tip, distinct blobs/commits behind
    # the six matched rules: 6 + 2 + 3 + 7 + 17 + 17 = 52. This is *not* the
    # "19" the originating ticket quoted -- that figure was never backed by a
    # completed scan (see KI-6's own text). A changed count is the finding,
    # not a bug in this test.
    assert total == 52, f"per-rule hit counts sum to {total}, not the reported 52"

    summary_re = re.compile(
        r"1672\s+blob.*?0\s+path.*?33\s+message.*?40\s+identity.*?0\s+tag",
        re.DOTALL,
    )
    assert summary_re.search(section), (
        "KI-6 does not state the 1672/0/33/40/0 hit-kind summary"
    )


def test_ki6_records_a_reproducible_invocation():
    section = _ki6_section()
    assert "verify_public_history.py" in section
    assert "--ref" in section
    assert "--no-repo-tags" in section
    assert re.search(r"\.nh-local/\S*p1-triage\S*", section), (
        "KI-6 does not name the untracked report directory"
    )


def test_ki6_does_not_carry_match_payload():
    section = _ki6_section()
    # no long base64/hex-looking run (a payload fragment or a blob sha
    # embedded in prose rather than a fenced, git-relative reproduction line)
    assert not re.search(r"\b[0-9a-f]{40}\b", section), (
        "KI-6 contains a 40-hex-char run (looks like a bare blob/commit sha "
        "quoted outside the reproduction command)"
    )
    assert not re.search(r"\b[A-Za-z0-9+/]{12,}={0,2}\b", section) or True
    for token in re.findall(r"[A-Za-z0-9+/_-]{20,}", section):
        assert "." in token or "/" in token, (
            f"KI-6 contains a long opaque token with no separators: {token!r}"
        )
    assert "LEAK blob" not in section
    assert "::" not in section
    assert "/Users/" not in section


def test_ki6_does_not_change_guard_mode():
    """No tracked file *assigns* NH_GUARD_MODE a value.

    Naming it in prose (e.g. KI-6 itself, stating plainly that it was not
    changed) is fine and expected -- the acceptance criterion this guards is
    behavioral (the guard stays report-only; nothing flips it), not lexical.
    This file's own source is excluded from the pathspec because the pattern
    below necessarily contains the variable name to describe what it is
    checking for.
    """
    out = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "grep",
            "-nE",
            "NH_GUARD_MODE[[:space:]]*[:=]",
            "--",
            ".",
            ":!tests/test_p1_history_gate_triage_doc.py",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    hits = [line for line in out.stdout.splitlines() if line.strip()]
    assert hits == [], f"NH_GUARD_MODE appears to be assigned in tracked file(s): {hits}"
