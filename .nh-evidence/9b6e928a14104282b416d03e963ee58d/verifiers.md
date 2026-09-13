# Verifiers

_Harness-captured record for task `9b6e928a`, commit `16d6683592098dd6336239cc259a611d5861beac` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions across the added test files (test_finalize_records_delivered_base.py, test_pr_base_event_kinds_indexed.py, test_wake_base_stale.py, test_wake_base_stale_followups.py) contain assert statements; the changes to test_readme_claims.py and test_structural_budget.py touch only data tables, not test-function bodies.",
    "evidence": "Every added test function contains assertions, e.g. test_info_unset_sentinel_is_not_none: `assert _INFO_UNSET is not None`; test_finalize_calls_record_at_delivery_and_merges_its_result: `assert out.status == TaskStatus.AWAITING_APPROVAL`",
    "file": "",
    "files_checked": [
      "tests/test_finalize_records_delivered_base.py",
      "tests/test_pr_base_event_kinds_indexed.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_wake_base_stale.py",
      "tests/test_wake_base_stale_followups.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1472,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the added code writes a task status directly \u2014 it only merges context keys and emits events, so it introduces no update_task(validate=False) status write. The statement holds for this diff.",
    "evidence": "The new/modified code in wake.py (_check_base_stale, _reverify_base_locally, _poll_mergeable, _check_open_pr) only updates task context via `await self.store.merge_context(task.id, {...})` and emits events via `await self._emit(...)`; no status write occurs. Any status change still routes through `self._resume(task)`. The orchestrator.py change only does `ctx.update(await delivered_base.record_at_delivery(...))`. There is no `update_task(... validate=False)` call anywhere in the diff.",
    "file": "",
    "files_checked": [
      "src/no_human/blockers/wake.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 808,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  },
  {
    "comment": "The diff only adds plain-text event labels and introduces no color literals, styles, or classNames, so no hard-coded colors were added and the statement holds vacuously.",
    "evidence": "The only change adds two text labels: pr_base_remeasured: \"PR base re-measured\" and pr_base_undetermined: \"PR base freshness undetermined\" in web/src/eventLabels.js",
    "file": "web/src/eventLabels.js",
    "files_checked": [
      "web/src/eventLabels.js"
    ],
    "line": 89,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 258,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
