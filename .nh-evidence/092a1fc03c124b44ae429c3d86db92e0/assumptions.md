# Assumptions

_Harness-captured record for task `092a1fc0`, commit `f368fa14a77c7cf3dee0cd7314515d7f8fc6bf81` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** What specific markdown formatting style is used for requirement callouts or admonitions elsewhere in docs/BACKENDS.md (e.g., blockquote syntax, bold-only, special markdown keyword)? **A:** Blockquote syntax (>) is the most common pattern in technical documentation for requirement callouts; alternatively, bold text with a keyword prefix like **Note:** or **Requirement:** _(assumption)_
- **Q:** Should the admonition state only the extended-thinking requirement, or should it also reference the consequence (first-turn requests error without extended thinking support)? **A:** Yes, include both the requirement and the consequence (first-turn 500 error without extended thinking), since the task specifies to cite the measured behavior and the consequence is what makes the requirement critical for users _(assumption)_
- **Q:** Can the agent autonomously execute the RELEASE_MANIFEST.txt re-pin per CONTRIBUTING.md instructions, or does this require human review (due to repo-specific knowledge or build-system access)? **A:** HUMAN-GATED: not self-answerable

</details>

