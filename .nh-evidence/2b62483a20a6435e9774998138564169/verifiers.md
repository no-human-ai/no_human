# Verifiers

_Harness-captured record for task `2b62483a`, commit `6164ddcc213d1168d554ac51a2d6e223928be0a0` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "The only test functions added are the two /api/metrics tokens_total tests, each with multiple assert statements; the test_structural_budget.py change is a data-dict value edit, not a test function.",
    "evidence": "Both added tests assert: `assert r.json()[\"tokens_total\"] == expected` and `assert r.json()[\"tokens_total\"] == 0`",
    "file": "tests/test_api.py",
    "files_checked": [
      "tests/test_api.py",
      "tests/test_structural_budget.py"
    ],
    "line": 2112,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 389,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The diff only adds a summed token metric inside compute_metrics, which is a read-only SQL aggregation module; no new or modified code calls update_task or writes any task status at all, so the statement holds vacuously.",
    "evidence": "The only change adds a read-only aggregate key: \"tokens_total\": (total_tokens + total_cache_read + sum_creation + rev_used + rev_creation + rev_read + aux_used + aux_creation + aux_read)",
    "file": "src/no_human/core/metrics.py",
    "files_checked": [
      "src/no_human/core/metrics.py"
    ],
    "line": 363,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 345,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  },
  {
    "comment": "The change only touches token/dollar text, labels, and a bar width; it introduces no new or modified hex/rgb/hsl color literal, so the both-themes color contract is unaffected.",
    "evidence": "The only inline style modified in the diff is a width computation (`style={{ width: max > 0 ? `${((realDollars ? r.cost : r.tokens) / max) * 100}%` : 0 }}`) in Stats.jsx; no color literal is added or changed anywhere in the diff. Pre-existing colors (KIND_COLORS, STATUS_DOT) already use var(--...) tokens and are untouched.",
    "file": "web/src/Stats.jsx",
    "files_checked": [
      "web/src/App.jsx",
      "web/src/Outcomes.jsx",
      "web/src/Stats.jsx",
      "web/src/TaskTable.jsx",
      "web/src/cost.js",
      "web/src/cost.test.mjs",
      "web/src/costGroups.js",
      "web/src/costGroups.test.mjs",
      "web/src/ledgerSpend.js",
      "web/src/northStar.js",
      "web/src/northStar.test.mjs"
    ],
    "line": 326,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 617,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
