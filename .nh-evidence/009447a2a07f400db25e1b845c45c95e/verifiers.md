# Verifiers

_Harness-captured record for task `009447a2`, commit `1149cf8ca4deea77a837f50e44e1053694b693b8` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new/modified test functions across the three test files contain at least one assert, pytest.raises, or assertion-helper call; no assertion-less test bodies were added.",
    "evidence": "Every added test function ends in assertions, e.g. test_node_tap_failing_ids_empty_on_no_not_ok_line: `assert _node_tap_failing_tests(...) == []` and `assert _node_tap_failing_tests(\"\") == []`",
    "file": "tests/test_runner.py",
    "files_checked": [
      "tests/test_missing_prereq_env_classification.py",
      "tests/test_ownership_unit.py",
      "tests/test_runner.py",
      "tests/test_structural_budget.py"
    ],
    "line": 1354,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 632,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The extracted helpers (_layered_tests_failed_outcome, _failed_tests_outcome, _environment_test_failure) only touch attempt status through update_attempt and never call update_task with validate=False, so the statement holds.",
    "evidence": "Every status write in the new/modified code is an attempt-status write via `await self.store.update_attempt(attempt_id, status=\"failed\", ...)`; there is no `update_task(..., validate=False)` call anywhere in the diff, and task-level outcomes are only expressed by constructing `TaskOutcome(task, status=TaskStatus.FAILED, ...)` (a returned value, not a status write).",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 11764,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 828,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
