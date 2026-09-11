# Verifiers

_Harness-captured record for task `2dcc6f80`, commit `909b23cba458793dbf45b2c309f14a64d7a9d9c0` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions across the four files (both new guard test files plus the additions to test_verification_receipts.py) contain at least one assert statement or pytest.raises block. The test_structural_budget.py change only edits frozen data dictionaries, adding/modifying no test functions.",
    "evidence": "Every added test function contains assertions, e.g. test_compose_returns_none_when_there_are_no_hooks_including_claim_hook uses `assert Orchestrator._compose_post_tool_hooks(None, None, None, None) is None` and test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim uses `with pytest.raises(QuotaExhausted):` plus several asserts.",
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
    "tokens_used": 1131,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the changed hunks touch task-status writing; they concern PostToolUse hook composition and the landed-claim guard, so no code bypasses set_status via update_task(validate=False).",
    "evidence": "The diff only adds/modifies the landed-claim-guard wiring (imports LandedClaimGuard, _agent_sink feed, _build_landed_claim_guard, and _ordered_post_tool_hooks/_compose_post_tool_hooks signatures). No added or modified line calls update_task(...) at all, let alone with validate=False, and none writes a task status.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 565,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
