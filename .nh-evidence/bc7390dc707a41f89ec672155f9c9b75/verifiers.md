# Verifiers

_Harness-captured record for task `bc7390dc`, commit `e9fb238064a882e4268592395100959284ba0f85` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new/modified test functions in test_approve_merge.py and test_profile_resolve.py contain assert statements; the changes in test_already_satisfied_landing.py only touch nested helper signatures within tests that retain their assertions, and test_readme_claims.py/test_structural_budget.py changes are module-level data only, not test functions.",
    "evidence": "Every added test function contains assertions, e.g. test_the_full_gate_runs_the_repo_profile_test_command has 'assert result.ok', 'assert result.gate == \"full\"', 'assert len(calls) == 1'; test_both_merge_callers_pass_the_profile_test_command has 'assert calls' and 'assert \"test_cmd\" in kw_names'.",
    "file": "tests/test_approve_merge.py",
    "files_checked": [
      "tests/test_already_satisfied_landing.py",
      "tests/test_approve_merge.py",
      "tests/test_profile_resolve.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py"
    ],
    "line": 2620,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1060,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is a pure profile-resolution extraction and touches no task-status writes at all, so no new/modified code bypasses set_status via update_task(validate=False).",
    "evidence": "The diff only refactors profile resolution (primary_repo_path/profile_usable_under_policy/usable_profile/resolve_test_cmd into new module profile_resolve.py). No added or modified line calls update_task, references validate=False, or writes any task status.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/profile_resolve.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 365,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
