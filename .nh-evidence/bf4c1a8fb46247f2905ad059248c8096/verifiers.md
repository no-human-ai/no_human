# Verifiers

_Harness-captured record for task `bf4c1a8f`, commit `92c525839875c845bc8d8a0dba6fc76c3a24da0d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added across test_api.py, test_cli_commands.py, test_queue_health.py, test_slot_wait_pool_paused_text.py, and test_scheduler_lease_write_retry.py contain at least one assert, pytest.raises block, or assertion helper; the only non-test edits (CITATION_TABLE, FROZEN_FILE_LINES) are data, not test functions.",
    "evidence": "Every added test function contains assertions, e.g. test_queue_health_endpoint_reports_lease_lost has `assert body[\"paused\"] is True`, test_a_lock_held_through_the_whole_budget_fails_closed uses `with pytest.raises(PoolLeaseLost)`, and test_the_classifier_retries_only_a_lock_message has `assert _is_transient_db_lock(exc) is expected`.",
    "file": "",
    "files_checked": [
      "tests/test_api.py",
      "tests/test_cli_commands.py",
      "tests/test_queue_health.py",
      "tests/test_readme_claims.py",
      "tests/test_scheduler_lease_write_retry.py",
      "tests/test_slot_wait_pool_paused_text.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1183,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the changed code writes a task status at all \u2014 no update_task(validate=False) and no set_status calls are introduced, so the statement holds vacuously; the only writes are to a health report object and the scheduler heartbeat/lease row.",
    "evidence": "The diff modifies only health.py (QueueHealth dataclass fields like h.paused/h.paused_reason \u2014 in-memory health report, not a task row), scheduler.py (adds _cas_heartbeat_with_retry wrapping store.cas_scheduler_heartbeat, a lease-row CAS, not a task status write), and slot_wait.py (pure text rendering). No occurrence of update_task or any status-write call appears in the new/modified code.",
    "file": "",
    "files_checked": [
      "src/no_human/core/health.py",
      "src/no_human/core/scheduler.py",
      "src/no_human/core/slot_wait.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 607,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  },
  {
    "comment": "The change is purely presentation-text/logic refactoring; it adds no new color literals and reuses existing class names, so it introduces no theme-breaking hard-coded color.",
    "evidence": "PausedIndicator renders only className-based elements (\"nh-status-indicator\", \"nh-ws-dot\", \"nh-status-label\") and tone tokens (\"warn\"/\"error\" consumed as `tone-${tone}` classes); no hex/rgb/hsl literal appears anywhere in the diff.",
    "file": "web/src/drainChip.js",
    "files_checked": [
      "web/src/App.jsx",
      "web/src/drainChip.js",
      "web/src/drainChip.test.mjs"
    ],
    "line": 105,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 343,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
