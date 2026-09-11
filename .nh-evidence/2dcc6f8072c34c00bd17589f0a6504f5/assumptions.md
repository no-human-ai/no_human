# Assumptions

_Harness-captured record for task `2dcc6f80`, commit `909b23cba458793dbf45b2c309f14a64d7a9d9c0` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Where should this early-claim containment check live — as a hook/step during the attempt loop itself (e.g. right when the agent asserts 'this already exists'), or as a pre-check invoked before the attempt is allowed to proceed with that claim? **A:** Implement as a hook/step inside the attempt loop itself, invoked at the moment the agent asserts an already-satisfied claim (not a separate orchestrator pre-flight). This mirrors how `landed_override.py` already runs its ancestry check inline as part of handling the claim/override request (src/no_human/blockers/landed_override.py:495-696), so the fix is a new early call-site into the same checking _(repo-evidence)_
- **Q:** What exact mechanism should be used to check ancestry — shell out to `git merge-base --is-ancestor` as delivery does, or an existing library/API wrapper already used elsewhere in the codebase? **A:** Reuse the existing `is_ancestor`/`commit_is_ancestor` wrapper (git merge-base --is-ancestor) already defined in src/no_human/vcs/git.py:983-994 and used throughout src/no_human/blockers/landed_override.py:178,430-687, rather than shelling out again ad hoc. This is the same mechanism delivery already uses, satisfying the 'same containment question' acceptance criterion. _(repo-evidence)_
- **Q:** What should the exact refusal message format be (e.g. must it literally state the commit sha and the base branch name), and is there an existing message template from the delivery-time refusal path that should be reused verbatim for consistency? **A:** Reuse the existing delivery-time refusal message template verbatim: the `_refusal_text` helper's format '{sha} is not an ancestor of {joined} — refusing. If it landed ...' (src/no_human/blockers/landed_override.py:469-480), which already names the commit sha and the branch(es) it is not on. Extract/call this shared function rather than authoring a new string, for consistency and to satisfy the 'na _(repo-evidence)_

</details>

