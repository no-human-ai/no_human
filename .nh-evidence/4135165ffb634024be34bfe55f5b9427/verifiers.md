# Verifiers

_Harness-captured record for task `4135165f`, commit `572548b75b5ece7e9f510ee6c936bab569c06cee` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "I checked every test function across the four files; each has at least one assertion, pytest.raises block, or assertion helper call, with none left assertion-free.",
    "evidence": "Every added test_* function contains assert statements and/or a pytest.raises block; e.g. test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim uses `with pytest.raises(QuotaExhausted):` plus multiple `assert backend.calls`, and even the shortest ones like test_unverifiable_does_not_block contain `assert _run(guard.hook(...)) == {}`.",
    "file": "tests/test_landed_claim_guard.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_structural_budget.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 122,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1400,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is confined to guard construction and hook ordering; it never mutates task status, so no code path introduces an update_task(validate=False) status write bypassing set_status. The statement holds vacuously for this diff.",
    "evidence": "The diff touches only the landed-claim guard (_build_landed_claim_guard, _agent_sink feed, _already_satisfied_subject's new `determinate` return, and _ordered_post_tool_hooks/_compose_post_tool_hooks). No added or modified line contains `update_task`, `set_status`, `validate=False`, or any task-status write.",
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
