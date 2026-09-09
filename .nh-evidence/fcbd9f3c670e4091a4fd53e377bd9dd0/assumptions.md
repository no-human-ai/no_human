# Assumptions

_Harness-captured record for task `fcbd9f3c`, commit `dcbce8ec5a5611c5d32e4e4422adb5d409361eb6` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** When emitting 'the last two test summaries' in the stuck event, what should each summary contain? Should it be (A) the test output hash used for comparison, (B) a structured summary like '5 passed, 2 failed', (C) raw test stdout/output text, or (D) a reference to logs rather than inline data? **A:** B - a structured summary like '5 passed, 2 failed' for each of the last two test invocations. This provides a human-readable outcome summary sufficient to verify that successive test runs produced identical results, without storing raw stdout which would be verbose or hashes which wouldn't be human-interpretable. _(assumption)_
- **Q:** Does the convergence tracker currently store and expose test output hashes or structured summaries for each test-runner invocation (queryable per-file and per-run), or does it only record that 'progress happened' without storing the actual output data? **A:** The convergence tracker currently only records that progress occurred without storing the actual test output data. New tracking fields would be required to store test output hashes or structured summaries for each test-runner invocation per file, enabling comparison logic to detect whether output changed between runs. _(assumption)_
- **Q:** How should the two acceptance tests be implemented? (A) Unit-level mocks of test-runner invocations in core/bounds.py_test, (B) integration tests that invoke real test runners (pytest), or (C) replays of actual captured test output from task f6e626fd? **A:** B - integration tests that invoke the orchestrator event sink with mocked or replayed test-runner outputs. The requirement to test 'through the real orchestrator event sink' and the phrase 'replay' in the task description indicate acceptance tests must validate the full orchestrator path, not isolated unit logic. Test outputs can be mocked/replayed rather than running pytest 20 times, but the orch _(assumption)_

</details>

