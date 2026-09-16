# Verifiers

_Harness-captured record for task `353fb335`, commit `3d80c85bd7a17d2cfd9eb9717080528ed89bb660` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All newly added test functions across the three new test files contain assert statements or pytest.raises blocks; the diffs to test_readme_claims.py and test_structural_budget.py only edit module-level data tables, not test-function bodies, so no assertion-free test is introduced.",
    "evidence": "Every added test function contains assertions, e.g. test_a_branch_diverged_before_the_run_is_recut_and_pushed has `assert new_branch == f\"{stem}-7\"`, test_recut_happens_at_most_once_per_branch uses `with pytest.raises(ReviewedShaMismatch)`, and all audit/refusal tests carry multiple assert statements.",
    "file": "",
    "files_checked": [
      "tests/test_branch_recut_after_divergence.py",
      "tests/test_diverged_audit.py",
      "tests/test_readme_claims.py",
      "tests/test_recut_preserves_delivery_refusal.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 921,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the added/changed code writes a task status; the only status-affecting path is via _escalate, and no update_task call uses validate=False, so the statement holds.",
    "evidence": "The new/modified code in orchestrator.py persists only via `store.update_attempt(attempt_id, branch_name=branch)`, `store.merge_context(task.id, {...})`, and the pre-existing `store.update_task(task)` \u2014 none pass `validate=False`, and none set `task.status`; status transitions still route through `_escalate` (e.g. `return await self._escalate(task, str(exc), ...)`). diverged_audit.py is read-only. No `update_task(..., validate=False)` appears anywhere in the diff.",
    "file": "",
    "files_checked": [
      "src/no_human/core/diverged_audit.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1063,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
