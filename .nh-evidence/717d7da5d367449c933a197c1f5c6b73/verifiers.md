# Verifiers

_Harness-captured record for task `717d7da5`, commit `ce2630bac3094afbd7b013175c4e9deaa9763620` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions across the three test files (and the single added function in test_base_staleness_pushed_branch.py) contain assert statements or pytest.raises blocks; test_structural_budget.py only changed comment/data lines, not test functions.",
    "evidence": "Every added/modified test function contains assert statements, e.g. test_the_rules_block_carries_the_merge_instruction_only_when_asked has 'assert baseline == unset', 'assert conflicted != baseline', etc.; test_no_git_subprocess_for_non_rewrite_commands has 'assert calls[\"n\"] == 0' and 'assert calls[\"n\"] > 0'.",
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
    "tokens_used": 537,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The new/modified code only writes task.context via update_task; it does not change any task status, so it neither uses validate=False for a status write nor bypasses set_status.",
    "evidence": "The only update_task call in the diff is `await self.store.update_task(task)` in _refresh_stale_base, which persists task.context['base_staleness'] only \u2014 no status field is set and no validate=False argument appears anywhere in the changed code.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/base_staleness.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/prompt_blocks.py"
    ],
    "line": 3547,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1173,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
