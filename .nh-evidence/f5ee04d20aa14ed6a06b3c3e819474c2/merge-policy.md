# Merge-ready policy

_Harness-captured record for task `f5ee04d2`, commit `935bf3979ef8af3d2794f04056d5e093a19db4fa` — not model-authored: no_human wrote this file from the repo's merge policy evaluated against this commit — advisory to the human, nothing merges on it. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "policy_changed_in_diff": false,
  "problems": [],
  "ready": false,
  "rules": [
    {
      "detail": "review PASSED on head",
      "name": "review_passed",
      "passed": true
    },
    {
      "detail": "tests: 1 failed of 13477 run",
      "name": "tests_ran_and_passed",
      "passed": false
    },
    {
      "detail": "tamper guard did not fire",
      "name": "tamper_guard_clear",
      "passed": true
    },
    {
      "detail": "repro gate pass",
      "name": "repro_gate",
      "passed": true
    },
    {
      "detail": "1 verifiers, none failed",
      "name": "verifiers_all_satisfied",
      "passed": true
    },
    {
      "detail": "ci: success",
      "name": "ci",
      "passed": true
    }
  ],
  "source": "default",
  "summary": "not ready \u2014 1 of 6 rules failed: tests_ran_and_passed"
}
```
