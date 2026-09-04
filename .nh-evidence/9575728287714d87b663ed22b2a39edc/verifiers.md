# Verifiers

_Harness-captured record for task `95757282`, commit `b26009e1c7b44aab9e77b80b1c5039704b01bba7` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new/modified test functions across the five files include at least one assertion (assert or pytest.raises); helper functions like _orch and _plain_repo are not test functions and need none.",
    "evidence": "Every added/modified test function contains asserts or pytest.raises, e.g. test_base_commits_are_excluded_for_main ends with `assert offenders == []` and test_commit_identities_blocks_injection uses `with pytest.raises(GitError):`",
    "file": "",
    "files_checked": [
      "tests/test_agent_commit_identity_enforced.py",
      "tests/test_push_hook_guard.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_vcs_git_ls_remote_exact.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 960,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change writes only an attempt-level audit field (base_pin_sha) via update_attempt and never touches task status, so it does not bypass set_status with an unvalidated update_task call.",
    "evidence": "The only DB writes added by the diff are `await self.store.update_attempt(attempt_id, base_pin_sha=base_pin)` and the new `base_pin_sha` column in db.py; no new or modified line calls `update_task` at all, let alone with `validate=False`, and no task status is written by the changed code.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/db.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 4814,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1097,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
