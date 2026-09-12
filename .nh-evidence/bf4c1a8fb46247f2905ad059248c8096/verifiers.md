# Verifiers

_Harness-captured record for task `bf4c1a8f`, commit `fbdb9bc38e906b20cb8e166dd0e0218ba06d4e26` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new/modified test functions (test_api.py, test_cli_commands.py, test_queue_health.py, test_scheduler_lease_write_retry.py) carry at least one assert or pytest.raises; the changes to test_readme_claims.py and test_structural_budget.py touch only module-level data tables, not test functions.",
    "evidence": "Every added/modified test function contains assertions \u2014 e.g. test_queue_health_endpoint_reports_lease_lost has `assert body[\"paused\"] is True`; the scheduler retry tests use `with pytest.raises(PoolLeaseLost)` and `assert calls[\"n\"] == 2`; test_the_classifier_retries_only_a_lock_message ends in `assert _is_transient_db_lock(exc) is expected`.",
    "file": "",
    "files_checked": [
      "tests/test_api.py",
      "tests/test_cli_commands.py",
      "tests/test_queue_health.py",
      "tests/test_readme_claims.py",
      "tests/test_scheduler_lease_write_retry.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 789,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new/modified code calls update_task(validate=False) \u2014 the changes are lease-heartbeat retry logic and QueueHealth pause fields, neither of which performs a task status transition.",
    "evidence": "The only status-adjacent writes in the diff are h.paused/h.paused_reason on the in-memory QueueHealth dataclass, and _cas_heartbeat_with_retry wrapping store.cas_scheduler_heartbeat (the pool lease); no update_task call appears anywhere in the diff.",
    "file": "src/no_human/core/scheduler.py",
    "files_checked": [
      "src/no_human/core/health.py",
      "src/no_human/core/scheduler.py"
    ],
    "line": 1418,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 696,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  },
  {
    "comment": "No new color literals are introduced; the change reuses existing className/tone-token conventions, so theme rendering is unaffected.",
    "evidence": "The diff changes only JS logic and text strings; JSX reuses existing classes (nh-status-indicator, nh-ws-dot, nh-status-label) and tone tokens (\"warn\"/\"error\") \u2014 no hex, rgb, or hsl literal appears anywhere.",
    "file": "web/src/drainChip.js",
    "files_checked": [
      "web/src/App.jsx",
      "web/src/drainChip.js",
      "web/src/drainChip.test.mjs"
    ],
    "line": 62,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 377,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
