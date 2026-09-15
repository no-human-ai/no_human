# Verifiers

_Harness-captured record for task `ed0aa16a`, commit `6dc7ab5adefb3dd9496a385ee56b60a1dd219acb` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions across the three new test files carry at least one assert or assertion-bearing call; the two modified files only edited citation/budget data, not test logic, so no assertion-free test was added or modified.",
    "evidence": "Every added test function contains asserts, e.g. test_record_at_delivery_fetches_before_resolving_the_tip ends with `assert patch == {\"pr_base_sha\": new_tip, \"pr_base_ref\": \"main\"}`; the modified files (test_readme_claims.py, test_structural_budget.py) only changed data tables/line numbers, no test bodies.",
    "file": "tests/test_finalize_records_delivered_base.py",
    "files_checked": [
      "tests/test_finalize_records_delivered_base.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_wake_base_stale.py",
      "tests/test_wake_base_stale_followups.py"
    ],
    "line": 34,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1348,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the added/modified code writes a task status at all \u2014 the new PR-base-staleness rung is observational, writing only context via merge_context, and the orchestrator change only updates a context dict. So the claim about status changes routing through set_status is not violated.",
    "evidence": "The new rung 4.5 (`_check_base_stale`) and its helper only write task context via `self.store.merge_context(task.id, ...)` and emit events; its docstring states 'NEVER RESUMES, ESCALATES, OR TOUCHES CONFLICT-ROUND STATE.' The orchestrator change only does `ctx.update(await delivered_base.record_at_delivery(...))`. No `update_task(..., validate=False)` or any status write appears in the diff.",
    "file": "",
    "files_checked": [
      "src/no_human/blockers/wake.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 647,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  },
  {
    "comment": "The diff only adds text label mappings; no color literals, className, inline styles, or CSS are introduced, so the theme-token requirement is vacuously satisfied.",
    "evidence": "The only change adds two string labels to EVENT_LABELS: pr_base_remeasured: \"PR base re-measured\" and pr_base_undetermined: \"PR base freshness undetermined\"",
    "file": "web/src/eventLabels.js",
    "files_checked": [
      "web/src/eventLabels.js"
    ],
    "line": 89,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 248,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
