# Verifiers

_Harness-captured record for task `b42eed43`, commit `4c2bdd7c047c53ec1dcd5fb64246ed1f7fbe1bc6` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added (4 in test_history_gate_hit_report.py, 10 in the new test_history_gate_range_attribution.py) contain at least one assert statement; helper functions and fixtures (_git, _commit, scratch_repo) are not tests and are not required to assert.",
    "evidence": "Every added test function contains assert statements, e.g. test_attribute_hits_splits_on_dedup_key: 'assert [h.path for h in introduced] == [\"src/new.py\"]' and test_range_verdict_reports_a_hit_planted_in_the_scanned_range: 'assert rc == 1'",
    "file": "",
    "files_checked": [
      "tests/test_history_gate_hit_report.py",
      "tests/test_history_gate_range_attribution.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 779,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
