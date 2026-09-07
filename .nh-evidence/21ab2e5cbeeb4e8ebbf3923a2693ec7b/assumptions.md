# Assumptions

_Harness-captured record for task `21ab2e5c`, commit `81d089acd586dce6de900078ccbfda89c53eb35f` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** For the test in test_onboarding_api.py that validates both darwin and non-darwin code paths on this host, which Python object should be monkeypatched to control the darwin decision—sys.platform, the home_skip() function itself, or does fs_suggest() need refactoring to accept darwin as a testable parameter? **A:** monkeypatch sys.platform to control the darwin decision; the code resolves darwin once at the call site as sys.platform == "darwin", so toggling sys.platform between "darwin" and an alternative value (e.g., "linux") in the test fixture tests both branches without refactoring fs_suggest _(assumption)_
- **Q:** Should the agent execute git commands (git add) and Python manifest scripts (check_release_manifest.py --write and --strict) as part of solution completion, or should these operations be left for human execution? **A:** HUMAN-GATED: not self-answerable

</details>

