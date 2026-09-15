# Verifiers

_Harness-captured record for task `beedca29`, commit `dc264a199aa8a85cea2bc9c6ff5ba69afcf2af11` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All twelve test functions in the new file contain at least one assert; the only change to test_structural_budget.py is a frozen dict value, not a test function, so no assertion-free test was added or modified.",
    "evidence": "Every added test function contains an assert, e.g. test_the_guard_can_actually_see_an_offender: `assert found == [\"~12034\"]` and `assert clean == []`; test_a_bare_grep_over_counts ends with `assert live_bare_hits >= 20`.",
    "file": "tests/test_no_approximate_line_anchors.py",
    "files_checked": [
      "tests/test_no_approximate_line_anchors.py",
      "tests/test_structural_budget.py"
    ],
    "line": 231,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 667,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is comment-only refactoring of code-reference citations; it introduces no status-write code at all, so it cannot bypass set_status via update_task(validate=False).",
    "evidence": "Every hunk in the diff edits only comments/docstrings (e.g. replacing '~2884-2892' with 'db.py::_lifetime_included_sql', '~4407' with \"_run_attempt's branch_prefix usage\", etc.); no executable statement is added or changed, so no new/modified line calls update_task(validate=False).",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 588,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
