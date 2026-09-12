# Verifiers

_Harness-captured record for task `7ee2d939`, commit `3ca689e203ca76d6c9b3e354b8be8f5d05e11e92` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new tests in test_onboarding_funnel_telemetry.py and the modified tests in test_telemetry.py (test_allowlist_is_the_documented_closed_set, test_client_allowlist_matches_the_deployed_lambda_contract) contain at least one assertion or pytest.raises block; the edits to test_structural_budget.py and _MIN_PROPS in test_telemetry_environment.py are data/dict changes, not test functions.",
    "evidence": "Every added/modified test function contains asserts or pytest.raises, e.g. test_never_started_emits_nothing: `assert _queue_lines(temp_home) == []`; test_unlisted_step_value_raises uses `with pytest.raises(ValueError, match=\"not allowed\")`.",
    "file": "tests/test_onboarding_funnel_telemetry.py",
    "files_checked": [
      "tests/test_onboarding_funnel_telemetry.py",
      "tests/test_structural_budget.py",
      "tests/test_telemetry.py",
      "tests/test_telemetry_environment.py"
    ],
    "line": 148,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1336,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No color literals (hex, rgb, hsl) or color-bearing styles are introduced by this change, so the theming concern is vacuously satisfied.",
    "evidence": "The diff only adds telemetry step-reporting logic (makeStepReporter, recordOnboardingStep, a useRef/useEffect in Onboarding.jsx) and a new pure module/test file \u2014 no JSX className, inline style, or CSS color is added or modified anywhere.",
    "file": "",
    "files_checked": [
      "web/src/Onboarding.jsx",
      "web/src/api.js",
      "web/src/onboardingFunnel.js",
      "web/src/onboardingFunnel.test.mjs"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 304,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
