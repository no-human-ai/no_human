# Verifiers

_Harness-captured record for task `4135165f`, commit `262e128c6e056d5970611da441842178d6e30b5e` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new/modified test functions across the changed files carry at least one assert or pytest.raises; the edits to test_readme_claims.py and test_structural_budget.py only altered frozen data constants, not test bodies.",
    "evidence": "Every added test function contains assertions, e.g. test_build_landed_claim_guard_fires_on_a_refutable_claim_via_the_real_probe uses `assert guard is not None`, `assert result`, `assert claimed_sha in message`; test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim uses `with pytest.raises(QuotaExhausted)` plus asserts; the modified test_readme_claims/test_structural_budget changes only touched data tables, not test-function bodies.",
    "file": "tests/test_landed_claim_early_refusal.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_vcs.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 90,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 931,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or modified code writes a task status. The changes are limited to a deterministic claim-classification guard, its PostToolUse hook ordering, and adding a 7th 'determinate' element to _already_satisfied_subject \u2014 none of which mutate task status, so the statement holds vacuously.",
    "evidence": "The diff adds a read-only LandedClaimGuard (probe wrapping _already_satisfied_subject, head_sha) and hook wiring; it contains no calls to update_task, no validate=False, and no task status transitions at all.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1200,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
