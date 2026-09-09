# Verifiers

_Harness-captured record for task `15b04ad6`, commit `2ebd5ebe5d3bf21c89abec239b83ffd5666d5f3d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new and modified test functions in the three test files contain assertions (assert statements or pytest.raises blocks); the only assertion-free function, _mid_run_task_with_open_attempt, is a setup helper, not a test.",
    "evidence": "Every added/modified test_* function carries an assert or pytest.raises, e.g. test_orphan_bucket_edges: `assert telemetry.orphan_bucket(n) == bucket`; test_task_ended_rejects_free_text_outcome uses `with pytest.raises(ValueError, match=\"not allowed\")`.",
    "file": "tests/test_task_ended_telemetry.py",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_task_ended_telemetry.py",
      "tests/test_telemetry.py",
      "tests/test_telemetry_environment.py"
    ],
    "line": 217,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 948,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The new/modified code performs no task-status writes at all \u2014 the scheduler helper is read-only and the orchestrator change only records telemetry \u2014 so no update_task(validate=False) status write exists.",
    "evidence": "count_dead_attempt_tasks docstring: 'Read-only: never mutates a task, attempt, or event.'; orchestrator change only calls telemetry.record(\"task_ended\", ...). No update_task call appears in the diff.",
    "file": "src/no_human/core/scheduler.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/scheduler.py"
    ],
    "line": 2628,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 764,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
