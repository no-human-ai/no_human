# Assumptions

_Harness-captured record for task `04a5bb58`, commit `4e58e4b2b050cc7ce788f66eac76fdf7e775684c` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** What is the Git repository URL for this codebase, and how should the agent authenticate (SSH key, HTTPS token, etc.)? **A:** HUMAN-GATED: not self-answerable
- **Q:** Should this fix be delivered as a pull request to a target branch, or as a direct commit? If a PR, what branch should it target? **A:** HUMAN-GATED: not self-answerable
- **Q:** Is RELEASE_MANIFEST.txt re-pinning part of the acceptance deliverable, or should the agent only update it if the CI gate fails during testing? **A:** RELEASE_MANIFEST.txt re-pinning is conditional: update it only if the CI gate fails during testing, not as an unconditional part of the fix's base deliverable. The fix itself (deleting the prose assertions, keeping the path assertions, running tests green) is the acceptance deliverable; RELEASE_MANIFEST.txt is a secondary action triggered by gate failure. _(assumption)_

</details>

