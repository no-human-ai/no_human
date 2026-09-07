# Verifiers

_Harness-captured record for task `3079d30a`, commit `ba03e615dbf07449ec8bf3cd5c4daf55919553b0` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added/modified test function in all four files has at least one assert statement; helpers (_check, _six_of_six_setup) and the autouse fixture are not test functions and are correctly excluded.",
    "evidence": "Every added test_ function contains assertions, e.g. test_failure_wins_and_names_failing_checks: `assert state == \"failure\"` / `assert names == (\"File inventory\",)`; test_no_delivered_github_pr_never_polls: `assert calls[\"n\"] == 0`; test_default_pr_checks_marks_required_from_a_second_gh_call: `assert by_name[\"File inventory\"][\"required\"] is True`.",
    "file": "",
    "files_checked": [
      "tests/test_ci_rollup.py",
      "tests/test_merge_policy.py",
      "tests/test_merge_policy_wiring.py",
      "tests/test_pr_watcher.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 810,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or modified code writes a task status via update_task with validate=False; the status transition uses _advance_after_review, and the merge_policy diff only adds advisory CI detail with no status writes.",
    "evidence": "The only status change in the modified orchestrator code goes through `_advance_after_review(task, TaskStatus.AWAITING_APPROVAL, ...)`; no new/modified line calls `update_task(..., validate=False)`. The merge_policy changes are purely advisory (ci_failed_checks detail string) and touch no task status.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/merge_policy.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 7227,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 676,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
