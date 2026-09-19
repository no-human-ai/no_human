# Verifiers

_Harness-captured record for task `f8cae5c4`, commit `25a6cebe6309ebadb760a91fcc548b6cf0351905` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added test functions in the two new files and the modified test_stalled_active_task_escalates_honestly contain at least one assert or pytest.raises; the only non-asserting new callables (_stuck_watcher, _active_task, _park, _run) are helpers, not test functions.",
    "evidence": "Every new/modified test contains assertions, e.g. test_escalation_closes_the_open_attempt_row_with_usage has `assert escalated is True` and many row assertions; test_the_sweep_does_not_stop_a_live_backend_coroutine uses `with pytest.raises(asyncio.CancelledError)`.",
    "file": "tests/test_stall_watchdog_ordering.py",
    "files_checked": [
      "tests/test_pr_ci_watch.py",
      "tests/test_stall_watchdog_ordering.py",
      "tests/test_wake_nonactive_unchanged.py"
    ],
    "line": 128,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 749,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code writes a task status via update_task(validate=False); the escalation's status transition goes through set_status, and abandon_open_attempt only touches the attempt row via update_attempt. Statement holds.",
    "evidence": "The only task status change in modified code is the pre-existing `await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)`; new code (stall_watchdog.py, abandon_open_attempt) only calls set_status/update_attempt/update_task_columns/merge_context \u2014 never update_task to write a task status.",
    "file": "src/no_human/blockers/wake.py",
    "files_checked": [
      "src/no_human/blockers/stall_watchdog.py",
      "src/no_human/blockers/wake.py",
      "src/no_human/core/db.py"
    ],
    "line": 522,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1253,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
