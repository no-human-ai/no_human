# Verifiers

_Harness-captured record for task `1f762d5b`, commit `1908563da78c005252a3a67084928c3b1c6278dd` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions in the new file (only added file in the diff) contain at least one assert statement; several also use pytest.fail helpers. No assertion-free test exists.",
    "evidence": "Every added test contains assertions, e.g. test_probe_healthy_on_200 has 'assert result.healthy is True' and 'assert result.checked_at'; test_ensure_fresh_before_poll... uses multiple 'assert calls == [...]'; test_probe_failure_never_blocks_start has 'assert task is None'.",
    "file": "tests/test_integrations_health.py",
    "files_checked": [
      "tests/test_integrations_health.py"
    ],
    "line": 66,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 332,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change introduces no new color literal \u2014 the Failing badge reuses the pre-existing `integration-chip tone-error` class (already used by statusChip's Error state), and the only BRAND_COLOR hex values are unmodified and render nothing. Colors keep flowing through existing theme-defined classes.",
    "evidence": "New badge reuses the existing className pattern: `<span className={`integration-chip tone-${badge.tone}`}>` with tone: \"error\", the same tone statusChip already emits; no new hex/rgb/hsl literal is added.",
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
    "tokens_used": 654,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
