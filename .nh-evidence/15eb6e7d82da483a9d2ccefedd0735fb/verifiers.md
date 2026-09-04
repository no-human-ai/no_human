# Verifiers

_Harness-captured record for task `15eb6e7d`, commit `e1b4943479b06d01a7b2c2078ae5cad27fda8e36` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new and modified test functions across the four files contain at least one assert, pytest.raises block, or assertion; no assertion-free test was introduced. Helper functions like _new_orch_and_task are not tests and are exempt.",
    "evidence": "Every added/modified test function contains an assertion, e.g. test_task_failed_rejects_non_enum_reason_category uses `with pytest.raises(ValueError, match=\"not allowed\")` and test_lambda_body_omits_reason_category uses `assert telemetry.flush(_ENABLED) == 1`.",
    "file": "tests/test_telemetry_failure_category.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_telemetry.py",
      "tests/test_telemetry_failure_category.py"
    ],
    "line": 55,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 929,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "All status transitions in the diff (e.g. in _fail) go through store.set_status; the new code only adds a reason_category argument and telemetry calls, none of which bypass validation via update_task(validate=False).",
    "evidence": "The only status write in the modified code is `await self.store.set_status(task, TaskStatus.FAILED)` inside `_fail`; no added/changed line calls `update_task` with `validate=False`.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 7391,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 435,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
