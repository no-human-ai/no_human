# Verifiers

_Harness-captured record for task `f7874965`, commit `cbe84e168c27a6dccec274bf20f399702b93b4fa` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new/modified test functions across the four shown files contain at least one assert statement or assertion-helper call (_assert_renders_unknown/_assert_renders_split, mock .assert_* calls), so none are assertion-free.",
    "evidence": "Every added/modified test ends in an assertion or assertion-helper call, e.g. test_base_run_errored_renders_unknown calls `_assert_renders_unknown(ids, newly)` which contains `assert newly is None`; test_the_split_is_computed_once_and_shared_with_billing has `assert owned_mock.await_count == 1`; modified test_flaky_non_owned... has `newly_failing_mock.assert_awaited_once()`.",
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
    "tokens_used": 784,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The new/changed code only writes attempt test_results via update_attempt and never writes a task status, so there is no update_task(validate=False) status write to flag.",
    "evidence": "The only store writes added/modified in the diff are `self.store.update_attempt(attempt_id, test_results=_bounded_test_results({...}))` in `_handle_pre_review_red`; no call to `update_task` with `validate=False` (nor any `update_task` status write) appears in the new or modified code.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 14005,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 552,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
