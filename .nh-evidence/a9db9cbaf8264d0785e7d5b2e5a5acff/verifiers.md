# Verifiers

_Harness-captured record for task `a9db9cba`, commit `1684b3a870aefaa604f0852d85b248b5947212c0` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All three added test functions contain multiple assert statements; the modified file test_structural_budget.py only changed frozen dict values, adding no test functions.",
    "evidence": "test_serial_rerun_green_lets_the_attempt_proceed has 'assert len(calls) == 2', test_serial_rerun_still_red_fails_as_today has 'assert sorted(tr[\"serial_rerun_failed\"]) == ...', test_pytest_command_is_never_serially_rerun has 'assert len(calls) == 1'",
    "file": "tests/test_orchestrator_serial_rerun.py",
    "files_checked": [
      "tests/test_orchestrator_serial_rerun.py",
      "tests/test_structural_budget.py"
    ],
    "line": 120,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 322,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The added serial-rerun logic touches only attempt test_results via update_attempt and never writes a task status, so it does not bypass set_status/the transition table via update_task(validate=False).",
    "evidence": "The only store mutation in the new/modified code is `await self.store.update_attempt(attempt_id, test_results=base_test_results,)`; no call to `update_task(..., validate=False)` appears in the diff, and the new `_node_serial_rerun` only reads results and returns a dict.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 6751,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1078,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
