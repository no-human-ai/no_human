# Verifiers

_Harness-captured record for task `15b04ad6`, commit `5154c1dc8c57fc3b8fbe21c609cd6f76aab0b765` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new/modified test functions across the diff (cancel, e2e, task_ended_telemetry, telemetry, and telemetry_environment min-props additions) carry at least one assert, pytest.raises, or assertion helper; helper functions like _mid_run_task_with_open_attempt are not test functions.",
    "evidence": "Every added/modified test function contains assertions, e.g. test_request_task_cancel_fires_task_ended_cancelled_exactly_once has `assert stopped is True` and `assert len(terminal) == 1`; test_task_ended_rejects_free_text_outcome uses `with pytest.raises(ValueError, match=\"not allowed\")`.",
    "file": "",
    "files_checked": [
      "tests/test_cancel_stops_session.py",
      "tests/test_e2e_orchestrator.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_task_ended_telemetry.py",
      "tests/test_telemetry.py",
      "tests/test_telemetry_environment.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1353,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code writes task status via update_task(validate=False); the added code paths are read-only queries and telemetry event emissions, and the emit's status=\"failed\" is an event property, not a DB status write.",
    "evidence": "The diff contains no update_task calls at all. New code is read-only (latest_attempt: 'SELECT * FROM attempts...'; count_dead_attempt_tasks: 'Read-only: never mutates a task, attempt, or event.') or telemetry emits. The one status-bearing addition, self.emit(\"cancelled_hard\", detail, status=\"failed\"), is an event/telemetry emission whose comment explicitly declines to flip DB status ('rather than relying on whatever caller ... to also flip the task's DB status').",
    "file": "",
    "files_checked": [
      "src/no_human/core/db.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/scheduler.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 915,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
