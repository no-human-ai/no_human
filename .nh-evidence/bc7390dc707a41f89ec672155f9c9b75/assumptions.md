# Assumptions

_Harness-captured record for task `bc7390dc`, commit `cc27598f341ca72495b523797b5a0dadb8e3a8d0` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** How should we determine whether a profile command is 'pytest-based' for the purpose of deciding between focused (change-scoped) gate vs. full gate? Should we use substring matching for 'pytest' anywhere in the command, or more precise patterns (e.g., command name is exactly 'pytest'/'py.test'/matches 'python -m pytest')? **A:** Use a pattern-based check for whether pytest is the actual test runner being invoked: check if the command name/first token is 'pytest' or 'py.test', or if the command contains 'python -m pytest' as a recognizable substring. This avoids false positives from substring matching 'pytest' anywhere (which could match wrapper scripts or comments), while remaining practical for complex commands like 'uv _(assumption)_
- **Q:** For the fallback case where the profile has no test_cmd and we default to `python -m pytest`, should we pre-check whether pytest is available (routing the check through the _run_pytest seam, not outside it), or simply attempt to run the fallback command and handle the failure through _run_pytest's error handling? **A:** Do not pre-check pytest availability outside the _run_pytest seam. For the fallback case (no profile test_cmd), simply attempt to run 'python -m pytest' through _run_pytest and let its existing error handling manage the failure. This avoids the regression where a subprocess pre-check ('import pytest') bypassed the single seam in frozen builds; the test stays GREEN because no interpreter subprocess _(assumption)_
- **Q:** Should the implementation be validated with actual test repositories that use different profile commands (e.g., npm test, uv run pytest -q -n 4), or can full validation be performed through unit tests with mocked profiles? **A:** HUMAN-GATED: not self-answerable

</details>

