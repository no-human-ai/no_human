# Assumptions

_Harness-captured record for task `7b4f0974`, commit `fcaf116cb471b6fe695eda64798c6c280867f943` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** Should measure() return a tuple (None, failure_reason_str) on scanner failure, or should it raise a typed exception? The task offers both approaches ('return a (None, reason) pair or raise a typed error'). **A:** Returning a tuple (None, failure_reason_str) is preferable to raising a typed exception. The explicit success/failure contract makes it clearer for callers to distinguish measurement completion from failure, avoids exception-for-control-flow, and is more testable for verifying that error detail is properly propagated into DerivedResolution without requiring exception-handling assertions in tests. _(assumption)_
- **Q:** Is measure() called only from _take_budget_hunks, or are there other call sites in the codebase that would need to handle a changed return type or error-handling behavior? **A:** measure() is most likely called primarily from _take_budget_hunks in the conflict resolution flow. The task heavily focuses this path as the root cause site, and other call sites (if they exist) are probably limited to tests or closely related scanner-measurement functions. Scope of modifications should be limited to measure() itself, its primary caller(s) in vcs/derived_conflict.py, and the error _(assumption)_

</details>

