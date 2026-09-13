# Verifiers

_Harness-captured record for task `4135165f`, commit `97d283df4083b7bfc8422f38d1390adcb4b83a85` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All newly added/modified test functions across the changed files carry at least one assert (or pytest.raises); non-test helpers like _git, fixtures, and _incident_result are excluded by the statement's scope.",
    "evidence": "Every added test function contains assertions, e.g. test_note_text_refutes_the_claim_before_any_delivery: `assert calls == []`, `assert result`, `assert calls == [\"probed\"]`; and test_guard_is_wired... uses `with pytest.raises(QuotaExhausted):` plus multiple asserts.",
    "file": "tests/test_landed_claim_guard.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_vcs.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 87,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1215,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the changed code performs a task-status write at all \u2014 no `update_task` or `set_status` calls appear in the diff \u2014 so the statement holds vacuously for this change.",
    "evidence": "The diff introduces LandedClaimGuard wiring, a `determinate` return element for `_already_satisfied_subject`, and hook-ordering changes; no new or modified line calls `update_task(validate=False)` or writes a task status by any means.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 731,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
