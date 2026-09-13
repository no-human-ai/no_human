# Verifiers

_Harness-captured record for task `e810bcdd`, commit `bd5ec30207b1533750136c2427e0c9c216500009` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions across the three test files carry at least one assert (or pytest.raises); the helper functions (_orch, _task, _git, _make_pushed_branch, etc.) are fixtures/utilities, not test functions, and the structural-budget edits only change dict literals, not test bodies.",
    "evidence": "Every added/modified test function contains asserts, e.g. test_the_implement_prompt_preamble_falls_back_when_base_pin_is_missing ends with `assert \"Do NOT rebase\" in prompt` and `assert \"the current base\" in prompt`; test_reconcile_remote_branch_raises_the_exact_string_the_guard_quotes uses `pytest.raises(ReviewedShaMismatch)` plus assertions.",
    "file": "tests/test_base_conflict_merge_instruction.py",
    "files_checked": [
      "tests/test_base_conflict_merge_instruction.py",
      "tests/test_base_staleness_pushed_branch.py",
      "tests/test_pushed_tip_rewrite_guard.py",
      "tests/test_structural_budget.py"
    ],
    "line": 156,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1518,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The changed code only writes task.context via update_task; there is no status mutation anywhere in the diff, and no update_task(..., validate=False) call, so the statement holds.",
    "evidence": "The only update_task call in the diff is `await self.store.update_task(task)` in `_refresh_stale_base`, which persists `task.context[\"base_staleness\"]` (a context field), not `task.status`, and passes no `validate=False` argument. No added/modified line assigns task.status or bypasses set_status.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/blockers/wake.py",
      "src/no_human/core/base_staleness.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/prompt_blocks.py"
    ],
    "line": 3848,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 846,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
