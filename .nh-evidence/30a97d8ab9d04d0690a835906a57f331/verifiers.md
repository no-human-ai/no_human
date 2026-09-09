# Verifiers

_Harness-captured record for task `30a97d8a`, commit `be5201fecdd71ccb1ba4685db9e84990794b91a5` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added test function (six in test_pre_review_red_reaches_coder.py) has at least one assert; the test_structural_budget.py diff only edits data dicts, not test bodies.",
    "evidence": "All six added test functions contain assert statements, e.g. test_the_learning_marker_matches_the_orchestrator_label: 'assert _PRE_REVIEW_RED_LABEL.lower() in _INFRA_FINDING_MARKERS'",
    "file": "tests/test_pre_review_red_reaches_coder.py",
    "files_checked": [
      "tests/test_pre_review_red_reaches_coder.py",
      "tests/test_structural_budget.py"
    ],
    "line": 786,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 645,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code calls update_task (with or without validate=False) or writes a task status; the only added store call is update_attempt for test_results, so the invariant holds vacuously for this diff.",
    "evidence": "The only store write added is `await self.store.update_attempt(attempt_id, test_results={... \"classified\": False})` in the pre-review red block \u2014 an attempt test_results write, not a task-status write, and not via update_task.",
    "file": "src/no_human/core/orchestrator.py",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 13766,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 926,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  },
  {
    "comment": "The only color added in the diff is the new .verdict-unclassified rule, which uses var(--text-dim) \u2014 a token defined in both :root (#8C96B2) and [data-theme=\"light\"] (#5D697F). No hex/rgb/hsl literal is introduced, so it renders in both themes.",
    "evidence": ".verdict-unclassified { font-size: 12px; color: var(--text-dim); }",
    "file": "web/src/styles.css",
    "files_checked": [
      "web/src/slideOverSummary.js",
      "web/src/slideOverSummary.test.mjs",
      "web/src/styles.css"
    ],
    "line": 4011,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 332,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
