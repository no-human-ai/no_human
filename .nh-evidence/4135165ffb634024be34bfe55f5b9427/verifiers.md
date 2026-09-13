# Verifiers

_Harness-captured record for task `4135165f`, commit `c8f77d57003edf0e8b35253e9d4637c55aa42344` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added test functions (across test_landed_claim_early_refusal.py, test_landed_claim_guard.py, test_vcs.py, test_verification_receipts.py) contain at least one assert or pytest.raises; the changes to test_readme_claims.py and test_structural_budget.py only edit data constants, not test bodies.",
    "evidence": "Every added test function contains assertions, e.g. test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim uses `with pytest.raises(QuotaExhausted):` plus multiple `assert` statements; test_composed_post_tool_hooks_place_the_claim_guard_after_receipts uses `assert Orchestrator._ordered_post_tool_hooks(...) == [...]`.",
    "file": "",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_vcs.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1025,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code writes a task status; there are no update_task(validate=False) calls anywhere in this change, so the statement is satisfied.",
    "evidence": "The diff adds LandedClaimGuard wiring, a `determinate` status code to `_already_satisfied_subject`, and hook-ordering changes; no added/modified line contains `update_task`, `validate=False`, or any task-status write.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 545,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
