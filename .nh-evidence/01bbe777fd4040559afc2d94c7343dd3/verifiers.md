# Verifiers

_Harness-captured record for task `01bbe777`, commit `bb8321a81ed61eb6dad8cd94730915cf4298fae3` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All five new tests in test_citation_root_mismatch.py, the new test in test_citation_rule.py, and the modified tests in test_gate_severity.py and test_reviewer.py each contain assert statements; test_structural_budget.py only edits a data dict, adding/modifying no test function.",
    "evidence": "Every added/modified test contains assertions, e.g. test_a_dirty_worktree_is_detected_and_a_clean_one_is_not: 'assert _citation_root_mismatch(repo) == \"\"'; test_passed_due_to_demotion_distinguishes_a_demoted_pass_from_a_clean_one: 'assert demoted.passed_due_to_demotion is True'; test_linked_repo_citation_demoted_only_without_the_linked_repo: 'assert demoted_single and single.severity == \"low\"'; test_angle_passes_skipped_after_a_decided_fail: 'assert d.passed is False'.",
    "file": "tests/test_citation_root_mismatch.py",
    "files_checked": [
      "tests/test_citation_root_mismatch.py",
      "tests/test_citation_rule.py",
      "tests/test_gate_severity.py",
      "tests/test_reviewer.py",
      "tests/test_structural_budget.py"
    ],
    "line": 116,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 891,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
