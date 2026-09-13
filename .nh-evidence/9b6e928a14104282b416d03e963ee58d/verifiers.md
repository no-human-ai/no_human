# Verifiers

_Harness-captured record for task `9b6e928a`, commit `9ea0906e833c81e89ad9e51407a80877c6605dbd` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 16 added test functions carry at least one assert; the two other modified files (test_readme_claims.py, test_structural_budget.py) change only module-level data (CITATION_TABLE / FROZEN_* dicts), not any test function body.",
    "evidence": "Every test_* function in the new tests/test_wake_base_stale.py contains asserts, e.g. test_delivery_records_the_trunk_tip_it_was_measured_against: `assert patch == {\"pr_base_sha\": expected, \"pr_base_ref\": \"main\"}`",
    "file": "tests/test_wake_base_stale.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_wake_base_stale.py"
    ],
    "line": 189,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1190,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "",
    "evidence": "no verdict after retry: verifier 'no-unvalidated-status-write' (defined in /Users/eyalgolan/.no_human/verifiers.yaml) never produced a parseable verdict \u2014 no verdict: no VERIFIER_JSON_START marker found",
    "file": "",
    "files_checked": [
      "src/no_human/blockers/wake.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": true,
    "passed": false,
    "severity": "high",
    "tokens_used": 1103,
    "unavailable": true,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
