# Verifiers

_Harness-captured record for task `1cbc1c65`, commit `e32c1d8b6e8fd0f3940a842169c13f69249df640` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions in test_gate_oneshot.py and test_plugin_drift.py carry at least one assertion (direct asserts, pytest.raises blocks, or asserts within the spy callbacks invoked during run_gate). The changes to test_readme_claims.py and test_structural_budget.py are data-only, not test-function bodies.",
    "evidence": "Every added/modified test function contains an assert or pytest.raises, e.g. test_the_gate_never_shells_out_to_a_write_command has `assert not bad` inside its subprocess spy, and refusal tests use `with pytest.raises(GateUnavailable, ...)`.",
    "file": "",
    "files_checked": [
      "tests/test_egress_allowlist.py",
      "tests/test_gate_oneshot.py",
      "tests/test_plugin_drift.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1853,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
