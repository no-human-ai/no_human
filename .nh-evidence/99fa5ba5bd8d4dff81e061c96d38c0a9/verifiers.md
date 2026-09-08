# Verifiers

_Harness-captured record for task `99fa5ba5`, commit `88cf94ba994ae423070eada8bbcc992a29bd8f63` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All six added test functions (the test_* coroutines) each contain multiple assert statements; the diff to test_structural_budget.py only edits frozen-value dicts, adding/modifying no test functions.",
    "evidence": "assert persisted[\"failing_tests\"] == ids[:_BOUND]",
    "file": "tests/test_failing_tests_bound.py",
    "files_checked": [
      "tests/test_failing_tests_bound.py",
      "tests/test_structural_budget.py"
    ],
    "line": 148,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 440,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change only touches test-result payloads and adds bounding helpers; no new or modified code calls update_task at all, let alone with validate=False, so the transition-table concern is not violated.",
    "evidence": "All status-related writes in the diff go through self.store.update_attempt(...) (e.g. `await self.store.update_attempt(attempt_id, status=\"failed\", failure_reason=detail, ...)`); no line in the diff calls update_task, and none passes validate=False.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 6788,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 611,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
