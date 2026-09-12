# Verifiers

_Harness-captured record for task `f7874965`, commit `1eb806515403f98df444f057829c3bf566e55d06` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions in the new and modified files contain at least one assertion, either directly (assert / assert-in-helper) or via the assertion helpers _assert_renders_unknown/_assert_renders_split; no assertion-free test was introduced.",
    "evidence": "Every added/modified test_* function ends in either explicit assert statements or a call to an assertion helper (e.g. test_base_run_errored_renders_unknown calls _assert_renders_unknown; test_the_split_is_computed_once_and_shared_with_billing has `assert owned_mock.await_count == 1`).",
    "file": "tests/test_base_check_unknown_renders_unknown.py",
    "files_checked": [
      "tests/test_base_check_unknown_renders_unknown.py",
      "tests/test_base_tree_gate.py",
      "tests/test_pre_review_red_attribution.py",
      "tests/test_pre_review_red_reaches_coder.py",
      "tests/test_structural_budget.py"
    ],
    "line": 245,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1315,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The added code (e.g. _handle_pre_review_red) only calls update_attempt to store test_results; it never writes a task status, never calls update_task, and never passes validate=False, so the statement holds for the changed code.",
    "evidence": "The only persistence calls in the new/modified code are `await self.store.update_attempt(attempt_id, test_results=_bounded_test_results({...}))` \u2014 writing test_results, not a task status. There are no calls to `update_task` and no `validate=False` argument anywhere in the diff.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 13995,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 591,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
