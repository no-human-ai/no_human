# Verifiers

_Harness-captured record for task `0d637473`, commit `34dd24dc85d7138d8e1394d4f7ab53157111555a` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All test functions added in the new file and the modified tests in test_telemetry.py contain assert statements or pytest.raises blocks; the modifications to test_telemetry_environment.py and test_structural_budget.py only touch data dicts, not test bodies.",
    "evidence": "Every added test function contains at least one assertion, e.g. test_never_started_emits_nothing has `assert _queue_lines(temp_home) == []` and test_unlisted_step_value_raises uses `with pytest.raises(ValueError, match=\"not allowed\")`.",
    "file": "tests/test_onboarding_funnel_telemetry.py",
    "files_checked": [
      "tests/test_onboarding_funnel_telemetry.py",
      "tests/test_structural_budget.py",
      "tests/test_telemetry.py",
      "tests/test_telemetry_environment.py"
    ],
    "line": 179,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1067,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "Purely behavioral/telemetry change with no styling or color touched, so the light/dark theme color-token constraint is vacuously satisfied.",
    "evidence": "The diff adds only onboarding funnel telemetry (recordOnboardingStep, makeStepReporter, a useEffect/useRef wiring, a replayScrub classification entry, and tests). No JSX className, inline style, or CSS color is added or modified, and no hex/rgb/hsl literal appears anywhere in the change.",
    "file": "",
    "files_checked": [
      "web/src/Onboarding.jsx",
      "web/src/api.js",
      "web/src/onboardingFunnel.js",
      "web/src/onboardingFunnel.test.mjs",
      "web/src/replayScrub.js"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 335,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
