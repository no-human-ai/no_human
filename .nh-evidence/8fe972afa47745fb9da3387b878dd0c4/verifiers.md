# Verifiers

_Harness-captured record for task `8fe972af`, commit `ddb91594053f28fa6aef162631355d1dcb2ef03c` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 12 test functions added in the new file have at least one assert/assertion; the two other files modified only touched non-test module-level constants, so no assertion-free test was added or modified.",
    "evidence": "Every new test function in test_pr_review_summaries.py contains assert statements, e.g. test_review_has_no_created_at_field: 'assert \"created_at\" not in _CHANGES_REQUESTED'; the changes to test_config.py and test_structural_budget.py only edited module-level data (a frozenset and the FROZEN_FILE_LINES dict), not test functions.",
    "file": "tests/test_pr_review_summaries.py",
    "files_checked": [
      "tests/test_config.py",
      "tests/test_pr_review_summaries.py",
      "tests/test_structural_budget.py"
    ],
    "line": 74,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 812,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is confined to bot-comment classification logic and introduces no task-status writes, so no `update_task(validate=False)` bypass of `set_status` is present; the statement holds vacuously.",
    "evidence": "The diff only adds `allow_comment_bot_authors` config parsing and a new `_is_bot_comment` helper, and rewires `_is_agent_or_bot_comment` to call it; none of the new/modified code calls `update_task` or writes a task status at all.",
    "file": "src/no_human/blockers/wake.py",
    "files_checked": [
      "src/no_human/blockers/wake.py"
    ],
    "line": 1283,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 412,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
