# Verifiers

_Harness-captured record for task `ed786a65`, commit `2fc637a7f54595de12161db17ae12a17ea9c48aa` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All twelve new test functions in tests/test_ui_evidence_default_walk.py and the one new test added to tests/test_ui_evidence_prompt.py include at least one assert statement; no assertion-free test was added or modified.",
    "evidence": "Every added/modified test function contains assert statements, e.g. test_block_shows_an_example_manifest_and_the_bare_landing_fallback has `assert _MARKER in prompt` and test_default_manifest_landing_only_shape has `assert actions == [...]`.",
    "file": "",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_ui_evidence_default_walk.py",
      "tests/test_ui_evidence_prompt.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 783,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the added or modified code touches task status transitions at all \u2014 no call to update_task (with or without validate=False) and no set_status appears in the change, so the statement holds vacuously.",
    "evidence": "The diff only modifies `_maybe_capture_ui_evidence`, `_deliver_ui_evidence`, `_default_walk_manifest`, and `ui_evidence_block` \u2014 all concerning UI screenshot/walk capture and prompt text; no line calls `update_task` or writes any task status.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/prompt_blocks.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 359,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
