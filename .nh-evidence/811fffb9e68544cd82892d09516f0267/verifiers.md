# Verifiers

_Harness-captured record for task `811fffb9`, commit `69b0a0d8df93022c2fc810faf8eb30c2726bfaca` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added in test_doctor.py and the new test_eval_sandbox_cleanup.py include at least one assert or pytest.raises block; non-test helpers (_tiny_repo, _backend_factory, _quick_run_task) are not test functions.",
    "evidence": "Every added/modified test function contains assertions, e.g. test_a_fully_successful_cleanup_records_nothing has 'assert result == []' / 'assert not base.exists()', and test_cleanup_never_raises... uses 'with pytest.raises(RuntimeError, match=\"original\")'.",
    "file": "",
    "files_checked": [
      "tests/test_doctor.py",
      "tests/test_eval_sandbox_cleanup.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1049,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
