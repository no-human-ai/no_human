# Verifiers

_Harness-captured record for task `967621c9`, commit `e8ff0c5a16126b37bab8a13627b38e9d6d9806e6` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Each of the six new test functions in test_scheduler.py contains one or more assert statements; the test_structural_budget.py change only edits a data dict, adding no test functions.",
    "evidence": "All six added test functions (e.g. test_a_pool_crash_increments_the_worker_death_counter) contain assert statements such as `assert sched.health_snapshot()[\"worker_deaths_total\"] == 0`",
    "file": "tests/test_scheduler.py",
    "files_checked": [
      "tests/test_scheduler.py",
      "tests/test_structural_budget.py"
    ],
    "line": 1667,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 854,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The new/modified lines in this hunk write only events and a counter, not a task status; the FAILED-marking below the diffed region is pre-existing context and unchanged, so nothing here introduces an unvalidated status write.",
    "evidence": "The modified crash handler only adds `await self.store.save_events(task.id, [crash_event])` and increments `_worker_deaths_total`; no added or changed line calls `update_task(..., validate=False)`. The 'Mark the task as FAILED' code past the save_events block is unchanged context, not part of this diff.",
    "file": "src/no_human/core/scheduler.py",
    "files_checked": [
      "src/no_human/core/scheduler.py"
    ],
    "line": 2345,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 746,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
