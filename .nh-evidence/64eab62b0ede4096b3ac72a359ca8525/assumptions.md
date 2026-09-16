# Assumptions

_Harness-captured record for task `64eab62b`, commit `9e386c38daba4db72105c0a94fdb84eff6530901` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** How should the agent access src/no_human? (GitHub or GitLab URL? Internal server? Local path? Do credentials or special permissions need to be configured?) **A:** HUMAN-GATED: not self-answerable
- **Q:** The task mentions 'a separate fix in flight' for vcs/approve_merge.py's interpreter logic. Should this task add encoding parameters to subprocess calls in that file, or should those encoding fixes be deferred to coordinate with the separate PR? **A:** Defer encoding parameter additions to vcs/approve_merge.py to coordinate with the separate PR fixing the interpreter logic there. The task description's 'SEQUENCING CONSTRAINT' explicitly states 'there is a separate fix in flight for the same file' and 'Coordinate rather than landing half of each,' with acceptance criteria conditioning any changes there on explicit coordination. This task should f _(assumption)_
- **Q:** What test framework does src/no_human use (pytest, unittest, or custom), where should regression tests be added, and what command runs the full test suite (for the 'whole-suite green' verification that must be quoted in the PR body)? **A:** Based on typical Python project conventions: pytest is the most likely test framework (standard in modern Python as of 2025); regression tests should be added to a tests/ directory (or similar conventional test location); the full suite command is most likely `pytest` or `python -m pytest` run from the project root. The task description's mention of 'testing/runner.py' as a heavy subprocess user s _(assumption)_

</details>

