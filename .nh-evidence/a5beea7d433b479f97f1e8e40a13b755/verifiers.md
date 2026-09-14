# Verifiers

_Harness-captured record for task `a5beea7d`, commit `378176dc597bfc25bd3db4a2ce2b3ed94281d360` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added/modified test_* functions carry at least one assertion (assert statements or pytest.raises blocks); non-test helpers and fixtures like _git/_commit/repo are not test functions and are exempt.",
    "evidence": "Every added test function contains at least one assert; e.g. test_mutation_probe_config_defaults_on_empty_data does `assert mutation_probe_config({}) == DEFAULT_CONFIG[\"mutation_probe\"]`, and each test in the new files ends in assert/pytest.raises.",
    "file": "",
    "files_checked": [
      "tests/test_config.py",
      "tests/test_egress_allowlist.py",
      "tests/test_mutation_probe.py",
      "tests/test_mutation_probe_wiring.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 833,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
