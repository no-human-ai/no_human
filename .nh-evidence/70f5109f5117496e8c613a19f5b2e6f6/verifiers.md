# Verifiers

_Harness-captured record for task `70f5109f`, commit `6f01a34cea8c0bcea3a1e7749811f9224dfc1c13` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added or changed in this diff contain assert statements; the modified list_* tests only gained a db_path line but retained their existing asserts, and the retry-window test that dropped pytest.raises still asserts on bounded_calls/timeouts.",
    "evidence": "Every added/modified test carries assertions, e.g. test_no_verdict_persists_on_the_attempt_row_and_in_task_context: 'assert len(persisted) == 1' and 'assert persisted[0][\"no_verdict\"] is True'; test_list_without_a_readable_db_still_exits_0_with_zero_counts: 'assert v[\"runs\"] == 0'. The only non-test addition (_seed_attempt_with_verifier_results) is a helper, not a test function.",
    "file": "",
    "files_checked": [
      "tests/test_verifier_quota_park.py",
      "tests/test_verifiers_cli.py",
      "tests/test_verifiers_gate.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1060,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The modified code neither adds nor changes any task-status write; it merely converts an unavailable-verifier escalation into an advisory pass-through, so there is no update_task(validate=False) status write to violate the statement.",
    "evidence": "The diff only edits comments and removes a `raise exc` (ReviewerUnavailable) block in the verifier-handling path of Orchestrator; it adds no status-writing code at all \u2014 no call to update_task(validate=False) or any status write appears in the added lines.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 13917,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 512,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
