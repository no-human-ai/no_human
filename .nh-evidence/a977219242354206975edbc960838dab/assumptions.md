# Assumptions

_Harness-captured record for task `a9772192`, commit `c4f5c983a92b6c2b533dad9a23bd457925c2099e` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** Does this project enforce specific rules for what counts as 'Conventional Commits v1.0.0 compliant' (e.g., restricted types, required/forbidden scope, breaking change handling), or should we implement the published spec as-is? **A:** Implement the published Conventional Commits v1.0.0 spec as-is unless the codebase documents project-specific overrides. The standard types (feat, fix, etc.), optional scope syntax, and breaking-change markers from the published spec provide sufficient clarity for validation without custom rules. _(assumption)_
- **Q:** Should the validation check the raw task_title before redact_for_publish() is applied, or the transformed subject line after redaction? **A:** Validate the subject line after redact_for_publish() is applied, since that is the exact string that will appear in the commit message and git history. This ensures the operator's hard rule is enforced on what actually lands on main. Task retitle adjusts the input (task_title), and validation catches the result. _(assumption)_

</details>

