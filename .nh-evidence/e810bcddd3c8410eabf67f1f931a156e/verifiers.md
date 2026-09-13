# Verifiers

_Harness-captured record for task `e810bcdd`, commit `9c109cbf1bbe78a5149539a992f16c2606e5787d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions across the three test files carry at least one assert or pytest.raises; the structural-budget file only changed data dicts, not test functions. No assertion-free test was introduced.",
    "evidence": "Every added/modified test_* function contains assert statements or pytest.raises blocks, e.g. test_the_rules_block_carries_the_merge_instruction_only_when_asked has 'assert baseline == unset' and test_reconcile_remote_branch_raises_the_exact_string_the_guard_quotes uses 'with pytest.raises(ReviewedShaMismatch)'.",
    "file": "tests/test_base_conflict_merge_instruction.py",
    "files_checked": [
      "tests/test_base_conflict_merge_instruction.py",
      "tests/test_base_staleness_pushed_branch.py",
      "tests/test_pushed_tip_rewrite_guard.py",
      "tests/test_structural_budget.py"
    ],
    "line": 84,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1771,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The diff changes prompt/staleness/comment text and adds a base_pin field; the single update_task call writes context (not status) with default validation, and nothing sets validate=False or bypasses set_status for a status transition.",
    "evidence": "The only update_task call in the diff is in _refresh_stale_base: `task.context = ctx; await self.store.update_task(task)` \u2014 it persists the base_staleness context, not a status, and passes no validate=False. No new/modified code writes task status at all.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/blockers/wake.py",
      "src/no_human/core/base_staleness.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/prompt_blocks.py"
    ],
    "line": 3847,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 612,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
