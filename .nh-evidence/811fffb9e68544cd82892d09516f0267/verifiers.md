# Verifiers

_Harness-captured record for task `811fffb9`, commit `139536b29691ba52d93b524e8e6b3bc2dd0b3187` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new and modified test functions in both test_doctor.py and test_eval_sandbox_cleanup.py contain at least one assert statement or pytest.raises block; the only assertion-free additions are non-test helpers (_tiny_repo, _backend_factory, _quick_run_task) which the statement does not cover.",
    "evidence": "Every added/modified test function contains assertions or pytest.raises, e.g. test_sandbox_residue_measures_files_not_directories has `assert residue[\"files\"] == 0` and test_cleanup_never_raises_and_never_masks_the_propagating_error uses `with pytest.raises(RuntimeError, match=\"original\")`.",
    "file": "",
    "files_checked": [
      "tests/test_doctor.py",
      "tests/test_eval_sandbox_cleanup.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 993,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
