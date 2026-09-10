# Verifiers

_Harness-captured record for task `2177586d`, commit `3a95dacba74077018328a92f87497b57223d5ec3` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added/modified test function (the new base-tree-split test, the renamed review-path test, the flaky/env_setup tests, and the runner-once test) contains assertion or mock-assertion calls; test_structural_budget.py only edits frozen data dicts, not test functions.",
    "evidence": "New test test_pre_review_red_run_shows_the_reviewer_the_base_tree_split contains 'newly_mock.assert_awaited_once()' and 'assert call[\"test_attribution\"] == \"attributed\"'; modified tests each retain/add asserts (e.g. 'newly_failing_mock.assert_awaited_once()', 'assert reviewer.snapshots == ...', 'assert owned_mock.await_count == 1').",
    "file": "tests/test_pre_review_red_reaches_coder.py",
    "files_checked": [
      "tests/test_pre_review_red_reaches_coder.py",
      "tests/test_structural_budget.py"
    ],
    "line": 641,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 849,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or modified code writes a task status at all \u2014 there are no update_task calls (let alone with validate=False), so the statement holds vacuously for this change.",
    "evidence": "The diff adds only test-attribution logic (`_pre_review_base_attribution`), a `self.emit(...)` event dict with `pre_existing_ids`/`new_ids`/`attribution` keys, a checklist item, and extra kwargs to the reviewer call; it contains no `update_task(` call and no `validate=False` argument anywhere.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 681,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
