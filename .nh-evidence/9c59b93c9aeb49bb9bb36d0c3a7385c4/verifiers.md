# Verifiers

_Harness-captured record for task `9c59b93c`, commit `e07006c96db544a35fac52f0f981cb87f61468c8` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new and modified test functions across the three test files contain at least one assert statement; the autouse fixtures and helpers (_mkvenv, _session, _decide) are not test functions and are exempt from the statement.",
    "evidence": "Every added/modified test_* function contains assert statements, e.g. test_installing_into_ones_own_worktree_venv_stays_allowed ends with `assert d.allow, ...` and test_mutating_subcommand_threads_cwd_to_the_inner_installer_skip ends with `assert subcommand == \"install\"`.",
    "file": "tests/test_case_fold_sweep.py",
    "files_checked": [
      "tests/test_case_fold_sweep.py",
      "tests/test_exec_names.py",
      "tests/test_structural_budget.py",
      "tests/test_venv_install_guard.py"
    ],
    "line": 227,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 768,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
