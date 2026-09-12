# Verifiers

_Harness-captured record for task `e46707ae`, commit `19741622aed4d90f6fe18f4cae8ec9112c5e56e8` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added or changed in this diff carry at least one assertion; the non-assertion additions (_advance_trunk, _recut, and the CITATION_TABLE/FROZEN_*_LINES data edits) are helpers or data, not test functions.",
    "evidence": "Every added/modified test function contains asserts, e.g. test_finalize_records_the_merge_base_it_measured_against has 'assert verdict.get(\"base_sha\") == expected_base' and 'assert verdict.get(\"tests_green\") is True'; the five new staleness tests in test_approve_ready_cli.py and the modified test_as_dict_round_trips_through_json_dumps all use assert statements.",
    "file": "tests/test_merge_policy_wiring.py",
    "files_checked": [
      "tests/test_approve_ready_cli.py",
      "tests/test_merge_policy.py",
      "tests/test_merge_policy_wiring.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py"
    ],
    "line": 1092,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 718,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified line writes a task status at all \u2014 no update_task(validate=False) and no set_status call appears in the diff \u2014 so the statement holds vacuously; the added code is purely advisory merge-policy fact plumbing.",
    "evidence": "The diff only adds read-only advisory fields (base_sha, tests_green) to GateFacts/PolicyVerdict, a pure helper stale_base_reason(), and computes policy_base_sha via repo.merge_base_with_trunk in the orchestrator; there are no update_task(...) calls and no task-status writes anywhere in the changed code.",
    "file": "",
    "files_checked": [
      "src/no_human/core/merge_policy.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 602,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
