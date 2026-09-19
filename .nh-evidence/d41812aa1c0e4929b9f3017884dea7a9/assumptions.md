# Assumptions

_Harness-captured record for task `d41812aa`, commit `f2697a233055781bd25542aa755eb413fd2f7075` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Does the requester prefer reformulating the tests to measure underlying mechanism (call counts, operation counts, or calibrated measurement) instead of wall-clock time, or moving them to a slower CI lane? **A:** Reformulate the tests to measure underlying mechanism (call counts, operation counts, or calibrated measurement independent of wall-clock time) rather than moving to a slower CI lane. The task description prioritizes this approach (presented first, with detailed acceptance criteria for mutation testing and contention validation), and moving tests out of the default path creates a regression-detect _(assumption)_
- **Q:** What is the target repository (URL/location)? Is it public, or does it require credentials/access? **A:** HUMAN-GATED: not self-answerable
- **Q:** Are there other wall-clock-based performance tests in the codebase exhibiting similar flakiness, beyond the two named tests? **A:** No other wall-clock-based performance tests are known beyond the two named (test_wake_tick_does_not_stall_scheduler.py::test_the_same_bound_holds_at_a_scaled_down_timeout and test_guard.py::test_the_gate_mention_scan_is_not_quadratic). The task description mentions one unrelated branch (run 35199082646) that exhibited scheduler flakiness but does not reference a broader pattern. Scope of work shou _(assumption)_

</details>

