# Verifiers

_Harness-captured record for task `3517050d`, commit `f8a20e0a9b22f4d5ca735f9202e4701c2fcb6234` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new and modified test functions in both files include at least one assert statement; helper functions and fixtures are not tests but they also contain asserts.",
    "evidence": "Every added/modified test function (e.g. test_a_frozen_value_under_the_measurement_is_still_grown, test_a_round_that_edits_after_re_anchoring_still_commits_the_final_count, test_no_fire_when_growth_is_not_tripped, test_reanchor_frozen_refuses_a_key_present_in_two_frozen_dicts) contains assert statements.",
    "file": "",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_structural_budget_preflight.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 975,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No modified line calls update_task with validate=False; the attempt failure path uses update_attempt (an attempt-status write, not a task-status write) and returns a TaskOutcome, so the claim about task status writes bypassing set_status via update_task validate=False holds for this diff.",
    "evidence": "The only status writes in the new/modified code are `await self.store.update_attempt(attempt_id, status=\"failed\", failure_reason=detail)` and `return TaskOutcome(task, status=TaskStatus.FAILED, detail=detail)`; there is no `update_task(..., validate=False)` call anywhere in the diff.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 618,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
