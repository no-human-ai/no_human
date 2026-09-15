# Assumptions

_Harness-captured record for task `b44e3779`, commit `0c491e4912d1aa88e23cbb18c92466bbdbd0bb4e` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** The working directory is currently a temporary location. Which repository contains the files being modified (web/e2e/replay-body-leak.mjs, docs/configuration.md, etc.), and do I have write access to it? **A:** HUMAN-GATED: not self-answerable
- **Q:** What is the complete list of web/e2e test harnesses in the repository, and should each one be audited for the fail-open pattern, or only a subset? **A:** All web/e2e test harnesses should be audited for the fail-open pattern. The task states 'Worth one sweep -- a check that can only flip an INFO line is not a check' and emphasizes this is a systematic quality issue. A senior engineer would want consistency across all e2e verification harnesses, not a subset. This is especially critical for security guarantees that 'can decay unseen.' Audit scope: a _(assumption)_
- **Q:** Should the acceptance criterion 'demonstrated by running both mutations before and after the fix' be satisfied by: (a) manual verification documented during review, (b) new automated test cases added to the suite, or (c) both? **A:** (c) Both. The acceptance criterion requires demonstration 'by running both mutations before and after the fix' (manual verification during review to prove the fix works), AND the security-critical nature of this guarantee ('decays silently') and the instruction to 'say so in the test' (about re-anchoring the control) together indicate automated test cases should be added to prevent regression. A p _(assumption)_

</details>

