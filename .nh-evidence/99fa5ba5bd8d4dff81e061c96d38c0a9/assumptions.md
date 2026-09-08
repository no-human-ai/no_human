# Assumptions

_Harness-captured record for task `99fa5ba5`, commit `88cf94ba994ae423070eada8bbcc992a29bd8f63` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** What should N (the truncation limit for failing_tests) be? The task suggests 200 as an example, but is that the target value, or should it be different? **A:** 200 is the target truncation limit (N=200). The task explicitly mentions it as the concrete example for 'keep the first N ids, e.g. 200', positioning it as the specific bound to implement throughout, and cites its effect on storage (953KB → much smaller for failing_tests alone). _(assumption)_
- **Q:** What is 'the tests event' referenced in the acceptance criteria? Is this a separate event table/record, a Buildkite event, or just referring to the update to attempts.test_results? **A:** 'The tests event' most likely refers to a structured record in the attempts table or an event record in the evidence ledger/event log system—a separate persisted representation of test results that mirrors the attempts.test_results column. It is not a Buildkite event, but rather the internal event/record created when test results are logged. _(assumption)_
- **Q:** Besides _newly_failing_vs_base and _owned_failing_tests, are there other in-memory code paths that need to receive the full unbounded failing_tests list for correct behavior? **A:** Besides _newly_failing_vs_base and _owned_failing_tests, the full unbounded list likely needs to reach any code path that performs attribution, blame assignment, or cross-test correlation for correctness. The task mentions 'pr_evidence' and 'nh task show' as readers; these paths should receive the full list if they also perform correctness logic (not just truncated display). _(assumption)_
- **Q:** Are there write sites that persist base_test_results beyond the core/orchestrator.py lines mentioned (~:6572 on 2df022b0, ~:6500/:6549/:6586/:6626/:6691 on main)? **A:** The primary write sites are in core/orchestrator.py where base_test_results is persisted (the ~6 lines cited). However, evidence_ledger.render_files is a secondary write site for tests.md (mentioned explicitly in the task), and potentially any event-recording or ledger-recording layer that serializes test results. The orchestrator.py sites are the primary truncation points. _(assumption)_

</details>

