# Verifiers

_Harness-captured record for task `0ff9125c`, commit `71a15a81480c3a01590b5415927aaefcc102f167` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 17 added test functions each carry at least one assert; the changes to test_egress_allowlist.py and test_structural_budget.py are data-table edits, not test-function bodies, so no assertion-free test was added or modified.",
    "evidence": "Every test_* function in the new tests/test_citation_drift_preflight.py contains assert statements, e.g. test_should_run_false_when_convention_absent: `assert citation_drift.should_run(tmp_path) is False`; the other two diffs only add entries to data dicts (ALLOWLIST, FROZEN_*), adding/modifying no test functions.",
    "file": "tests/test_citation_drift_preflight.py",
    "files_checked": [
      "tests/test_citation_drift_preflight.py",
      "tests/test_egress_allowlist.py",
      "tests/test_structural_budget.py"
    ],
    "line": 199,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1081,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The added citation-drift preflight code never writes a task status; it only calls store.update_attempt for commit_sha and emits events, so no unvalidated status write is introduced.",
    "evidence": "The diff's new/modified code contains no update_task(...) calls at all; the only store write added is `await self.store.update_attempt(attempt_id, commit_sha=commit.sha)`, which writes an attempt's commit_sha, not a task status, and passes no validate=False.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 509,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
