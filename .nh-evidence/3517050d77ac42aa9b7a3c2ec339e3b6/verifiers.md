# Verifiers

_Harness-captured record for task `3517050d`, commit `58a1894f754a1ad4775a6bca87040e36a22d600e` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All eight added/modified test functions (two in test_structural_budget.py, six in test_structural_budget_preflight.py) contain at least one assert statement; the modified `_guard_text` and the backend classes are helpers, not test functions.",
    "evidence": "Every added test contains assert statements, e.g. test_a_frozen_value_under_the_measurement_is_still_grown has `assert new == []`, `assert stale == []`, `assert len(grown) == 1`, `assert \"pkg/mod.py:foo\" in grown[0]`",
    "file": "",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_structural_budget_preflight.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 660,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The new code writes an attempt status via update_attempt and returns a TaskOutcome dataclass; it never calls update_task with validate=False, so the transition-table concern the statement guards against does not arise.",
    "evidence": "The only status writes added are `await self.store.update_attempt(attempt_id, status=\"failed\", failure_reason=detail)` and `return TaskOutcome(task, status=TaskStatus.FAILED, detail=detail)`; no `update_task(..., validate=False)` call appears anywhere in the diff.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 9427,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 967,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
