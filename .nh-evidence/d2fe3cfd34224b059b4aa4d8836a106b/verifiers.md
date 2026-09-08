# Verifiers

_Harness-captured record for task `d2fe3cfd`, commit `a5efb2ceae1e4bd92e2b9e8c2c19c5e6cc63809d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All eight added test functions in the new file contain assert statements; the changes to the other test files touch only data dicts/comments (GIT_SUBCOMMANDS, CITATION_TABLE, FROZEN_FILE_LINES), not any test function body.",
    "evidence": "Every new test function (e.g. test_delivery_fast_forwards_stale_branch) contains multiple assert statements such as `assert calls, \"open_pr was never called\"` and `assert attempts[-1][\"status\"] == \"succeeded\"`",
    "file": "tests/test_delivery_fast_forward.py",
    "files_checked": [
      "tests/test_delivery_fast_forward.py",
      "tests/test_egress_allowlist.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py"
    ],
    "line": 213,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 874,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code in this diff writes a task status; the changes are confined to delivery-sha verification, so there is no update_task(validate=False) status write to violate the rule. The statement holds vacuously.",
    "evidence": "The diff only adds/modifies sha-reconciliation helpers (_review_history_records, _passing_review_shas_in_order, _reconcile_remote_branch, _ahead_reviewed_candidate, _assert_delivery_sha); none of them call update_task or write a task status at all.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 500,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
