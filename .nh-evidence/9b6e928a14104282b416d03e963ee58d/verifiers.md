# Verifiers

_Harness-captured record for task `9b6e928a`, commit `0482582cbda6251961c06bfeb9f78594e395fc3f` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Across all new/changed test files, each test function includes at least one assert; test_readme_claims.py and test_structural_budget.py changed only data tables, not test bodies. Statement holds.",
    "evidence": "Every added/modified test function carries assert statements, e.g. test_finalize_calls_record_at_delivery_and_merges_its_result has `assert out.status == TaskStatus.AWAITING_APPROVAL` and `assert calls == [(str(work), \"main\")]`; test_info_unset_sentinel_is_not_none has `assert _INFO_UNSET is not None`.",
    "file": "tests/test_finalize_records_delivered_base.py",
    "files_checked": [
      "tests/test_finalize_records_delivered_base.py",
      "tests/test_pr_base_event_kinds_indexed.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_wake_base_stale.py",
      "tests/test_wake_base_stale_followups.py"
    ],
    "line": 123,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1571,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "",
    "evidence": "no verdict after retry: verifier 'no-unvalidated-status-write' (defined in /Users/eyalgolan/.no_human/verifiers.yaml) never produced a parseable verdict \u2014 no verdict: no VERIFIER_JSON_START marker found",
    "file": "",
    "files_checked": [
      "src/no_human/blockers/wake.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": true,
    "passed": false,
    "severity": "high",
    "tokens_used": 1180,
    "unavailable": true,
    "verifier_id": "no-unvalidated-status-write"
  },
  {
    "comment": "The diff introduces no colors at all \u2014 only text label entries \u2014 so no hard-coded color literal is added and the theme-color condition holds vacuously.",
    "evidence": "The only change adds two string labels: pr_base_remeasured: \"PR base re-measured\" and pr_base_undetermined: \"PR base freshness undetermined\"; no color literals (hex/rgb/hsl), className, or style are added.",
    "file": "web/src/eventLabels.js",
    "files_checked": [
      "web/src/eventLabels.js"
    ],
    "line": 89,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 292,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
