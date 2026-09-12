# Verifiers

_Harness-captured record for task `af1602af`, commit `0c854e4256518fe5d242ccee49c1431559b42aa7` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added test function includes at least one assert statement, and the other two changed files only alter data constants without touching any test function body.",
    "evidence": "All 8 new test functions in test_task_show_preserves_brackets.py contain assert statements, e.g. test_description_with_brackets_survives_the_render has `assert result.exit_code == 0` and `assert \"never_push_to=[main,master]\" in result.output`; the diffs to test_readme_claims.py and test_structural_budget.py only modify data tables (CITATION_TABLE, FROZEN_FILE_LINES), not test functions.",
    "file": "tests/test_task_show_preserves_brackets.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_task_show_preserves_brackets.py"
    ],
    "line": 88,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 667,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
