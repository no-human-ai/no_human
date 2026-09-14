# Verifiers

_Harness-captured record for task `9c59b93c`, commit `d9acad71e658db759e0366c3192e9f00baa66eb3` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added/modified test_* functions (across the three files) contain at least one assert or pytest.raises/skip; non-test helpers like _mkvenv/_now_denied are not test functions and are exempt.",
    "evidence": "Every added/modified test function contains assertions, e.g. test_installing_into_ones_own_worktree_venv_stays_allowed ends with `assert d.allow, f\"own-worktree install must stay allowed: {d.reason}\"`",
    "file": "tests/test_case_fold_sweep.py",
    "files_checked": [
      "tests/test_case_fold_sweep.py",
      "tests/test_exec_names.py",
      "tests/test_venv_install_guard.py"
    ],
    "line": 249,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1212,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
