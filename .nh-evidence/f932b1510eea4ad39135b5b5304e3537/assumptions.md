# Assumptions

_Harness-captured record for task `f932b151`, commit `c9cee8bbcc83eda6074e390813070ff591c90a95` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** For the fake-fixture detection, should monkeypatch calls "inside that fixture's own body" include calls made within helper functions that the fixture invokes, or only direct calls in the fixture function itself? **A:** Only direct calls within the fixture function body itself, not transitive calls through helper functions. The phrase 'inside that fixture's own body' indicates syntactic presence in the fixture's immediate code block. _(assumption)_
- **Q:** For item 3 (adjudicator truncation handling), should we: (a) increase the diff cap for this decision, (b) make TRUNCATED_DIFF a distinct reported outcome the caller must handle, (c) implement both, or (d) is item 3 out of scope for this task? **A:** Option (c) - implement both: increase the diff cap for this decision type AND make TRUNCATED_DIFF a distinct reported outcome. This ensures diffs are not silently truncated and the caller knows when truncation occurs, making it a robust, reversible approach that doesn't assume a larger cap alone will be sufficient. _(assumption)_
- **Q:** What is the target repository (URL or local path), and should changes be committed to a new branch or integrated into the 0ff9125c branch currently under development by parallel tasks? **A:** HUMAN-GATED: not self-answerable

</details>

