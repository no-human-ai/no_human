# Verifiers

_Harness-captured record for task `1cbc1c65`, commit `07f17f5e0c697421b130346a634e6d35658c3b31` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added test function carries at least one assertion \u2014 plain asserts, pytest.raises blocks, or asserts inside the invoked _spy nested functions. The test_readme_claims/test_structural_budget edits touch only data tables, not test bodies.",
    "evidence": "Each new test in test_gate_oneshot.py has assert/pytest.raises, e.g. test_the_gate_renders_file_and_line_citations: 'assert \"src/a.py:41\" in text'; refusal tests use 'with pytest.raises(GateUnavailable, ...)'; new test_plugin_drift.py tests use assert statements.",
    "file": "tests/test_gate_oneshot.py",
    "files_checked": [
      "tests/test_egress_allowlist.py",
      "tests/test_gate_oneshot.py",
      "tests/test_plugin_drift.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py"
    ],
    "line": 132,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1560,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
