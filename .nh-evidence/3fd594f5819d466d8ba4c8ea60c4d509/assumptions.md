# Assumptions

_Harness-captured record for task `3fd594f5`, commit `cca5bccad63f393b6b52da701b2f0a423fe2edb2` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** Will you preserve the single-extraMetadata design (stated in the file header) by computing nhCanAutoUpdate per-platform within it, or restructure to platform-specific metadata blocks for Windows and Linux? If restructuring, how does that address the header's argument for the single-payload design? **A:** Preserve the single-extraMetadata design by computing nhCanAutoUpdate per-platform within it using conditional logic. Introduce platform-specific conditionals that check the target platform and only set nhCanAutoUpdate=true for macOS when signed, while forcing it false for Windows and Linux regardless of environment. This respects the stated architectural constraint in the file header without sile _(assumption)_
- **Q:** Should the test verify that Windows and Linux targets keep nhCanAutoUpdate=false when Apple credentials are absent (in addition to testing with credentials), to confirm the value is always false regardless of environment? **A:** Yes, the test should verify both scenarios: with Apple credentials present (to confirm the fix applies them only to macOS) and without credentials (to confirm Windows and Linux always get false). Testing only the credentialed case leaves a gap where a future environment change could reintroduce the bug. Both credential states are needed for robustness. _(assumption)_

</details>

