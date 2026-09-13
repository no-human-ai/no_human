# Verifiers

_Harness-captured record for task `3a6bcf97`, commit `289ca5aafa4d4ff87ce201f169cef7ba83d35757` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All newly added test functions in the new test file carry at least one assert (or pytest-style equality assert); the other two files' diffs modify only module-level data constants, adding/modifying no test functions.",
    "evidence": "Every added test_* function in tests/test_task_retitle.py contains assert statements, e.g. test_retitle_shows_through_task_show: 'assert result.exit_code == 0, result.output' and 'assert \"title: new title\" in shown.output'. The changes to test_readme_claims.py and test_structural_budget.py only edit data tables (CITATION_TABLE / FROZEN_FILE_LINES), not test function bodies.",
    "file": "tests/test_task_retitle.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_task_retitle.py"
    ],
    "line": 111,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 672,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The changes add a commit_subject helper, a title-preservation CASE in update_task/update_task_columns, and a targeted update_task_title that writes only title/context/updated_at. None of the new or modified code writes a task status, so status transitions still flow exclusively through set_status.",
    "evidence": "The new update_task_title does `UPDATE tasks SET title = ?, context = json_set(...), updated_at = ?` \u2014 it writes only title/context/updated_at, never status; update_task/update_task_columns modifications only add title-preservation logic and do not touch status (status remains excluded per their docstrings). No new/modified code calls update_task with validate=False to write status.",
    "file": "",
    "files_checked": [
      "src/no_human/core/db.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/task.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 731,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
