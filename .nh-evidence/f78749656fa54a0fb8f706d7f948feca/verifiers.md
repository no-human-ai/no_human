# Verifiers

_Harness-captured record for task `f7874965`, commit `cbe84e168c27a6dccec274bf20f399702b93b4fa` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new tests across the three test files use assertion helpers (_assert_renders_unknown/_assert_renders_split) or direct asserts, and the two modified tests in test_pre_review_red_reaches_coder.py retain explicit assert/mock-assertion statements. No test function lacks an assertion.",
    "evidence": "Every added/modified test_ function contains assertions or assertion-helper calls, e.g. test_base_run_errored_renders_unknown calls _assert_renders_unknown (which runs `assert newly is None`, `assert new_ids == []`, etc.), and test_the_split_is_computed_once_and_shared_with_billing has `assert owned_mock.await_count == 1`.",
    "file": "",
    "files_checked": [
      "tests/test_base_check_unknown_renders_unknown.py",
      "tests/test_base_tree_gate.py",
      "tests/test_pre_review_red_attribution.py",
      "tests/test_pre_review_red_reaches_coder.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 760,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The changes only add/move `update_attempt(..., test_results=...)` writes for the pre-review test-result column and never touch task status transitions, so nothing bypasses set_status.",
    "evidence": "The only new/modified store writes in the diff are `await self.store.update_attempt(attempt_id, test_results=...)` calls (in `_handle_pre_review_red` and the removed inline block); no new or modified code calls `update_task` with `validate=False`, and no task status is written.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 921,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
