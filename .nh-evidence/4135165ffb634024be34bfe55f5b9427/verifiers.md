# Verifiers

_Harness-captured record for task `4135165f`, commit `b2c4fdc67f355a0d1d0142b178b6baef7add959d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added/modified test functions across the four files carry at least one assertion or pytest.raises block; the structural_budget changes are dict-data edits, not test function bodies.",
    "evidence": "Every added test_ function contains assert statements or pytest.raises, e.g. test_a_commit_that_is_on_the_base_branch_is_not_blocked ends with `assert result == {}` and test_guard_is_wired_into_the_real_run_attempt uses `with pytest.raises(QuotaExhausted):` plus several asserts.",
    "file": "tests/test_landed_claim_early_refusal.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_structural_budget.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 232,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1054,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code performs any task status write, so there is no update_task(validate=False) status bypass introduced; the statement holds vacuously for this change.",
    "evidence": "The diff contains no calls to update_task, validate=False, or set_status; the changes concern the LandedClaimGuard, PostToolUse hook ordering, and _already_satisfied_subject's added `determinate` return element \u2014 none of which write a task status.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 519,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
