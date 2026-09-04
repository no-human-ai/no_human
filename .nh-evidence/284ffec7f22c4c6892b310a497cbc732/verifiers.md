# Verifiers

_Harness-captured record for task `284ffec7`, commit `04533e041c6152b2af9716f2c4694ce1dfd78e13` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "The two newly added test functions each contain multiple assert statements. The modified `_watcher` and new `_derived_shape` are non-test helpers (no `test_` prefix, not invoked as tests), so the assertion requirement does not apply to them.",
    "evidence": "test_a_failed_mechanical_resolution_escalation_names_the_detail asserts `out == \"escalated_pr_conflict\"` and `DISTINCTIVE in event_texts[0]`; test_an_over_long_resolution_detail_is_capped_in_the_escalation asserts `\"X\" * 500 in text and \"X\" * 501 not in text`",
    "file": "tests/test_wake_conflict.py",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_wake_conflict.py"
    ],
    "line": 545,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 596,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The new/modified code routes the status transition through store.set_status (unchanged context line) and adds no update_task status write, so the statement holds. Note set_status is still passed validate=False, but that predates this diff and isn't an update_task call.",
    "evidence": "The status write in the touched hunk is `await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)`; the modified lines only change data[\"question\"], data[\"evidence\"], and the event's extra dict \u2014 no update_task(..., validate=False) status write is introduced.",
    "file": "src/no_human/blockers/wake.py",
    "files_checked": [
      "src/no_human/blockers/wake.py"
    ],
    "line": 2292,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 906,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
