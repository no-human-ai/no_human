# Verifiers

_Harness-captured record for task `7a7713e3`, commit `817bcb2c8be0d887dd515fc26e3353a595e1ba41` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added test functions (the three report-nudge e2e tests, the two prompt-block tests) contain assert statements, and the only modified test (test_every_coder_sink_session_has_a_stated_stop_disposition) retains its existing asserts. Added classes/helpers are not test functions.",
    "evidence": "Every added test function contains asserts, e.g. test_a_zero_diff_deferral_is_not_report_nudged ends with `assert backend.nudges == [], backend.prompts`",
    "file": "tests/test_e2e_orchestrator.py",
    "files_checked": [
      "tests/test_e2e_orchestrator.py",
      "tests/test_prompt_blocks.py",
      "tests/test_server_stop_checkpoint.py",
      "tests/test_structural_budget.py"
    ],
    "line": 4981,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 660,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "",
    "evidence": "New code writes only attempt rows: `await self.store.update_attempt(attempt_id, full_final_text=...)` and `await self.store.add_attempt_usage(...)`; no `update_task(validate=False)` appears in any added/modified line, and the only status write nearby is the unchanged context line `await self.store.set_status(task, TaskStatus.REVIEWING)`.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/prompt_blocks.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1015,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
