# Verifiers

_Harness-captured record for task `e83b0b6d`, commit `86c25f59d6975ee86f913aebaa0fb124a08d595b` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added test function (the three diverged-branch tests and the protected-branch merge test) and the modified test_no_force_push_introduced_by_this_fix contain assert statements and/or pytest.raises blocks; the readme/budget files only changed data tables, no test functions.",
    "evidence": "test_no_divergence_advisory_when_the_branch_has_no_remote_tip ends with `assert advisories == []` and `assert \"diverged\" not in t.context[\"base_staleness\"]`; test_merge_base_into_branch_refuses_a_protected_branch_without_touching_refs uses `with pytest.raises(ProtectedBranch, ...)` plus multiple asserts",
    "file": "tests/test_base_staleness_pushed_branch.py",
    "files_checked": [
      "tests/test_base_staleness_pushed_branch.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py"
    ],
    "line": 348,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 663,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The modified code writes task context (the base_staleness record with the new diverged flag), not a task status, and never calls update_task with validate=False; no status transitions are introduced or bypassed.",
    "evidence": "The only store write in the changed code is `await self.store.update_task(task)` persisting `task.context['base_staleness']`; it passes no `validate=False` argument and sets no status field \u2014 the change only adds a `diverged` flag to the context payload.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/base_staleness.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 3406,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 639,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
