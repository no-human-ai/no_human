# Verifiers

_Harness-captured record for task `e810bcdd`, commit `159ea60396433a519ae5c9ec06e38feb518d77de` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions across the new/modified test files (test_base_conflict_merge_instruction.py, test_base_staleness_pushed_branch.py, test_pushed_tip_rewrite_guard.py) contain assert statements or pytest.raises blocks; non-test helpers/fixtures without assertions are not test functions. The structural_budget change is data/comments only.",
    "evidence": "Every added/modified test function contains assertions, e.g. test_no_git_subprocess_for_non_rewrite_commands has `assert calls[\"n\"] == 0` and `assert calls[\"n\"] > 0`; test_the_rules_block_carries_the_merge_instruction_only_when_asked has multiple asserts.",
    "file": "",
    "files_checked": [
      "tests/test_base_conflict_merge_instruction.py",
      "tests/test_base_staleness_pushed_branch.py",
      "tests/test_pushed_tip_rewrite_guard.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 769,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The diff adds a base_pin threaded into staleness_record and one update_task call that writes task.context only; no status transition is written via update_task(validate=False), so the statement holds for the changed code.",
    "evidence": "The only update_task call in the diff is `await self.store.update_task(task)` in `_refresh_stale_base`, which persists `task.context['base_staleness']` (a context payload), not a status, and passes no `validate=False`. No new/modified code performs a status write at all.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/blockers/wake.py",
      "src/no_human/core/base_staleness.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/prompt_blocks.py"
    ],
    "line": 3849,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 743,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
