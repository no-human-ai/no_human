# Verifiers

_Harness-captured record for task `2370a036`, commit `ff24ede7c4210bf501fdbff76427ec15f477b5f5` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added or modified carry at least one assertion or pytest.raises block; the other two files' diffs touch only module-level data constants, not test functions.",
    "evidence": "Every added test function in test_rework_after_rejection_reconverge.py contains assert statements and/or pytest.raises blocks, e.g. test_is_rework_after_rejection_requires_pr_branch_match_and_feedback (asserts), test_reconverge_refuses_unrelated_histories_and_leaves_the_branch_untouched (with pytest.raises(GitError...) + assert); the diffs to test_readme_claims.py (CITATION_TABLE data) and test_structural_budget.py (FROZEN_FILE_LINES data/comment) modify module-level data, not test functions.",
    "file": "tests/test_rework_after_rejection_reconverge.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_rework_after_rejection_reconverge.py",
      "tests/test_structural_budget.py"
    ],
    "line": 145,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 975,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the added or modified code calls update_task with validate=False; task-status transitions go through the escalation/blocker helpers and the only status write is store.update_attempt (an attempt, not a task).",
    "evidence": "The diff introduces no update_task calls at all. New/modified code writes only via store.update_attempt(attempt_id, status=...) (attempt status, not task status) and store.merge_context(...), and routes status changes through _escalate / _escalate_reviewed_sha_mismatch / _raise_blocker. No update_task(..., validate=False) is present.",
    "file": "",
    "files_checked": [
      "src/no_human/blockers/__init__.py",
      "src/no_human/blockers/report.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 566,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
