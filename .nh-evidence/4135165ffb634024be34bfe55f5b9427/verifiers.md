# Verifiers

_Harness-captured record for task `4135165f`, commit `e1dcbbe396fea5fbc9842fbd8ab6ced968b5a912` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Reviewed all added/modified test functions across the four files; each contains at least one assert (many use pytest.raises or parametrized asserts too). No assertion-free test function exists in the diff.",
    "evidence": "Every added test function contains assert statements, e.g. test_a_commit_that_is_on_the_base_branch_is_not_blocked ends with `assert result == {}, \"a commit already reachable from main must not be blocked\"`",
    "file": "tests/test_landed_claim_early_refusal.py",
    "files_checked": [
      "tests/test_landed_claim_early_refusal.py",
      "tests/test_landed_claim_guard.py",
      "tests/test_structural_budget.py",
      "tests/test_verification_receipts.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 913,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is confined to the read-only landed-claim guard and already-satisfied classification plumbing; it performs no task-status writes at all, so nothing bypasses set_status via update_task(validate=False).",
    "evidence": "The diff adds the landed-claim guard (LandedClaimGuard, _build_landed_claim_guard, _already_satisfied_subject's new 'determinate' return value, and PostToolUse hook composition). None of the added or modified code contains any call to update_task, and there is no status-write with validate=False anywhere in the change.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1103,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
