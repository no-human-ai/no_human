# Verifiers

_Harness-captured record for task `ed786a65`, commit `19c650d41ddcae7dea6dfa78666cc0280884237d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 11 new tests in test_ui_evidence_default_walk.py and the one new test in test_ui_evidence_prompt.py each contain assert statements; the test_structural_budget.py change is only a data-value edit, not a test function. No assertion-free test was added or modified.",
    "evidence": "Every added/modified test function contains assert statements, e.g. test_default_manifest_landing_only_shape asserts `actions == [...]` and test_block_shows_an_example_manifest_and_the_bare_landing_fallback asserts multiple substrings in `prompt`.",
    "file": "tests/test_ui_evidence_default_walk.py",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_ui_evidence_default_walk.py",
      "tests/test_ui_evidence_prompt.py"
    ],
    "line": 62,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 841,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the changed code touches task status persistence; there is no update_task call with validate=False (nor any update_task/set_status call) in the diff, so the statement holds vacuously.",
    "evidence": "The entire diff modifies UI-evidence capture/rendering (`_default_walk_manifest`, `_maybe_capture_ui_evidence`, `_deliver_ui_evidence`, `ui_evidence_block`) and the coder-prompt text. No new or modified line calls `update_task`, references `validate`, or writes a task status at all.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/prompt_blocks.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 524,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
