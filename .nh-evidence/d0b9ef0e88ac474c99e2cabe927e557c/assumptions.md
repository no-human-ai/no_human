# Assumptions

_Harness-captured record for task `d0b9ef0e`, commit `210395e8f7f6f33ecf44719217f48621543d76fd` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** What is the repository URL or file path? What access credentials, SSH keys, or permissions (if any) are required? **A:** HUMAN-GATED: not self-answerable
- **Q:** Should this work be based on the current main/master branch (which includes the already-landed PRs #126 and #127), or should it check out the specific commit SHAs mentioned (3a7ce1f9 for PR #126, adaa7555 for PR #127)? **A:** Work on the current main/master branch (which includes the already-landed PRs #126 and #127). The task describes these as 'landings' with SHAs mentioned for reference context, and the follow-up work adds tests to validate behavior that already exists on main. Starting from the current state is simpler and reflects where the code already is. _(assumption)_
- **Q:** For the NON_MAC test in item (2), the PR body must prove the test fails when the NON_MAC_ROOTS candidate drop is removed. Should this proof include: (A) git diffs of the removal plus pytest output, (B) shell commands for manual reproduction, (C) separate temporary commits showing the test RED and GREEN, or (D) narrative description? **A:** (A) git diffs of the removal plus pytest output. The PR body should show a git diff removing the NON_MAC_ROOTS drop line, then pytest output showing the test fails, then the restoration with pytest passing. This is the most concrete and reviewable evidence in a PR body, consistent with standard practice. _(assumption)_

</details>

