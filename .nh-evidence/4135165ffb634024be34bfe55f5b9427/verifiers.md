# Verifiers

_Harness-captured record for task `4135165f`, commit `2b4d1e77d4356d90b275b0e6d81d6f445d78eeaf` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Scanned every added/modified test function across all five test files; each contains at least one assert statement or a pytest.raises block. Non-test helpers/fixtures/frozen dicts are not test functions and are out of scope.",
    "evidence": "Every added test_* function contains assert statements or pytest.raises; e.g. test_a_commit_that_is_on_the_base_branch_is_not_blocked ends with `assert result == {}` and test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim uses `with pytest.raises(QuotaExhausted)` plus several asserts.",
    "file": "tests/test_landed_claim_early_refusal.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_vcs.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 133,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1486,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The modified code only touches read-only classification (`_already_satisfied_subject`) and hook wiring; no new or modified line writes a task status, so the claim holds vacuously.",
    "evidence": "The diff adds the landed-claim guard, changes `_already_satisfied_subject` to return a 7th `determinate` tuple element, and rewires PostToolUse hook ordering \u2014 none of it contains `update_task`, `validate=False`, or any task-status write; there are no status transitions in the changed code at all.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 456,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
