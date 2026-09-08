# Verifiers

_Harness-captured record for task `7a7713e3`, commit `85b7daa5dab9041bc3a41db363f1f5c72fbabb71` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions (the four report-nudge tests, the two prompt-block tests) contain assert statements; the added backend classes are helpers, not test functions, and the modified test_every_coder_sink_session_has_a_stated_stop_disposition retains its assertion. No assertionless test was added.",
    "evidence": "Every added/modified test function contains assertions, e.g. test_a_zero_diff_deferral_is_not_report_nudged ends with `assert backend.nudges == [], backend.prompts`, and test_the_background_run_rule_is_coder_only has `assert calls == [\"_build_implement_prompt\"]`.",
    "file": "tests/test_e2e_orchestrator.py",
    "files_checked": [
      "tests/test_e2e_orchestrator.py",
      "tests/test_prompt_blocks.py",
      "tests/test_server_stop_checkpoint.py",
      "tests/test_structural_budget.py"
    ],
    "line": 5055,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 722,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The added recovery-nudge code writes only attempt-level records (update_attempt/add_attempt_usage), never a task status, and the only status transition in scope goes through set_status. No update_task with validate=False was introduced.",
    "evidence": "New _report_nudge code calls only self.store.update_attempt(attempt_id, full_final_text=...) and self.store.add_attempt_usage(...); the sole task-status write nearby is the unchanged `await self.store.set_status(task, TaskStatus.REVIEWING)`. No update_task(validate=False) appears anywhere in the diff.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/prompt_blocks.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1410,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
