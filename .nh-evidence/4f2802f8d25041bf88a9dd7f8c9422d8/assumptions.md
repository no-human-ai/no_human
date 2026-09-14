# Assumptions

_Harness-captured record for task `4f2802f8`, commit `efaa1e33fd3f484d812fd3f3e50b91575dc34699` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** What is the Git repository URL or filesystem path containing this codebase? **A:** HUMAN-GATED: not self-answerable
- **Q:** What is the name of the public function that serves as the entry point for guard command evaluation (the one that acceptance tests must call, not internal helpers)? **A:** sh, bash, timeout, xargs _(assumption)_
- **Q:** What is the complete list of all shell runners that the guard currently recurses into for command inspection? **A:** Testing with host_folds_case()=True is sufficient, since the task description explicitly states that neither defect reads host_folds_case() and both were confirmed with host_folds_case()=True already. The structural and lexical defects exist independently of case-folding configuration. _(assumption)_
- **Q:** Must the fix be validated to work correctly when host_folds_case() returns False, or is testing with True sufficient? **A:** (unanswered)

</details>

