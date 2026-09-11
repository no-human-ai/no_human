# Verifiers

_Harness-captured record for task `2177586d`, commit `b561adf05b16927ab4cb74b3f5acb6784c90b339` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added and modified test functions across the three files include at least one assertion (assert statements or mock assertion helpers such as assert_awaited_once/assert_not_awaited). Non-test helpers like the stub reviewer classes are not test functions and are not in scope.",
    "evidence": "Every added/modified test function contains assertions, e.g. test_pre_review_red_run_shows_the_reviewer_the_base_tree_split has newly_mock.assert_awaited_once() and assert outcome.status is TaskStatus.FAILED; test_base_attribution_is_asked_once_so_the_two_paths_cannot_disagree has assert newly_mock.await_count == 1; test_gate_review_hands_the_backend_a_prompt_with_the_base_tree_attribution has assert backend.prompts and assert \"Already red on the base tree\" in prompt.",
    "file": "",
    "files_checked": [
      "tests/test_pre_review_red_reaches_coder.py",
      "tests/test_reviewer.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1206,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new/modified code writes a task status \u2014 the change is confined to pre-review test-attribution evidence and per-round memoization caches, with no update_task(validate=False) call anywhere in the diff.",
    "evidence": "The diff adds test-attribution caches and helpers (_owned_failing_tests_once, _newly_failing_vs_base_once, _pre_review_base_attribution, pre-review evidence bounding); none of the new or modified lines call update_task at all, and there is no occurrence of validate=False.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 513,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
