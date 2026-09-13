# Verifiers

_Harness-captured record for task `af1602af`, commit `9c2fb35ddcaed989a210416b9976d5a21a82a16b` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added test functions carry at least one assertion; the edits to test_readme_claims.py and test_structural_budget.py only changed data tables/constants, not test-function bodies.",
    "evidence": "Every new test function in test_task_show_preserves_brackets.py contains assert statements, e.g. test_kind_with_brackets_survives_the_render ends with `assert kind in result.output, result.output`",
    "file": "tests/test_task_show_preserves_brackets.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_task_show_preserves_brackets.py"
    ],
    "line": 121,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 381,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
