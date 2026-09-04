# Verifiers

_Harness-captured record for task `1012f285`, commit `02a306808f84ee92e3e53a9f393ff3679e8cfcfb` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added test functions (across budget_terminal, pr_body_truthfulness, tamper_adjudication, telemetry) and the two modified readme_claims tests carry at least one assert or pytest.raises; the test_structural_budget.py change only edits a data dict, not a test function.",
    "evidence": "Every added test contains assertions, e.g. test_the_failed_event_carries_blocker_category_for_telemetry: `assert failed_event[\"blocker_category\"] == \"BUDGET_EXHAUSTED\"`; test_out_of_enum_reason_category_raises uses `with pytest.raises(ValueError, match=\"not allowed\")`.",
    "file": "tests/test_budget_terminal.py",
    "files_checked": [
      "tests/test_budget_terminal.py",
      "tests/test_pr_body_truthfulness.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_tamper_adjudication.py",
      "tests/test_telemetry.py"
    ],
    "line": 176,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1202,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "All status transitions in the diff route through `store.set_status`; there are no `update_task(..., validate=False)` calls in new or modified code, so the transition table remains enforced.",
    "evidence": "The only status write in the changed code is `await self.store.set_status(task, TaskStatus.FAILED)` inside `_fail`; no new/modified line calls `update_task` with `validate=False`. The rest of the diff only adds `reason_category`/telemetry plumbing.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 7530,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 591,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
