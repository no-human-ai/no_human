# Verifiers

_Harness-captured record for task `9b6e928a`, commit `29364b0f3c6d2c9171b4c1bec8e756ecc938bd21` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 14 added test functions in the new file contain at least one assert; the edits to test_structural_budget.py only touch frozen data-dict comments and values, not test functions, so the invariant holds.",
    "evidence": "Every test function in the new tests/test_wake_base_stale.py contains assert statements, e.g. test_delivery_records_the_trunk_tip_it_was_measured_against: 'assert patch == {\"pr_base_sha\": expected, \"pr_base_ref\": \"main\"}'",
    "file": "tests/test_wake_base_stale.py",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_wake_base_stale.py"
    ],
    "line": 232,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 576,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the added or changed code writes a task status; it only merges context and emits events, so there is no update_task(validate=False) status write bypassing set_status.",
    "evidence": "The new/modified code (e.g. `_check_base_stale`, `_check_open_pr` changes) only writes via `self.store.merge_context(task.id, patch)` and `await self._emit(...)`, and the orchestrator hunk only does `ctx.update(await delivered_base.record_at_delivery(...))`. No call to `update_task(..., validate=False)` appears anywhere in the diff; the new rung's docstring even states it 'NEVER RESUMES, ESCALATES, OR TOUCHES CONFLICT-ROUND STATE'.",
    "file": "",
    "files_checked": [
      "src/no_human/blockers/wake.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 811,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
