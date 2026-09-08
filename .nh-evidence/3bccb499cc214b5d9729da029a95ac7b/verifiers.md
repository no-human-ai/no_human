# Verifiers

_Harness-captured record for task `3bccb499`, commit `b4e8abf8b4e42445a329a73940c12f2555a39c2a` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All six added test_* functions contain assert statements; the structural_budget change only edits a frozen-value dict with no test functions added or modified. Every test has at least one assertion.",
    "evidence": "Each added test function (e.g. test_node_tap_failing_blocks_survive_a_ten_kilobyte_tail, test_pytest_failed_sections_are_surfaced, test_a_green_run_emits_no_blocks_and_writes_no_file) contains multiple `assert` statements.",
    "file": "tests/test_red_run_failure_blocks.py",
    "files_checked": [
      "tests/test_red_run_failure_blocks.py",
      "tests/test_structural_budget.py"
    ],
    "line": 158,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 603,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is entirely about red-test failure-block detail and full-output artifacts; no new or modified code writes a task status at all, so none does so via update_task(validate=False).",
    "evidence": "The diff only adds/changes calls to self.emit(...), self.store.update_attempt(..., test_results=...), and new artifact/detail helpers (_red_test_detail, _write_test_output_artifact); there is no update_task call anywhere in the changed code, let alone one with validate=False.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 369,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
