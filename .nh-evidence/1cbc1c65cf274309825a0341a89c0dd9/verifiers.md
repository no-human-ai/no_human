# Verifiers

_Harness-captured record for task `1cbc1c65`, commit `916d3e1f9ac8d38c944a264e6aabf67d38e61023` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions in test_gate_oneshot.py and test_plugin_drift.py end in an assert, a pytest.raises context, or the assertion-installing spy helper; the changes to test_readme_claims.py and test_structural_budget.py only touch data tables, not test-function bodies.",
    "evidence": "Every added/modified test carries an assertion mechanism: direct `assert`, a `pytest.raises(...)` block, or an assertion-helper call. E.g. `test_the_gate_never_shells_out_to_a_write_command` calls `_install_fail_closed_git_spy(monkeypatch, real_repo=repo)`, a fail-closed spy whose `_check` runs `assert ...` on every subprocess during `run_gate`.",
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
    "tokens_used": 1990,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
