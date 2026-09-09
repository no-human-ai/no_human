# Verifiers

_Harness-captured record for task `4e0299ad`, commit `03267ead5e5cbb8c336964f73482cf9ebe1ba15d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "The three test_* functions (test_pre_review_red_run_reaches_coder_when_review_fails_unrelated, test_pre_review_red_run_still_fails_round_when_reviewer_passes, test_build_review_prompt_carries_fixed_failing_ids_section) each contain multiple assert statements; test_structural_budget.py's diff only edits FROZEN_* dict values, not test functions.",
    "evidence": "All three added test functions contain assert statements, e.g. test_build_review_prompt_carries_fixed_failing_ids_section: `assert \"tests/test_calc.py::test_mul\" in prompt`",
    "file": "tests/test_pre_review_red_reaches_coder.py",
    "files_checked": [
      "tests/test_pre_review_red_reaches_coder.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 572,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The new pre-review-red code only emits a 'tests' event and calls update_attempt to record test_results; it never writes a task status, so it cannot bypass set_status/update_task validation.",
    "evidence": "The only store write introduced by the diff is `await self.store.update_attempt(attempt_id, test_results={...})`, which updates attempt test_results, not task status; there are no `update_task(..., validate=False)` calls anywhere in the added/modified code.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 464,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
