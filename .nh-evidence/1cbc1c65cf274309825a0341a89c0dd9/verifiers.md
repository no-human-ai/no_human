# Verifiers

_Harness-captured record for task `1cbc1c65`, commit `e2272ed090020a01ab2ca34702085676f687c1cb` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added/modified test function carries an assertion \u2014 either an explicit assert/pytest.raises, or (for the two fail-closed spy tests) an installed assertion-helper that raises AssertionError during run_gate.",
    "evidence": "Added tests contain assertions: e.g. `assert result.passed is False`, `with pytest.raises(GateUnavailable, match=...)`, and the two shell-spy tests call `_install_fail_closed_git_spy(...)` whose `_check` raises AssertionError on any disallowed subprocess.",
    "file": "tests/test_gate_oneshot.py",
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
    "tokens_used": 2918,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
