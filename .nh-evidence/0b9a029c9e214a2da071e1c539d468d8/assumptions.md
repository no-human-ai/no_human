# Assumptions

_Harness-captured record for task `0b9a029c`, commit `af5cbadaa8b40beee5a05d59fda84e43238fcb41` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** Do the board, nh status, and per-task rollups all call the same cost calculation functions from metrics.py, or does each system have independent cost implementations? **A:** Yes, the board, nh status, and per-task rollups all call the same cost calculation functions from metrics.py. The task describes the problem as universal across all reporting surfaces (all omit unattributed_usage data equally) and the fix as a single change to metrics.py with unified logic applied to board, nh status, and rollups. Multiple independent implementations would require coordinated fixe _(assumption)_
- **Q:** Can the same pre-attempt spend tokens be recorded in both the attempts table and the unattributed_usage ledger table for the same task, or are these tables always mutually exclusive for a given task's spend? **A:** These tables are designed to be mutually exclusive by task: pre-attempt spend (intake evaluator, grill, plan, decompose) is flushed to unattributed_usage with task_id before any attempt row exists, while attempt spend is drained to the attempts table only on attempt exits. They represent sequential, non-overlapping phases of a task's lifecycle. _(assumption)_

</details>

