# Assumptions

_Harness-captured record for task `2fa8f1bd`, commit `bb065b5bf24952abdd8a8e3236420b5c34b4ecc6` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** The acceptance criteria requires 'a deterministic in-suite probe' that reproduces the deadlock without the drain and passes with it. Should this be: (A) a separate test function that deliberately omits the drain to demonstrate the timeout, (B) an additional test function with the drain that validates the fix, (C) a parametrized or multi-scenario test, or (D) another structure? **A:** (C) a parametrized or multi-scenario test _(assumption)_
- **Q:** Should the stdout drain fix be applied only to the specified test in tests/test_codex_oversized_jsonl_line.py, or should we also identify and fix similar subprocess teardown patterns (finally blocks with await proc.wait() but no pipe draining) in other test files? **A:** Only the specified test in tests/test_codex_oversized_jsonl_line.py should be fixed. Do not expand to identify and fix similar patterns in other test files. _(assumption)_

</details>

