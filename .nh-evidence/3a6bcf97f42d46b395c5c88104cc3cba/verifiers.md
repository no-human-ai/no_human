# Verifiers

_Harness-captured record for task `3a6bcf97`, commit `83816b0d5a306ad2a2582aa7c42260c53374ff54` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 13 added test functions carry at least one assert; the diffs in test_readme_claims.py and test_structural_budget.py only touch module-level data constants, not test functions, so the statement holds.",
    "evidence": "Every test_* function in the new tests/test_task_retitle.py contains assert statements, e.g. test_retitle_shows_through_task_show: 'assert result.exit_code == 0, result.output' and 'assert \"title: new title\" in shown.output'; the two modified files only changed data tables (CITATION_TABLE, FROZEN_FILE_LINES), not any test function body.",
    "file": "tests/test_task_retitle.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_task_retitle.py"
    ],
    "line": 105,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 879,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or modified code writes a task status at all; the added update_task_title is a targeted title-only UPDATE, and the other diffs are pure commit-subject formatting, so no status write bypasses set_status.",
    "evidence": "The only new DB writer, update_task_title, runs \"UPDATE tasks SET title = ?, updated_at = ? WHERE id = ?\" \u2014 it touches only title/updated_at, writes no status, and does not call update_task or pass validate=False. The orchestrator/task.py changes only add commit_subject() for commit/PR subject formatting.",
    "file": "src/no_human/core/db.py",
    "files_checked": [
      "src/no_human/core/db.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/task.py"
    ],
    "line": 2176,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 526,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
