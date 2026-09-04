# Assumptions

_Harness-captured record for task `9058bf10`, commit `b5e9c56d007c52a5d1fc30883d0fe6e40ed8484a` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** Which timestamp field does the task payload currently provide for elapsed time—dispatch_time, started_time, created_at, or another? If none exist, should we add a read-only field to the task summary API? **A:** Most likely `started_at` or `created_at` timestamp field already exists in the task payload; if the board receives task data with active-state tracking (planning/implementing/testing/reviewing), it should have dispatch or start metadata. If only `created_at` is available, use that for elapsed time. Do not add a new API field unless neither dispatch/start nor created_at timestamps exist in the curr _(assumption)_
- **Q:** Is the list of terminal states (done/failed/escalated/awaiting approval) exhaustive, or are there other end-states the chip's filtering logic must exclude? **A:** The terminal-state list (done/failed/escalated/awaiting approval) is likely complete for the task tracking system's scope as described. However, consider whether 'cancelled' or 'blocked' are possible end-states; if the system distinguishes between 'awaiting approval' (requires external action to resolve) and 'blocked' (internal wait state), verify with the actual task state schema. Conservatively _(assumption)_

</details>

