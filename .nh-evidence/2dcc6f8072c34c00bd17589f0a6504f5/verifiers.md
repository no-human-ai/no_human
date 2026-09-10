# Verifiers

_Harness-captured record for task `2dcc6f80`, commit `ec151501f54abb618c0206444736652aa9118a0c` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All newly added test functions across the four files include at least one assert (or an assertion-bearing helper); the structural_budget change only edits frozen-value dicts and adds no test functions, and no test lacks an assertion.",
    "evidence": "Every added/modified test_* function contains assert statements, e.g. test_note_text_never_raises ends with `assert _run(guard2.hook({}, None, None)) == {}` and test_a_firing_claim_guard_cannot_suppress_receipt_capture has `assert out` and `assert len(seen) == 1`.",
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
    "tokens_used": 1041,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or modified code touches task status; there are no update_task or set_status calls in the change, so the statement holds vacuously for this diff.",
    "evidence": "The diff adds only landed-claim guard wiring (_build_landed_claim_guard, _agent_sink feed, hook composition changes in _ordered_post_tool_hooks/_compose_post_tool_hooks); it contains no update_task(...) calls at all, let alone one with validate=False, and no task-status writes.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 523,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
