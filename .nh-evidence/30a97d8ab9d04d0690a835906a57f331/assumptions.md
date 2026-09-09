# Assumptions

_Harness-captured record for task `30a97d8a`, commit `be5201fecdd71ccb1ba4685db9e84990794b91a5` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** What is the target repository (URL/path) and which branch should these changes be applied to? **A:** HUMAN-GATED: not self-answerable
- **Q:** For the explicit unclassified marker on test_results: should it be a boolean field (e.g., classified: true/false), a string/stage field (e.g., stage: 'pre_review'), or another type? What should the exact field name be? **A:** The explicit unclassified marker should be a boolean field named 'classified' with initial value false for pre-review test_results rows. TESTING overwrites it to true when it classifies the run (with additional fields for the classification type: flaky_excused, pre-existing, or owned). This is simpler than a string 'stage' field and provides a clear boolean contract: pre-review runs start with cla _(assumption)_
- **Q:** What specific text, state value, or visual indicator should render in the board attempt view for the unclassified state to be visually distinct from a billed failure, and what should the web unit test assertion check? **A:** The board attempt view should render unclassified attempts with a distinct neutral/gray badge or container with the text label 'Pre-review' or a '?' indicator, rather than the red failure color. The visual difference from a billed failure should be enforced by a CSS class like 'attempt--pre-review' and a class like 'attempt--failed' for actual failures. The web unit test assertion should check: (1 _(assumption)_

</details>

