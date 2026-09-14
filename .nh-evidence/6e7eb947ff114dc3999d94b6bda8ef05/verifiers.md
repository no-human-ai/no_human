# Verifiers

_Harness-captured record for task `6e7eb947`, commit `0612885770b68bf94545b20f7857a84954a7a45d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added or modified in this diff contain at least one assert statement (or assertion helper). The only non-assertion change in test_structural_budget.py is to the FROZEN_FILE_LINES data dict, not a test function.",
    "evidence": "Every added test in the new file test_review_angle_skip_visible.py contains assertions, e.g. test_a_no_verdict_angle_is_recorded_not_passed_and_never_green has `assert len(notes) == len(REVIEW_ANGLES)` and `assert all(i.passed is False for i in notes)`; every modified function in the other files (e.g. test_summary_ready_shape, test_rule_names_has_nine_entries..., test_evidence_gathered_once_backs_every_section, test_angle_timeout_never_fails_the_gate) retains at least one assert statement.",
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
    "line": 111,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1231,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code writes a task status at all \u2014 the changes are confined to merge-policy rules and PR-body evidence rendering, so there is no update_task(validate=False) status write to be concerned about; the statement holds vacuously for this diff.",
    "evidence": "The diff only touches merge_policy.py (adds required_angles_ran rule/GateFacts fields), orchestrator.py (skipped_angles reporting into review_verdict dict + PR body row), and pr_evidence.py (review_angles_pin). None of these hunks call update_task, set_status, or write any task status field.",
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
    "tokens_used": 418,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
