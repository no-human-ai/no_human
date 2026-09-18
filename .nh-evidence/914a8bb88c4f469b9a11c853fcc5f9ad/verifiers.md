# Verifiers

_Harness-captured record for task `914a8bb8`, commit `097bb77669a57c2a78062679f17261802346ce10` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "The only test function added in this change is test_web_e2e_job_runs_the_ci_lane_unconditionally, which contains several assert statements; the other diff hunk edits a data dict, not a test body.",
    "evidence": "test_web_e2e_job_runs_the_ci_lane_unconditionally contains assert \"web_e2e\" in jobs, assert \"if\" not in job, assert \"needs\" not in job, and assert any(\"npm run e2e:ci\" in r ...)",
    "file": "tests/test_ci_network_step_bounds.py",
    "files_checked": [
      "tests/test_ci_network_step_bounds.py"
    ],
    "line": 213,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 373,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified colors appear anywhere in the change \u2014 the extracted module holds only step metadata and the new/edited test files merely read source text or assert on existing tokens, so there is no hard-coded hex/rgb/hsl literal to break either theme.",
    "evidence": "The diff only moves BASE_STEPS (plain step data with no color) into onboardingSteps.js and adds test files; no JSX className, inline style, or CSS color declaration is added or modified. onboardingSteps.js contains only { key, title } objects.",
    "file": "",
    "files_checked": [
      "web/src/Onboarding.jsx",
      "web/src/e2eManifest.test.mjs",
      "web/src/onboardingConsent.test.mjs",
      "web/src/onboardingDiscord.test.mjs",
      "web/src/onboardingDocsKickoff.test.mjs",
      "web/src/onboardingEmailStep.test.mjs",
      "web/src/onboardingNav.test.mjs",
      "web/src/onboardingSteps.js"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 388,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
