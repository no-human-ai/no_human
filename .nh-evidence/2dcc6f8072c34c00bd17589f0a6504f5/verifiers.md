# Verifiers

_Harness-captured record for task `2dcc6f80`, commit `8d59015f936554ff66a81f951a2993cd9445839d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added across the four files (early_refusal, guard, verification_receipts) contain at least one assert; the structural_budget change only edits frozen dict data/comments, not test bodies.",
    "evidence": "Every added test function contains assert statements, e.g. test_compose_returns_none_when_there_are_no_hooks_including_claim_hook asserts `Orchestrator._compose_post_tool_hooks(None, None, None, None) is None`",
    "file": "",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_structural_budget.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 739,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The changed code introduces a deterministic guard and hook wiring only \u2014 it performs no task-status writes, so it neither calls update_task(validate=False) nor bypasses set_status. The statement holds vacuously for this diff.",
    "evidence": "The diff adds only the landed-claim guard: _build_landed_claim_guard, note_text feeding in _agent_sink, and PostToolUse hook composition/ordering (_ordered_post_tool_hooks, _compose_post_tool_hooks). None of the new or modified lines call update_task or write a task status at all; there is no `validate=False` anywhere in the change.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 558,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
