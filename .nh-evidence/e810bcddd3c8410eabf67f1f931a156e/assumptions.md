# Assumptions

_Harness-captured record for task `e810bcdd`, commit `e86424009932000c77dfe719c56973bc44f8df2b` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** Will the successfully rebased commits be pushed to main, or left on a feature branch for human review? **A:** HUMAN-GATED: not self-answerable
- **Q:** Are the specific location of the 'pr_conflict instruction' in blockers/wake.py and the name of the test that should turn red upon reversion already identified? **A:** Yes. The criterion 'every file:line citation resolves to what it names in THIS commit' requires citations to be accurate in the rebased version. If line numbers shift during rebase, comments, docstrings, and PR body citations must be updated so they point to the correct locations in the new commit. _(assumption)_
- **Q:** After rebasing, if line numbers shift in referenced files, should file:line citations in code comments, docstrings, and the PR body be updated to match their new positions? **A:** Captured test output and logs are the appropriate deliverable for 'demonstrated on THIS commit' criteria. For runtime claims (runner set identity, guard behavior), executing the tests and capturing their output is the only way to prove the claim against the current code; describing the approach without execution output does not constitute demonstration. _(assumption)_
- **Q:** Should demonstrating criteria compliance—particularly runner set identity at runtime and test execution—include captured test output/logs as deliverable artifacts, or is describing the verification approach sufficient? **A:** (unanswered)

</details>

