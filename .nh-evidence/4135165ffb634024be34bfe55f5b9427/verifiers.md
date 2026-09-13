# Verifiers

_Harness-captured record for task `4135165f`, commit `262e128c6e056d5970611da441842178d6e30b5e` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All newly added/modified test functions across the five test files contain at least one assert statement or pytest.raises block; the readme/structural-budget edits only touch data constants, not test bodies.",
    "evidence": "Every added test function contains assertions, e.g. test_build_landed_claim_guard_fires_on_a_refutable_claim_via_the_real_probe uses `assert guard is not None`, `assert result, ...`, and test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim uses `with pytest.raises(QuotaExhausted):` plus multiple asserts.",
    "file": "tests/test_landed_claim_early_refusal.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_vcs.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 76,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 885,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified line in the diff writes a task status at all; there is no update_task(validate=False) call, so the transition-table-bypass concern the statement guards against does not arise in this change.",
    "evidence": "The diff adds/modifies the landed-claim guard, `_agent_sink` feeding, `_already_satisfied_subject` (returning a new `determinate` element), `_build_landed_claim_guard`, and the PostToolUse hook ordering \u2014 none of these contain any call to `update_task` (let alone with `validate=False`) or any task-status write.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 503,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
