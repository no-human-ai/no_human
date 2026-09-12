# Merge-ready policy

_Harness-captured record for task `7ee2d939`, commit `3ca689e203ca76d6c9b3e354b8be8f5d05e11e92` — not model-authored: no_human wrote this file from the repo's merge policy evaluated against this commit — advisory to the human, nothing merges on it. It records what the gate produced; it is not a verdict of the model that wrote the code._

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
      "detail": "tests: 1 failed of 12368 run",
      "name": "tests_ran_and_passed",
      "passed": false
    },
    {
      "detail": "tamper guard did not fire",
      "name": "tamper_guard_clear",
      "passed": true
    },
    {
      "detail": "repro gate fail",
      "name": "repro_gate",
      "passed": false
    },
    {
      "detail": "2 verifiers, none failed",
      "name": "verifiers_all_satisfied",
      "passed": true
    },
    {
      "detail": "ci: none reported (tolerated)",
      "name": "ci",
      "passed": true
    }
  ],
  "source": "default",
  "summary": "not ready \u2014 2 of 6 rules failed: tests_ran_and_passed, repro_gate"
}
```
