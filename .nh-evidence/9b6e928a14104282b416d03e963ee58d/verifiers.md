# Verifiers

_Harness-captured record for task `9b6e928a`, commit `5f5d623b8cf5fb704023961f912efd08325e5b57` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All nine added test functions (test_a_landing_on_trunk_remeasures..., test_delivery_records..., test_the_rung_acts..., test_a_fresh_mergeable..., test_unknown_mergeable..., test_behind_merge_state..., test_unreadable_trunk..., test_missing_recorded_base_sha..., test_measure_is_a_three_state_answer) contain at least one assert. The budget-file edit changes only data dicts, not test functions.",
    "evidence": "Every test_* function in the new tests/test_wake_base_stale.py contains assert statements, e.g. test_delivery_records_the_trunk_tip_it_was_measured_against: 'assert patch == {\"pr_base_sha\": expected, \"pr_base_ref\": \"main\"}'",
    "file": "",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_wake_base_stale.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 751,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The new/modified code never writes task status at all \u2014 it only merges into task.context and emits events \u2014 so there is no update_task(validate=False) status write to flag.",
    "evidence": "The new rung `_check_base_stale` only writes via `self.store.merge_context(task.id, patch)` (patching pr_base_* context keys) and `self._emit(...)`; no `update_task(..., validate=False)` and no status write appears anywhere in the diff. `_poll_mergeable` only calls `self._pr_mergeable(url)`.",
    "file": "src/no_human/blockers/wake.py",
    "files_checked": [
      "src/no_human/blockers/wake.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 2489,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 727,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
