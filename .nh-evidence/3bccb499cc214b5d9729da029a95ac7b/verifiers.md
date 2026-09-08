# Verifiers

_Harness-captured record for task `3bccb499`, commit `8a2413fd1753158e8db0f1c3d77a95c2b5510900` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All nine added test functions (and the one modified-only budget dict in test_structural_budget.py adds no test) contain multiple assert statements; helper functions like _run_attempt_with_result and _big_tap are not test functions and are exempt.",
    "evidence": "Every test_* function in tests/test_red_run_failure_blocks.py contains assert statements, e.g. test_a_green_run_emits_no_blocks_and_writes_no_file: `assert persisted[\"failure_blocks\"] == []` and `assert not log_path.exists()`",
    "file": "tests/test_red_run_failure_blocks.py",
    "files_checked": [
      "tests/test_red_run_failure_blocks.py",
      "tests/test_structural_budget.py"
    ],
    "line": 605,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 807,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is confined to test_results/failure_blocks plumbing; it introduces no update_task calls at all, let alone any with validate=False, so the statement holds.",
    "evidence": "The only status writes in the diff are `await self.store.update_attempt(attempt_id, status=\"failed\", failure_reason=detail, test_results={...})` \u2014 these call update_attempt (attempt status), not update_task, and no `update_task(..., validate=False)` call appears anywhere in the new or modified code.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 6639,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 589,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
