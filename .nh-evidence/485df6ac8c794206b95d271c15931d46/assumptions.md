# Assumptions

_Harness-captured record for task `485df6ac`, commit `71cbbbcc316596824814efd017a8cbe46e9e79e8` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** In the codebase schema, what condition identifies a FAILED attempt for selecting the blocker quote? Should we use WHERE failure_reason IS NOT NULL, WHERE status='FAILED', WHERE review_passed IS NULL, or another column/condition? **A:** WHERE failure_reason IS NOT NULL. The task describes needing 'the last FAILED attempt's failure_reason' to quote in the blocker text, and indicates the current code incorrectly pulls from 'newest verdict-carrying' rows instead of truly failed ones. A failure_reason column being non-null directly signals an attempt that failed and has a reason to report, distinguishing it from attempts that may hav _(assumption)_
- **Q:** Does the `_mechanical_round` helper function already exist in orchestrator.py, or does it need to be created as part of this change? **A:** The `_mechanical_round` helper function already exists in orchestrator.py. The task describes routing `_resume_human_gated` 'through the same helper' and acceptance criteria specify routing 'through the `_mechanical_round` helper', implying an existing function to call rather than new code to create. _(assumption)_
- **Q:** What specific entry or version should be updated in RELEASE_MANIFEST for this change (e.g., a version bump, dependency pin, or codebase-specific re-pin key)? **A:** A version or codebase-specific re-pin entry related to the db.py changes (likely a changelog bump or component version), but the exact RELEASE_MANIFEST entry key cannot be determined without inspecting the file structure. The task indicates re-pins accompany code changes but does not name a specific entry. _(assumption)_

</details>

