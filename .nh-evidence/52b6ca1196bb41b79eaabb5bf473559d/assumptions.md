# Assumptions

_Harness-captured record for task `52b6ca11`, commit `37fee8c8be6ac4ec7ff88c69a726271643f26b63` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 1 assumption made on your behalf — verify at review</summary>

- **Q:** For drainChip.js: Should we remove the user-chosen profile name from the title attribute entirely (stronger, blocks the leak at the source), or add ph-no-capture to keep it present but masked by the replay mechanism (preserves the information for operators)? **A:** Remove the user-chosen profile name from the title attribute entirely. The visible label already communicates the operational need (paused status and reset time); the profile name is the only identifying information and is user-chosen, so it could contain sensitive content. Removal blocks the leak at the source (stronger than masking), operators do not require the profile name to understand that w _(assumption)_

</details>

