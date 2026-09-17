# Verifiers

_Harness-captured record for task `0d637473`, commit `8f2da7fdddbb9618993ca10b17b684f2b595d91b` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 27 added test functions and the modified tests in test_telemetry.py contain at least one assertion or pytest.raises block; no assertion-free test was introduced.",
    "evidence": "Every def test_* in the new file and modified tests contains asserts/pytest.raises, e.g. test_never_started_emits_nothing has `assert _queue_lines(temp_home) == []` and test_unlisted_step_value_raises uses `with pytest.raises(ValueError, match=\"not allowed\")`.",
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
    "tokens_used": 1315,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "This change is purely funnel-telemetry plumbing; it introduces no colors at all, so the no-hardcoded-color / theme-token requirement is satisfied vacuously.",
    "evidence": "The diff adds only telemetry logic (onboardingFunnel.js, recordOnboardingStep, a useEffect/useRef step reporter, replayScrub classification) \u2014 no JSX className color changes, no inline style properties, and no hex/rgb/hsl color literals anywhere in the changed lines.",
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
    "tokens_used": 350,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
