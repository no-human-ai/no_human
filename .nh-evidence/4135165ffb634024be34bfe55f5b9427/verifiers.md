# Verifiers

_Harness-captured record for task `4135165f`, commit `61c21852e90bfdc51e15c105fc8fec1f7c1f221f` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions across the new and modified test files (including parametrized ones and the ones in test_verification_receipts.py, test_vcs.py, and test_landed_claim_guard.py) contain at least one assert, pytest.raises block, or assertion helper call. The changes to test_readme_claims.py and test_structural_budget.py only edit data tables, not test functions.",
    "evidence": "Every added/modified test_* function contains assert statements or pytest.raises, e.g. test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim uses `with pytest.raises(QuotaExhausted):` plus multiple `assert` lines, and test_the_new_outer_predicate... asserts `result == (False, \"\", \"\")`.",
    "file": "tests/test_landed_claim_early_refusal.py",
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
    "tokens_used": 1036,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code writes a task status via update_task(validate=False); the change is confined to the landed-claim guard and already-satisfied classification, so the statement holds vacuously.",
    "evidence": "The diff only adds/modifies the landed-claim guard wiring (`_build_landed_claim_guard`, `_agent_sink` feed, hook ordering) and `_already_satisfied_subject`'s return tuple/`determinate` code; no line in the diff calls `update_task(..., validate=False)` or writes a task status directly.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 782,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
