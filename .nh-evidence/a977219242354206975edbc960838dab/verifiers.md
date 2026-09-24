# Verifiers

_Harness-captured record for task `a9772192`, commit `0f6ef66a679e0d333832ab0aeff3fa36b56e755a` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All six newly added test functions contain assert statements, and every modified test function retained its original assertions since the edits only changed title literals from 'Add feature' to 'feat: add feature'.",
    "evidence": "Every added test has assertions, e.g. test_conventional_subject_variants_are_accepted loops with 'assert conventional_subject_error(subject) is None, subject' and 'assert conventional_subject_error(subject) is not None, subject'; modified tests retain their existing asserts (only the task_title string changed).",
    "file": "tests/test_approve_merge.py",
    "files_checked": [
      "tests/test_approve_merge.py",
      "tests/test_approve_merge_identity_repro.py",
      "tests/test_pr_closed_on_completion.py"
    ],
    "line": 972,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 596,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The diff adds a pure string-validation helper for Conventional Commits subjects and nothing else; no code writes a task status, so it cannot introduce an update_task(validate=False) status write bypassing set_status.",
    "evidence": "The only new/modified code is the addition of `conventional_subject_error(subject)` plus `_CONVENTIONAL_TYPE_RE`/`_CONVENTIONAL_SCOPE_RE`; it performs commit-subject string validation and contains no call to `update_task`, no `validate=False`, and no status write of any kind.",
    "file": "src/no_human/core/task.py",
    "files_checked": [
      "src/no_human/core/task.py"
    ],
    "line": 50,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 403,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
