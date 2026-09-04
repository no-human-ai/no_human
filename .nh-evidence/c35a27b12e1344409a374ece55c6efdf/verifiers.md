# Verifiers

_Harness-captured record for task `c35a27b1`, commit `4f1e0bffd8d5f564286859f235dc2d6937b107b5` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions in test_hint_signals.py and the two modified tests in test_api.py contain assert statements; the test_structural_budget.py change only edits frozen data dicts, not test-function bodies. No assertion-free test function exists.",
    "evidence": "Every added/modified test function contains asserts, e.g. test_multi_family_fires_on_three_families: `assert \"multi_family\" in signals` and `assert reasons`",
    "file": "tests/test_hint_signals.py",
    "files_checked": [
      "tests/test_api.py",
      "tests/test_hint_signals.py",
      "tests/test_structural_budget.py"
    ],
    "line": 122,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 373,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified line invokes update_task at all (with or without validate=False) or otherwise writes a task status, so the statement holds vacuously.",
    "evidence": "The diff only adds/edits pure signal-computation code (compute_tier, _tier_signals, hint_signals, hint_signals_enabled, estimate_feasibility); none of it calls update_task, and hint_signals is documented as 'Pure \u2014 no task.context mutation, no store_tier call'.",
    "file": "",
    "files_checked": [
      "src/no_human/core/complexity.py",
      "src/no_human/core/feasibility.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 358,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
