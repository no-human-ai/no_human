# Verifiers

_Harness-captured record for task `a9eb5e52`, commit `ab882fb2213ab04a390e1329fbcd8dd616503d10` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test_* functions and the two modified ones (test_task_branch_only_claim_is_landed_by_approve, test_task_is_not_done_when_landing_fails \u2014 modified only in a nested helper signature, still assert-bearing) carry at least one assert/pytest.raises; the assertion-free new defs are non-test helper functions, which the statement does not cover.",
    "evidence": "Every added/modified test function contains assertions, e.g. test_a_changed_pr_head_alone_triggers_no_review_round ends with `assert out is None`, `assert resumed[\"count\"] == 0`, `assert fresh.status is TaskStatus.AWAITING_APPROVAL`; the only assertion-free new functions are helpers (_land_env_config_with_poll_timeout, _fake_monotonic_clock, _write_glab_stub, _remote_ref, _install_post_receive) which are not test functions.",
    "file": "",
    "files_checked": [
      "tests/test_already_satisfied_landing.py",
      "tests/test_approve_merge.py",
      "tests/test_egress_allowlist.py",
      "tests/test_land_head_push_no_rewake.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1625,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "This diff is comment-only; it introduces no code that writes a task status, so it cannot call update_task with validate=False. The statement holds vacuously for the change.",
    "evidence": "The only change in the diff is to a comment block inside the `if state == \"CLOSED\":` branch (lines ~1710-1725 of wake.py); it rewrites explanatory text about MERGED/CLOSED landings and contains no code at all \u2014 no call to update_task, set_status, or any status write.",
    "file": "src/no_human/blockers/wake.py",
    "files_checked": [
      "src/no_human/blockers/wake.py"
    ],
    "line": 1713,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 375,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
