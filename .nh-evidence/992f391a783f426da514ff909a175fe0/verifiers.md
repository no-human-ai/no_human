# Verifiers

_Harness-captured record for task `992f391a`, commit `89b9838b6af40684d77b80d50b2f20087d901730` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All nine new test functions and every modified test in the diff contain at least one assert (or pytest.raises / AssertionError-raising helper). The test_structural_budget.py edits only touch the FROZEN_FILE_LINES data dict, not any test-function body.",
    "evidence": "Every added test_* function contains assert statements, e.g. test_classify_empty_sha_is_unverifiable: 'assert verdict.verdict == UNVERIFIABLE'; and each modified test (e.g. test_every_done_writer_leaves_a_task_event) retains its existing asserts while only adding context-dict keys.",
    "file": "tests/test_already_satisfied_landing.py",
    "files_checked": [
      "tests/test_already_satisfied_landing.py",
      "tests/test_api.py",
      "tests/test_cli_commands.py",
      "tests/test_false_done_completion.py",
      "tests/test_landing_actor.py",
      "tests/test_structural_budget.py"
    ],
    "line": 133,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1115,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The new/modified code writes context via merge_context and sets status via set_status; it introduces no update_task(validate=False) status write, so the statement holds.",
    "evidence": "The only status transition in the changed code is `await self.store.set_status(task, TaskStatus.AWAITING_APPROVAL, validate=False)` (context line); the added lines call `self.store.merge_context(...)` and build `landing_note`/`detail` text \u2014 no `update_task` call appears in the diff.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 9358,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 738,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
