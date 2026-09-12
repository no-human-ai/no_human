# Assumptions

_Harness-captured record for task `af1602af`, commit `ec467625e7407a8f65806f361eb80ef7ba14a3a8` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** Should this round enumerate which other CLI surfaces are affected by the same markup-deletion issue, and: (a) fix all affected surfaces found, or (b) fix only nh task show and document others for future action? **A:** Fix only nh task show and document other affected surfaces for future action (option b). The acceptance criteria specify nh task show only, the previous 9 attempts produced no shipped fix, and the straightforward markup-prevention fix (console.print with markup=False or escape()) should be narrowly scoped to unblock the immediate critical bug. Document the enumerate of other surfaces (investigate, _(assumption)_
- **Q:** Which fields does nh task show render that are operator-supplied text and need the same bracketed-text protection? The task examples include description, title, acceptance-criteria, and repo. Are there other user-editable fields? **A:** The task description identifies four user-editable fields requiring protection: description, title, acceptance-criteria, and repo. These should all be included in test cases per the acceptance criteria. Other user-editable fields may exist (e.g., tags, status, priority, assignee) depending on the task schema, which would require inspection of the commands.py render code to enumerate completely, bu _(assumption)_

</details>

