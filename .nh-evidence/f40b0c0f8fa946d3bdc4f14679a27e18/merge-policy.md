# Merge-ready policy

_Harness-captured record for task `f40b0c0f`, commit `55ca5765b2bbd6cb4db7a1c4e5eb2743fe3101cd` — not model-authored: no_human wrote this file from the repo's merge policy evaluated against this commit — advisory to the human, nothing merges on it. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "policy_changed_in_diff": false,
  "problems": [],
  "ready": false,
  "rules": [
    {
      "detail": "no round has judged this head",
      "name": "review_passed",
      "passed": false
    },
    {
      "detail": "tests: 0 failed of 13081 run",
      "name": "tests_ran_and_passed",
      "passed": true
    },
    {
      "detail": "tamper guard fired, unwaived",
      "name": "tamper_guard_clear",
      "passed": false
    },
    {
      "detail": "repro gate error",
      "name": "repro_gate",
      "passed": false
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
  "summary": "not ready \u2014 3 of 6 rules failed: review_passed, tamper_guard_clear, repro_gate"
}
```
