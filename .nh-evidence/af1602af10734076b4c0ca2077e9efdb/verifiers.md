# Verifiers

_Harness-captured record for task `af1602af`, commit `ec467625e7407a8f65806f361eb80ef7ba14a3a8` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added test functions carry assert statements, and the other two files' changes are to module-level data (CITATION_TABLE, FROZEN_FILE_LINES), not test functions, so no assertion-free test is introduced.",
    "evidence": "Each new test function in test_task_show_preserves_brackets.py contains at least one assert, e.g. test_kind_with_brackets_survives_the_render: 'assert kind in result.output, result.output'; the diffs to test_readme_claims.py and test_structural_budget.py only edit data tables, not test bodies.",
    "file": "tests/test_task_show_preserves_brackets.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_task_show_preserves_brackets.py"
    ],
    "line": 148,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 399,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
