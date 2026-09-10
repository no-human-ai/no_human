# Verifiers

_Harness-captured record for task `c4873934`, commit `077e54b89a658e6f07ac3e6c5dbfca51872962fe` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new test functions in the funnel-telemetry module and all new/modified test functions in test_telemetry.py contain at least one assert or pytest.raises block; the changes in test_structural_budget.py and test_telemetry_environment.py are data-dict edits, not test functions.",
    "evidence": "Every added/modified test function carries an assertion, e.g. test_step_viewed_emits_the_step asserts `recorded.count(...) == 1`, test_onboarding_step_viewed_rejects_free_text_step uses `pytest.raises(ValueError)`, and test_onboarding_steps_match_the_wizard_steps asserts `keys == telemetry.ONBOARDING_STEPS`.",
    "file": "tests/test_onboarding_funnel_telemetry.py",
    "files_checked": [
      "tests/test_onboarding_funnel_telemetry.py",
      "tests/test_structural_budget.py",
      "tests/test_telemetry.py",
      "tests/test_telemetry_environment.py"
    ],
    "line": 106,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 736,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified colors are introduced by this change, so the theme-token constraint is vacuously satisfied \u2014 nothing added affects light/dark rendering.",
    "evidence": "The diff only adds telemetry wiring (recordOnboardingStep/verifyAuthLive), two useEffect hooks, an api.js export, and test files \u2014 no JSX className color changes, no inline style colors, and no hex/rgb/hsl literals anywhere in the change.",
    "file": "",
    "files_checked": [
      "web/src/Onboarding.jsx",
      "web/src/api.js",
      "web/src/api.test.mjs",
      "web/src/onboardingFunnelWiring.test.mjs"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 310,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
