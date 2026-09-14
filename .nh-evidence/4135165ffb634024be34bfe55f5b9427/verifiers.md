# Verifiers

_Harness-captured record for task `4135165f`, commit `42698a12c369eb5f66a90989fb3e7a70b6e0f3fc` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added or modified in this change carry at least one assert, pytest.raises, or assertion helper; the non-asserting definitions (_git, _config, _orch, _incident_result, backend/probe helpers) are fixtures/helpers, not test functions.",
    "evidence": "Every added test_* function contains at least one assertion, e.g. test_a_commit_that_is_on_the_base_branch_is_not_blocked ends with `assert result == {}` and test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim uses `with pytest.raises(QuotaExhausted)` plus multiple asserts.",
    "file": "tests/test_landed_claim_early_refusal.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_vcs.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 1,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1124,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code writes a task status; the change is confined to the landed-claim guard and the _already_satisfied_subject return signature, so the constraint is satisfied vacuously.",
    "evidence": "The diff only adds LandedClaimGuard construction/wiring (_build_landed_claim_guard, _agent_sink feed, hook composition) and extends _already_satisfied_subject's return tuple with a `determinate` status code; no `update_task` call (validated or not) and no task status write appears anywhere in the new or modified code.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 740,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
