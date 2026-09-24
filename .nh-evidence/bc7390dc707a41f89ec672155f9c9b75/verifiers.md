# Verifiers

_Harness-captured record for task `bc7390dc`, commit `a5e49997b1c123e1d576532983e695f072010e41` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All newly added test functions in test_approve_merge.py and test_profile_resolve.py contain assert statements; the modified functions in test_already_satisfied_landing.py only changed nested helper signatures while retaining their existing assertions, and the changes to test_readme_claims.py/test_structural_budget.py touch module-level data tables, not test-function bodies.",
    "evidence": "Every added/modified test_* function contains assertions, e.g. test_the_full_gate_runs_the_repo_profile_test_command has `assert result.ok`, `assert result.gate == \"full\"`, `assert len(calls) == 1`; test_a_runner_that_cannot_start_fails_closed_naming_the_runner has `assert not result.ok`; test_both_merge_callers_pass_the_profile_test_command has `assert calls` and `assert \"test_cmd\" in kw_names`.",
    "file": "",
    "files_checked": [
      "tests/test_already_satisfied_landing.py",
      "tests/test_approve_merge.py",
      "tests/test_profile_resolve.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1257,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is a pure extraction of profile/test-command resolution logic and touches no task status transitions, so it neither introduces nor modifies any update_task(validate=False) status write.",
    "evidence": "The diff only refactors profile resolution (primary_repo_path, profile_usable_under_policy, usable_profile, resolve_test_cmd) into a new module core/profile_resolve.py; no added or modified line references update_task, validate=False, or any task status write.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/profile_resolve.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 323,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
