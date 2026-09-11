# Verifiers

_Harness-captured record for task `2dcc6f80`, commit `701dd1fa860d49d1d07d77bd19d5c82cfbb8fa59` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions across the three test files carry assertions (assert statements or pytest.raises); non-test helpers like _git/_config/_orch/_real_probe are not test functions and are correctly out of scope.",
    "evidence": "Every added/modified test function contains at least one assert or pytest.raises, e.g. test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim uses `with pytest.raises(QuotaExhausted):` plus multiple asserts, and test_compose_returns_none_when_there_are_no_hooks_including_claim_hook has `assert Orchestrator._compose_post_tool_hooks(None, None, None, None) is None`.",
    "file": "",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_structural_budget.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1118,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the changed code touches task-status writes; there is no update_task(validate=False) call anywhere in the diff, so the statement holds vacuously for the modified code.",
    "evidence": "The diff only wires up a LandedClaimGuard (imports, _agent_sink feed, _build_landed_claim_guard, and the _ordered_post_tool_hooks/_compose_post_tool_hooks signature changes). No added or modified line calls update_task at all, let alone with validate=False, and none writes a task status.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 528,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
