# Assumptions

_Harness-captured record for task `f8cae5c4`, commit `25a6cebe6309ebadb760a91fcc548b6cf0351905` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** In acceptance criterion 1, should the effective stuck_active threshold be strictly greater than (>) the attempt_timeout_s, or is greater-than-or-equal-to (>=) sufficient? This affects the derivation formula—e.g., should it be exactly `ceil(attempt_timeout_s/60)` or add margin like `ceil(attempt_timeout_s/60) + X_minutes`? **A:** Strictly greater than (>). The effective threshold should be derived as something like `ceil(attempt_timeout_s / 60) + 1` or similar, ensuring it is always strictly greater than the attempt bound. This prevents simultaneous firing of both guards, guarantees the attempt-level watchdog fires first and completes its work before the task-level watchdog can intervene, and avoids edge cases where they f _(assumption)_
- **Q:** When an attempt row is closed due to escalation by the sweep, which terminal state should it transition to (e.g., ESCALATED, ERROR, ABANDONED)? **A:** ABANDONED. This terminal state semantically represents the system giving up on the attempt due to detected stall, allows proper attribution of turns and usage to a terminal row state rather than leaving it orphaned in in_progress, and distinguishes from successful completion, normal errors, or timeouts—this is an explicit system abandonment of a hung attempt. _(assumption)_
- **Q:** For acceptance criterion 4 (establishing whether the backend coroutine survives after ESCALATED status is set): what command, test harness, or environment setup should the implementer use to establish this empirically? Should the code handle both outcomes (survives and stops) or only the verified one? **A:** HUMAN-GATED: not self-answerable
- **Q:** Should the fix include a migration to close existing orphaned in_progress attempt rows from before this change, or only prevent new ones going forward? **A:** HUMAN-GATED: not self-answerable

</details>

