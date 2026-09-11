# Verifiers

_Harness-captured record for task `37b0fb67`, commit `c9054e4201f8819deaeb284d28826710bad87240` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All nine added test functions (two in test_guard.py, seven in test_venv_install_guard.py) contain at least one assert statement; the only non-asserting additions are helpers (_unreadable, _oserror_swallowing_call_sites) which are not test functions, and test_structural_budget.py only changed a frozen data value.",
    "evidence": "Every added test function contains asserts, e.g. test_an_unreadable_venv_pyvenv_cfg_still_denies_the_install: `assert before is not None`, `assert primary_venv in before`, `assert inside is not None`, `assert not d.allow`, `assert after is not None`",
    "file": "tests/test_venv_install_guard.py",
    "files_checked": [
      "tests/test_guard.py",
      "tests/test_structural_budget.py",
      "tests/test_venv_install_guard.py"
    ],
    "line": 585,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 783,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
