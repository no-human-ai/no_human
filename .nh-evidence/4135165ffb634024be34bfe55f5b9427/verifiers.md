# Verifiers

_Harness-captured record for task `4135165f`, commit `df8933b8a124a457cc348a48a74c876fc48167de` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions across the new and modified test files (early_refusal, landed_claim_guard, vcs, verification_receipts) contain at least one assert statement or pytest.raises block; the readme/structural_budget changes only edit data tables, not test bodies.",
    "evidence": "Every added test function contains assertions, e.g. test_a_commit_that_is_on_the_base_branch_is_not_blocked ends with `assert result == {}` and test_guard_is_wired_into_the_real_run_attempt uses `with pytest.raises(QuotaExhausted)` plus multiple asserts.",
    "file": "tests/test_landed_claim_early_refusal.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_vcs.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 179,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1564,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code writes task status at all (no update_task calls appear in the diff), so the claim holds vacuously \u2014 the change is confined to claim-guard wiring and the _already_satisfied_subject signature.",
    "evidence": "The diff adds/modifies only the landed-claim guard (_build_landed_claim_guard, _agent_sink feed, hook composition) and _already_satisfied_subject's return tuple/determinate status code; none of the added or modified lines contain an update_task(...) call, a validate=False argument, or any task-status write.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 12461,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 644,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
