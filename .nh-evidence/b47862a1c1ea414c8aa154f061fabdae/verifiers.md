# Verifiers

_Harness-captured record for task `b47862a1`, commit `aff8022f0c220fdc7f0b6994179245780b887122` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All newly added test functions contain at least one assert statement or pytest.raises block; the only non-asserting additions are helpers (_seed_escalated), which are not test functions.",
    "evidence": "Every added test_ function (e.g. test_escalated_task_with_landed_content_completes) contains asserts or pytest.raises; test_escalated_cancelled_task_is_refused uses `with pytest.raises(OverrideRefused, match=\"cancelled\")` plus `assert fresh.status is TaskStatus.ESCALATED`",
    "file": "tests/test_landed_override.py",
    "files_checked": [
      "tests/test_landed_override.py",
      "tests/test_structural_budget.py"
    ],
    "line": 1584,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 859,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The added/modified code introduces the escalated_hand_landed shape via _resolve_shape and its audit text, none of which writes status directly; status transitions continue to flow through set_status, and no update_task(validate=False) is introduced.",
    "evidence": "The new ESCALATED branch only returns the shape string \"escalated_hand_landed\"; no update_task(..., validate=False) call appears anywhere in the diff, and the docstring notes only the pre-existing done_no_evidence shape bypasses Store.set_status while the other shapes (including the new escalated one) go through set_status.",
    "file": "src/no_human/blockers/landed_override.py",
    "files_checked": [
      "src/no_human/blockers/landed_override.py"
    ],
    "line": 314,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 1331,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
