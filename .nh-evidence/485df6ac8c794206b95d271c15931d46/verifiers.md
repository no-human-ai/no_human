# Verifiers

_Harness-captured record for task `485df6ac`, commit `71cbbbcc316596824814efd017a8cbe46e9e79e8` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added or modified test function across the four files carries at least one assertion (assert or pytest.raises); the only non-asserting modification is the _make_orchestrator helper, which is not a test function.",
    "evidence": "Each added test (e.g. test_latest_failed_attempt_is_the_newest_reason_bearing_row) ends with `assert row[\"failure_reason\"] == \"third failure\"`; all new/modified test functions contain assert statements or pytest.raises.",
    "file": "tests/test_db.py",
    "files_checked": [
      "tests/test_db.py",
      "tests/test_draft_pr_force_after_rebase.py",
      "tests/test_post_pass_mechanical_rounds.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py"
    ],
    "line": 664,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 817,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The added/changed code touches attempt rows and read helpers only; there is no update_task(validate=False) call nor any task-status write bypassing set_status in this diff.",
    "evidence": "The diff's writes are Store.create_attempt(...) and store.update_attempt(attempt_id, branch_name=..., commit_sha=...); no call to update_task with validate=False appears, and no new/modified code writes a task status at all \u2014 the changes only read attempt rows (latest_review_attempt/latest_failed_attempt) and append diagnostic text.",
    "file": "",
    "files_checked": [
      "src/no_human/core/db.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 675,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
