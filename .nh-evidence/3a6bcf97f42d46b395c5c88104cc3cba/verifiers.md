# Verifiers

_Harness-captured record for task `3a6bcf97`, commit `ffdee1b25051c81bae8e87ef66f4fb7e898cac2d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "The only functional changes are the new test file (all test_ functions contain asserts) and pure data-table edits in test_readme_claims.py and test_structural_budget.py (no test functions modified). The statement holds.",
    "evidence": "Every test_* function in the new tests/test_task_retitle.py contains assert statements, e.g. test_retitle_shows_through_task_show has `assert result.exit_code == 0` and `assert \"title: new title\" in shown.output`; test_commit_subject_helper_matches_orchestrator has four asserts.",
    "file": "tests/test_task_retitle.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_task_retitle.py"
    ],
    "line": 96,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 445,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the added/modified code writes a task status; the new update_task_title only updates the title column and there is no update_task(validate=False) status write introduced.",
    "evidence": "The only new write path added is `update_task_title`, whose body is `UPDATE tasks SET title = ?, updated_at = ? WHERE id = ?` \u2014 it touches only title/updated_at, never status, and does not call update_task or use validate=False. The other changes (commit_subject, _commit_message) do no DB writes at all.",
    "file": "src/no_human/core/db.py",
    "files_checked": [
      "src/no_human/core/db.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/task.py"
    ],
    "line": 2177,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 469,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
