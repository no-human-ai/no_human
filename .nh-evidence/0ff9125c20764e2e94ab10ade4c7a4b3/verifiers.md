# Verifiers

_Harness-captured record for task `0ff9125c`, commit `a561bc92b6afc275c345ea8802a8694d821b7a1a` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All ~39 test functions added carry at least one assertion or pytest.raises block; the helper backend classes are not test functions, and the other two diffs only add dict entries, not tests.",
    "evidence": "Every `def test_*` in tests/test_citation_drift_preflight.py contains an assert or pytest.raises, e.g. `assert citation_drift.should_run(tmp_path) is False` and `with pytest.raises(TypeError): Orchestrator._revert_worktree_writes_unguarded(...)`.",
    "file": "tests/test_citation_drift_preflight.py",
    "files_checked": [
      "tests/test_citation_drift_preflight.py",
      "tests/test_egress_allowlist.py",
      "tests/test_structural_budget.py"
    ],
    "line": 191,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1536,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The added citation-drift preflight only calls update_attempt(commit_sha=...) and emit(); it makes no status write, so there is no update_task(validate=False) transition-table bypass to flag.",
    "evidence": "The only store mutation added by the diff is `await self.store.update_attempt(attempt_id, commit_sha=commit.sha)`, which writes a commit SHA to an attempt record \u2014 not a task status. No new or modified line calls `update_task` at all, let alone with `validate=False`; the new citation-drift code ends attempts by returning `TaskOutcome`, never by writing status directly.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 9779,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 802,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
