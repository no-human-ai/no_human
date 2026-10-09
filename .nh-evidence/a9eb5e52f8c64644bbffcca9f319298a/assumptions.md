# Assumptions

_Harness-captured record for task `a9eb5e52`, commit `0cf6665fe612b4a0c871605f3ac4d0d913819104` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** Is the PR's source branch in the same repository as the base branch, or in a fork? If in a fork, do we have write permissions to force-push to it? **A:** HUMAN-GATED: not self-answerable
- **Q:** How is the PR's head commit SHA (as it existed at review time) provided to land_task: as a function parameter, from context of a prior approval step, or fetched from GitHub? **A:** The PR's head commit SHA is most likely provided as a function parameter to land_task from the prior approval/review step context, following the pattern referenced in 'the head it reviewed/landed from' in the acceptance criteria. The alternative (fetched from GitHub) would be inefficient and inconsistent with the approval workflow. _(assumption)_
- **Q:** What check verifies that GitHub marked the PR as merged—which gh pr view fields or API response—and if that check itself errors, is the PR closed as fallback? **A:** The check verifies that GitHub marked the PR as merged by querying 'gh pr view --json state,mergedAt' and checking that the state field equals 'MERGED' and mergedAt is populated. If this check errors or returns state != MERGED, the PR is closed as a fallback, with the closure reported as a warning rather than a primary outcome. _(assumption)_
- **Q:** What is the 'PR watcher' system, and what test infrastructure exists (or must be built) to prove that a head-branch push does not re-trigger review rounds? **A:** The 'PR watcher' is infrastructure (likely a background process or webhook handler) that monitors incoming PR status changes and may trigger review rounds or re-runs of tasks when a PR is updated. Test infrastructure to prove head-branch pushes don't re-wake it would require either mocking the watcher's trigger mechanism (e.g., a webhook endpoint or polling loop) or using a real temp repo with ins _(assumption)_

</details>

