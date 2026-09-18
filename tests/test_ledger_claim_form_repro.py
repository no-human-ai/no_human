"""Repro witness for the ledger claim-form bug, kept in its own file on
purpose.

The bug: a movement-ledger comment above `FROZEN_FILE_LINES["agent/guard.py"]`
in `tests/test_structural_budget.py` asserted "...actual 3019 -- matching the
frozen value below exactly." That was true when written but false the moment
a later entry moved the frozen value past 3019 (it is 3036 now) -- a claim of
equality with a value later entries are expected to move rots on its own,
with no editor touching the sentence. The fix rewords the entry to a
point-in-time statement and adds `test_no_ledger_entry_claims_equality_with_
a_frozen_value` (in `test_structural_budget.py`) to ban the claim form
repo-wide, going forward.

Why THIS test cannot also live in `test_structural_budget.py`: the repro
gate's fails-before check builds a worktree at the pre-fix commit and copies
the *entire current content* of each declared test's own file on top of it
(`repro_gate._test_files` resolves a node id to its whole file; the copy is
`shutil.copy2`, not a diff or hunk). A test declared inside
`test_structural_budget.py` would carry that same file's already-fixed
ledger text into the "before" worktree for free, so it would pass at the
pre-fix commit too and prove nothing. Declaring the witness in a separate
file means only *this* file gets planted in the "before" worktree; the
neighbouring `tests/test_structural_budget.py` is left exactly as it was
checked out at that commit, so this test reads the real, unfixed ledger text
there and the real, fixed text on the attempt's own tree.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_BUDGET_FILE = REPO_ROOT / "tests" / "test_structural_budget.py"
_BANNED_PHRASE = "matching the frozen value below exactly"


def test_ledger_entry_no_longer_claims_equality_with_the_frozen_value():
    text = _BUDGET_FILE.read_text(encoding="utf-8")
    # Only the ledger/header portion of the file, not the guard test's own
    # source -- that test embeds this exact phrase as its positive-control
    # synthetic string, which would otherwise make this witness un-passable.
    ledger_region, _, _ = text.partition(
        "def test_no_ledger_entry_claims_equality_with_a_frozen_value"
    )
    normalized = " ".join(
        line.strip().lstrip("#").strip() for line in ledger_region.splitlines()
    )
    assert _BANNED_PHRASE not in normalized, (
        "a ledger entry still claims equality/match against a frozen value "
        "that a later entry is expected to move -- see the LEDGER CONVENTION "
        "note above FROZEN_FUNCTION_LINES in tests/test_structural_budget.py"
    )
