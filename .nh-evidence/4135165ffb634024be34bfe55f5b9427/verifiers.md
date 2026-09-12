# Verifiers

_Harness-captured record for task `4135165f`, commit `181dd44ee056487d82feba66a606e45cdfc0ee58` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions across the three touched files (integration, unit, and receipts) contain at least one assert or pytest.raises block; the assertion-free defs (_git, fixtures, _orch, _Backend, _ClaimFeedingBackend, _incident_result) are helpers/fixtures, not test functions.",
    "evidence": "Every added/modified test_* function contains assertions, e.g. test_a_commit_that_is_on_the_base_branch_is_not_blocked ends with 'assert result == {}', and test_the_claim_guard_is_ordered_second_behind_the_receipt_observer contains multiple assert statements.",
    "file": "tests/test_landed_claim_early_refusal.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_structural_budget.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 189,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 944,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or modified code touches task status at all \u2014 it wires a PostToolUse landed-claim guard. There are no update_task(validate=False) writes to bypass set_status, so the statement holds vacuously for this diff.",
    "evidence": "The diff only adds landed-claim-guard wiring (_build_landed_claim_guard, probe(), hook ordering in _ordered_post_tool_hooks/_compose_post_tool_hooks, and _agent_sink feeding). No added or modified line calls update_task, nor writes any task status; there is no update_task(..., validate=False) or set_status call anywhere in the change.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 829,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
