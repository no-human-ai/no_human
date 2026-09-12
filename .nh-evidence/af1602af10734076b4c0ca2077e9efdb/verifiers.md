# Verifiers

_Harness-captured record for task `af1602af`, commit `a14de447adf09586040f6961431801afa3c5190b` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All nine new test functions contain assert statements; the changes to test_readme_claims.py and test_structural_budget.py only edit module-level data tables (CITATION_TABLE, FROZEN_FILE_LINES), not test functions, so no assertion-less test was added or modified.",
    "evidence": "Every added test function ends with assert statements, e.g. test_bracketed_text_round_trips_byte_exact_in_the_database: 'assert reread.title == title ... assert json.loads(row[2]) == acs'; test_title_with_brackets_survives_the_render: 'assert f\"title: {t.title}\" in result.output'.",
    "file": "tests/test_task_show_preserves_brackets.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_task_show_preserves_brackets.py"
    ],
    "line": 96,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 614,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
