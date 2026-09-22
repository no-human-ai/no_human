# Assumptions

_Harness-captured record for task `bb90844c`, commit `4fce5a12ec2470b7b6553c74d650de9e6c1522cd` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** When run_budget_test needs to pass the primary checkout path (containing pytest's .venv) to proc.real_python, how should it obtain this path? Should it be: passed as a new parameter from the caller in derived_conflict.py, accessed from an existing repo/worktree object it already receives, derived from git worktree metadata/environment, or determined another way? **A:** It should be passed as a new parameter from the caller in derived_conflict.py. The primary checkout root is conceptually distinct from the worktree root (a fresh nh-derived-* worktree has no .venv), and since derived_conflict.py knows it is passing a worktree to run_budget_test, it should explicitly pass the primary checkout path as well. This makes the dependency clear and avoids assumptions abou _(assumption)_
- **Q:** Does obtaining or accessing the primary checkout path require any external credentials, system permissions, or API calls beyond standard repository filesystem access? **A:** HUMAN-GATED: not self-answerable

</details>

