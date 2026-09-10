# Verifiers

_Harness-captured record for task `2dcc6f80`, commit `6e55c1f39daef26bdaff524efe99e82260ccadcd` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added across the four files contain at least one assert or pytest.raises block; no assertion-free test was introduced, and the structural-budget file changed only data constants (no test functions).",
    "evidence": "Every added test function contains assertions, e.g. test_compose_returns_none_when_there_are_no_hooks_including_claim_hook: 'assert Orchestrator._compose_post_tool_hooks(None, None, None, None) is None'",
    "file": "tests/test_verification_receipts.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_structural_budget.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 1015,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1053,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or changed code performs a task status write at all, so no code path writes status via update_task(validate=False); the statement holds vacuously for this change.",
    "evidence": "The diff only adds the landed-claim guard wiring (imports LandedClaimGuard, adds _agent_sink feed, _build_landed_claim_guard, and threads claim_hook through _ordered/_compose_post_tool_hooks). No added or modified line calls update_task, references validate=False, or writes a task status directly.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 426,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
