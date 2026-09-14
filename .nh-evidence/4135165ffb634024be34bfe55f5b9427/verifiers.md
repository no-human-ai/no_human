# Verifiers

_Harness-captured record for task `4135165f`, commit `93688d1b4decac6a50300521de908765cc8fc47e` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions across the two new files and the additions to test_vcs.py and test_verification_receipts.py carry assert statements and/or pytest.raises blocks; the changes to test_readme_claims.py and test_structural_budget.py only edit module-level data tables, not test functions.",
    "evidence": "Every added/modified test function contains assertions, e.g. test_composed_post_tool_hooks_place_the_claim_guard_after_receipts uses 'assert Orchestrator._ordered_post_tool_hooks(...) == [...]'; test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim uses 'with pytest.raises(QuotaExhausted)' plus multiple asserts; test_note_text_refutes_the_claim_before_any_delivery uses 'assert calls == []' etc.",
    "file": "",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_vcs.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1136,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code writes task status; the changes concern the already-satisfied claim guard and its return signature, so the statement holds (vacuously) for this diff.",
    "evidence": "The diff only adds/modifies the landed-claim guard wiring (_build_landed_claim_guard, _agent_sink feed, hook composition) and extends _already_satisfied_subject to return a 7th 'determinate' element. None of the added or modified code writes a task status \u2014 there are no update_task(..., validate=False) calls nor any status-transition writes at all.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 556,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
