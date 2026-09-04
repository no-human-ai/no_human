# Verifiers

_Harness-captured record for task `1f762d5b`, commit `9178907ee479b7dad0c0185b39dd39e2b2ea04ec` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions in this new file include at least one assert statement (and helpers like pytest.fail/pytest.raises where relevant); no assertion-free test exists.",
    "evidence": "Every added test contains assertions, e.g. test_probe_healthy_on_200 has 'assert result.healthy is True' and 'assert result.checked_at'; test_disabled_integration_is_never_probed has 'assert \"jira\" not in h.probe_targets(cfg)' plus pytest.fail-based boom; test_probe_failure_never_blocks_start has 'assert task is None'.",
    "file": "tests/test_integrations_health.py",
    "files_checked": [
      "tests/test_integrations_health.py"
    ],
    "line": 62,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 392,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change only reuses the pre-existing `integration-chip tone-error` class (same tone statusChip already emits) and introduces no new color literal; the BRAND_COLOR hexes are pre-existing and untouched.",
    "evidence": "New badge renders via existing class: `<span className={`integration-chip tone-${badge.tone}`}>` with badge.tone === \"error\", already used by statusChip's Error state; no new hex/rgb/hsl literal is added anywhere in the diff.",
    "file": "web/src/Integrations.jsx",
    "files_checked": [
      "web/src/Integrations.jsx",
      "web/src/integrationChip.js",
      "web/src/integrationChip.test.mjs",
      "web/src/integrations.test.mjs"
    ],
    "line": 205,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 447,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
