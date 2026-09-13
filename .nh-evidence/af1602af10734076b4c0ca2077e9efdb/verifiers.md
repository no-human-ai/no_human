# Verifiers

_Harness-captured record for task `af1602af`, commit `f054fc76e32a2f74e24fbbaa47824b8bed7ff392` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 11 added test functions assert at least once, and no test functions were modified in the other two files (only module-level data constants), so the statement holds.",
    "evidence": "Every new test function in test_task_show_preserves_brackets.py contains assert statements, e.g. 'assert result.exit_code == 0' and 'assert \"never_push_to=[main,master]\" in result.output'; the edits to test_readme_claims.py and test_structural_budget.py only changed data tables (CITATION_TABLE, FROZEN_FILE_LINES), not any test function body.",
    "file": "tests/test_task_show_preserves_brackets.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_task_show_preserves_brackets.py"
    ],
    "line": 105,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 681,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
