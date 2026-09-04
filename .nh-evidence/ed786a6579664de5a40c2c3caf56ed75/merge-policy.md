# Merge-ready policy

_Harness-captured record for task `ed786a65`, commit `2fc637a7f54595de12161db17ae12a17ea9c48aa` — not model-authored: no_human wrote this file from the repo's merge policy evaluated against this commit — advisory to the human, nothing merges on it. It records what the gate produced; it is not a verdict of the model that wrote the code._

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
      "detail": "tests: 0 failed of 11266 run",
      "name": "tests_ran_and_passed",
      "passed": true
    },
    {
      "detail": "tamper guard fired, unwaived",
      "name": "tamper_guard_clear",
      "passed": false
    },
    {
      "detail": "repro gate pass",
      "name": "repro_gate",
      "passed": true
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
  "summary": "not ready \u2014 1 of 6 rules failed: tamper_guard_clear"
}
```
