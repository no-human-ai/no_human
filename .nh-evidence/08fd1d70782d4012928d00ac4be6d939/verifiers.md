# Verifiers

_Harness-captured record for task `08fd1d70`, commit `648ef2c9861104d0a256caeff5867cd27ae10e12` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added test functions in test_landability.py and the new --ready tests in test_approve_ready_cli.py contain multiple assert statements; the test_readme_claims.py and test_structural_budget.py diffs only edit data tables, not test function bodies.",
    "evidence": "Every added test_* function contains assert statements, e.g. test_probe_writes_no_refs_and_leaves_worktree_clean ends with 'assert after_refs == before_refs' / 'assert after_branch == before_branch' / 'assert after_status == before_status'",
    "file": "tests/test_landability.py",
    "files_checked": [
      "tests/test_approve_ready_cli.py",
      "tests/test_landability.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py"
    ],
    "line": 187,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1056,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
