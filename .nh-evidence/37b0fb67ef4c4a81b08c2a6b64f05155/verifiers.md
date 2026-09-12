# Verifiers

_Harness-captured record for task `37b0fb67`, commit `9897ab118d88092fbeda864a3c82fe735ebf4ea8` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new/modified test functions across both test files include explicit assert statements; the only diff to test_structural_budget.py is a frozen-dict numeric value, not a test function. Non-test helpers like _unreadable and _oserror_swallowing_call_sites are not tests.",
    "evidence": "Every added test function (e.g. test_an_unreadable_venv_pyvenv_cfg_still_denies_the_install, test_probe_distinguishes_absence_from_unreadability, test_no_changed_probe_decides_through_an_oserror_swallowing_helper, test_installs_into_the_sessions_own_worktree_venv_stay_allowed, test_a_nonexistent_installer_path_is_still_allowed_and_logged, plus the two test_protected_venvs_* functions) contains at least one assert statement.",
    "file": "",
    "files_checked": [
      "tests/test_guard.py",
      "tests/test_structural_budget.py",
      "tests/test_venv_install_guard.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 796,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
