# Verifiers

_Harness-captured record for task `4135165f`, commit `f2735ac2583d6bde0d3be9114cd871f0d840a5e2` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added or modified across the diff include at least one assert/pytest.raises; the readme and structural-budget edits touch only data tables, not test bodies.",
    "evidence": "Every new test function contains assertions, e.g. test_a_commit_that_is_on_the_base_branch_is_not_blocked ends with `assert result == {}, \"a commit already reachable from main must not be blocked\"`, and test_guard_is_wired_into_the_real_run_attempt_and_fires_on_a_refutable_claim uses `with pytest.raises(QuotaExhausted):` plus multiple asserts.",
    "file": "tests/test_landed_claim_early_refusal.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_vcs.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 216,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1236,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code writes task status at all \u2014 the change is about the landed-claim guard and probe return signatures, so there is no update_task(validate=False) bypass of set_status to find.",
    "evidence": "The diff adds/modifies only LandedClaimGuard wiring, _already_satisfied_subject (adds a 'determinate' return element), _build_landed_claim_guard, and PostToolUse hook ordering; no update_task(validate=False) or any task-status write appears in the changed code.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 817,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
