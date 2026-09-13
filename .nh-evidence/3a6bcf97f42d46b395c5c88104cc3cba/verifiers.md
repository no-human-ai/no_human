# Verifiers

_Harness-captured record for task `3a6bcf97`, commit `f7790964a6507d7c5e5bca51d03c5475bd627ebf` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions carry at least one assert statement, and the changes to the two existing test files touch only module-level data, not test bodies. The statement holds.",
    "evidence": "Every added test function in tests/test_task_retitle.py contains assertions, e.g. test_retitle_shows_through_task_show has `assert result.exit_code == 0` and `assert \"title: new title\" in shown.output`; the diffs to test_readme_claims.py and test_structural_budget.py only edited data tables (CITATION_TABLE / FROZEN_FILE_LINES), not test function bodies.",
    "file": "tests/test_task_retitle.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_task_retitle.py"
    ],
    "line": 108,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 538,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "All new/modified writers (update_task_title and the update_task/update_task_columns UPDATEs) touch title/context/metadata only and never write the status column, so no status change bypasses set_status. The change is consistent with the stated invariant.",
    "evidence": "The new update_task_title writes only `title = ?, context = json_set(...), updated_at = ?` (no status column), and the modified update_task/update_task_columns SET clauses list external_id, source, title (CASE), description, ..., context, plan, config, updated_at \u2014 status is absent. No modified code calls update_task with validate=False to write a status.",
    "file": "src/no_human/core/db.py",
    "files_checked": [
      "src/no_human/core/db.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/task.py"
    ],
    "line": 2216,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 771,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
