# Assumptions

_Harness-captured record for task `15b04ad6`, commit `2ebd5ebe5d3bf21c89abec239b83ffd5666d5f3d` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** Which repository should these changes be applied to, and do we have the necessary credentials/write access (to push commits and open pull requests)? **A:** HUMAN-GATED: not self-answerable
- **Q:** The description lists 'an attempt interrupted because the app/server was closed' as an end state currently emitting no telemetry, but the acceptance criteria specify task_ended outcomes only as {escalated, parked_quota, parked_infra, needs_answer, cancelled}. Should 'interrupted' be included as a valid outcome for task_ended, or should app-closure cases be handled entirely by tasks_orphaned detect **A:** No, 'interrupted' should NOT be included in task_ended outcomes. The acceptance criteria explicitly restrict task_ended outcomes to {escalated, parked_quota, parked_infra, needs_answer, cancelled}. App-closure cases where the heartbeat dies should be detected and reported via the separate tasks_orphaned mechanism on server restart, not as an immediate task_ended event. _(assumption)_
- **Q:** For orphan detection ('attempt heartbeat is dead'), what timeout threshold (seconds/minutes) determines when a heartbeat is considered dead? Is there an existing heartbeat timeout mechanism in the codebase we should reuse? **A:** The heartbeat timeout threshold is not specified in the task description. A reasonable assumption for a senior engineer: adopt an existing heartbeat or attempt timeout constant already present in the codebase (likely in the 60-300 second range, typical for distributed system dead detection), and verify it against core/orchestrator.py or configuration constants before implementation. _(assumption)_
- **Q:** Should tasks_orphaned always be emitted on server start (including with bucket=0 if no orphaned tasks are found), or only when orphaned_count > 0? **A:** Yes, tasks_orphaned should always be emitted on server start, including when bucket=0 (no orphaned tasks found). The acceptance criteria explicitly lists (0, 1, 2-5, 6+) as valid bucket values, signaling that a 0-count is a meaningful metric state to always record. _(assumption)_

</details>

