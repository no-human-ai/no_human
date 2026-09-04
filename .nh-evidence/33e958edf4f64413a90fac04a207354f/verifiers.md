# Verifiers

_Harness-captured record for task `33e958ed`, commit `37ddb4f2a5561943cbe6122478085ae72e18850e` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All five new test functions in test_metrics.py contain assert statements; the _receipt helper is not a test, and the test_structural_budget.py change only edits a frozen-lines constant, not a test function.",
    "evidence": "Each added test (e.g. test_empty_db_yields_a_null_rate_not_a_crash, test_rate_counts_attempts_with_at_least_one_receipt) contains assert statements such as `assert r == {\"attempts\": 0, ...}` and `assert r[\"attempts\"] == 3`.",
    "file": "tests/test_metrics.py",
    "files_checked": [
      "tests/test_metrics.py",
      "tests/test_structural_budget.py"
    ],
    "line": 281,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 543,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The diff adds a single read-only SQL metrics function and modifies no write paths, so there is no update_task(validate=False) status write to contradict the statement.",
    "evidence": "The only added code is `verification_receipt_rate`, a read-only aggregate: `SELECT COUNT(*) ... FROM attempts a WHERE a.status IN ('succeeded','failed')` \u2014 no update_task, no set_status, no writes at all.",
    "file": "src/no_human/core/metrics.py",
    "files_checked": [
      "src/no_human/core/metrics.py"
    ],
    "line": 384,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 300,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
