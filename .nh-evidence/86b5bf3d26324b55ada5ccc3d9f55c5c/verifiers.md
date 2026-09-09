# Verifiers

_Harness-captured record for task `86b5bf3d`, commit `07e19290c7a8743ef2d82a277e7649a4d8b65d31` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions in the new file and the three added tests in test_reanchor_citations.py contain at least one assert statement; the assertion-free functions (_git, _config, _run_one_task_attempt, _write_table, etc.) are helpers, not test functions.",
    "evidence": "Every added/modified test_* function contains assertions, e.g. test_touched_cited_is_the_sorted_intersection: `assert citations.touched_cited(cited, changed) == [\"pkg/a.py\", \"pkg/b.py\"]`",
    "file": "tests/test_citation_drift_preflight.py",
    "files_checked": [
      "tests/test_citation_drift_preflight.py",
      "tests/test_egress_allowlist.py",
      "tests/test_reanchor_citations.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1116,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The added `_citation_drift_preflight` and helpers introduce no `update_task(validate=False)` call; the sole status write targets attempts (`update_attempt`), and task-status transitions flow through the returned TaskOutcome rather than an unvalidated direct write.",
    "evidence": "The only status write in the new code is `await self.store.update_attempt(attempt_id, status=\"failed\", failure_reason=detail)`, which writes an ATTEMPT status, not a task status; the task status is returned via `TaskOutcome(task, status=TaskStatus.FAILED, detail=detail)`. No new/modified code calls `update_task` with `validate=False`.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 656,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
