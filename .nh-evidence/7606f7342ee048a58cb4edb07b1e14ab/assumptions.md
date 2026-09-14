# Assumptions

_Harness-captured record for task `7606f734`, commit `6290a069c1f3fcd740bf220a95a5e1e667b37829` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** Beyond the `config` column, are there other columns in the task table or across the schema that are written by multiple zones and currently lack the concurrent-write protection that `status` and `title` already have? **A:** Based on the task description, context, status, title, and config form the identified class of multi-writer columns. The task states config is 'the remaining column in that class' lacking protection. No other columns in this class are mentioned. Without a schema audit, assume config is the sole remaining unprotected multi-writer column. _(assumption)_
- **Q:** Should we protect `config` using a timestamp marker (like `config_updated_at` with database-side CASE logic, matching the title fix), or through a different mechanism such as excluding `config` from `update_task` and `update_task_columns`? **A:** Use the timestamp marker approach (config_updated_at with database-side CASE logic, matching the title_updated_at pattern). Config is a simple field without merge semantics like context, making it suitable for the same guard mechanism already proven effective for title. This maintains consistency and avoids expanding the exclusion list. _(assumption)_
- **Q:** Should this PR include identifying and correcting existing incorrect `config` values in production databases, or is data remediation a separate operational task? **A:** HUMAN-GATED: not self-answerable
- **Q:** For the acceptance criterion 'a test drives the actual race', should the test verify the fix for `config` alone as a mechanism proof, or extend to all multi-writer columns identified in your answer to the column audit question? **A:** The test should extend to all multi-writer columns identified in the Q0 audit, to fully satisfy the acceptance criterion that 'every column written by more than one zone' is properly guarded. Config is the primary mechanism proof; additional columns identified in Q0 should have similar race tests added. _(assumption)_

</details>

