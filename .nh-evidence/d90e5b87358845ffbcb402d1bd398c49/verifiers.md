# Verifiers

_Harness-captured record for task `d90e5b87`, commit `71588a0fbf8772facfb1db8efb1fcdf54b818e85` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "The change modifies exactly one test function, and it retains/adds multiple assertions and a pytest.raises block, satisfying the requirement.",
    "evidence": "The one modified test function `test_check_credential_consults_the_reviewers_role_backend` contains `with pytest.raises(GateUnavailable) as exc_info:` plus `assert calls == [(\"codex\", _CodexConfig.data)]`, `assert \"test double\" in str(exc_info.value)`, and `assert \"claude CLI\" not in str(exc_info.value)`.",
    "file": "tests/test_gate_oneshot.py",
    "files_checked": [
      "tests/test_gate_oneshot.py"
    ],
    "line": 1339,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 394,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
