# Verifiers

_Harness-captured record for task `77eb6fc4`, commit `c76e5cbeac8575c77378efd1c4055e9f8b86d1c6` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added or modified across both files contain at least one assert statement; the only new non-test callable (_OutputTierBackend.run) is a helper backend, not a test. The statement holds.",
    "evidence": "Every added/modified test function contains asserts, e.g. test_the_fallback_prices_the_output_premium_too has 'assert without_output.cost == 1000' and test_a_night_total_inside_the_band_holds has 'assert ok is True, lines'",
    "file": "tests/test_funnel_eval.py",
    "files_checked": [
      "tests/test_funnel_criteria.py",
      "tests/test_funnel_eval.py"
    ],
    "line": 349,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 815,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
