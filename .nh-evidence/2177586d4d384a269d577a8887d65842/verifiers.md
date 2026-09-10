# Verifiers

_Harness-captured record for task `2177586d`, commit `80fffff74b7f211c4e7d398ed41e62495e75ad45` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added or modified in this diff contain at least one assertion, assertion helper call, or mock assert_* call; none is assertion-free.",
    "evidence": "Every added/modified test function contains asserts, e.g. new test_pre_review_red_run_shows_the_reviewer_the_base_tree_split has 'newly_mock.assert_awaited_once()', 'assert outcome.status is TaskStatus.FAILED', 'assert call[\"test_attribution\"] == \"attributed\"'; test_owned_red_id_is_never_shown_as_pre_existing_and_still_fails asserts owned_id membership; test_pre_review_attribution_ids_are_bounded_at_the_call_site asserts cap/dropped counts; modified test_flaky_non_owned_red_run_not_blamed_when_review_fails_unrelated uses assert_awaited_once/assert_not_awaited.",
    "file": "tests/test_pre_review_red_reaches_coder.py",
    "files_checked": [
      "tests/test_pre_review_red_reaches_coder.py",
      "tests/test_structural_budget.py"
    ],
    "line": 762,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 867,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the added or modified code writes a task status; the change is purely reviewer-facing attribution evidence, so there is no update_task(validate=False) path to bypass the transition table.",
    "evidence": "The entire diff concerns evidence-only test-attribution (new `_pre_review_base_attribution` helper, `_bounded_test_results` id-list truncation, and pre-review red checklist/reviewer-kwargs plumbing). It contains no `update_task` call, no `validate=False`, and no `set_status` call \u2014 no task-status write of any kind.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1092,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
