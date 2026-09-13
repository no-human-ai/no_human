# Verifiers

_Harness-captured record for task `9b6e928a`, commit `0e482f761222805717e4526560311b4963932ea6` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 13 test functions added in the new file include at least one assertion; the only other diff (test_structural_budget.py) changes frozen-dict data values and comments, not any test function body.",
    "evidence": "Every test function in the new tests/test_wake_base_stale.py contains assert statements, e.g. test_delivery_records_the_trunk_tip_it_was_measured_against: `assert patch == {\"pr_base_sha\": expected, \"pr_base_ref\": \"main\"}`",
    "file": "tests/test_wake_base_stale.py",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_wake_base_stale.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 803,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The added code is observational only \u2014 it records freshness via merge_context and emits events, and never changes task status, so it cannot bypass set_status/update_task validation. The statement holds (vacuously, since no status is written).",
    "evidence": "The new/modified code (`_check_base_stale`, `_poll_mergeable`, and the orchestrator delivery hook) only calls `self.store.merge_context(...)`, `self._emit(...)`, and `ctx.update(await delivered_base.record_at_delivery(...))`; no `update_task(validate=False)` and no direct task-status write appears anywhere in the diff. The added rung is documented as 'NEVER RESUMES, ESCALATES, OR TOUCHES CONFLICT-ROUND STATE'.",
    "file": "src/no_human/blockers/wake.py",
    "files_checked": [
      "src/no_human/blockers/wake.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 2560,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1436,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
