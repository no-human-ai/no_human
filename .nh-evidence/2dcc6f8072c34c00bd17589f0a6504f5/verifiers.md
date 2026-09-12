# Verifiers

_Harness-captured record for task `2dcc6f80`, commit `0649c534a63c55ce2c54a6a1bc7e4c50b89a02e2` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added/modified test function carries at least one assertion (assert statement or pytest.raises). Non-test helpers like _git, _orch, _ClaimFeedingBackend are not test functions, and the test_structural_budget.py change only edits frozen data dicts, not test bodies.",
    "evidence": "Every test_* function in the two new files and the added tests in test_verification_receipts.py contains assert statements or a pytest.raises block, e.g. test_guard_is_wired_into_the_real_run_attempt uses `with pytest.raises(QuotaExhausted):` plus multiple asserts, and test_composed_post_tool_hooks_place_the_claim_guard_after_receipts uses `assert Orchestrator._ordered_post_tool_hooks(...) == [...]`.",
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
    "tokens_used": 1231,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or modified code writes a task status; the changes are confined to constructing/wiring the LandedClaimGuard PostToolUse hook, so there is no unvalidated status write to flag.",
    "evidence": "The diff adds only landed-claim-guard wiring (_build_landed_claim_guard, _agent_sink feed, PostToolUse hook ordering/composition changes). No added or modified line calls update_task at all, let alone with validate=False, and no status transition is written directly.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 507,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
