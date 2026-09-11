# Verifiers

_Harness-captured record for task `3a6bcf97`, commit `17639c826574006d57c1090a3842ea656bd79389` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added test function contains at least one assert; the other two files only modify data constants, not test bodies.",
    "evidence": "All 13 new test functions in tests/test_task_retitle.py contain assert statements, e.g. test_commit_subject_helper_matches_orchestrator: assert commit_subject(\"fix the bug\", None, \"\") == \"fix the bug\". The only changes in test_readme_claims.py and test_structural_budget.py are to data tables (CITATION_TABLE / FROZEN_FILE_LINES), not test functions.",
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
    "tokens_used": 814,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the added or modified code writes a task status; the new update_task_title is a targeted title-only UPDATE, so no status change bypasses set_status via update_task(validate=False).",
    "evidence": "The only new DB write, update_task_title, runs \"UPDATE tasks SET title = ?, updated_at = ? WHERE id = ?\" \u2014 it touches only title/updated_at, never status, and does not call update_task(validate=False). The other changes (commit_subject, _commit_message) build commit/PR subjects and write no status at all.",
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
    "tokens_used": 587,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
