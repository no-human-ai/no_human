# Verifiers

_Harness-captured record for task `bc7390dc`, commit `cc27598f341ca72495b523797b5a0dadb8e3a8d0` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All newly added test functions in test_approve_merge.py and test_profile_resolve.py, and the two touched test functions in test_already_satisfied_landing.py, contain assert statements; the readme/budget changes only edit data tables, not test bodies.",
    "evidence": "Every added test function contains assert statements, e.g. test_a_runner_that_cannot_start_fails_closed_naming_the_runner has 'assert not result.ok', 'assert result.step == \"tests\"', 'assert \"npm\" in result.stderr'",
    "file": "tests/test_approve_merge.py",
    "files_checked": [
      "tests/test_already_satisfied_landing.py",
      "tests/test_approve_merge.py",
      "tests/test_profile_resolve.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py"
    ],
    "line": 2551,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 908,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is a pure extraction of profile/test-command resolution logic and touches no task-status persistence path, so it neither adds nor modifies any update_task(validate=False) status write.",
    "evidence": "The diff only refactors profile resolution (primary_repo_path/profile_usable_under_policy/usable_profile/resolve_test_cmd into profile_resolve.py); no added or modified line calls update_task, set_status, or references validate=False or task status writes at all.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/profile_resolve.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 338,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
