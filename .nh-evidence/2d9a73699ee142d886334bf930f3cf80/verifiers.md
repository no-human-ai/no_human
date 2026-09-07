# Verifiers

_Harness-captured record for task `2d9a7369`, commit `af78f7e61ff35608aec14157b028daf34de06177` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All five added test functions include assert statements and/or pytest.raises blocks; the egress_allowlist change is only an edit to a string literal, not a test function.",
    "evidence": "Each new test_ function contains assertions, e.g. test_the_real_pre_commit_gate_refuses_the_control_commit uses `with pytest.raises(GitError, match=\"REFUSED\")` and `assert parse_manifest_refusal(str(excinfo.value)) == [\"src/pkg/mod.py\"]`",
    "file": "tests/test_vcs.py",
    "files_checked": [
      "tests/test_egress_allowlist.py",
      "tests/test_vcs.py"
    ],
    "line": 1653,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 617,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
