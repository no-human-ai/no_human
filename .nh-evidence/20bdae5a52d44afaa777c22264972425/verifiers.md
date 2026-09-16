# Verifiers

_Harness-captured record for task `20bdae5a`, commit `f7b5f3597ef786e53d457f31b5454c5ac7de44ab` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All six added test functions contain at least one assert; the added non-test helpers (_raise_attribute_error, _raise_and_handle_unrelated) are fixtures, not test functions, so they are out of scope for the statement.",
    "evidence": "Each added test_* function ends with assert statements, e.g. test_a_failed_crash_event_write_still_marks_the_task_failed has `assert t.status is TaskStatus.FAILED` and `assert sched.inflight == set()`",
    "file": "tests/test_scheduler.py",
    "files_checked": [
      "tests/test_scheduler.py",
      "tests/test_structural_budget.py"
    ],
    "line": 1809,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 637,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The modified code writes an event via save_events, not a task status; there is no update_task(validate=False) call introduced or altered, so the statement holds (vacuously).",
    "evidence": "The diff only adds `_traceback_excerpt`, a truncation-marker constant, and appends a `traceback` field to `crash_event` written via `await self.store.save_events(...)`; no `update_task` call appears anywhere in the changed lines.",
    "file": "src/no_human/core/scheduler.py",
    "files_checked": [
      "src/no_human/core/scheduler.py"
    ],
    "line": 2487,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 398,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
