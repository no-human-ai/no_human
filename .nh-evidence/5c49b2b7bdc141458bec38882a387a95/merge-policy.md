# Merge-ready policy

_Harness-captured record for task `5c49b2b7`, commit `c2dde8d7c10ae98e0fbe9838dc8b48b85bdf9bc7` — not model-authored: no_human wrote this file from the repo's merge policy evaluated against this commit — advisory to the human, nothing merges on it. It records what the gate produced; it is not a verdict of the model that wrote the code._

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
      "detail": "tests: 0 failed of 12199 run",
      "name": "tests_ran_and_passed",
      "passed": true
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
  "summary": "not ready \u2014 1 of 6 rules failed: review_passed"
}
```
