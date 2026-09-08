# Verifiers

_Harness-captured record for task `c9cdc537`, commit `70fcfcca6e006e9747f4342ad23594c1e55a1a4b` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All ten new test functions carry assertions (assert statements checking prompt contents, unreadable flags, decision.action, ordering, etc.); no assertion-free test was added or modified.",
    "evidence": "Every added test method in test_supervisor_send_back.py contains at least one assert, e.g. test_formatter_tolerates_bare_string_entries has 'assert unreadable is False' and 'assert \"just a bare string message\" in text'; the change to test_structural_budget.py is only a frozen dict value, not a test function.",
    "file": "tests/test_supervisor_send_back.py",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_supervisor_send_back.py"
    ],
    "line": 44,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 637,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change only reads task context and forwards send_back_feedback to the supervisor; it introduces no task-status write at all, so it cannot bypass set_status via update_task(validate=False).",
    "evidence": "The only new/modified code reads `send_back_feedback = (task.context or {}).get(\"send_back_feedback\")` and passes it to `SupervisorHook`; there are no `update_task(...)` calls, no `validate=False`, and no task status writes anywhere in the diff.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 15551,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 377,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
