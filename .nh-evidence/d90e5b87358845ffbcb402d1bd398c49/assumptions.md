# Assumptions

_Harness-captured record for task `d90e5b87`, commit `71588a0fbf8772facfb1db8efb1fcdf54b818e85` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** What is the local file path to the repository root containing src/no_human/review/oneshot.py and tests/test_gate_oneshot.py? **A:** HUMAN-GATED: not self-answerable
- **Q:** At which level should the test patch to make codex unavailable: at `assert_task_backend_usable` (the function _check_credential calls), or deeper at `find_codex_cli` or inside `assert_codex_mode` (which are called internally)? **A:** Patch at `assert_task_backend_usable`: it is the seam that _check_credential explicitly calls to verify backend availability. Patching there makes codex deterministically unavailable to _check_credential while preserving the full credential-checking logic under test. Patching deeper at `find_codex_cli` or `assert_codex_mode` risks testing only a partial path and could allow _check_credential to su _(assumption)_
- **Q:** How should the test prove the codex path was taken: by verifying mocked functions were called (e.g., checking mock.call_count), by checking exception type or message content, or by another method? **A:** Verify the mocked `assert_task_backend_usable` was called with 'codex' as an argument (check mock.call_args or mock.call_count > 0 with correct args), rather than checking the exception message. This proves the codex path was taken, not merely that 'codex' appears in an error from some other source. Use pytest.raises context with mock assertion inside it. _(assumption)_

</details>

