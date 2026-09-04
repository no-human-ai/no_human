# Verifiers

_Harness-captured record for task `1f762d5b`, commit `9178907ee479b7dad0c0185b39dd39e2b2ea04ec` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions in this new file include at least one assertion (or a pytest.raises/pytest.fail helper), so the no-empty-test invariant holds.",
    "evidence": "Every added test function contains assert statements, e.g. test_probe_healthy_on_200 has 'assert result.healthy is True' / 'assert result.checked_at'; test_status_endpoint_exposes_health_fields asserts on status_code and JSON fields.",
    "file": "tests/test_integrations_health.py",
    "files_checked": [
      "tests/test_integrations_health.py"
    ],
    "line": 63,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 310,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change adds a 'Failing' badge whose color comes from the pre-existing `integration-chip tone-error` class (already used by statusChip's Error state), and the only hex values touched (BRAND_COLOR) are unchanged and render nothing. No new hard-coded hex/rgb/hsl color is introduced, so both themes are unaffected.",
    "evidence": "New badge renders via existing tone class: `<span className={`integration-chip tone-${badge.tone}`}>` with tone \"error\", the same tone statusChip already returns for its Error state; healthBadge introduces no color literal.",
    "file": "web/src/integrationChip.js",
    "files_checked": [
      "web/src/Integrations.jsx",
      "web/src/integrationChip.js",
      "web/src/integrationChip.test.mjs",
      "web/src/integrations.test.mjs"
    ],
    "line": 37,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 581,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
