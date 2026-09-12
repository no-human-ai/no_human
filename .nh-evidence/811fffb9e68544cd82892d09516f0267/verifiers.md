# Verifiers

_Harness-captured record for task `811fffb9`, commit `e3a6733cd935f979087f57a9f848a5ce084f1561` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new and modified test functions in both files contain at least one assert statement or pytest.raises block; none is assertion-free.",
    "evidence": "Every added/modified test carries assertions, e.g. test_sandbox_residue_measures_files_not_directories has `assert residue[\"files\"] == 0`, and test_cleanup_never_raises... uses `with pytest.raises(RuntimeError, match=\"original\")`.",
    "file": "tests/test_doctor.py",
    "files_checked": [
      "tests/test_doctor.py",
      "tests/test_eval_sandbox_cleanup.py"
    ],
    "line": 138,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 736,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
