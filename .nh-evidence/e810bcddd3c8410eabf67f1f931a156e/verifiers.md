# Verifiers

_Harness-captured record for task `e810bcdd`, commit `e86424009932000c77dfe719c56973bc44f8df2b` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added/modified test functions carry at least one assertion or pytest.raises block; the non-asserting functions in the diff (_orch, _task, _git, _make_pushed_branch, _merge_instruction_call_sites, etc.) are helpers, not test functions.",
    "evidence": "Every test_* function added contains assert statements or a pytest.raises block, e.g. test_the_rules_block_carries_the_merge_instruction_only_when_asked asserts baseline==unset, and test_reconcile_remote_branch_raises_the_exact_string_the_guard_quotes uses `with pytest.raises(ReviewedShaMismatch)` plus asserts.",
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
    "tokens_used": 979,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The modified code persists a `base_staleness` context payload, not a task status, and the update_task call uses no validate=False. No new/modified code performs a status write bypassing set_status.",
    "evidence": "The only update_task call in the diff writes context: `ctx[\"base_staleness\"] = staleness_record(...)` / `task.context = ctx` / `await self.store.update_task(task)` \u2014 no `status` field is set and no `validate=False` argument is passed.",
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
    "tokens_used": 639,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
