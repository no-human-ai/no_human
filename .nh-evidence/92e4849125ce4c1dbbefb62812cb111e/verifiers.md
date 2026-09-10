# Verifiers

_Harness-captured record for task `92e48491`, commit `a381e8330cb141bbd1e30cc1c13aad976214b292` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added or modified in this change carry at least one assertion or pytest.raises block; the readme-claims and structural-budget edits touch only data tables, not test functions.",
    "evidence": "test_status_prints_lease_lost_stopped_line ends with `assert \"STOPPED\" in out`; every new test in test_scheduler_lease_visibility.py and test_scheduler_lease_write_retry.py contains assert/pytest.raises (e.g. `with pytest.raises(PoolLeaseLost)` and `assert body[\"healthy\"] is False`)",
    "file": "tests/test_scheduler_lease_write_retry.py",
    "files_checked": [
      "tests/test_cli_commands.py",
      "tests/test_readme_claims.py",
      "tests/test_scheduler_lease_visibility.py",
      "tests/test_scheduler_lease_write_retry.py",
      "tests/test_structural_budget.py"
    ],
    "line": 145,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1193,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or modified code performs a task status write \u2014 the only assignments (h.paused, h.paused_reason) are on the QueueHealth report object, and the scheduler changes touch the pool-lease heartbeat, not task statuses. The statement holds (vacuously).",
    "evidence": "The diff modifies only health.py (setting paused/paused_reason on the QueueHealth dataclass) and scheduler.py (adding _is_transient_db_error, a lease_lost property, and CAS-write retry logic in _claim_pool_lease). No added or changed line calls update_task at all, let alone with validate=False; h.paused = True / h.paused_reason = \"lease_lost\" mutate an in-memory dataclass, not a task-status row.",
    "file": "",
    "files_checked": [
      "src/no_human/core/health.py",
      "src/no_human/core/scheduler.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 613,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
