# Verifiers

_Harness-captured record for task `99fa5ba5`, commit `70579137edf637e706d82208cdfdcb5c8907c10c` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test_* functions in the new file and the three new tests in test_prior_attempt_evidence.py contain multiple assert statements; the only modified non-test helper (_tests_event) is a fixture, not a test function.",
    "evidence": "Every added/modified test function contains assertions, e.g. test_a_red_run_with_more_than_the_bound... asserts `outcome.status is TaskStatus.FAILED`, `persisted[\"failing_tests\"] == ids[:_BOUND]`, etc.; new tests in test_prior_attempt_evidence.py assert `ev[\"failing_tests_dropped\"] == 800` and similar.",
    "file": "tests/test_failing_tests_bound.py",
    "files_checked": [
      "tests/test_failing_tests_bound.py",
      "tests/test_prior_attempt_evidence.py",
      "tests/test_structural_budget.py"
    ],
    "line": 174,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 544,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code calls update_task with validate=False; the single update_task call passes only the task object with no status, and status transitions use update_attempt, so the statement holds.",
    "evidence": "The only update_task call in the diff is `await self.store.update_task(task)` (no validate=False, no status kwarg); all status changes in the diff go through `update_attempt(..., status=\"failed\", ...)` which is an attempt write, not a task-status update_task call.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 18407,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1122,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  },
  {
    "comment": "The diff modifies only test-verdict text/label logic \u2014 it introduces no new hex/rgb/hsl literal and touches no color styling, so the theme-token requirement holds vacuously.",
    "evidence": "The only code change is in testResultVerdict, adding `const dropped = Number(testResults.pre_existing_failures_dropped) || 0;` and text-label construction (`\u2026 and ${dropped} more`); no className, inline style, or color literal is touched.",
    "file": "web/src/slideOverSummary.js",
    "files_checked": [
      "web/src/slideOverSummary.js",
      "web/src/slideOverSummary.test.mjs"
    ],
    "line": 575,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 341,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
