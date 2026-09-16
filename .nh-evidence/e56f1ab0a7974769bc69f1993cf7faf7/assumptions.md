# Assumptions

_Harness-captured record for task `e56f1ab0`, commit `413834779e2717d3ac805f8471c3d7862b39f9ab` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** For workflow_dispatch runs, should the concurrency group discriminant be `github.run_id` (ensuring no two dispatch runs on the same ref execute concurrently) or the string `'dispatch'` (allowing multiple concurrent dispatch runs on the same ref, but isolating them from pushes)? **A:** 'dispatch' (string constant) — ensures dispatch runs on the same ref serialize with each other but are isolated from pushes, matching the intended release workflow pattern of one release build at a time _(assumption)_
- **Q:** Should this concurrency fix apply only to the main branch or to all branches in the repository? **A:** All branches (universal) — workflow_dispatch can be triggered on any branch, the concurrency bug is a fundamental configuration issue independent of branch, and the fix using event_name conditions applies uniformly without requiring branch-level conditionals _(assumption)_

</details>

