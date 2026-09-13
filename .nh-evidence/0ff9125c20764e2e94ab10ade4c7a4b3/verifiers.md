# Verifiers

_Harness-captured record for task `0ff9125c`, commit `a3e3beec80b1062e6f8a5089c5ae199b7de8e970` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added test functions carry assert statements or pytest.raises blocks; the backend helper classes' run() methods are not test functions and the edits to the other two files only add data-dict entries, not test functions.",
    "evidence": "Every test_ function in the new file contains at least one assertion, e.g. test_revert_worktree_writes_unguarded_requires_component_argument uses `with pytest.raises(TypeError):` and test_should_run_false_when_convention_absent uses `assert citation_drift.should_run(tmp_path) is False`.",
    "file": "tests/test_citation_drift_preflight.py",
    "files_checked": [
      "tests/test_citation_drift_preflight.py",
      "tests/test_egress_allowlist.py",
      "tests/test_structural_budget.py"
    ],
    "line": 176,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1246,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code writes a task status via update_task(validate=False); the added code only records an attempt's commit_sha and never bypasses the status transition table.",
    "evidence": "The only store mutation added in the new code is `await self.store.update_attempt(attempt_id, commit_sha=commit.sha)` \u2014 an attempt commit_sha write, not a task status write; the diff contains no `update_task` call and no `validate=False` anywhere.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 9750,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 587,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
