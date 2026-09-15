# Verifiers

_Harness-captured record for task `12ae1f0f`, commit `4dbfa768159c6ea7b2155e5e15efd11d03e112d7` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All eight test functions added in this diff (including the parametrized ones and the non-fixture assertion-only test) contain at least one assert statement; no test was added or modified without an assertion.",
    "evidence": "Each added test contains assert statements, e.g. test_the_gc_exclusions_are_exact_label_scoped_names: 'assert \"*\" not in name and \"?\" not in name' and 'assert rw._VOLATILE_GIT_PREFIX == \"logs/\"'",
    "file": "tests/test_reviewer_worktree.py",
    "files_checked": [
      "tests/test_reviewer_worktree.py"
    ],
    "line": 254,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 578,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code writes task status; the change is purely git worktree integrity-guard logic, so no update_task(validate=False) status write exists to violate the rule.",
    "evidence": "The entire diff modifies src/no_human/core/reviewer_worktree.py (Snapshot.alternates, _VOLATILE_COMMON_EXACT, _is_volatile_git_path, snapshot(), compare()) \u2014 no reference to update_task, set_status, validate=, or task status anywhere.",
    "file": "",
    "files_checked": [
      "src/no_human/core/reviewer_worktree.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 477,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
