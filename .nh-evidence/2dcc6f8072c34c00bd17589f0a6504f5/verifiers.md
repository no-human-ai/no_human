# Verifiers

_Harness-captured record for task `2dcc6f80`, commit `d81f22334e976a1dc484c0b9662ab071eb0de912` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All newly added and modified test functions across the diff (integration, unit, line-endings, encoding, verification-receipts) contain assert statements, pytest.raises blocks, or parametrized assertions; helper/fixture functions with no assertions are not test functions.",
    "evidence": "Every added/modified test function contains at least one assert, e.g. test_note_text_refutes_the_claim_before_any_delivery uses `assert calls == []`, `assert result`, `assert calls == [\"probed\"]`; test_the_claim_guard_is_ordered_second_behind_the_receipt_observer uses multiple `assert Orchestrator._ordered_post_tool_hooks(...) == [...]`.",
    "file": "",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_line_endings.py",
      "tests/test_structural_budget.py",
      "tests/test_text_reads_declare_encoding.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 775,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or modified code writes a task status; the changed lines add the LandedClaimGuard, reorder PostToolUse hooks, and add explicit utf-8 encoding to file reads/writes, so no update_task(validate=False) status write is introduced.",
    "evidence": "The diff contains no calls to update_task at all (grep for 'update_task' or 'validate=False' in the changed hunks yields nothing); changes are the landed-claim guard wiring, hook ordering, and encoding='utf-8' additions to read_text/write_text.",
    "file": "",
    "files_checked": [
      "src/no_human/core/db.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/reviewer_worktree.py",
      "src/no_human/core/worktree.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 513,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
