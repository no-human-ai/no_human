# Verifiers

_Harness-captured record for task `ed786a65`, commit `08987988d2376008241861ff9bdb3f1c799cd0b1` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 11 new test functions in test_ui_evidence_default_walk.py and the one new test in test_ui_evidence_prompt.py each contain at least one assert statement; non-test helper functions (FakePage, _drive, etc.) are not test functions and are exempt.",
    "evidence": "Every added test_* function contains assert statements, e.g. test_default_manifest_landing_only_shape asserts m is not None and actions == [...]",
    "file": "tests/test_ui_evidence_default_walk.py",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_ui_evidence_default_walk.py",
      "tests/test_ui_evidence_prompt.py"
    ],
    "line": 71,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 734,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is confined to UI-evidence capture/delivery and prompt-block wording. No new or modified line touches task status, so the statement holds vacuously \u2014 there is no `update_task(..., validate=False)` status write to violate it.",
    "evidence": "The diff only modifies `_maybe_capture_ui_evidence`, `_default_walk_manifest`, `_deliver_ui_evidence`, and `ui_evidence_block`/prompt text \u2014 none of the changed code calls `update_task` (with or without `validate=False`) or writes any task status; there are no status transitions in the diff at all.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/prompt_blocks.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 467,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
