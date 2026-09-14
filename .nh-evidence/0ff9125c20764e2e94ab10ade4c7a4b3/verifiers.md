# Verifiers

_Harness-captured record for task `0ff9125c`, commit `a07b942d962e68934d9d31a106ce4cc2748057ab` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added test functions include at least one assert, pytest.raises block, or assertion helper; the modified data files (test_egress_allowlist.py, test_structural_budget.py) only edit module-level dict constants, not test functions.",
    "evidence": "Every `def test_*`/`async def test_*` in tests/test_citation_drift_preflight.py contains assertions; e.g. test_revert_worktree_writes_unguarded_requires_component_argument uses `with pytest.raises(TypeError):` and all others use `assert` statements (test_should_run_false_when_convention_absent: `assert citation_drift.should_run(tmp_path) is False`).",
    "file": "tests/test_citation_drift_preflight.py",
    "files_checked": [
      "tests/test_citation_drift_preflight.py",
      "tests/test_egress_allowlist.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1414,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The new citation-drift code writes no task status at all; its only persistence is update_attempt(commit_sha=...) and event emissions, so it never bypasses set_status/the transition table via update_task(validate=False).",
    "evidence": "The only store mutation added is `await self.store.update_attempt(attempt_id, commit_sha=commit.sha)` \u2014 an attempt row update, not a task status write. No `update_task` call (with or without `validate=False`) appears anywhere in the diff; all `status=` occurrences are keyword args to `self.emit(...)` event narration (e.g. `self.emit(\"citation_drift\", ..., status=outcome.status.value)`), not task-status transitions.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 9713,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 822,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
