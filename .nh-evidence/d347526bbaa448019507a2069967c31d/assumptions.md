# Assumptions

_Harness-captured record for task `d347526b`, commit `efb13bc1f6a201017491332f94a03f11773c05e3` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** When the tests step fails, should the surfaced error output show ONLY the pytest failure summary (FAILED test lines and counts), or should it show both the currently head-capped output AND the summary? **A:** Show ONLY the pytest failure summary (FAILED test lines and counts), not both head-capped output and summary. The current head-cap is the failure mode being fixed; showing both would be redundant and not solve the core visibility problem. The acceptance criteria specify the output should 'show pytest's failure summary' to identify which test failed. _(assumption)_
- **Q:** How should we identify and extract the pytest failure summary? Should we parse specific text patterns (regex for 'FAILED' lines), use pytest's structured output option (JSON/XML), or extract the last N lines of output? **A:** Extract the last N lines of pytest output (e.g., final 50-100 lines). The task description states the actionable summary is 'at the END of the run,' making a tail-extraction approach most robust—it doesn't require parsing fragile regex patterns and adapts automatically to pytest output format changes. _(assumption)_
- **Q:** If a pytest run fails with hundreds of test failures, should the extracted summary itself be truncated (and at what length), or shown in full regardless of size? **A:** Show the summary in full without additional truncation. Pytest failure summaries are typically modest in size (a few lines per failed test plus counts). Cap the entire surfaced output at a higher limit than 4000 chars (e.g., 10000-15000) if needed, but don't truncate the summary section itself—readers need complete FAILED identifiers to locate the failure. _(assumption)_
- **Q:** Should the test that validates this fix use actual pytest test failure(s), or mocked/simulated output to trigger the tests-step failure path? **A:** Use actual pytest test failure(s) to exercise the real failure scenario. Mocked output risks diverging from real pytest behavior. A real failing test ensures the extraction logic handles actual pytest output format and proves the fix works end-to-end. _(assumption)_

</details>

