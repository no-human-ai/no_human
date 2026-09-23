# Verifiers

_Harness-captured record for task `8fe972af`, commit `446107e73eff0e976d97137b40dbc8103f050f1d` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 13 newly added test functions in test_pr_review_summaries.py carry assertions; the edits to test_config.py and test_structural_budget.py only change module-level data (a frozenset and frozen-size dicts), not any test function body, so no assertion-free test function is introduced.",
    "evidence": "Every added test in test_pr_review_summaries.py contains at least one assert, e.g. test_review_fixture_pins_no_created_at_field ends with `assert \"created_at\" not in _CHANGES_REQUESTED`",
    "file": "tests/test_pr_review_summaries.py",
    "files_checked": [
      "tests/test_config.py",
      "tests/test_pr_review_summaries.py",
      "tests/test_structural_budget.py"
    ],
    "line": 235,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 935,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The modified code is purely about classifying PR comments as bot vs. human and never touches task-status persistence, so there is no new status write bypassing set_status/the transition table.",
    "evidence": "The diff only adds `allow_comment_bot_authors` and the `_is_bot_comment` helper (login/author_type-based bot detection) and rewires `_is_bot_or_agent_comment` to call it; no changed line calls `update_task(...)`, references `validate=False`, or writes any task status.",
    "file": "src/no_human/blockers/wake.py",
    "files_checked": [
      "src/no_human/blockers/wake.py"
    ],
    "line": 1283,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 468,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
