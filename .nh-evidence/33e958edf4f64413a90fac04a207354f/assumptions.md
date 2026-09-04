# Assumptions

_Harness-captured record for task `33e958ed`, commit `37ddb4f2a5561943cbe6122478085ae72e18850e` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** You've hit your session limit · resets 6:40pm (Asia/Jerusalem) ('personal2' subscription)

<details><summary>⚠️ 15 assumptions made on your behalf — verify at review</summary>

- **Q:** Which files or modules in the current public main branch should host the verification-receipt aggregate feature? **A:** A new module in the verification or checks subsystem (e.g., `verification/receipt_aggregate.py` or `checks/receipt_aggregator.py`, or a dedicated `verification_receipts/` package) that aggregates and reports metrics across verification attempts, with supporting data models for receipt storage and aggregation logic. _(assumption)_
- **Q:** What specific verification checks should the 'coder ran its checks before claiming done' rate track? **A:** Boolean tracking of whether each verification check was *executed* (not merely passed): linting/format checks, type checking, unit test suite, integration tests, security checks, and any other pre-completion validation gates defined in the codebase. The rate measures what fraction of submission attempts included execution of all configured checks. _(assumption)_
- **Q:** How is 'per-attempt' granularly defined in this context (e.g., per test run, per submission, per session)? **A:** Per-attempt is defined per submission or task completion event—each time a coder marks work complete or submits for review. The aggregation window groups attempts by timeframe or batch (e.g., daily aggregate, per-session, or per-release cycle) to compute the overall rate of compliance across that window. _(assumption)_
- **Q:** Can you provide access to the original private PR #689 branch or a detailed summary of its key implementation details? **A:** HUMAN-GATED: not self-answerable
- **Q:** Does the original PR #689 implementation depend on private-only utilities, libraries, or internal APIs that won't be available on public main? **A:** HUMAN-GATED: not self-answerable
- The 'verification-receipt aggregate' feature computes the rate (as a percentage or proportion) of code submission attempts where developers executed verification checks before claiming completion
- All file:line references from private PR #689 will be cross-referenced against the current public main branch codebase; if locations have shifted, the agent will search for the equivalent functionality by semantic purpose rather than exact line numbers
- The feature must be reimplemented from first principles based on the intent described in the task, NOT by cherry-picking or copying code from the private branch
- The new implementation must produce functionally identical verification-receipt metrics output compared to the original private PR #689
- The per-attempt 'coder ran its checks before claiming done' is a binary event (true/false) tracked at submission time, with aggregation computing the rate of true occurrences across the relevant time window or dataset
- The test suite baseline of 10199 passing tests refers to the total passing tests in the full suite after implementation; this is a lower bound, not an exact target
- RELEASE_MANIFEST is a single-file version/component tracking file that will be updated in the same commit as the feature implementation to reflect this re-homing
- docs/superpowers/ is a documentation directory that should not be created, modified, or referenced; private-only export tooling refers to build/export modules that should not exist in the public branch
- If any component from the original private implementation cannot be located in the current codebase, the agent will implement it as a new module integrated into the current architecture
- The implementation is considered complete when it passes independent engineer review confirming feature completeness, correctness, and integration without functionality gaps

</details>

