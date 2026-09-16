# Assumptions

_Harness-captured record for task `01bbe777`, commit `bb8321a81ed61eb6dad8cd94730915cf4298fae3` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** When `diff_override` and `before_ref` are supplied, what commit should be treated as the 'after side of the reviewed range' for the working tree validation—always the current HEAD of `repo_path`, or something else (e.g., an additional parameter, or derived from the diff)? Without this, the mismatch detection cannot determine what tree state to expect. **A:** HUMAN-GATED: not self-answerable
- **Q:** The acceptance criteria require the decision to distinguish 'passed with no blocking findings' from 'passed only after every blocking finding was demoted.' How should this distinction be represented in the returned decision object? Should it be a new boolean field, a structured reason, a separate metadata field, or something else? **A:** A boolean field (e.g., `all_blocking_findings_demoted` or `passed_due_to_demotion`) on the returned Decision object itself, set to True when the review passes only because blocking findings were demoted, and False otherwise. This is part of the main decision object (not a separate metadata field) so callers naturally inspect it; it is reversible (any caller checking the field benefits, those not c _(assumption)_

</details>

