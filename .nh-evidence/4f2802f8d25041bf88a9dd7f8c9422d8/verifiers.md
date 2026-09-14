# Verifiers

_Harness-captured record for task `4f2802f8`, commit `3503c8319b2e49781a4fcb84968ac1890fc4302a` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions in both test_exec_names.py (the runner-recursion/git-push/linearity tests) and test_guard.py (the two capitalised-merge/approve tests) contain at least one assert statement; the modified helper _verdicts_with_fold also carries an assert on returncode. No assertion-free test was added or modified.",
    "evidence": "Every added/modified test function contains asserts, e.g. test_every_capitalised_merge_spelling_is_denied_on_a_folding_host has `assert denied == {cmd: True for cmd in _CASE_MATRIX_ROWS}, denied`",
    "file": "tests/test_exec_names.py",
    "files_checked": [
      "tests/test_exec_names.py",
      "tests/test_guard.py",
      "tests/test_structural_budget.py"
    ],
    "line": 421,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 714,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
