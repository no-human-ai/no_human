# Assumptions

_Harness-captured record for task `4f82bae5`, commit `8637d821131e2497ceb619ede40aff785d3666a6` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** Should the work delete the marker mechanism and its tests, or reword the two sentences in docs/configuration.md and src/no_human/core/worktree.py to clarify what the marker guards? **A:** HUMAN-GATED: not self-answerable
- **Q:** If taking the reword path: should the two sentences use the exact phrase 'a re-entry into the same worktree path, which no current caller performs', or is this phrase just an explanation of the concept to convey? **A:** 'Strict manifest OK' refers to validation that the project's manifest (likely MANIFEST.in, setup.py, or pyproject.toml) is consistent with the actual codebase structure, imports, and declared files. It should be verified by running the project's manifest validation check, typically `python -m build --sdist` or a dedicated `python setup.py check -s` command, or by examining CI/linting step document _(assumption)_
- **Q:** What does 'strict manifest OK' mean in the verification checklist, and how should it be verified? **A:** Beyond the three specified files, the marker mechanism is likely referenced in: setup/initialization utilities that write or check the marker file, logging statements or error messages when the marker is read, type definitions or dataclass fields for worktree state, and possibly in comments or docstrings of functions that call run_setup_commands or manage worktree lifecycle. A complete search woul _(assumption)_
- **Q:** Beyond docs/configuration.md (~895–897), src/no_human/core/worktree.py (~92–93), and tests/test_worktree_setup_cmds.py, are there other files, docstrings, or code locations that reference or describe the marker mechanism? **A:** (unanswered)

</details>

