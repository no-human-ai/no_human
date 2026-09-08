# Verifiers

_Harness-captured record for task `e9e90630`, commit `6938b03744c2f870a1db95374c9ec07ae2003d6d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added/modified `test_*` function contains at least one assert; the only assertion-free additions are the `_make_conflicting_overlap_branch` helper and edited comment blocks, which are not test functions.",
    "evidence": "test_the_coder_preamble_decides_with_should_rebase_too has `assert fn is not None`, `assert \"should_rebase\" in called`, `assert not bare`; test_a_conflicting_overlap_rebase_still_tells_the_coder has multiple asserts (e.g. `assert attempt[\"failure_reason\"] is None`, `assert \"calc.py\" in prompt`)",
    "file": "tests/test_retry_base_staleness.py",
    "files_checked": [
      "tests/test_base_staleness_overlap.py",
      "tests/test_readme_claims.py",
      "tests/test_retry_base_staleness.py",
      "tests/test_structural_budget.py"
    ],
    "line": 300,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 616,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the modified code writes a task status; the changes concern rebase/staleness measurement and prompt text, so there is no update_task(validate=False) write to flag.",
    "evidence": "Diff changes are limited to BASE_STALENESS_REBASE_THRESHOLD comments, the staleness_record(...) call gaining overlapping_files=overlap, and the should_rebase(...) preamble branch \u2014 no update_task or set_status calls appear in the changed lines.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 3260,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 583,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
