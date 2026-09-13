# Verifiers

_Harness-captured record for task `0ff9125c`, commit `0295e53514c594ab13a0f591248800b30d0ab5f2` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 20 test functions in the new file carry at least one assert; the changes to test_egress_allowlist.py and test_structural_budget.py only modify data dictionaries/allowlist entries, not test functions.",
    "evidence": "Every test_* function in tests/test_citation_drift_preflight.py contains assert statements, e.g. test_should_run_false_when_convention_absent: `assert citation_drift.should_run(tmp_path) is False`",
    "file": "tests/test_citation_drift_preflight.py",
    "files_checked": [
      "tests/test_citation_drift_preflight.py",
      "tests/test_egress_allowlist.py",
      "tests/test_structural_budget.py"
    ],
    "line": 178,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 999,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The added citation-drift preflight code performs no direct task status write; its only persistence call is update_attempt(commit_sha=...), and status-affecting outcomes flow back as TaskOutcome returns rather than bypassing set_status via update_task(validate=False).",
    "evidence": "The only store write added by the new code is `await self.store.update_attempt(attempt_id, commit_sha=commit.sha)` in `_citation_drift_preflight`, which sets a commit SHA on an attempt \u2014 not a task status. There is no `update_task(..., validate=False)` call anywhere in the diff; all `status=...` occurrences are keyword args to `self.emit(...)` event emissions, and status-ending flows return `TaskOutcome` objects.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 746,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
