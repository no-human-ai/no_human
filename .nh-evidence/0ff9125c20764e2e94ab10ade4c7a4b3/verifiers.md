# Verifiers

_Harness-captured record for task `0ff9125c`, commit `0ce237c405e3decd752a05faa222a41cb30e8bb4` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Each added test function has at least one assertion \u2014 plain asserts throughout, plus a pytest.raises block in the component-argument mutation test. Helper functions and scripted-backend classes are not test functions and are correctly excluded.",
    "evidence": "Every test_* function contains assertions; e.g. test_revert_worktree_writes_unguarded_requires_component_argument uses `with pytest.raises(TypeError):`, and all others use `assert` statements (e.g. `assert citation_drift.should_run(tmp_path) is False`).",
    "file": "tests/test_citation_drift_preflight.py",
    "files_checked": [
      "tests/test_citation_drift_preflight.py",
      "tests/test_egress_allowlist.py",
      "tests/test_structural_budget.py"
    ],
    "line": 155,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 979,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The added citation-drift preflight touches task status nowhere; its sole persistence call writes an attempt's commit_sha, so no code bypasses set_status or calls update_task with validate=False.",
    "evidence": "The only store mutation added in the diff is `await self.store.update_attempt(attempt_id, commit_sha=commit.sha)`, which records a commit SHA on an attempt \u2014 not a task status. There is no `update_task(...)` call, and no `validate=False` argument, anywhere in the new or modified code.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 521,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
