# Verifiers

_Harness-captured record for task `ed0aa16a`, commit `36a382d1d755c87b66205f45ca1b57a19ebc8289` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All newly added test functions across the three new test files include at least one assert (or assert-on-CLI-output), and the two modified existing files change only data-table constants, not test-function bodies.",
    "evidence": "Every added test function contains assertions, e.g. test_record_at_delivery_fetches_before_resolving_the_tip ends with `assert patch == {\"pr_base_sha\": new_tip, \"pr_base_ref\": \"main\"}`; the changes to test_readme_claims.py and test_structural_budget.py touch only module-level data tables (CITATION_TABLE / FROZEN_* dicts), not any test function.",
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
    "tokens_used": 1649,
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
    "tokens_used": 963,
    "unavailable": true,
    "verifier_id": "no-unvalidated-status-write"
  },
  {
    "comment": "The diff introduces no color at all \u2014 it only adds two human-readable event-label strings \u2014 so no hard-coded hex/rgb/hsl literal is introduced and the statement holds vacuously.",
    "evidence": "The only change adds two string labels: pr_base_remeasured: \"PR base re-measured\" and pr_base_undetermined: \"PR base freshness undetermined\"",
    "file": "web/src/eventLabels.js",
    "files_checked": [
      "web/src/eventLabels.js"
    ],
    "line": 89,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 272,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
