# Verifiers

_Harness-captured record for task `3b5f49b4`, commit `b1da4613bee932c6bc8fdce18302ad5e71d2c18c` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All five test functions added by the diff (four async gate tests plus the source-count test) contain multiple assert statements; the only other changed file (test_structural_budget.py) modifies a frozen-value comment/number, not any test body.",
    "evidence": "test_the_sibling_branch_decision_exists_in_exactly_one_place: assert src.count(\"remote_branches_containing\") == 1",
    "file": "tests/test_already_satisfied_subject_tree.py",
    "files_checked": [
      "tests/test_already_satisfied_subject_tree.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 760,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The changed code deals purely with git branch/remote verification and never touches task-status writes, so it introduces no `update_task(validate=False)` status change; the statement holds vacuously for this diff.",
    "evidence": "The diff only modifies the delivery-branch resolution logic in `_delivered_head` (branch_sha/head comparison, remote_branch_relation, sibling-branch fallback). It contains no calls to `update_task` or `set_status` and does not write any task status.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 986,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
