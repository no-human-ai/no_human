# Verifiers

_Harness-captured record for task `717d7da5`, commit `c4f717d8be0c74a490d0ab6dbd8767d8c3b5a109` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions across the three test files contain assertions (assert statements or assertion-helper calls). The test_structural_budget.py change only modifies module-level FROZEN dict values, adding no test functions. No assertion-free test exists.",
    "evidence": "Every added/modified test function contains assert statements, e.g. test_the_rules_block_carries_the_merge_instruction_only_when_asked has `assert baseline == unset` and `assert \"`git merge abc123def456`\" in conflicted`",
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
    "tokens_used": 1035,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The changed code only writes task.context (the base_staleness payload), not task status, and calls update_task with no validate=False; no status transition is written, so nothing bypasses set_status.",
    "evidence": "The only persistence call added/modified is `task.context = ctx; await self.store.update_task(task)` in _refresh_stale_base, which writes task.context['base_staleness'] and never sets task.status or passes validate=False.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/base_staleness.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/prompt_blocks.py"
    ],
    "line": 3546,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1439,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
