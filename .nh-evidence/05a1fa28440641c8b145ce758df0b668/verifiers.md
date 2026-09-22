# Verifiers

_Harness-captured record for task `05a1fa28`, commit `522703d96728f91723afdfed6d0cafd5c413207b` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All modified test bodies in test_bounds.py and all newly added tests in test_scheduler_quota_probe.py contain at least one assert; test_structural_budget.py only changed frozen dict values, not test bodies.",
    "evidence": "Every added/modified test function contains assert statements, e.g. test_parse_quota_reset_the_issues_own_message ends with `assert r == datetime(2026, 9, 8, 9, 0, 0, tzinfo=timezone.utc)` and each new scheduler test (e.g. test_one_probe_dispatched_after_a_fallback_wall_lapses) has multiple `assert len(started) == ...` calls.",
    "file": "tests/test_scheduler_quota_probe.py",
    "files_checked": [
      "tests/test_bounds.py",
      "tests/test_scheduler_quota_probe.py",
      "tests/test_structural_budget.py"
    ],
    "line": 165,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 666,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the new or modified code in bounds.py, orchestrator.py, or scheduler.py writes a task status via update_task(validate=False); the changes are metadata stamps and dispatch-gating logic only, so the statement holds for this diff.",
    "evidence": "The diff adds metadata fields (reset_exact) and probe/dispatch/cooldown logic but contains no call to update_task with validate=False, and no status write bypassing set_status. The only update_task mention is a pre-existing comment ('`update_task` would rewrite the whole context blob') that argues against a full-blob write, not a status write.",
    "file": "",
    "files_checked": [
      "src/no_human/core/bounds.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/scheduler.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 887,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
