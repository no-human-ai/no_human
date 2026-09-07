# Verifiers

_Harness-captured record for task `05b71017`, commit `298518c1cd941d9e68b57de23a3c0552f24a5602` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All nine newly added test functions include at least one assert (and one also uses pytest.raises/pytest.fail); the egress-allowlist diff only edits dict data, not test functions.",
    "evidence": "Every added test in test_vcs.py contains assertions, e.g. test_a_new_untracked_file_lands_pinned_and_passes_strict has `assert result.sha`, and the protected-branch test uses `with pytest.raises(ProtectedBranch):`",
    "file": "tests/test_vcs.py",
    "files_checked": [
      "tests/test_egress_allowlist.py",
      "tests/test_vcs.py"
    ],
    "line": 1811,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 709,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
