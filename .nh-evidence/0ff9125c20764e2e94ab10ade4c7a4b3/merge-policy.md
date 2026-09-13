# Merge-ready policy

_Harness-captured record for task `0ff9125c`, commit `0ce237c405e3decd752a05faa222a41cb30e8bb4` — not model-authored: no_human wrote this file from the repo's merge policy evaluated against this commit — advisory to the human, nothing merges on it. It records what the gate produced; it is not a verdict of the model that wrote the code._

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
      "detail": "tests: 0 failed of 12763 run",
      "name": "tests_ran_and_passed",
      "passed": true
    },
    {
      "detail": "tamper fire waived as legitimate",
      "name": "tamper_guard_clear",
      "passed": true
    },
    {
      "detail": "repro gate error",
      "name": "repro_gate",
      "passed": false
    },
    {
      "detail": "2 verifiers, none failed",
      "name": "verifiers_all_satisfied",
      "passed": true
    },
    {
      "detail": "ci: failure",
      "name": "ci",
      "passed": false
    }
  ],
  "source": "default",
  "summary": "not ready \u2014 2 of 6 rules failed: repro_gate, ci"
}
```
