# Merge-ready policy

_Harness-captured record for task `19dd94b3`, commit `71c7204ad1dbcd59b1700b8f3e528a5b4705c716` — not model-authored: no_human wrote this file from the repo's merge policy evaluated against this commit — advisory to the human, nothing merges on it. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "policy_changed_in_diff": false,
  "problems": [],
  "ready": false,
  "rules": [
    {
      "detail": "review verdict is FAIL on head",
      "name": "review_passed",
      "passed": false
    },
    {
      "detail": "tests: 1 failed of 12363 run",
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
      "detail": "0 verifiers ran",
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
  "summary": "not ready \u2014 2 of 6 rules failed: review_passed, tests_ran_and_passed"
}
```
