# Verifiers

_Harness-captured record for task `6e7eb947`, commit `c17a77abb33b78247464bdb88276705af902830a` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added or modified in this diff include at least one assert statement (or pytest-style assertion). No assertion-free test was introduced.",
    "evidence": "Every added/modified test function contains assertions, e.g. test_a_no_verdict_angle_is_retried_exactly_once_then_recorded ends with `assert backend.calls == 1 + 2 * len(REVIEW_ANGLES)`",
    "file": "tests/test_review_angle_skip_visible.py",
    "files_checked": [
      "tests/test_merge_policy.py",
      "tests/test_merge_policy_wiring.py",
      "tests/test_pr_evidence.py",
      "tests/test_review_angle_skip_visible.py",
      "tests/test_review_checklist_comment.py",
      "tests/test_review_fail_closed.py",
      "tests/test_reviewer.py",
      "tests/test_structural_budget.py"
    ],
    "line": 137,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1697,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or modified code writes a task status \u2014 the change is confined to the advisory merge-policy / review-angle reporting path (merge_policy is explicitly non-binding), so no `update_task(validate=False)` status write exists to violate the transition-table discipline.",
    "evidence": "The diff touches only merge_policy.py (a new advisory `required_angles_ran` rule + GateFacts fields), orchestrator.py (threading `angles_skipped`/`angles_skipped_required` into the review-verdict dict and a PR-body row), and pr_evidence.py (a `review_angles_pin` renderer). No added or modified line calls `update_task` at all, let alone with `validate=False`; there are no task-status transitions anywhere in the change.",
    "file": "",
    "files_checked": [
      "src/no_human/core/merge_policy.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/pr_evidence.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 542,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
