# Verifiers

_Harness-captured record for task `a9772192`, commit `c4f5c983a92b6c2b533dad9a23bd457925c2099e` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added and modified test functions contain at least one assert statement; the string-only edits (task_title changes) leave the existing assertions intact, and the four new test functions each assert on results or via a pytest table loop.",
    "evidence": "New tests like test_non_conventional_subject_refuses_before_any_push contain 'assert result.ok is False' and test_conventional_subject_variants_are_accepted contains 'assert conventional_subject_error(subject) is None'; every modified test (e.g. test_commit_message_shape) retains its original assert statements.",
    "file": "tests/test_approve_merge.py",
    "files_checked": [
      "tests/test_approve_merge.py",
      "tests/test_approve_merge_identity_repro.py",
      "tests/test_pr_closed_on_completion.py"
    ],
    "line": 908,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 570,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The diff introduces only a pure string-validation helper and two regex constants; nothing new touches task status, so no path calls update_task(validate=False), and the statement holds vacuously for the changed code.",
    "evidence": "The only added code is the module-level regexes (_CONVENTIONAL_TYPE_RE/_CONVENTIONAL_SCOPE_RE) and the pure function conventional_subject_error(), which parses a commit subject string and returns None or an error message \u2014 it contains no calls to update_task, set_status, or any status-writing logic.",
    "file": "src/no_human/core/task.py",
    "files_checked": [
      "src/no_human/core/task.py"
    ],
    "line": 60,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 365,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
