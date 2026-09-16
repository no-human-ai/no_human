# Verifiers

_Harness-captured record for task `f932b151`, commit `c9cee8bbcc83eda6074e390813070ff591c90a95` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All eleven added test functions include at least one assert or pytest.raises block; the only assertion-free function (_isolation_only_autouse_file) is a data helper, not a test.",
    "evidence": "Every test_* function contains assert statements, e.g. 'assert tamper_guard.count_faking_fixtures(src) == 0' and the last test uses 'with pytest.raises(SyntaxError):' plus asserts.",
    "file": "tests/test_tamper_guard_attribution.py",
    "files_checked": [
      "tests/test_tamper_guard_attribution.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 695,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
