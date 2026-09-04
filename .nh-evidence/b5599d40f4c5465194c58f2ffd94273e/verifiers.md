# Verifiers

_Harness-captured record for task `b5599d40`, commit `6c038af96463fa8b0e1c9ab0545d1682c367a567` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All nine new test functions include at least one assert or pytest.raises block; the changes in test_structural_budget.py only edit frozen dict values, adding/modifying no test functions.",
    "evidence": "Every added test function in test_setup_mode_boot.py contains assertions, e.g. test_scrub_still_runs_on_the_missing_credential_path uses `with pytest.raises(MissingCredentialError, ...)` and `assert \"ANTHROPIC_AUTH_TOKEN\" not in ...`",
    "file": "tests/test_setup_mode_boot.py",
    "files_checked": [
      "tests/test_setup_mode_boot.py",
      "tests/test_structural_budget.py"
    ],
    "line": 293,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 621,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The only new/modified code in this diff is the per-tick auth check, which idles by returning an empty list and emitting events \u2014 it never writes a task status, so it cannot bypass set_status via update_task(validate=False).",
    "evidence": "The added dispatch-pause block only returns [] and calls self._on_event(\"setup_required\"/\"setup_complete\", ...); it contains no update_task call at all, let alone one with validate=False.",
    "file": "src/no_human/core/scheduler.py",
    "files_checked": [
      "src/no_human/core/scheduler.py"
    ],
    "line": 2093,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 356,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
