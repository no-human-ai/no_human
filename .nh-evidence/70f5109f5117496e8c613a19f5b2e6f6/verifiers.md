# Verifiers

_Harness-captured record for task `70f5109f`, commit `29a1a1bb6151c6ad75258fc6cded07008b42003e` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new and modified test functions across the five touched files contain assertions; the only helper functions added do not start with test_ and are correctly out of scope.",
    "evidence": "Every added/modified test function carries at least one assert (e.g. test_verifiers_all_satisfied_pass_unavailable_only_is_advisory: `assert v.passed`; the modified test_the_bounded_retry_window_is_one_shorter_call_both_ways retains `assert reviewer2.bounded_calls == 2`); helpers like `_seed_attempt_with_verifier_results` are not test functions.",
    "file": "",
    "files_checked": [
      "tests/test_merge_policy.py",
      "tests/test_pr_evidence.py",
      "tests/test_verifier_quota_park.py",
      "tests/test_verifiers_cli.py",
      "tests/test_verifiers_gate.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 2020,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified line in the change set writes a task status at all \u2014 there are no update_task or set_status calls in the diff \u2014 so the statement holds vacuously: nothing bypasses the transition table via validate=False.",
    "evidence": "The diff touches only merge_policy.py (adds verifiers_unavailable bucket), orchestrator.py (removes a ReviewerUnavailable raise, adds _carry_usage token-folding on verifier no-verdict), and pr_evidence.py (verifiers_pin rendering). None of these hunks contain any call to update_task or set_status, let alone update_task(..., validate=False).",
    "file": "",
    "files_checked": [
      "src/no_human/core/merge_policy.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/pr_evidence.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 603,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
