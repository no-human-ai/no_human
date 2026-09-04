# Verifiers

_Harness-captured record for task `ec3fbb0e`, commit `5add0aaa699d62c1041b7d3afa13b5b2d5c674cd` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 15 test functions in the new file carry at least one assert; the diffs to test_egress_allowlist.py and test_structural_budget.py only touch data/comments (no test functions added or modified). The statement holds.",
    "evidence": "Every added test_* function in test_ui_evidence_build_cmd.py contains assert statements, e.g. test_build_nonzero_exit_yields_disclosed_skip_and_never_spawns has `assert srv.mode == \"boot-failed\"`, `assert srv.cause == \"build-failed\"`, `assert spawn.calls == []`",
    "file": "tests/test_ui_evidence_build_cmd.py",
    "files_checked": [
      "tests/test_egress_allowlist.py",
      "tests/test_structural_budget.py",
      "tests/test_ui_evidence_build_cmd.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1012,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The diff only changes a comment and adds two branches that build a human-readable failure reason string for boot-failed dev servers. No status-writing code (update_task/set_status) is added or modified, so the statement is trivially satisfied for this change.",
    "evidence": "The only modified code adds reason strings for `build-timeout`/`build-failed` boot-failed dev-server causes (e.g. reason = \"the UI build command timed out before the dev server started\"); it does not call update_task, set_status, or write any task status.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 401,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
